import hashlib
from publication_pages_github import PagesGitHub
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


REPO = 'bc-solutions-coder/wallow-pages-acceptance-20260908'
API = 'https://api.github.com/repos/' + REPO


def request(url, headers, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, headers=headers, data=body)
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read(1024 * 1024 + 1)
        assert len(content) <= 1024 * 1024
        return response.status, content


def main():
    assert os.environ['GITHUB_REPOSITORY'] == REPO and os.environ['GITHUB_REF'] == 'refs/heads/main'
    source = os.environ['BUILD_SOURCE_SHA']
    assert re.fullmatch('[a-f0-9]{40}', source) and source != os.environ['GITHUB_SHA']
    oidc_url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    assert oidc_url.startswith('https://')
    client = PagesGitHub(REPO, os.environ['GH_TOKEN'])
    oidc = client.oidc(oidc_url, os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'])
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'}
    payload = {'schema': 1, 'source_sha': source, 'controller_sha': os.environ['GITHUB_SHA'],
               'run_id': int(os.environ['GITHUB_RUN_ID']), 'artifact_id': int(os.environ['ARTIFACT_ID']),
               'tar_sha256': hashlib.sha256(Path('artifact.tar').read_bytes()).hexdigest(),
               'synthetic_protocol_only': True}
    intent = client.intent(payload)
    assert intent['sha'] == source and intent['payload'] == payload
    status, raw = request(API + '/deployments/' + str(intent['id']), headers)
    assert status == 200 and json.loads(raw)['payload'] == payload
    deployment = client.deploy(int(os.environ['ARTIFACT_ID']), source, oidc)
    status = 200
    result = {'repository': REPO, 'controller_sha': os.environ['GITHUB_SHA'], 'source_sha': source,
              'artifact_id': int(os.environ['ARTIFACT_ID']), 'create_status': status,
              'intent_id': intent['id'], 'intent_sha': intent['sha'], 'intent_payload_readback': True,
              'intent_creator': {key: intent['creator'][key] for key in ('id', 'login', 'type')}}
    Path('pages-proof.json').write_text(json.dumps(result, indent=2) + '\n')
    if status not in (200, 201):
        raise RuntimeError('Pages create returned HTTP ' + str(status))
    ident = deployment['id']
    assert re.fullmatch('[A-Za-z0-9_-]+', ident)
    result['deployment_id'] = ident
    result['deployment_status'] = client.wait_deployment(ident)['status']
    expected = Path('site/index.html').read_bytes()
    for _ in range(24):
        status, body = request('https://bc-solutions-coder.github.io/wallow-pages-acceptance-20260908/index.html?run=' + os.environ['GITHUB_RUN_ID'], {'Cache-Control': 'no-cache'})
        if status == 200 and body == expected:
            result['served_bytes_sha256'] = hashlib.sha256(body).hexdigest()
            result['exact_served_bytes'] = True
            break
        time.sleep(5)
    else:
        raise RuntimeError('Published Pages bytes differ from this artifact')
    completion = client.status(intent['id'], 'success', int(os.environ['GITHUB_RUN_ID']), 'https://bc-solutions-coder.github.io/wallow-pages-acceptance-20260908/', 'Actual helper transport verified')
    index = Path('site/index.html').read_bytes()
    result['helper_index_readback'] = client.verify_index('https://bc-solutions-coder.github.io/wallow-pages-acceptance-20260908/', {'index_size': len(index), 'index_sha256': hashlib.sha256(index).hexdigest()})
    status, raw = request(API + '/deployments/' + str(intent['id']) + '/statuses', headers)
    assert status == 200 and any(item['id'] == completion['id'] and item['state'] == 'success' for item in json.loads(raw))
    result['intent_success_status_id'] = completion['id']
    Path('pages-proof.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
