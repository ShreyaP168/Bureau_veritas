import json
from pathlib import Path
import uuid

import pytest

from veritas.operations import Operations
from veritas.uc.storage import write_json


@pytest.fixture
def workspace():
    root = Path('runtime') / ('operations-tests-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    return root


def save_run(root, country, currency, amount, status, issued='2026-11-13T10:00:00+00:00', month='2026-10'):
    run_id, item_id = uuid.uuid4().hex, uuid.uuid4().hex
    item = {'id': item_id, 'status': status, 'step': 'final_review' if status == 'needs_review' else status,
            'wip': {'contract_number': '=FORMULA', 'audit_type': 'Special Audit', 'start_date': month + '-12', 'currency': currency},
            'data': {}, 'error': 'Amount mismatch' if status == 'blocked' else None}
    events = []
    if amount:
        item['data']['download_final'] = {'net_amount': amount, 'gross_amount': amount, 'currency': currency}
        events.append({'action': 'download_final', 'status': 'completed', 'at': issued, 'item_id': item_id, 'actor': 'Billing operator'})
    run = {'id': run_id, 'country': country, 'month': month, 'mode': 'demo', 'items': [item], 'events': events}
    write_json(root / 'invoicing' / 'demo' / (run_id + '.json'), run)
    return run


def test_dashboard_counts_final_invoice_before_delivery_and_separates_currencies(workspace):
    save_run(workspace, 'Dubai', 'AED', '1000.10', 'needs_review')
    save_run(workspace, 'South Africa', 'USD', '50.20', 'completed')
    save_run(workspace, 'Dubai', 'AED', None, 'blocked')
    app = Operations(workspace)
    report = app.get_dashboard('demo')
    assert report['counts'] == {'total': 3, 'processed': 1, 'pending': 1, 'exceptions': 1, 'awaiting_review': 1, 'finalized': 2}
    assert report['values_by_currency']['AED']['net'] == '1000.10'
    assert report['values_by_currency']['USD']['net'] == '50.20'
    # Queue periods and issuance periods must not be conflated for backlog.
    november = app.get_dashboard('demo', '2026-11')
    assert november['counts']['total'] == 0
    assert november['values_by_currency']['AED']['count'] == 1
    october = app.get_dashboard('demo', '2026-10')
    assert october['counts']['total'] == 3
    assert october['values_by_currency'] == {}
    assert len(report['daily']) == len(report['monthly']) == 2


def test_exports_escape_formulas_and_preserve_actor(workspace):
    save_run(workspace, 'Dubai', 'AED', '1', 'completed')
    exported = Operations(workspace).export_csv('demo').decode('utf-8-sig')
    assert "'=FORMULA" in exported
    assert 'Billing operator' in exported


def test_case_metadata_cannot_change_or_override_financial_state(workspace):
    run = save_run(workspace, 'Dubai', 'AED', None, 'blocked')
    app = Operations(workspace)
    path = workspace / 'invoicing' / 'demo' / (run['id'] + '.json')
    before = path.read_bytes()
    case = app.update_case('demo', run['id'], run['items'][0]['id'],
                           {'actor': 'Reviewer', 'owner': 'Finance', 'note': 'Asked for corrected proposal', 'status': 'in_progress'})
    assert case['owner'] == 'Finance'
    assert case['history'][0]['actor'] == 'Reviewer'
    with pytest.raises(ValueError, match='Resolution notes'):
        app.update_case('demo', run['id'], run['items'][0]['id'], {'actor': 'Reviewer', 'status': 'resolved'})
    app.update_case('demo', run['id'], run['items'][0]['id'], {'actor': 'Reviewer', 'status': 'resolved', 'resolution': 'Corrected evidence requested'})
    assert len(app.list_cases('demo')[0]['history']) == 2
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match='Unknown case'):
        app.update_case('demo', run['id'], run['items'][0]['id'], {'actor': 'Reviewer', 'amount': '1'})


def test_case_rejects_unknown_or_nonexception_item(workspace):
    run = save_run(workspace, 'Dubai', 'AED', '1', 'needs_review')
    app = Operations(workspace)
    with pytest.raises(ValueError, match='Only exception'):
        app.update_case('demo', run['id'], run['items'][0]['id'], {'actor': 'Reviewer'})
    with pytest.raises(ValueError, match='Invalid run'):
        app.update_case('demo', '../secret', run['items'][0]['id'], {'actor': 'Reviewer'})


def test_settings_are_validated_audited_and_never_expose_credentials(workspace, monkeypatch):
    app = Operations(workspace)
    before = app.get_settings()
    with pytest.raises(ValueError, match='Unknown settings'):
        app.save_settings({'actor': 'Admin', 'token': 'no-secrets'})
    assert app.get_settings() == before
    saved = app.save_settings({'actor': 'Admin', 'scope': {'automation': 'assisted'},
                              'client_details': {'escalation_owner': 'Finance queue'},
                              'baseline': {'Dubai': {'monthly_volume': 120, 'minutes_per_invoice': 25, 'hourly_cost': 45, 'day_rate': 500}}})
    assert saved['baseline']['Dubai']['monthly_volume'] == '120'
    assert saved['history'][0]['actor'] == 'Admin'
    with pytest.raises(ValueError, match='out of range'):
        app.save_settings({'actor': 'Admin', 'baseline': {'Dubai': {'error_rate_percent': 150}}})
    monkeypatch.setenv('VERITAS_INVOICE_GATEWAY_URL', 'https://example.test/secret-url')
    monkeypatch.setenv('VERITAS_INVOICE_GATEWAY_TOKEN', 'private-token')
    ready = app.readiness()
    assert ready['gateway_configured'] is True
    assert ready['production_ready'] is False
    assert 'private-token' not in json.dumps(ready)
    assert 'secret-url' not in json.dumps(ready)


def test_invalid_period_and_currency_data_are_explicit(workspace):
    run = save_run(workspace, 'Dubai', 'AED', 'NaN', 'completed')
    app = Operations(workspace)
    assert app.get_dashboard('demo')['warnings']
    assert not app.get_dashboard('demo')['values_by_currency']
    with pytest.raises(ValueError, match='month'):
        app.get_dashboard('demo', 'not-a-month')


def test_issued_period_uses_client_timezone_at_month_end(workspace):
    save_run(workspace, 'Dubai', 'AED', '10', 'completed', issued='2026-10-31T20:00:00+00:00')
    app = Operations(workspace)
    assert app.get_dashboard('demo', '2026-10')['values_by_currency'] == {}
    november = app.get_dashboard('demo', '2026-11')
    assert november['daily'][0]['date'] == '2026-11-01'
    assert november['values_by_currency']['AED']['net'] == '10'
