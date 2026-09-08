"""Authenticate protected automation and retain exact release PR origins before expiry."""

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile

from publication import Producer, PublicationError, matches, positive_integer
from publication_artifacts import select_artifact, unpack_payload
from publication_release_github import ACTIONS_ACTOR, ReleaseGitHub
from release_evidence import ACTION, actor


PR_JOB = 'Record release PR origins'
RECEIPT_JOB = 'Record release provenance'
ACTION_JOB = 'Release automation / Release Please'
COMMENT_PREFIX = 'Wallow release automation provenance. This record preserves the exact PR revision for release verification; it does not authorize publication.\n\n```json\n'
COMMENT_SUFFIX = '\n```'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError):
        raise PublicationError('Provenance timestamp is invalid') from None


def frame(client, context, run_id, attempt, job_name, state='active'):
    controller = client.controller(context)
    run = client.get(f'/actions/runs/{run_id}/attempts/{attempt}')
    workflow = client.get('/actions/workflows/publish.yml')
    repository = client.get('')
    if not all(positive_integer(value) for value in (run_id, attempt, workflow.get('id'), repository.get('id'))) or run.get('id') != run_id or run.get('run_attempt') != attempt or run.get('workflow_id') != workflow['id'] or workflow.get('path') != '.github/workflows/publish.yml' or run.get('path') != workflow['path'] or run.get('head_sha') != controller or run.get('head_branch') != 'main' or run.get('event') != context['event_name']:
        raise PublicationError('Provenance invocation differs from protected main workflow identity')
    for key in ('repository', 'head_repository'):
        if run.get(key, {}).get('id') != repository['id'] or run.get(key, {}).get('full_name') != client.repository:
            raise PublicationError('Provenance invocation belongs to another repository')
    jobs = client.list(f'/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
    selected = [job for job in jobs if job.get('name') == job_name]
    if len(selected) != 1 or not positive_integer(selected[0].get('id')):
        raise PublicationError('Provenance job is missing or ambiguous')
    job = selected[0]
    if job.get('run_id') != run_id or job.get('run_attempt') != attempt or job.get('head_sha') != controller or (state == 'success' and (job.get('status') != 'completed' or job.get('conclusion') != 'success')) or (state == 'active' and job.get('status') != 'in_progress') or (state == 'finished' and job.get('status') != 'completed') or state not in ('active', 'success', 'finished'):
        raise PublicationError('Exact protected provenance job did not reach the required state')
    record = {'repository': client.repository, 'repository_id': repository['id'], 'controller_sha': controller, 'workflow_id': workflow['id'],
              'workflow_ref': context['workflow_ref'], 'event_name': context['event_name'], 'run_id': run_id, 'run_attempt': attempt,
              'job_id': job['id'], 'job_name': job_name}
    return record, job


def recorded_job(client, record, job_name):
    if not isinstance(record, dict):
        raise PublicationError('Missing protected provenance invocation')
    context = {'repository': record.get('repository'), 'ref': 'refs/heads/main', 'workflow_ref': record.get('workflow_ref'),
               'workflow_sha': record.get('controller_sha'), 'event_name': record.get('event_name')}
    actual, job = frame(client, context, record.get('run_id'), record.get('run_attempt'), job_name, 'finished')
    if actual != record:
        raise PublicationError('Provenance record differs from its exact successful API job')
    return job


def verify_frame(client, record, job_name):
    job = recorded_job(client, record, job_name)
    if job.get('conclusion') != 'success':
        raise PublicationError('Protected provenance job did not complete successfully')
    return job


def action_evidence(client, run_id, attempt):
    run = client.get(f'/actions/runs/{run_id}/attempts/{attempt}')
    context = {'repository': client.repository, 'ref': 'refs/heads/main', 'workflow_ref': client.repository + '/.github/workflows/publish.yml@refs/heads/main',
               'workflow_sha': run.get('head_sha'), 'event_name': run.get('event')}
    invocation, _ = frame(client, context, run_id, attempt, ACTION_JOB, 'success')
    producer = Producer(client.repository, invocation['repository_id'], invocation['controller_sha'], run_id, attempt, invocation['workflow_id'], invocation['workflow_ref'])
    artifacts = client.list(f'/actions/runs/{run_id}/artifacts', 'artifacts')
    artifact = select_artifact(artifacts, producer, 'release-evidence')
    with tempfile.TemporaryDirectory(prefix='wallow-release-origin-') as directory:
        root = Path(directory)
        archive = client.download(artifact, root / 'evidence.zip')
        payload = unpack_payload(archive, root / 'verified', artifact, producer, 'evidence.json', 'release-evidence', 'release-please', 1024 * 1024)
        data = payload.read_bytes()
        document = json.loads(data)
    if not isinstance(document, dict) or not isinstance(document.get('invocation'), dict):
        raise PublicationError('Release Please evidence is malformed')
    evidence_invocation = document['invocation']
    if document.get('schema') != 1 or document.get('publication_authorized') is not False or document.get('action_outcome') != 'success' or 'reconciliation_error' in document or evidence_invocation.get('action') != ACTION:
        raise PublicationError('Release Please evidence does not prove successful protected automation')
    keys = ('repository', 'controller_sha', 'workflow_id', 'workflow_ref', 'run_id', 'run_attempt', 'job_id')
    if any(evidence_invocation.get(key) != invocation[key] for key in keys):
        raise PublicationError('Release Please evidence differs from its producing API invocation')
    source = evidence_invocation.get('producer', {})
    actual, _ = client.producer(source.get('run_id'), source.get('run_attempt'))
    if asdict(actual) != source or actor(run.get('actor')) != evidence_invocation.get('actor') or actor(run.get('triggering_actor')) != evidence_invocation.get('triggering_actor'):
        raise PublicationError('Release Please invocation producer or actor identity differs')
    actor(evidence_invocation.get('credential_actor'))
    return {'frame': invocation, 'artifact': asdict(artifact), 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(), 'document': document}


def comment_record(comment):
    if not isinstance(comment, dict) or not positive_integer(comment.get('id')) or {key: comment.get('user', {}).get(key) for key in ACTIONS_ACTOR} != ACTIONS_ACTOR or comment.get('created_at') != comment.get('updated_at'):
        raise PublicationError('PR origin is not an unedited GitHub Actions provenance comment')
    body = comment.get('body')
    if not isinstance(body, str) or len(body.encode()) > 60000 or not body.startswith(COMMENT_PREFIX) or not body.endswith(COMMENT_SUFFIX):
        raise PublicationError('PR origin comment has an invalid bounded format')
    try:
        record = json.loads(body[len(COMMENT_PREFIX):-len(COMMENT_SUFFIX)])
    except (ValueError, RecursionError):
        raise PublicationError('PR origin record is malformed') from None
    if not isinstance(record, dict) or record.get('schema') != 1 or record.get('type') != 'release-pr-origin' or record.get('publication_authorized') is not False:
        raise PublicationError('PR origin record has an unexpected schema')
    return record


def verify_pr_origin(client, comment, pr):
    record = comment_record(comment)
    if comment.get('issue_url') != f'https://api.github.com/repos/{client.repository}/issues/{pr.get("number")}':
        raise PublicationError('PR origin comment belongs to another issue')
    job = verify_frame(client, record.get('recorder'), PR_JOB)
    created = timestamp(comment['created_at'])
    if not timestamp(job.get('started_at')) <= created <= timestamp(job.get('completed_at')):
        raise PublicationError('PR origin comment was not created during its recorded job')
    observed = record.get('pull_request', {})
    evidence = record.get('release_please', {})
    verify_frame(client, evidence.get('frame'), ACTION_JOB)
    if observed.get('id') != pr.get('id') or observed.get('number') != pr.get('number') or observed.get('head_sha') != pr.get('head', {}).get('sha') or observed.get('head_ref') != pr.get('head', {}).get('ref') or pr.get('base', {}).get('ref') != 'main':
        raise PublicationError('Merged PR differs from its recorded automation revision')
    if any(pr.get(branch, {}).get('repo', {}).get('full_name') != client.repository for branch in ('head', 'base')):
        raise PublicationError('Release PR belongs to another repository')
    invocation = evidence.get('invocation', {})
    if observed.get('author') != actor(pr.get('user')) or observed['author'] != invocation.get('credential_actor'):
        raise PublicationError('Release PR actor differs from its protected credential principal')
    return {'comment_id': comment['id'], 'body': comment['body'], 'sha256': 'sha256:' + hashlib.sha256(comment['body'].encode()).hexdigest(), 'record': record}


def record_pr_origins(client, context, run_id, attempt):
    recorder, _ = frame(client, context, run_id, attempt, PR_JOB)
    evidence = action_evidence(client, run_id, attempt)
    result = []
    for observed in evidence['document'].get('pull_requests', []):
        number = observed.get('number')
        if not positive_integer(number):
            raise PublicationError('Release Please returned an invalid PR identity')
        pr = client.get('/pulls/' + str(number))
        if pr.get('id') != observed.get('id') or pr.get('head', {}).get('sha') != observed.get('head_sha') or pr.get('base', {}).get('ref') != 'main' or actor(pr.get('user')) != evidence['document']['invocation']['credential_actor']:
            raise PublicationError('PR changed after the protected Release Please observation')
        existing = []
        for comment in client.array('/issues/' + str(number) + '/comments'):
            if isinstance(comment.get('body'), str) and comment['body'].startswith(COMMENT_PREFIX) and comment.get('user', {}).get('id') == ACTIONS_ACTOR['id']:
                record = comment_record(comment)
                if record.get('pull_request', {}).get('head_sha') == observed['head_sha']:
                    if recorded_job(client, record.get('recorder'), PR_JOB).get('conclusion') != 'success':
                        continue
                    existing.append(verify_pr_origin(client, comment, pr))
        if existing:
            result.append(min(existing, key=lambda item: item['comment_id']))
            continue
        record = {'schema': 1, 'type': 'release-pr-origin', 'publication_authorized': False, 'recorder': recorder, 'pull_request': observed,
                  'release_please': {'frame': evidence['frame'], 'artifact': evidence['artifact'], 'sha256': evidence['sha256'], 'invocation': evidence['document']['invocation']}}
        body = COMMENT_PREFIX + canonical(record).decode() + COMMENT_SUFFIX
        comment = client.comment(number, body)
        if comment_record(comment) != record or comment.get('body') != body:
            raise PublicationError('PR origin write did not return the exact provenance bytes')
        readback = client.get('/issues/comments/' + str(comment['id']))
        if any(readback.get(key) != comment.get(key) for key in ('id', 'body', 'user', 'created_at', 'updated_at', 'issue_url')):
            raise PublicationError('PR origin readback differs from the created comment')
        result.append({'comment_id': comment['id'], 'body': body, 'sha256': 'sha256:' + hashlib.sha256(body.encode()).hexdigest(), 'record': record})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        context = {key: os.environ.get('GITHUB_' + key.upper(), '') for key in ('repository', 'ref', 'workflow_ref', 'workflow_sha', 'event_name')}
        numbers = (os.environ.get('GITHUB_RUN_ID'), os.environ.get('GITHUB_RUN_ATTEMPT'))
        if os.environ.get('ENABLE_RELEASE_AUTOMATION') != 'true' or not all(matches(r'[1-9][0-9]*', value) for value in numbers):
            raise PublicationError('PR origin recording requires explicit automation enablement')
        client = ReleaseGitHub(context['repository'], os.environ.get('GH_TOKEN'))
        result = {'schema': 1, 'publication_authorized': False, 'origins': record_pr_origins(client, context, *(int(value) for value in numbers))}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')
    except (PublicationError, OSError, ValueError, TypeError, RecursionError):
        parser.exit(1, 'Release PR origin recording failed.\n')


if __name__ == '__main__':
    main()
