"""Narrow Pages deployment transport with no credentials on public readback."""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from publication import PublicationError, matches, positive_integer
from publication_pages_history import TASK
from publication_release_github import ReleaseGitHub


class PagesGitHub(ReleaseGitHub):
    def post(self, path, body, expected):
        request = self.request(path)
        request.method, request.data = 'POST', json.dumps(body).encode()
        request.add_header('Content-Type', 'application/json')
        return self.send(request, expected)

    def intent(self, payload):
        return self.post('/deployments', {
            'ref': payload['source_sha'], 'task': TASK, 'auto_merge': False,
            'required_contexts': [], 'environment': 'github-pages',
            'production_environment': True, 'payload': payload,
        }, 201)

    def status(self, deployment_id, state, run_id, site_url, description):
        if not positive_integer(deployment_id) or not positive_integer(run_id) or state not in ('in_progress', 'success', 'failure'):
            raise PublicationError('Invalid Pages intent status identity')
        return self.post(f'/deployments/{deployment_id}/statuses', {
            'state': state, 'auto_inactive': False, 'description': description,
            'log_url': f'https://github.com/{self.repository}/actions/runs/{run_id}',
            'environment_url': site_url,
        }, 201)

    def oidc(self, url, token):
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.actions.githubusercontent.com') and not parsed.username and not parsed.password and not parsed.fragment and parsed.port in (None, 443)
        except ValueError:
            valid = False
        if not valid or not token:
            raise PublicationError('Pages requires the trusted Actions OIDC endpoint')
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise PublicationError('Pages OIDC request failed')
                raw = response.read(32769)
                if len(raw) > 32768:
                    raise PublicationError('Pages OIDC response exceeds its limit')
                value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get('value'), str) or not value['value']:
                raise PublicationError('Pages OIDC response is malformed')
            return value['value']
        except urllib.error.HTTPError as error:
            error.close()
            raise PublicationError('Pages OIDC request failed') from None
        except (urllib.error.URLError, OSError, ValueError):
            raise PublicationError('Pages OIDC request failed') from None

    def deploy(self, artifact_id, source, oidc):
        if not positive_integer(artifact_id) or not matches(r'[a-f0-9]{40}', source) or not isinstance(oidc, str) or not oidc:
            raise PublicationError('Pages requires exact artifact, source and OIDC identities')
        result = self.post('/pages/deployments', {
            'artifact_id': artifact_id, 'pages_build_version': source,
            'oidc_token': oidc, 'environment': 'github-pages',
        }, 200)
        if not matches(r'[A-Za-z0-9_-]{1,128}', result.get('id')):
            raise PublicationError('Pages did not return a valid deployment identity')
        return result

    def wait_deployment(self, deployment_id, pause=time.sleep):
        if not matches(r'[A-Za-z0-9_-]{1,128}', deployment_id):
            raise PublicationError('Invalid Pages deployment identity')
        for _ in range(90):
            result = self.get('/pages/deployments/' + deployment_id)
            status = result.get('status') if isinstance(result, dict) else None
            if status == 'succeed':
                return result
            if status in ('deployment_failed', 'deployment_content_failed', 'deployment_cancelled', 'deployment_lost') or not matches(r'[a-z_]{1,64}', status):
                raise PublicationError('Pages deployment failed or returned an unknown state')
            pause(5)
        raise PublicationError('Pages deployment did not finish within its polling limit')

    def verify_index(self, site_url, identity, pause=time.sleep):
        try:
            parsed = urllib.parse.urlsplit(site_url)
            valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.port in (None, 443) and parsed.path.endswith('/')
        except ValueError:
            valid = False
        if not valid or type(identity.get('index_size')) is not int or not 0 <= identity['index_size'] <= 100 * 1024 * 1024 or not matches(r'[a-f0-9]{64}', identity.get('index_sha256')):
            raise PublicationError('Pages readback requires a bounded exact index and HTTPS site')
        url = site_url + 'index.html?wallow-content=' + identity['index_sha256']
        for _ in range(24):
            request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache', 'User-Agent': 'wallow-pages-readback'})
            try:
                with self.opener.open(request, timeout=30) as response:
                    body = response.read(identity['index_size'] + 1)
                    if response.status == 200 and len(body) == identity['index_size'] and hashlib.sha256(body).hexdigest() == identity['index_sha256']:
                        return {'url': site_url + 'index.html', 'sha256': identity['index_sha256'], 'size': len(body)}
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if code not in (404, 502, 503, 504):
                    raise PublicationError('Pages readback returned an unexpected response') from None
            except (urllib.error.URLError, OSError):
                pass
            pause(5)
        raise PublicationError('Served Pages index differs from the validated artifact')
