"""Behavior checks for the special audit orchestration and its safety gates."""
from pathlib import Path
import uuid

import pytest

from veritas.invoicing import DemoAdapter, InvoiceWorkflow, OutcomeUnknown, UnconfiguredAdapter


@pytest.fixture
def workspace():
    # Keep artifacts inside the project on managed Windows hosts.
    root = Path('runtime') / ('invoice-tests-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    return root


@pytest.fixture
def wip_row():
    return {
        'country': 'Dubai', 'business_unit': 'Dubai CER',
        'invoice_status': 'Completed', 'start_date': '2026-11-04',
        'audit_type': 'Special Audit', 'contract_number': 'TEST-100',
        'amount': '1000.00', 'currency': 'AED',
        'po_number': 'PO-100', 'comments': '',
    }


def workflow(root, adapter=None, **kwargs):
    return InvoiceWorkflow(root / 'runs', adapter or DemoAdapter(root / 'artifacts'), **kwargs)


def test_filters_and_duplicate_rows(workspace, wip_row):
    excluded = [dict(wip_row, **change) for change in (
        {'country': 'South Africa'}, {'business_unit': 'Other'},
        {'invoice_status': 'New'}, {'audit_type': 'Surveillance'},
        {'start_date': '2026-12-04'},
    )]
    engine = workflow(workspace)
    run = engine.create([wip_row, *excluded], '2026-11')
    assert len(run['items']) == 1
    assert run['excluded_rows'] == 5
    with pytest.raises(ValueError, match='Duplicate'):
        engine.create([wip_row, wip_row], '2026-11')


def test_approval_gates_resume_and_no_repeat_delivery(workspace, wip_row):
    class RecordingDemo(DemoAdapter):
        def __init__(self, root):
            super().__init__(root)
            self.calls = []

        def execute(self, action, item, key):
            self.calls.append((action, key))
            return super().execute(action, item, key)

    adapter = RecordingDemo(workspace / 'artifacts')
    engine = workflow(workspace, adapter)
    run = engine.create([wip_row], '2026-11')
    run = engine.advance(run['id'])
    assert run['status'] == 'needs_review'
    assert run['items'][0]['step'] == 'draft_review'
    assert 'request_final' not in [a for a, _ in adapter.calls]
    with pytest.raises(ValueError, match='Reviewer'):
        engine.approve(run['id'], '')
    # A fresh orchestrator resumes the persisted draft review.
    engine = workflow(workspace, adapter)
    run = engine.approve(run['id'], 'Draft Reviewer')
    assert run['items'][0]['step'] == 'final_review'
    assert 'send_email' not in [a for a, _ in adapter.calls]
    run = engine.approve(run['id'], 'Final Reviewer')
    assert run['status'] == 'completed'
    assert [a['reviewer'] for a in run['items'][0]['approvals']] == ['Draft Reviewer', 'Final Reviewer']
    assert run['items'][0]['data']['send_email']['simulated'] is True
    before = list(adapter.calls)
    assert engine.advance(run['id'])['status'] == 'completed'
    assert adapter.calls == before
    assert engine.get(run['id'])['status'] == 'completed'


def test_amount_mismatch_blocks_before_charge_creation(workspace, wip_row):
    class WrongAmount(DemoAdapter):
        def execute(self, action, item, key):
            response = super().execute(action, item, key)
            if action == 'contract':
                response['amount'] = '999.00'
            return response

    engine = workflow(workspace, WrongAmount(workspace / 'artifacts'))
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['status'] == 'blocked'
    assert 'does not match WIP' in run['items'][0]['error']
    assert 'create_charges' not in run['items'][0]['attempts']


def test_unconfigured_live_adapter_fails_closed(workspace, wip_row):
    engine = workflow(workspace, UnconfiguredAdapter())
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['mode'] == 'live'
    assert run['status'] == 'blocked'
    assert 'not configured' in run['items'][0]['error']
    assert not run['items'][0]['data']


def test_bounded_polling_and_same_idempotency_key_on_retry(workspace, wip_row):
    class WaitingDemo(DemoAdapter):
        def __init__(self, root):
            super().__init__(root)
            self.keys = []

        def execute(self, action, item, key):
            if action == 'charge_status':
                self.keys.append(key)
                return {'ready': False}
            return super().execute(action, item, key)

    adapter = WaitingDemo(workspace / 'artifacts')
    engine = workflow(workspace, adapter, poll_limit=2)
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['status'] == 'waiting'
    assert len(adapter.keys) == 2
    run = engine.advance(run['id'])
    assert run['status'] == 'waiting'
    assert len(adapter.keys) == 4
    assert len(set(adapter.keys)) == 1
    assert run['items'][0]['attempts']['create_charges'] == 1


def test_missing_reports_prevents_invoice_download(workspace, wip_row):
    class MissingReport(DemoAdapter):
        def execute(self, action, item, key):
            if action == 'draft_reports':
                return {'ready': True, 'reports': ['Sales Order']}
            return super().execute(action, item, key)

    engine = workflow(workspace, MissingReport(workspace / 'artifacts'))
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['status'] == 'blocked'
    assert 'both required' in run['items'][0]['error']
    assert 'download_draft' not in run['items'][0]['attempts']


def test_adapter_mode_cannot_mutate_reviewed_run(workspace, wip_row):
    engine = workflow(workspace)
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['status'] == 'needs_review'
    before = engine.get(run['id'])
    other = workflow(workspace, UnconfiguredAdapter())
    with pytest.raises(ValueError, match='mode'):
        other.approve(run['id'], 'Reviewer')
    assert engine.get(run['id']) == before


def test_duplicate_create_after_restart_is_rejected(workspace, wip_row):
    first = workflow(workspace).create([wip_row], '2026-11')
    with pytest.raises(ValueError, match='already exists'):
        workflow(workspace).create([wip_row], '2026-11')
    assert len(workflow(workspace).list_runs()) == 1
    assert workflow(workspace).get(first['id'])['status'] == 'ready'


def test_ambiguous_mutation_requires_reconciliation_without_retry(workspace, wip_row):
    class UncertainCharge(DemoAdapter):
        def __init__(self, root):
            super().__init__(root)
            self.charge_calls = 0

        def execute(self, action, item, key):
            if action == 'create_charges':
                self.charge_calls += 1
                raise OutcomeUnknown('Request timed out after submission')
            return super().execute(action, item, key)

    adapter = UncertainCharge(workspace / 'artifacts')
    engine = workflow(workspace, adapter)
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['status'] == 'reconciliation_required'
    assert engine.advance(run['id'])['status'] == 'reconciliation_required'
    assert adapter.charge_calls == 1


def test_changed_draft_artifact_cannot_be_approved(workspace, wip_row):
    engine = workflow(workspace)
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    artifact = Path(run['items'][0]['data']['download_draft']['path'])
    artifact.write_text('Changed after validation', encoding='utf-8')
    with pytest.raises(ValueError, match='artifact changed'):
        engine.approve(run['id'], 'Reviewer')
    persisted = engine.get(run['id'])
    assert not persisted['items'][0]['approvals']
    assert 'request_final' not in persisted['items'][0]['attempts']


def test_reconciliation_validates_external_outcome_and_never_replays(workspace, wip_row):
    class UncertainCharge(DemoAdapter):
        def __init__(self, root):
            super().__init__(root)
            self.calls = []

        def execute(self, action, item, key):
            self.calls.append(action)
            if action == 'create_charges':
                raise OutcomeUnknown('Charge submission outcome unknown')
            return super().execute(action, item, key)

    adapter = UncertainCharge(workspace / 'artifacts')
    engine = workflow(workspace, adapter)
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    item_id = run['items'][0]['id']
    before = engine.get(run['id'])
    # Reviewer attribution cannot bypass amount or response validation.
    for invalid in ({}, {'invoice_number': 'VERIFIED-100', 'amount': '1.00'}):
        with pytest.raises(ValueError):
            engine.reconcile(run['id'], item_id, invalid, 'External Reviewer')
        assert engine.get(run['id']) == before
    calls_before = list(adapter.calls)
    run = engine.reconcile(run['id'], item_id,
                           {'invoice_number': 'VERIFIED-100', 'amount': wip_row['amount']},
                           'External Reviewer')
    assert adapter.calls == calls_before
    assert run['items'][0]['step'] == 'send_charges'
    assert run['items'][0]['reconciliations'][0]['reviewer'] == 'External Reviewer'
    assert run['items'][0]['reconciliations'][0]['action'] == 'create_charges'
    run = engine.advance(run['id'])
    assert run['status'] == 'needs_review'
    assert run['items'][0]['step'] == 'draft_review'
    assert adapter.calls.count('create_charges') == 1


def test_gateway_timeout_is_an_unknown_outcome(monkeypatch):
    from veritas import invoice_gateway

    class TimedOutOpener:
        def open(self, request, timeout):
            raise TimeoutError('Timeout after request submission')

    monkeypatch.setattr(invoice_gateway, 'build_opener', lambda *_: TimedOutOpener())
    gateway = invoice_gateway.GatewayAdapter('https://gateway.example.invalid', 'test-token')
    with pytest.raises(OutcomeUnknown, match='outcome unknown'):
        gateway.execute('create_charges', {'wip': {'country': 'Dubai', 'amount': '1000'}, 'data': {}}, 'stable-key')


def test_country_codes_and_no_po_audit_date_comment(workspace, wip_row):
    engine = workflow(workspace)
    row = dict(wip_row, country='AE', po_number='', comments='Client reference')
    run = engine.advance(engine.create([row], '2026-11', 'AE')['id'])
    contract = run['items'][0]['data']['contract']
    assert run['country'] == 'Dubai'
    assert contract['comments'] == 'Client reference; Audit date: 04/11/2026'
    draft = run['items'][0]['data']['download_draft']
    assert (draft['net_amount'], draft['tax_amount'], draft['gross_amount']) == ('1000.00', '50.00', '1050.00')


def test_vat_total_mismatch_blocks_draft_review(workspace, wip_row):
    class WrongTax(DemoAdapter):
        def execute(self, action, item, key):
            response = super().execute(action, item, key)
            if action == 'download_draft':
                response['gross_amount'] = '1000'
            return response
    engine = workflow(workspace, WrongTax(workspace / 'artifacts'))
    run = engine.advance(engine.create([wip_row], '2026-11')['id'])
    assert run['status'] == 'blocked'
    assert 'VAT' in run['items'][0]['error']


def test_south_africa_line_totals_and_coordinator_gate(workspace):
    from veritas.wip import load_wip
    from veritas.invoice_gateway import parameters
    row, = load_wip('scripts/wip-south-africa-demo.csv')
    engine = workflow(workspace)
    run = engine.create([row, row], '2026-04', 'ZA')
    assert run['duplicate_rows'] == 1
    run = engine.advance(run['id'])
    assert run['status'] == 'needs_review', run['errors']
    item = run['items'][0]
    assert item['data']['create_charges']['amount'] == '5520.0'
    assert item['data']['download_draft']['net_amount'] == '5520.0'
    assert parameters('request_draft', item)['report'] == 'Standard Draft'
    assert len(parameters('create_charges', item)['charge_lines']) == 3
    assert parameters('flex', item)['header_attachment']['po_number'] == ''
    with pytest.raises(ValueError, match='country_coordinator'):
        engine.approve(run['id'], 'Ordinary reviewer')
    assert 'request_final' not in engine.get(run['id'])['items'][0]['attempts']
    run = engine.approve(run['id'], 'Demo Coordinator', 'country_coordinator')
    assert run['items'][0]['step'] == 'final_review'
    run = engine.approve(run['id'], 'Demo Final reviewer')
    assert run['status'] == 'completed'
    assert run['items'][0]['data']['send_email']['simulated']


def test_south_africa_man_days_mismatch_and_live_disabled(workspace):
    from veritas.wip import load_wip
    row, = load_wip('scripts/wip-south-africa-demo.csv')
    engine = workflow(workspace)
    run = engine.advance(engine.create([dict(row, man_days='6')], '2026-04', 'ZA')['id'])
    assert run['status'] == 'blocked'
    assert 'man-days' in run['items'][0]['error']
    with pytest.raises(ValueError, match='live policy'):
        workflow(workspace / 'live', UnconfiguredAdapter()).create([row], '2026-04', 'ZA')
