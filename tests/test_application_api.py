"""HTTP integration checks for the complete local assisted application."""
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import pymupdf
import pytest

from veritas import web
from veritas.invoice_rules import audit_comment


@pytest.fixture
def server(monkeypatch):
    output = Path('runtime') / ('application-http-' + uuid.uuid4().hex)
    output.mkdir(parents=True)
    monkeypatch.setattr(web, 'OUTPUT', output)
    monkeypatch.setattr(web, 'STATE', {'busy': False, 'message': 'Ready', 'error': None, 'results': []})
    httpd = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield 'http://127.0.0.1:' + str(httpd.server_port), output
    httpd.shutdown()
    httpd.server_close()


def request(base, path, payload=None, body=None):
    if payload is not None:
        body = json.dumps(payload).encode()
    req = Request(base + path, data=body)
    with urlopen(req) as response:
        return json.load(response)


def pdf_bytes():
    with pymupdf.open() as document:
        page = document.new_page()
        # Long text keeps this fixture on the local PDF text path, not OCR.
        text = 'Signed contract. Proposal number: TEST-900. Special Audit. Customer Example. '
        text += 'The client and provider agree to the audit scope, schedule and commercial offer. ' * 35
        page.insert_textbox(pymupdf.Rect(30, 30, 560, 780), text, fontsize=10)
        return document.tobytes()


def test_settings_policies_and_dashboard_are_available_and_audited(server):
    base, output = server
    assert request(base, '/api/application/dashboard?mode=assisted')['counts']['total'] == 0
    with pytest.raises(HTTPError):
        request(base, '/api/application/settings', {'scope': {'automation': 'assisted'}})
    saved = request(base, '/api/application/settings', {'actor': 'Admin', 'baseline': {'Dubai': {'monthly_volume': 100}}})
    assert saved['baseline']['Dubai']['monthly_volume'] == '100'
    assert saved['history'][-1]['actor'] == 'Admin'
    policies = request(base, '/api/application/policies')
    assert 'A' in policies['Dubai']['aliases']
    policies['Dubai']['live_enabled'] = False
    with pytest.raises(HTTPError):
        request(base, '/api/application/policies', {'policies': policies})
    with pytest.raises(HTTPError):
        request(base, '/api/application/policies', {'actor': 'Admin'})
    request(base, '/api/application/policies', {'actor': 'Admin', 'policies': policies})
    assert (output / 'operations' / 'countries.json').exists()
    readiness = request(base, '/api/application/readiness')
    assert readiness['country_policy_status']['Dubai']['live_enabled'] is False
    assert readiness['production_ready'] is False


def test_missing_live_access_stops_report_fetch(server, monkeypatch):
    base, _ = server
    monkeypatch.delenv('VERITAS_INVOICE_GATEWAY_URL', raising=False)
    monkeypatch.delenv('VERITAS_INVOICE_GATEWAY_TOKEN', raising=False)
    with pytest.raises(HTTPError) as error:
        request(base, '/api/application/fetch-wip', {'actor': 'Operator', 'country': 'Dubai', 'month': '2026-11'})
    assert error.value.code == 400
    assert b'not configured' in error.value.read()


def test_evidence_upload_download_and_hash_validation(server):
    base, _ = server
    body = pdf_bytes()
    with pytest.raises(HTTPError):
        request(base, '/api/application/documents?name=contract.pdf', body=body)
    uploaded = request(base, '/api/application/documents?name=contract.pdf&actor=Operator', body=body)
    assert uploaded['sha256']
    listing = request(base, '/api/application/documents')
    assert len(listing) == 1
    identifier = uploaded.get('id') or uploaded['sha256']
    with urlopen(base + '/api/application/document?id=' + identifier) as response:
        assert response.headers['Content-Type'] == 'application/pdf'
        assert response.read() == body
    Path(uploaded['path']).write_bytes(b'changed')
    with pytest.raises(HTTPError) as error:
        request(base, '/api/application/document?id=' + identifier)
    assert error.value.code == 400


