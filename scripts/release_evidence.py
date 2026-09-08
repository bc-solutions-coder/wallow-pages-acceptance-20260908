"""Record a protected Release Please invocation; these observations do not authorize publication."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from publication import PublicationError, load_catalog, matches, positive_integer
from publication_github import GitHub


ACTION = 'googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7'


class CredentialGitHub(GitHub):
    def request(self, path):
        if path == '/user':
            return urllib.request.Request('https://api.github.com/user', headers={
                'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'wallow-release-evidence'})
        return super().request(path)


def actor(value):
    if not isinstance(value, dict) or not positive_integer(value.get('id')) or not matches(r'[A-Za-z0-9_.\[\]-]{1,100}', value.get('login')):
        raise PublicationError('Missing exact GitHub actor identity')
    if value.get('type') not in ('User', 'Bot'):
        raise PublicationError('Unexpected GitHub actor type')
    return {'id': value['id'], 'login': value['login'], 'type': value['type']}


def sha(value):
    if not matches(r'[a-f0-9]{40}', value):
        raise PublicationError('Missing exact Git commit identity')
    return value


def begin(client, credential_client, context, run_id, attempt, invocation_run, invocation_attempt):
    controller = client.controller(context)
    producer, _ = client.producer(run_id, attempt)
    run = client.get(f'/actions/runs/{invocation_run}/attempts/{invocation_attempt}')
    if run.get('id') != invocation_run or run.get('run_attempt') != invocation_attempt or not positive_integer(run.get('workflow_id')) or run.get('path') != '.github/workflows/publish.yml':
        raise PublicationError('Release invocation does not match the protected workflow run')
    repository = run.get('repository', {})
    if repository.get('id') != producer.repository_id or repository.get('full_name') != producer.repository:
        raise PublicationError('Release invocation belongs to another repository')
    jobs = client.list(f'/actions/runs/{invocation_run}/attempts/{invocation_attempt}/jobs', 'jobs')
    current = [job for job in jobs if job.get('name') in ('Release Please', 'Release automation / Release Please')]
    if len(current) != 1 or not positive_integer(current[0].get('id')):
        raise PublicationError('Release invocation job identity is missing or ambiguous')
    return {'schema': 1, 'publication_authorized': False, 'job_id': current[0]['id'], 'repository': producer.repository,
            'producer': asdict(producer), 'controller_sha': controller, 'workflow_ref': context['workflow_ref'],
            'run_id': invocation_run, 'run_attempt': invocation_attempt, 'workflow_id': run['workflow_id'],
            'actor': actor(run.get('actor')), 'triggering_actor': actor(run.get('triggering_actor')),
            'credential_actor': actor(credential_client.get('/user')),
            'main_before': sha(client.get('/git/ref/heads/main').get('object', {}).get('sha')),
            'action': ACTION, 'target_branch': 'main', 'config_file': 'release-please-config.json',
            'manifest_file': '.release-please-manifest.json', 'started_at': datetime.now(timezone.utc).isoformat()}


def output_list(outputs, key):
    raw = outputs.get(key, '[]')
    if not isinstance(raw, str) or len(raw.encode()) > 100 * 1024:
        raise PublicationError('Release Please output exceeds its bounded JSON limit')
    try:
        values = json.loads(raw)
    except (ValueError, RecursionError):
        raise PublicationError('Release Please output is invalid JSON') from None
    if not isinstance(values, list) or len(values) > 20:
        raise PublicationError('Release Please output must be a bounded list')
    return values


def release_commit(client, tag):
    if not isinstance(tag, str) or not 0 < len(tag) <= 200 or any(character in tag for character in ('\n', '\r', '\\', '?', '#')) or '..' in tag or tag.startswith('/'):
        raise PublicationError('Release tag is invalid')
    reference = client.get('/git/ref/tags/' + urllib.parse.quote(tag, safe=''))
    if reference.get('ref') != 'refs/tags/' + tag:
        raise PublicationError('Release tag lookup returned another reference')
    value = reference.get('object', {})
    chain = []
    for _ in range(5):
        digest = sha(value.get('sha'))
        chain.append(digest)
        if value.get('type') == 'commit':
            return digest, chain
        if value.get('type') != 'tag':
            break
        value = client.get('/git/tags/' + digest).get('object', {})
    raise PublicationError('Release tag does not resolve to a bounded commit identity')


def collect_outputs(client, invocation, outputs, outcome, catalog, result):
    if not isinstance(outputs, dict) or len(outputs) > 200 or outcome not in ('success', 'failure', 'cancelled', 'skipped'):
        raise PublicationError('Invalid Release Please completion evidence')
    prs, releases = result['pull_requests'], result['releases']
    seen = set()
    for reported in output_list(outputs, 'prs'):
        number = reported.get('number') if isinstance(reported, dict) else None
        if not positive_integer(number) or number in seen:
            raise PublicationError('Release Please reported an invalid or duplicate PR number')
        seen.add(number)
        pr = client.get(f'/pulls/{number}')
        head, base = pr.get('head', {}), pr.get('base', {})
        if pr.get('number') != number or not positive_integer(pr.get('id')) or base.get('ref') != 'main' or any(branch.get('repo', {}).get('full_name') != invocation['repository'] for branch in (head, base)):
            raise PublicationError('Reported Release Please PR belongs to another repository or base')
        if pr.get('state') not in ('open', 'closed') or type(pr.get('merged')) is not bool or not isinstance(head.get('ref'), str) or not 0 < len(head['ref']) <= 255:
            raise PublicationError('PR state or branch evidence is invalid')
        if pr.get('merge_commit_sha') is not None:
            sha(pr['merge_commit_sha'])
        if ('id' in reported and reported['id'] != pr['id']) or ('headBranchName' in reported and reported['headBranchName'] != head['ref']):
            raise PublicationError('PR API evidence differs from action output')
        prs.append({'id': pr['id'], 'number': number, 'author': actor(pr.get('user')), 'head_sha': sha(head.get('sha')),
                    'head_ref': head.get('ref'), 'base_sha': sha(base.get('sha')), 'state': pr.get('state'),
                    'merged': pr.get('merged'), 'merge_commit_sha': pr.get('merge_commit_sha')})
    paths = output_list(outputs, 'paths_released')
    allowed = {component['path'] for component in catalog['components']}
    if any(not isinstance(path, str) or path not in allowed for path in paths) or len(set(paths)) != len(paths):
        raise PublicationError('Release Please reported an unknown or duplicate component path')
    for path in paths:
        prefix = '' if path == '.' else path + '--'
        ident = outputs.get(prefix + 'id')
        if not matches(r'[1-9][0-9]*', ident):
            raise PublicationError('Release Please output lacks an exact release ID')
        release = client.get('/releases/' + ident)
        if release.get('id') != int(ident) or release.get('tag_name') != outputs.get(prefix + 'tag_name'):
            raise PublicationError('Release API evidence differs from action output')
        component = next(component for component in catalog['components'] if component['path'] == path)
        tag = release.get('tag_name')
        if not isinstance(tag, str) or not tag.startswith(component['tag_prefix']):
            raise PublicationError('Release tag does not match its registered component')
        version = tag[len(component['tag_prefix']):]
        if not matches(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?', version) or outputs.get(prefix + 'version', version) != version:
            raise PublicationError('Release version differs from action or catalog metadata')
        if type(release.get('draft')) is not bool or type(release.get('prerelease')) is not bool:
            raise PublicationError('Release state metadata is invalid')
        commit, chain = release_commit(client, tag)
        if prefix + 'sha' in outputs and sha(outputs[prefix + 'sha']) != commit:
            raise PublicationError('Release tag commit differs from action output')
        releases.append({'id': release['id'], 'component': component['id'], 'version': version, 'component_path': path, 'tag_name': release['tag_name'],
                         'author': actor(release.get('author')), 'draft': release.get('draft'), 'prerelease': release.get('prerelease'),
                         'commit_sha': commit, 'tag_object_chain': chain, 'matches_producer_sha': commit == invocation['producer']['source_sha']})
    result['main_after'] = sha(client.get('/git/ref/heads/main').get('object', {}).get('sha'))


def finish(client, invocation, outputs, outcome, catalog):
    result = {'schema': 1, 'publication_authorized': False, 'invocation': invocation, 'action_outcome': outcome,
              'pull_requests': [], 'releases': [], 'completed_at': datetime.now(timezone.utc).isoformat()}
    try:
        collect_outputs(client, invocation, outputs, outcome, catalog, result)
    except (PublicationError, TypeError, KeyError, AttributeError, ValueError, RecursionError):
        result['reconciliation_error'] = 'Release output reconciliation failed; recorded objects are observations only.'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('begin', 'finish'))
    parser.add_argument('--run-id')
    parser.add_argument('--attempt')
    parser.add_argument('--invocation')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        context = {key: os.environ.get('GITHUB_' + key.upper(), '') for key in ('repository', 'ref', 'workflow_ref', 'workflow_sha', 'event_name')}
        client = GitHub(context['repository'], os.environ.get('GH_TOKEN'))
        if args.mode == 'begin':
            numbers = (args.run_id, args.attempt, os.environ.get('GITHUB_RUN_ID'), os.environ.get('GITHUB_RUN_ATTEMPT'))
            if os.environ.get('ENABLE_RELEASE_AUTOMATION') != 'true' or not all(matches(r'[1-9][0-9]*', value) for value in numbers):
                raise PublicationError('Release automation requires explicit enablement and exact run identities')
            credential = CredentialGitHub(context['repository'], os.environ.get('RELEASE_PLEASE_TOKEN'))
            result = begin(client, credential, context, *(int(value) for value in numbers))
        else:
            raw = os.environ.get('RELEASE_OUTPUTS', '{}')
            if len(raw.encode()) > 100 * 1024:
                raise PublicationError('Release Please outputs exceed their size limit')
            invocation_path = Path(args.invocation)
            if invocation_path.stat().st_size > 64 * 1024:
                raise PublicationError('Invocation record exceeds its size limit')
            invocation = json.loads(invocation_path.read_text())
            result = finish(client, invocation, json.loads(raw), os.environ.get('RELEASE_OUTCOME'), load_catalog(Path(__file__).resolve().parents[2]))
        with Path(args.output).open('x') as output:
            json.dump(result, output, indent=2)
            output.write('\n')
        if 'reconciliation_error' in result:
            raise PublicationError('Release output reconciliation failed')
    except (PublicationError, OSError, ValueError, TypeError, RecursionError):
        parser.exit(1, 'Release Please invocation evidence could not be verified.\n')


if __name__ == '__main__':
    main()
