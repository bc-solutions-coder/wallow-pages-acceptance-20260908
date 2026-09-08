import hashlib
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
    status, raw = request(oidc_url, {'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    assert status == 200
    oidc = json.loads(raw)['value']
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'}
    payload = {'schema': 1, 'source_sha': source, 'controller_sha': os.environ['GITHUB_SHA'],
               'run_id': int(os.environ['GITHUB_RUN_ID']), 'artifact_id': int(os.environ['ARTIFACT_ID']),
               'tar_sha256': hashlib.sha256(Path('artifact.tar').read_bytes()).hexdigest(),
               'synthetic_protocol_only': True}
    status, raw = request(API + '/deployments', headers,
                          {'ref': source, 'task': 'wallow-pages-v1', 'auto_merge': False,
                           'required_contexts': [], 'environment': 'github-pages',
                           'payload': payload, 'production_environment': False})
    assert status == 201, 'Intent create HTTP ' + str(status)
    intent = json.loads(raw)
    assert intent['sha'] == source and intent['payload'] == payload
    status, raw = request(API + '/deployments/' + str(intent['id']), headers)
    assert status == 200 and json.loads(raw)['payload'] == payload
    status, raw = request(API + '/pages/deployments', headers,
                          {'artifact_id': int(os.environ['ARTIFACT_ID']), 'pages_build_version': source, 'oidc_token': oidc})
    result = {'repository': REPO, 'controller_sha': os.environ['GITHUB_SHA'], 'source_sha': source,
              'artifact_id': int(os.environ['ARTIFACT_ID']), 'create_status': status,
              'intent_id': intent['id'], 'intent_sha': intent['sha'], 'intent_payload_readback': True,
              'intent_creator': {key: intent['creator'][key] for key in ('id', 'login', 'type')}}
    Path('pages-proof.json').write_text(json.dumps(result, indent=2) + '\n')
    if status not in (200, 201):
        raise RuntimeError('Pages create returned HTTP ' + str(status))
    deployment = json.loads(raw)
    ident = deployment['id']
    assert re.fullmatch('[A-Za-z0-9_-]+', ident)
    result['deployment_id'] = ident
    for _ in range(60):
        status, raw = request(API + '/pages/deployments/' + ident, headers)
        assert status == 200
        state = json.loads(raw)['status']
        result['deployment_status'] = state
        Path('pages-proof.json').write_text(json.dumps(result, indent=2) + '\n')
        if state == 'succeed':
            break
        if state in ('deployment_failed', 'deployment_content_failed', 'deployment_cancelled', 'failed', 'cancelled'):
            raise RuntimeError('Pages deployment failed')
        time.sleep(5)
    else:
        raise RuntimeError('Pages deployment did not finish')
    expected = Path('site/acceptance.txt').read_bytes()
    for _ in range(24):
        status, body = request('https://bc-solutions-coder.github.io/wallow-pages-acceptance-20260908/acceptance.txt?run=' + os.environ['GITHUB_RUN_ID'], {'Cache-Control': 'no-cache'})
        if status == 200 and body == expected:
            result['served_bytes_sha256'] = hashlib.sha256(body).hexdigest()
            result['exact_served_bytes'] = True
            break
        time.sleep(5)
    else:
        raise RuntimeError('Published Pages bytes differ from this artifact')
    status, raw = request(API + '/deployments/' + str(intent['id']) + '/statuses', headers,
                          {'state': 'success', 'auto_inactive': False,
                           'description': 'Exact synthetic artifact deployed and served bytes verified',
                           'log_url': 'https://github.com/' + REPO + '/actions/runs/' + os.environ['GITHUB_RUN_ID'],
                           'environment_url': 'https://bc-solutions-coder.github.io/wallow-pages-acceptance-20260908/'})
    assert status == 201
    completion = json.loads(raw)
    status, raw = request(API + '/deployments/' + str(intent['id']) + '/statuses', headers)
    assert status == 200 and any(item['id'] == completion['id'] and item['state'] == 'success' for item in json.loads(raw))
    result['intent_success_status_id'] = completion['id']
    Path('pages-proof.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