def test_assisted_contract_task_requires_actor_and_verified_uploaded_evidence(server):
    base, _ = server
    source = Path('scripts/wip-demo.csv').read_bytes()
    created = request(base, '/api/invoicing/import?mode=assisted&month=2026-11&country=Dubai&name=wip.csv&actor=Operator', body=source)
    path = '/api/invoicing/' + created['id'] + '/advance?mode=assisted'
    with pytest.raises(HTTPError):
        request(base, path, {})
    waiting = request(base, path, {'actor': 'Operator'})
    assert waiting['items'][0]['status'] == 'waiting_manual'
    tasks = request(base, '/api/application/tasks?mode=assisted')
    assert len(tasks) == 1 and tasks[0]['action'] == 'contract'
    task = tasks[0]
    document = request(base, '/api/application/documents?name=contract.pdf&actor=Operator', body=pdf_bytes())
    wip = waiting['items'][0]['wip']
    response = dict(task['response_template'])
    response.update(signed_contract_path=document['path'], proposal_number='DBA/TEST/900',
                    client_email='customer@example.invalid', customer_name='Customer Example',
                    amount=wip['amount'], currency=wip['currency'], audit_type=wip['audit_type'],
                    start_date=wip['start_date'], line_item_id='LINE-1', work_order_id='WO-1',
                    work_order_exists=True, work_order_start_date=wip['start_date'],
                    work_order_amount=wip['amount'], work_order_currency=wip['currency'],
                    work_order_audit_type=wip['audit_type'], work_order_address='Dubai address',
                    tax_rate_area='AE10', tax_rate='0.05', billing_jurisdiction='Dubai',
                    comments=audit_comment(wip['start_date'], wip.get('comments', '')))
    submit = '/api/application/tasks/' + task['id'] + '/submit?mode=assisted'
    with pytest.raises(HTTPError):
        request(base, submit, {'response': response})
    with pytest.raises(HTTPError):
        request(base, submit, {'actor': 'Operator', 'response': dict(response, amount='1.00')})
    mismatch = request(base, '/api/application/dashboard?mode=assisted')
    assert mismatch['counts']['exceptions'] == 1
    assert 'does not match WIP' in mismatch['items'][0]['exception_reason']
    advanced = request(base, submit, {'actor': 'Operator', 'response': response})
    assert advanced['items'][0]['step'] == 'create_charges'
    assert advanced['items'][0]['data']['contract']['work_order_id'] == 'WO-1'
    assert any(event.get('actor') == 'Operator' for event in advanced['events'])
    contract = advanced['items'][0]['data']['contract']
    common_invoice = {'path': document['path'], 'invoice_number': 'INV-TEST-1',
                      'amount': '1500.00', 'net_amount': '1500.00', 'tax_amount': '75.00',
                      'gross_amount': '1575.00', 'currency': 'AED', 'tax_rate': '0.05',
                      'customer_name': contract['customer_name'], 'audit_dates': [wip['start_date']],
                      'contract_number': wip['contract_number'], 'order_number': 'ORDER-TEST-1',
                      'billing_address': contract['work_order_address'], 'tax_rate_area': 'AE10'}
    from veritas.invoicing import REQUIRED_JOBS
    responses = {
        'create_charges': {'invoice_number': 'CHARGE-TEST-1', 'amount': '1500.00'},
        'send_charges': {'sent': True},
        'charge_status': {'ready': True, 'status': 'Handled'},
        'flex': {'order_number': 'ORDER-TEST-1', 'proposal_number': contract['proposal_number'],
                 'billing_address': contract['work_order_address'], 'tax_rate_area': 'AE10'},
        'request_draft': {'job_id': 'DRAFT-JOB-1'},
        'draft_reports': {'ready': True, 'reports': sorted(REQUIRED_JOBS)},
        'download_draft': common_invoice,
        'request_final': {'job_id': 'FINAL-JOB-1'},
        'final_reports': {'ready': True, 'reports': sorted(REQUIRED_JOBS)},
        'download_final': common_invoice,
        'send_email': {'message_id': 'EXTERNAL-RECORDED-MESSAGE', 'recipient': contract['client_email']},
    }
    for _ in range(20):
        if advanced['status'] == 'completed':
            break
        if advanced['status'] == 'needs_review':
            advanced = request(base, '/api/invoicing/' + created['id'] + '/approve?mode=assisted',
                               {'actor': 'Reviewer', 'reviewer': 'Reviewer', 'reviewer_role': 'reviewer'})
            continue
        assert advanced['status'] == 'waiting_manual', advanced['errors']
        step = advanced['items'][0]['step']
        tasks = request(base, '/api/application/tasks?mode=assisted')
        active = next(task for task in tasks if task['action'] == step and task['status'] == 'pending')
        advanced = request(base, '/api/application/tasks/' + active['id'] + '/submit?mode=assisted',
                           {'actor': 'Operator', 'response': responses[step], 'reason': 'Fixture verified by operator'})
    assert advanced['status'] == 'completed'
    final = advanced['items'][0]['data']['download_final']
    assert Path(final['path']).is_file()
    assert 'filed' in Path(final['path']).parts
    assert final['filing']['invoice_number'] == 'INV-TEST-1'
    dashboard = request(base, '/api/application/dashboard?mode=assisted')
    assert dashboard['counts']['processed'] == 1
    assert dashboard['values_by_currency']['AED']['gross'] == '1575.00'


def test_exception_assignment_records_case_without_modifying_financial_state(server):
    from veritas.uc.storage import write_json
    base, output = server
    run_id, item_id = uuid.uuid4().hex, uuid.uuid4().hex
    run = {'id': run_id, 'mode': 'assisted', 'country': 'Dubai', 'month': '2026-11',
           'items': [{'id': item_id, 'status': 'blocked', 'wip': {'contract_number': 'TEST-1'}, 'error': 'Amount mismatch'}]}
    path = output / 'invoicing' / 'assisted' / (run_id + '.json')
    write_json(path, run)
    before = path.read_bytes()
    payload = {'actor': 'Finance reviewer', 'run_id': run_id, 'item_id': item_id, 'owner': 'Finance', 'note': 'Requested correction'}
    case = request(base, '/api/application/cases?mode=assisted', payload)
    assert case['owner'] == 'Finance'
    assert case['history'][0]['actor'] == 'Finance reviewer'
    assert path.read_bytes() == before
    assert len(request(base, '/api/application/cases?mode=assisted')) == 1
