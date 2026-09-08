"""Bind downloaded Actions archives to an authorized producer and sealed payload."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import zipfile

from publication import PublicationError, matches, positive_integer


@dataclass(frozen=True)
class Artifact:
    id: int
    name: str
    digest: str
    size: int


def select_artifact(artifacts, producer, prefix, now=None):
    """The caller supplies the complete paginated artifact list for this run."""
    if not matches(r'[a-z][a-z0-9-]*', prefix) or not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
        raise PublicationError('Malformed artifact selection')
    name = f'{prefix}-{producer.run_id}-{producer.run_attempt}'
    candidates = [item for item in artifacts if item.get('name') == name]
    if len(candidates) != 1:
        raise PublicationError('Expected exactly one artifact for this producer attempt')
    item = candidates[0]
    run = item.get('workflow_run')
    if (
        not positive_integer(item.get('id'))
        or not positive_integer(item.get('size_in_bytes'))
        or not matches(r'sha256:[0-9a-f]{64}', item.get('digest'))
        or item.get('expired') is not False
        or not isinstance(run, dict)
        or any(not positive_integer(run.get(key)) for key in ('id', 'repository_id', 'head_repository_id'))
        or run['id'] != producer.run_id
        or run['repository_id'] != producer.repository_id
        or run['head_repository_id'] != producer.repository_id
        or run.get('head_sha') != producer.source_sha
        or run.get('head_branch') != 'main'
    ):
        raise PublicationError('Artifact identity, digest or retention metadata is invalid')
    try:
        expiry = datetime.fromisoformat(item['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= (now or datetime.now(timezone.utc)):
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError):
        raise PublicationError('Artifact has expired or has invalid expiry metadata') from None
    return Artifact(item['id'], name, item['digest'], item['size_in_bytes'])


def unpack_payload(archive, destination, artifact, producer, payload, kind, variant, max_payload_bytes):
    """Write one verified regular payload into a new, dedicated directory."""
    if not matches(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', payload) or not positive_integer(max_payload_bytes):
        raise PublicationError('Invalid payload name or size limit')
    archive = Path(archive)
    destination = Path(destination)
    if archive.is_symlink() or not archive.is_file() or destination.exists() or destination.is_symlink():
        raise PublicationError('Archive must be a regular file and destination must be new')
    with archive.open('rb') as stream:
        if archive.stat().st_size != artifact.size or 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest() != artifact.digest:
            raise PublicationError('Downloaded artifact differs from GitHub digest or size')
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) != 2 or {member.filename for member in members} != {payload, payload + '.json'}:
                raise PublicationError('Artifact contains missing, duplicate or unexpected members')
            for member in members:
                file_type = stat.S_IFMT(member.external_attr >> 16)
                limit = max_payload_bytes if member.filename == payload else 16384
                if member.is_dir() or file_type not in (0, stat.S_IFREG) or member.flag_bits & 1 or member.file_size > limit:
                    raise PublicationError('Artifact contains an unsafe or oversized member')
            manifest = json.loads(bundle.read(payload + '.json'))
            expected = {
                'schema': 1, 'repository': producer.repository, 'sha': producer.source_sha,
                'run_id': str(producer.run_id), 'run_attempt': str(producer.run_attempt),
                'workflow_ref': producer.workflow_ref, 'kind': kind, 'variant': variant,
                'file': payload,
            }
            if not isinstance(manifest, dict) or set(manifest) != set(expected) | {'sha256'} or any(manifest[key] != value or type(manifest[key]) is not type(value) for key, value in expected.items()) or not matches(r'[0-9a-f]{64}', manifest.get('sha256')):
                raise PublicationError('Payload seal does not match the authorized producer')
            destination.mkdir(parents=False)
            output = destination / payload
            try:
                digest = hashlib.sha256()
                size = 0
                with bundle.open(payload) as source, output.open('xb') as target:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > max_payload_bytes:
                            raise PublicationError('Payload exceeds its size limit')
                        digest.update(chunk)
                        target.write(chunk)
                if digest.hexdigest() != manifest['sha256']:
                    raise PublicationError('Payload checksum differs from its seal')
            except BaseException:
                output.unlink(missing_ok=True)
                destination.rmdir()
                raise
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, NotImplementedError, RuntimeError) as error:
        raise PublicationError('Invalid artifact archive') from error
    return output
