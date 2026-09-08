"""Read-only GitHub transport with separate authenticated API and archive requests."""

import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from publication import PublicationError, authorize_controller, authorize_main_producer, matches, positive_integer


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class GitHub:
    def __init__(self, repository, token, opener=None):
        if not matches(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not token:
            raise PublicationError('Repository and read-only GitHub token are required')
        self.repository = repository
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, path):
        if (path and not path.startswith('/')) or any(part in ('.', '..') for part in path.split('/')) or any(character in path for character in ('#', '\\', '\r', '\n')):
            raise PublicationError('Invalid GitHub API path')
        return urllib.request.Request('https://api.github.com/repos/' + self.repository + path, headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'wallow-publication',
        })

    def get(self, path):
        try:
            with self.opener.open(self.request(path), timeout=30) as response:
                if response.status != 200:
                    raise PublicationError('GitHub API did not return success')
                data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise PublicationError('GitHub API response exceeds its size limit')
                return json.loads(data)
        except urllib.error.HTTPError as error:
            error.close()
            raise PublicationError('GitHub metadata could not be read') from None
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            raise PublicationError('GitHub metadata could not be read') from None

    def list(self, path, key):
        results = []
        expected = None
        for page in range(1, 1001):
            data = self.get(f'{path}?per_page=100&page={page}')
            if not isinstance(data, dict) or type(data.get('total_count')) is not int or data['total_count'] < 0 or not isinstance(data.get(key), list):
                raise PublicationError('Malformed paginated GitHub response')
            if expected is None:
                expected = data['total_count']
            if expected != data['total_count']:
                raise PublicationError('GitHub collection changed during pagination; retry')
            results.extend(data[key])
            if len(results) == expected:
                return results
            if not data[key] or len(results) > expected:
                break
        raise PublicationError('GitHub collection could not be read completely')

    def main_comparison(self, sha):
        if not matches(r'[0-9a-f]{40}', sha):
            raise PublicationError('Invalid revision to compare with main')
        main = self.get('/git/ref/heads/main')
        if not isinstance(main, dict) or main.get('ref') != 'refs/heads/main' or not isinstance(main.get('object'), dict) or main['object'].get('type') != 'commit' or not matches(r'[0-9a-f]{40}', main['object'].get('sha')):
            raise PublicationError('Could not resolve the current main revision')
        return self.get(f"/compare/{sha}...{main['object']['sha']}")

    def controller(self, context):
        if not isinstance(context, dict):
            raise PublicationError('Missing controller context')
        repository = self.get('')
        if not isinstance(repository, dict) or repository.get('full_name') != self.repository:
            raise PublicationError('Unexpected controller repository')
        return authorize_controller(context, repository, self.main_comparison(context.get('workflow_sha')))

    def producer(self, run_id, attempt):
        """Resolve one explicit attempt; never substitute a newer successful run."""
        if not positive_integer(run_id) or not positive_integer(attempt):
            raise PublicationError('An explicit producer run and attempt are required')
        repository = self.get('')
        workflow = self.get('/actions/workflows/ci.yml')
        run = self.get(f'/actions/runs/{run_id}/attempts/{attempt}')
        jobs = self.list(f'/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
        if not isinstance(run, dict) or not matches(r'[0-9a-f]{40}', run.get('head_sha')):
            raise PublicationError('Missing producer source revision')
        comparison = self.main_comparison(run['head_sha'])
        gates = [job for job in jobs if isinstance(job, dict) and job.get('name') == 'CI / required']
        if len(gates) != 1:
            raise PublicationError('Missing or ambiguous producer aggregate')
        prefix = f'https://api.github.com/repos/{self.repository}/check-runs/'
        url = gates[0].get('check_run_url')
        if not isinstance(url, str) or not url.startswith(prefix) or not matches(r'[1-9][0-9]*', url[len(prefix):]):
            raise PublicationError('Required check does not belong to this repository')
        check = self.get('/check-runs/' + url[len(prefix):])
        producer = authorize_main_producer(repository, workflow, run, run_id, attempt, jobs, check, comparison)
        if producer.repository != self.repository:
            raise PublicationError('GitHub repository identity differs from the requested repository')
        return producer, jobs

    def download(self, artifact, destination):
        if not positive_integer(artifact.id) or not positive_integer(artifact.size) or not matches(r'sha256:[0-9a-f]{64}', artifact.digest):
            raise PublicationError('Invalid authorized artifact descriptor')
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise PublicationError('Artifact download destination must be new')
        try:
            response = self.opener.open(self.request(f'/actions/artifacts/{artifact.id}/zip'), timeout=30)
        except urllib.error.HTTPError as error:
            try:
                if error.code != 302:
                    raise PublicationError('Artifact is unavailable; ordinary publication cannot rebuild it') from None
                location = error.headers.get('Location', '')
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError):
            raise PublicationError('Artifact download could not be resolved') from None
        else:
            response.close()
            raise PublicationError('Artifact API did not return its expected redirect')
        try:
            parsed = urllib.parse.urlsplit(location)
            valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment and parsed.port in (None, 443)
        except ValueError:
            valid = False
        if not valid:
            raise PublicationError('Artifact download redirect is invalid')
        # The signed URL comes directly from the fixed GitHub API endpoint.
        # A new request deliberately carries no GitHub Authorization header.
        request = urllib.request.Request(location, headers={'User-Agent': 'wallow-publication'})
        created = False
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status != 200:
                    raise PublicationError('Artifact storage did not return success')
                with destination.open('xb') as output:
                    created = True
                    digest, size = hashlib.sha256(), 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > artifact.size:
                            raise PublicationError('Artifact download exceeds its recorded size')
                        digest.update(chunk)
                        output.write(chunk)
                    if size != artifact.size or 'sha256:' + digest.hexdigest() != artifact.digest:
                        raise PublicationError('Artifact download differs from its recorded size or digest')
        except urllib.error.HTTPError as error:
            error.close()
            if created:
                destination.unlink(missing_ok=True)
            raise PublicationError('Artifact storage could not be read') from None
        except (urllib.error.URLError, TimeoutError):
            if created:
                destination.unlink(missing_ok=True)
            raise PublicationError('Artifact storage could not be read') from None
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        return destination
