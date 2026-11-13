import json
import threading
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from veritas import web


@pytest.fixture
def server(monkeypatch):
    tmp_path = Path('runtime') / ('web-check-' + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    monkeypatch.setattr(web, 'OUTPUT', tmp_path)
    monkeypatch.setattr(web, 'STATE', {'busy': False, 'message': 'Ready', 'error': None, 'results': [
        {'filename': 'proposal.pdf', 'fields': {key: None for key in web.FIELDS}, 'warnings': []}
    ]})
    httpd = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}', tmp_path
    httpd.shutdown()
    httpd.server_close()


def test_review_persists_exports_and_history(server):
    base, output = server
    values = {key: 'Confirmed value' for key in web.FIELDS}
    payload = json.dumps({'index': 0, 'values': values}).encode()
    with urlopen(Request(base + '/api/review', data=payload, headers={'Content-Type': 'application/json'})) as response:
        assert response.status == 200
    saved = json.loads((output / 'results.json').read_text())
    assert saved[0]['fields']['audit_type']['source'] == 'manual'
    assert not saved[0]['fields']['amount']['needs_review']
    assert len(saved[0]['review_history']) == 5
    assert (output / 'results.csv').exists()


def test_cross_origin_write_is_rejected(server):
    base, _ = server
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + '/api/upload?name=test.pdf', data=b'fake', headers={'Origin': 'https://example.com'}))
    assert error.value.code == 403


def test_invalid_file_and_document_rejected(server):
    base, _ = server
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + '/api/upload?name=test.exe', data=b'fake'))
    assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        urlopen(base + '/api/document?index=-1')
    assert error.value.code == 404


def test_invoice_api_completes_demo_with_two_reviews(server):
    base, _ = server
    source = Path('scripts/wip-demo.csv').read_bytes()
    with urlopen(Request(base + '/api/invoicing/import?mode=demo&month=2026-11&name=wip.csv', data=source)) as response:
        run = json.load(response)
    url = base + '/api/invoicing/' + run['id']
    with urlopen(Request(url + '/advance?mode=demo', data=b'{}')) as response:
        draft = json.load(response)
    assert draft['items'][0]['step'] == 'draft_review'
    review = json.dumps({'reviewer': 'Demo tester'}).encode()
    with urlopen(Request(url + '/approve?mode=demo', data=review)) as response:
        final = json.load(response)
    assert final['items'][0]['step'] == 'final_review'
    with urlopen(Request(url + '/approve?mode=demo', data=review)) as response:
        done = json.load(response)
    assert done['status'] == 'completed'
    assert done['items'][0]['data']['send_email']['simulated'] is True
    with urlopen(url + '?mode=demo') as response:
        assert json.load(response)['status'] == 'completed'


def test_ui_samples_are_idempotent_and_artifacts_verified(server):
    base, _ = server
    def seed():
        with urlopen(Request(base + '/api/invoicing/seed-demo?mode=demo', data=b'{}')) as response:
            return json.load(response)
    runs = seed()
    assert {r['country'] for r in runs} == {'Dubai', 'South Africa'}
    assert all(r['status'] == 'needs_review' for r in runs[:2])
    assert runs[2]['status'] == 'completed'
    assert runs[3]['status'] == 'blocked'
    assert {r['id'] for r in seed()} == {r['id'] for r in runs}
    with urlopen(base + '/api/invoicing?mode=demo') as response:
        assert len(json.load(response)) == 4
    run = runs[0]
    item = run['items'][0]
    url = base + '/api/invoicing/' + run['id'] + '/artifact?mode=demo&item=' + item['id']
    with urlopen(url + '&stage=download_draft') as response:
        assert response.headers['Content-Type'] == 'application/pdf'
        import pymupdf
        with pymupdf.open(stream=response.read(), filetype='pdf') as document:
            assert 'DEMO' in ''.join(page.get_text() for page in document)
    with pytest.raises(HTTPError):
        urlopen(url + '&stage=../../secrets')
    Path(item['data']['download_draft']['path']).write_text('Tampered', encoding='utf-8')
    with pytest.raises(HTTPError) as error:
        urlopen(url + '&stage=download_draft')
    assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + '/api/invoicing/seed-demo?mode=live', data=b'{}'))
    assert error.value.code == 400
