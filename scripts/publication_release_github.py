"""Bounded fixed-repository GitHub operations for release provenance records."""

import hashlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from publication import PublicationError, matches, positive_integer
from publication_github import GitHub


RECORD_LIMIT = 4 * 1024 * 1024
ACTIONS_ACTOR = {'id': 41898282, 'login': 'github-actions[bot]', 'type': 'Bot'}


class ReleaseGitHub(GitHub):
    def array(self, path):
        result, seen = [], set()
        separator = '&' if '?' in path else '?'
        for page in range(1, 101):
            values = self.get(f'{path}{separator}per_page=100&page={page}')
            if not isinstance(values, list) or any(not isinstance(value, dict) or not positive_integer(value.get('id')) or value['id'] in seen for value in values):
                raise PublicationError('GitHub record enumeration is incomplete or ambiguous')
            ids = [value['id'] for value in values]
            if len(ids) != len(set(ids)):
                raise PublicationError('GitHub record enumeration contains duplicate identities')
            seen.update(ids)
            result.extend(values)
            if len(values) < 100:
                return result
        raise PublicationError('GitHub record enumeration exceeds its bounded limit')

    def collection(self, path, key):
        result, total = [], None
        separator = '&' if '?' in path else '?'
        for page in range(1, 101):
            document = self.get(f'{path}{separator}per_page=100&page={page}')
            if not isinstance(document, dict):
                raise PublicationError('GitHub workflow enumeration is malformed')
            count, values = document.get('total_count'), document.get(key)
            if type(count) is not int or not 0 <= count <= 1000 or not isinstance(values, list) or any(not isinstance(value, dict) for value in values) or (total is not None and count != total):
                raise PublicationError('GitHub workflow enumeration is incomplete or changed')
            total = count
            result.extend(values)
            if len(result) == total:
                ids = [value.get('id') for value in result]
                if any(not positive_integer(value) for value in ids) or len(ids) != len(set(ids)):
                    raise PublicationError('GitHub workflow enumeration is ambiguous')
                return result
            if not values or len(result) > total:
                break
        raise PublicationError('GitHub workflow enumeration is incomplete')

    def send(self, request, expected):
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status != expected:
                    raise PublicationError('GitHub provenance operation did not return success')
                data = response.read(RECORD_LIMIT + 1)
                if len(data) > RECORD_LIMIT:
                    raise PublicationError('GitHub provenance response exceeds its limit')
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise PublicationError('GitHub provenance response is malformed')
                return value
        except urllib.error.HTTPError as error:
            error.close()
            raise PublicationError('GitHub provenance operation failed') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError):
            raise PublicationError('GitHub provenance operation failed') from None

    def comment(self, number, body):
        if not positive_integer(number) or not isinstance(body, str) or len(body.encode()) > 60000:
            raise PublicationError('PR provenance comment exceeds its bounded identity or size')
        request = self.request(f'/issues/{number}/comments')
        request.method, request.data = 'POST', json.dumps({'body': body}).encode()
        request.add_header('Content-Type', 'application/json')
        return self.send(request, 201)

    def upload(self, release_id, name, body):
        if not positive_integer(release_id) or not (name in ('wallow-release-origin-v1.json', 'wallow-release-selection-v1.json') or matches(r'wallow-release-endorsement-v1-[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*\.json', name)) or not isinstance(body, bytes) or not 0 < len(body) <= RECORD_LIMIT:
            raise PublicationError('Release receipt requires a fixed name and bounded bytes')
        url = 'https://uploads.github.com/repos/' + self.repository + f'/releases/{release_id}/assets?name=' + urllib.parse.quote(name, safe='')
        request = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                                             'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'wallow-release-provenance'}, method='POST')
        return self.send(request, 201)

    def asset_bytes(self, asset):
        if not positive_integer(asset.get('id')) or not positive_integer(asset.get('size')) or asset['size'] > RECORD_LIMIT or not matches(r'sha256:[a-f0-9]{64}', asset.get('digest')):
            raise PublicationError('Release asset metadata lacks exact bounded identity')
        request = self.request('/releases/assets/' + str(asset['id']))
        request.add_header('Accept', 'application/octet-stream')
        try:
            response = self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            try:
                if error.code != 302:
                    raise PublicationError('Release asset is unavailable') from None
                location = error.headers.get('Location', '')
            finally:
                error.close()
            try:
                parsed = urllib.parse.urlsplit(location)
                valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment and parsed.port in (None, 443)
            except ValueError:
                valid = False
            if not valid:
                raise PublicationError('Release asset storage redirect is invalid')
            request = urllib.request.Request(location, headers={'User-Agent': 'wallow-release-provenance'})
            try:
                response = self.opener.open(request, timeout=60)
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                raise PublicationError('Release asset storage could not be read') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise PublicationError('Release asset could not be read') from None
        try:
            with response:
                if response.status != 200:
                    raise PublicationError('Release asset storage did not return success')
                body = response.read(RECORD_LIMIT + 1)
            if len(body) != asset['size'] or 'sha256:' + hashlib.sha256(body).hexdigest() != asset['digest']:
                raise PublicationError('Release asset differs from its API size or digest')
            return body
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise PublicationError('Release asset could not be read') from None
