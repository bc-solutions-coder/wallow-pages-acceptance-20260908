"""Validate publication configuration and exact, authorized output identities."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re


class PublicationError(ValueError):
    pass


@dataclass(frozen=True)
class Producer:
    repository: str
    repository_id: int
    source_sha: str
    run_id: int
    run_attempt: int
    workflow_id: int
    workflow_ref: str


def positive_integer(value):
    return type(value) is int and value > 0


def main_ancestor(comparison, sha):
    return (
        isinstance(comparison, dict)
        and comparison.get('status') in ('ahead', 'identical')
        and isinstance(comparison.get('base_commit'), dict)
        and comparison['base_commit'].get('sha') == sha
        and isinstance(comparison.get('merge_base_commit'), dict)
        and comparison['merge_base_commit'].get('sha') == sha
    )


def authorize_controller(context, repository, comparison):
    """Comparison must be fetched for workflow_sha...the observed main tip."""
    if not isinstance(context, dict) or not isinstance(repository, dict):
        raise PublicationError('Missing controller or repository metadata')
    name = repository.get('full_name')
    if not matches(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name) or repository.get('default_branch') != 'main':
        raise PublicationError('Publication requires a repository with main as its default branch')
    sha = context.get('workflow_sha')
    if (
        context.get('repository') != name
        or context.get('ref') != 'refs/heads/main'
        or context.get('workflow_ref') != name + '/.github/workflows/publish.yml@refs/heads/main'
        or context.get('event_name') not in ('workflow_run', 'workflow_dispatch')
        or not matches(r'[0-9a-f]{40}', sha)
        or not main_ancestor(comparison, sha)
    ):
        raise PublicationError('Publication control code is not an approved main workflow revision')
    return sha


def authorize_main_producer(repository, workflow, run, run_id, attempt, jobs, required_check, comparison):
    """Authorize API-fetched metadata; comparison is head_sha...observed main tip."""
    if not all(isinstance(value, dict) for value in (repository, workflow, run, required_check)) or not isinstance(jobs, list):
        raise PublicationError('Missing producer authorization metadata')
    name = repository.get('full_name')
    repository_id = repository.get('id')
    workflow_id = workflow.get('id')
    if not matches(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name) or not positive_integer(repository_id) or repository.get('default_branch') != 'main':
        raise PublicationError('Invalid producer repository')
    if not positive_integer(workflow_id) or workflow.get('path') != '.github/workflows/ci.yml':
        raise PublicationError('Unexpected CI workflow identity')
    if not all(positive_integer(value) for value in (run_id, attempt, run.get('id'), run.get('run_attempt'))) or run['id'] != run_id or run['run_attempt'] != attempt:
        raise PublicationError('Requested producer run or attempt does not match')
    for key in ('repository', 'head_repository'):
        identity = run.get(key)
        if not isinstance(identity, dict) or not positive_integer(identity.get('id')) or identity['id'] != repository_id or identity.get('full_name') != name:
            raise PublicationError('Producer belongs to a foreign repository')
    sha = run.get('head_sha')
    if (
        not positive_integer(run.get('workflow_id'))
        or run['workflow_id'] != workflow_id
        or run.get('path') != workflow['path']
        or run.get('event') != 'push'
        or run.get('head_branch') != 'main'
        or run.get('status') != 'completed'
        or run.get('conclusion') != 'success'
        or not matches(r'[0-9a-f]{40}', sha)
        or not main_ancestor(comparison, sha)
    ):
        raise PublicationError('Producer is not a successful approved main CI push')
    if any(not isinstance(job, dict) for job in jobs):
        raise PublicationError('Malformed producer job metadata')
    gates = [job for job in jobs if job.get('name') == 'CI / required']
    if len(gates) != 1:
        raise PublicationError('Producer must contain exactly one required aggregate job')
    gate = gates[0]
    if (
        not positive_integer(gate.get('id'))
        or not positive_integer(gate.get('run_id'))
        or not positive_integer(gate.get('run_attempt'))
        or gate.get('run_id') != run_id
        or gate.get('run_attempt') != attempt
        or gate.get('head_sha') != sha
        or gate.get('status') != 'completed'
        or gate.get('conclusion') != 'success'
    ):
        raise PublicationError('Required aggregate does not prove this producer attempt')
    app = required_check.get('app')
    if (
        required_check.get('name') != 'CI / required'
        or required_check.get('head_sha') != sha
        or required_check.get('status') != 'completed'
        or required_check.get('conclusion') != 'success'
        or not isinstance(app, dict)
        or not positive_integer(app.get('id'))
        or app.get('id') != 15368
        or app.get('slug') != 'github-actions'
        or not positive_integer(required_check.get('id'))
        or gate.get('check_run_url') != f"https://api.github.com/repos/{name}/check-runs/{required_check['id']}"
    ):
        raise PublicationError('Required check is not the expected GitHub Actions check for this job')
    return Producer(name, repository_id, sha, run_id, attempt, workflow_id, name + '/.github/workflows/ci.yml@refs/heads/main')


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise PublicationError('Missing or unexpected publication configuration fields')


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_catalog(catalog, release_config, manifests):
    fields(catalog, ('schema', 'default_branch', 'producer_workflow', 'package_registry', 'package_scope', 'components', 'images', 'validation_only_images', 'validation_only_packages', 'docs'))
    if type(catalog['schema']) is not int or catalog['schema'] != 1:
        raise PublicationError('Unsupported publication catalog schema')
    if catalog['default_branch'] != 'main' or catalog['producer_workflow'] != '.github/workflows/ci.yml':
        raise PublicationError('Publication requires the approved main CI producer')
    if catalog['package_registry'] != 'https://npm.pkg.github.com' or not matches(r'@[a-z0-9][a-z0-9-]*', catalog['package_scope']):
        raise PublicationError('Invalid package registry or owner scope')
    components = catalog['components']
    images = catalog['images']
    if not isinstance(components, list) or not components or not isinstance(images, list) or not images:
        raise PublicationError('Publication components and images must be nonempty lists')
    if not isinstance(release_config, dict) or not isinstance(release_config.get('packages'), dict):
        raise PublicationError('Missing release component configuration')
    if not isinstance(manifests, dict):
        raise PublicationError('Missing package manifests')
    component_ids, paths, prefixes, package_names = set(), set(), set(), set()
    for component in components:
        fields(component, ('id', 'path', 'tag_prefix'), ('package',))
        component_id, path, prefix = component['id'], component['path'], component['tag_prefix']
        if not matches(r'[a-z0-9]+(?:-[a-z0-9]+)*', component_id) or component_id in component_ids:
            raise PublicationError('Invalid or duplicate release component')
        if not matches(r'\.|packages/[a-z0-9]+(?:-[a-z0-9]+)*', path) or path in paths:
            raise PublicationError('Invalid or duplicate component path')
        config = release_config['packages'].get(path)
        if not isinstance(config, dict):
            raise PublicationError('Catalog component is absent from release configuration')
        if 'package' in component:
            package = component['package']
            fields(package, ('name', 'tarball'))
            if path == '.' or package['name'] != catalog['package_scope'] + '/' + component_id:
                raise PublicationError('Published package does not match its owner scope and component')
            if package['name'] in package_names or package['tarball'] != component_id + '.tgz':
                raise PublicationError('Duplicate package or unexpected tarball name')
            manifest = manifests.get(path)
            if not isinstance(manifest, dict) or manifest.get('name') != package['name'] or manifest.get('private', False) is not False:
                raise PublicationError('Package source identity is invalid or private')
            publish_config = manifest.get('publishConfig')
            if not isinstance(publish_config, dict) or publish_config.get('registry') != catalog['package_registry']:
                raise PublicationError('Package registry differs from the publication catalog')
            if config.get('package-name') != package['name'] or config.get('component') != component_id or config.get('release-type') != 'node':
                raise PublicationError('Release Please package mapping differs from the catalog')
            package_names.add(package['name'])
        elif path != '.':
            raise PublicationError('A package-path component must declare its package')
        include_component = config.get('include-component-in-tag', release_config.get('include-component-in-tag', False))
        include_v = config.get('include-v-in-tag', release_config.get('include-v-in-tag', True))
        separator = config.get('tag-separator', release_config.get('tag-separator', '-'))
        if type(include_component) is not bool or type(include_v) is not bool or separator not in ('-', '/'):
            raise PublicationError('Unsupported release tag configuration')
        expected_prefix = (component_id + separator if include_component else '') + ('v' if include_v else '')
        if prefix != expected_prefix or prefix in prefixes:
            raise PublicationError('Release tag prefix is inconsistent or ambiguous')
        component_ids.add(component_id)
        paths.add(path)
        prefixes.add(prefix)
    if paths != set(release_config['packages']) or '.' not in paths:
        raise PublicationError('Catalog must cover every configured release component')
    companions = catalog['validation_only_packages']
    if not isinstance(companions, list):
        raise PublicationError('Validation-only packages must be an explicit list')
    tarballs = {component['package']['tarball'] for component in components if 'package' in component}
    for package in companions:
        fields(package, ('name', 'tarball'))
        if not matches(re.escape(catalog['package_scope']) + r'/[a-z0-9]+(?:-[a-z0-9]+)*', package['name']):
            raise PublicationError('Validation-only package must belong to the trusted owner scope')
        if package['name'] in package_names or package['tarball'] != package['name'].split('/')[1] + '.tgz' or package['tarball'] in tarballs:
            raise PublicationError('Duplicate or invalid validation-only package identity')
        package_names.add(package['name'])
        tarballs.add(package['tarball'])
    image_ids, suffixes, tags = set(), set(), set()
    for image in images:
        fields(image, ('id', 'repository_suffix', 'bundle', 'component', 'tags', 'build_args'))
        if not matches(r'[a-z0-9]+(?:-[a-z0-9]+)*', image['id']) or image['id'] in image_ids:
            raise PublicationError('Invalid or duplicate image identity')
        if not matches(r'(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?', image['repository_suffix']) or image['repository_suffix'] in suffixes:
            raise PublicationError('Invalid or duplicate image repository suffix')
        if image['bundle'] not in ('app', 'infra', 'docs') or image['component'] not in component_ids:
            raise PublicationError('Image references an unknown bundle or release component')
        fields(image['tags'], ('linux/amd64', 'linux/arm64'))
        for tag in image['tags'].values():
            if not matches(r'[a-z0-9][a-z0-9_.-]*:[a-zA-Z0-9_.-]+', tag) or tag in tags:
                raise PublicationError('Invalid or duplicate local image tag')
            tags.add(tag)
        if not isinstance(image['build_args'], dict) or any(not matches(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str) for key, value in image['build_args'].items()):
            raise PublicationError('Invalid image build arguments')
        image_ids.add(image['id'])
        suffixes.add(image['repository_suffix'])
    companions = catalog['validation_only_images']
    if not isinstance(companions, list):
        raise PublicationError('Validation-only images must be an explicit list')
    for image in companions:
        fields(image, ('bundle', 'platform', 'tag'))
        if image['bundle'] not in ('app', 'infra', 'docs') or image['platform'] not in ('linux/amd64', 'linux/arm64'):
            raise PublicationError('Invalid validation-only image bundle or platform')
        if not matches(r'[a-z0-9][a-z0-9_.-]*:[a-zA-Z0-9_.-]+', image['tag']) or image['tag'] in tags:
            raise PublicationError('Invalid or duplicate validation-only local image tag')
        tags.add(image['tag'])
    if catalog['docs'] != {'bundle': 'docs', 'payload': 'site.tar.gz', 'variant': 'docfx'}:
        raise PublicationError('Unexpected validated documentation identity')
    return catalog


def image_repository(repository, image):
    if not matches(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise PublicationError('Invalid repository identity')
    return 'ghcr.io/' + repository.lower() + image['repository_suffix']


def load_catalog(root):
    root = Path(root)
    catalog = json.loads((root / '.github/ci/publication.json').read_text())
    release_config = json.loads((root / 'release-please-config.json').read_text())
    manifests = {str(path.parent.relative_to(root)): json.loads(path.read_text()) for path in (root / 'packages').glob('*/package.json')}
    return validate_catalog(catalog, release_config, manifests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['catalog'])
    parser.add_argument('--root', default='.')
    args = parser.parse_args()
    try:
        catalog = load_catalog(args.root)
    except (PublicationError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f'Publication catalog validation failed: {error}\n')
    print(f"Validated {len(catalog['components'])} release components and {len(catalog['images'])} image variants.")


if __name__ == '__main__':
    main()
