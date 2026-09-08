"""Authenticate durable Pages intents and keep retries forward-only."""

from datetime import datetime, timezone
import hashlib

from publication import PublicationError, main_ancestor, matches, positive_integer
from publication_release_github import ACTIONS_ACTOR
from publication_release_origin import canonical, recorded_job, timestamp


TASK = 'wallow-pages-v1'
JOB = 'Publish validated Pages'


def site_identity(site):
    if not isinstance(site, dict) or not matches(r'[a-f0-9]{64}', site.get('sha256')) or not positive_integer(site.get('size')):
        raise PublicationError('Prepared Pages content identity is missing')
    files = site.get('files')
    if not isinstance(files, list) or not files or len(files) > 100000:
        raise PublicationError('Prepared Pages file inventory is missing or oversized')
    names = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'} or not isinstance(item['path'], str) or not matches(r'[a-f0-9]{64}', item['sha256']) or type(item['size']) is not int or item['size'] < 0:
            raise PublicationError('Prepared Pages file identity is invalid')
        names.append(item['path'])
    if names != sorted(set(names)) or 'index.html' not in names:
        raise PublicationError('Prepared Pages inventory is duplicated, unordered or lacks index.html')
    index = files[names.index('index.html')]
    return {'tar_sha256': site['sha256'], 'tar_size': site['size'],
            'inventory_sha256': hashlib.sha256(canonical(files)).hexdigest(),
            'file_count': len(files), 'index_sha256': index['sha256'], 'index_size': index['size']}


def inspect_intent(client, deployment, current=None):
    if not isinstance(deployment, dict) or not positive_integer(deployment.get('id')) or deployment.get('task') != TASK or deployment.get('environment') != 'github-pages' or {key: deployment.get('creator', {}).get(key) for key in ACTIONS_ACTOR} != ACTIONS_ACTOR:
        raise PublicationError('Pages deployment has unknown provenance')
    payload = deployment.get('payload')
    if not isinstance(payload, dict) or set(payload) != {'schema', 'source_sha', 'producer', 'artifact', 'site', 'recorder'} or type(payload['schema']) is not int or payload['schema'] != 1 or not matches(r'[a-f0-9]{40}', payload['source_sha']) or deployment.get('sha') != payload['source_sha']:
        raise PublicationError('Pages intent identity differs from its deployment')
    producer, artifact, site = payload['producer'], payload['artifact'], payload['site']
    if not isinstance(producer, dict) or producer.get('repository') != client.repository or producer.get('source_sha') != payload['source_sha'] or not all(positive_integer(producer.get(key)) for key in ('repository_id', 'run_id', 'run_attempt', 'workflow_id')):
        raise PublicationError('Pages intent lacks an exact same-repository producer')
    if not isinstance(artifact, dict) or set(artifact) != {'id', 'name', 'digest', 'size'} or not positive_integer(artifact['id']) or not positive_integer(artifact['size']) or not matches(r'sha256:[a-f0-9]{64}', artifact['digest']):
        raise PublicationError('Pages intent lacks an exact artifact identity')
    if not isinstance(site, dict) or set(site) != {'tar_sha256', 'tar_size', 'inventory_sha256', 'file_count', 'index_sha256', 'index_size'} or not all(matches(r'[a-f0-9]{64}', site[key]) for key in ('tar_sha256', 'inventory_sha256', 'index_sha256')) or not all(positive_integer(site[key]) for key in ('tar_size', 'file_count')) or type(site['index_size']) is not int or site['index_size'] < 0:
        raise PublicationError('Pages intent lacks complete content identity')
    if current is None:
        job = recorded_job(client, payload['recorder'], JOB)
        end = timestamp(job.get('completed_at'))
    else:
        recorder, job = current
        if payload['recorder'] != recorder:
            raise PublicationError('New Pages intent differs from the active protected writer')
        end = datetime.now(timezone.utc)
    if artifact['name'] != f"prepared-site-{payload['recorder']['run_id']}-{payload['recorder']['run_attempt']}":
        raise PublicationError('Pages artifact belongs to another preparation invocation')
    if not timestamp(job.get('started_at')) <= timestamp(deployment.get('created_at')) <= end:
        raise PublicationError('Pages intent was not created during its protected writer job')
    if not main_ancestor(client.main_comparison(payload['source_sha']), payload['source_sha']):
        raise PublicationError('Prior Pages source is outside current main history')
    return payload


def pages_action(client, source, site, intents):
    """Every supplied intent has passed inspect_intent, including failed jobs."""
    if not matches(r'[a-f0-9]{40}', source) or not main_ancestor(client.main_comparison(source), source):
        raise PublicationError('Requested Pages source is outside current main history')
    action = 'deploy'
    for intent in intents:
        previous = intent['source_sha']
        if previous == source:
            if intent['site'] != site:
                raise PublicationError('Same-source Pages retry conflicts with previously prepared bytes')
            continue
        comparison = client.get(f'/compare/{previous}...{source}')
        if main_ancestor(comparison, previous):
            continue
        if isinstance(comparison, dict) and comparison.get('status') == 'behind' and comparison.get('base_commit', {}).get('sha') == previous and comparison.get('merge_base_commit', {}).get('sha') == source:
            action = 'skip-older'
            continue
        raise PublicationError('Pages source history is incomparable or unresolved')
    return action
