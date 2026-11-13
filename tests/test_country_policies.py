"""Configuration, backlog and three-source matching controls."""
from pathlib import Path
import uuid

import pytest

from veritas.country_policies import CountryPolicies
from veritas.invoicing import DemoAdapter, InvoiceWorkflow, REQUIRED_JOBS


@pytest.fixture
def workspace():
    root = Path('runtime') / ('country-policies-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    return root


def row(**changes):
    return {'country':'A', 'business_unit':'CCE/RCE', 'invoice_status':'Confirmed',
            'start_date':'2026-10-15', 'audit_type':'Special Audit',
            'contract_number':'BACKLOG-001', 'amount':'1900', 'currency':'AED',
            'comments':'', 'po_number':'', **changes}


def workflow(root, adapter=None, **options):
    return InvoiceWorkflow(root / 'runs', adapter or DemoAdapter(root / 'artifacts'), **options)


def test_dubai_alias_backlog_and_explicit_invoiced_exclusion(workspace):
    engine = workflow(workspace, actor='Billing Operator')
    run = engine.create([row(), row(contract_number='CURRENT', start_date='2026-11-01'),
                         row(contract_number='FUTURE', start_date='2026-12-01'),
                         row(contract_number='DONE', already_invoiced='yes')], '2026-11', 'A')
    assert len(run['items']) == 2
    assert run['excluded_rows'] == 2
    assert run['items'][0]['wip']['business_unit'] == 'Dubai CER'
    assert run['events'][0]['actor'] == 'Billing Operator'
    with pytest.raises(ValueError, match='already exists'):
        engine.create([row()], '2026-12', 'AE')


def test_policy_validation_and_generic_country_without_new_code(workspace):
    data = CountryPolicies().export()
    data['Pilot Country'] = {'aliases':['PC'], 'business_units':['Pilot Audit'],
                            'business_unit_aliases':{'Audit':'Pilot Audit'},
                            'audit_types':['Special Audit'], 'statuses':['Confirmed'],
                            'include_backlog':True, 'draft_approval_role':'finance_reviewer',
                            'live_enabled':False,
                            'tax_jurisdictions':{'Pilot Country':{'area':'PC-TAX','rate':'0.2'}}}
    policy = CountryPolicies(data=data)
    engine = workflow(workspace, policies=policy)
    run = engine.advance(engine.create([row(country='PC', business_unit='Audit', currency='GBP')], '2026-11', 'PC')['id'])
    assert run['status'] == 'needs_review', run['errors']
    contract = run['items'][0]['data']['contract']
    assert contract['tax_rate_area'] == 'PC-TAX'
    assert run['items'][0]['data']['download_draft']['gross_amount'] == '2280.00'
    with pytest.raises(ValueError, match='finance_reviewer'):
        engine.approve(run['id'], 'Reviewer')
    assert engine.approve(run['id'], 'Finance Reviewer', 'finance_reviewer')['items'][0]['step'] == 'final_review'
    data['Pilot Country']['include_backlog'] = 'yes'
    with pytest.raises(ValueError, match='boolean'):
        CountryPolicies(data=data)


@pytest.mark.parametrize('change, expected', [
    ({'work_order_exists':False}, 'Work Order existence'),
    ({'work_order_amount':'1'}, 'Work Order amount'),
    ({'work_order_currency':'USD'}, 'Work Order amount'),
    ({'work_order_start_date':'2026-10-16'}, 'Work Order Start Date'),
    ({'audit_type':'SV1'}, 'Contract audit type'),
    ({'work_order_audit_type':'SV1'}, 'Work Order audit type'),
])
def test_three_source_mismatch_never_creates_charges(workspace, change, expected):
    class Mismatch(DemoAdapter):
        def execute(self, action, item, key):
            response = super().execute(action, item, key)
            if action == 'contract':
                response.update(change)
            return response
    engine = workflow(workspace, Mismatch(workspace / 'artifacts'))
    run = engine.advance(engine.create([row()], '2026-11')['id'])
    assert run['status'] == 'blocked'
    assert expected in run['items'][0]['error']
    assert 'create_charges' not in run['items'][0]['attempts']


def test_three_jobs_and_other_emirates_tax_area(workspace):
    engine = workflow(workspace)
    run = engine.advance(engine.create([row(billing_jurisdiction='Other emirates')], '2026-11')['id'])
    item = run['items'][0]
    assert item['data']['contract']['tax_rate_area'] == 'AE11'
    assert REQUIRED_JOBS.issubset(set(item['data']['draft_reports']['reports']))
    class MissingPrint(DemoAdapter):
        def execute(self, action, item, key):
            if action == 'draft_reports':
                return {'ready':True, 'reports':['Sales Order Batch','EDI Invoice Extraction']}
            return super().execute(action, item, key)
    other = workflow(workspace / 'other', MissingPrint(workspace / 'other' / 'artifacts'))
    blocked = other.advance(other.create([row()], '2026-11')['id'])
    assert blocked['status'] == 'blocked'
    assert 'download_draft' not in blocked['items'][0]['attempts']


def test_demo_final_pdf_is_filed_and_approval_actor_recorded(workspace):
    engine = workflow(workspace)
    run = engine.advance(engine.create([row()], '2026-11')['id'])
    run = engine.approve(run['id'], 'Named reviewer')
    final = run['items'][0]['data']['download_final']
    assert Path(final['path']).read_bytes().startswith(b'%PDF-')
    assert final['filing']['filename'].endswith('.pdf')
    assert any(event['actor'] == 'Named reviewer' and event['status'] == 'approved' for event in run['events'])
