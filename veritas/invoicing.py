"""Durable special-audit invoice workflow with explicit system adapters.

Adapters must honor the supplied idempotency key, including after a timeout.
DemoAdapter is synthetic and never contacts Smart Cert, Flex or customers.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from veritas.uc.storage import read_json, write_json
from veritas import invoice_rules as rules
from veritas.country_policies import CountryPolicies, CONTROL_VERSION

LOCK = threading.RLock()
DUBAI_UNITS = {'Dubai CER', 'Dubai CER Food', 'Dubai Cyber Security'}
REPORTS = {'Sales Order', 'EDI Invoice Extraction'}
REQUIRED_JOBS = {'Print Invoices - Contract Billing', 'Sales Order Batch', 'EDI Invoice Extraction'}
STEPS = ['contract', 'create_charges', 'send_charges', 'charge_status', 'flex',
         'request_draft', 'draft_reports', 'download_draft', 'draft_review',
         'request_final', 'final_reports', 'download_final', 'final_review', 'send_email', 'completed']
AGENTS = {'contract': 'ContractVerificationAgent', 'create_charges': 'SmartCertAgent',
          'send_charges': 'SmartCertAgent', 'charge_status': 'SmartCertAgent', 'flex': 'FlexAgent',
          'request_draft': 'DraftInvoiceAgent', 'draft_reports': 'DraftInvoiceAgent',
          'download_draft': 'DraftInvoiceAgent', 'draft_review': 'HumanReviewer',
          'request_final': 'FinalInvoiceAgent', 'final_reports': 'FinalInvoiceAgent',
          'download_final': 'FinalInvoiceAgent', 'final_review': 'HumanReviewer',
          'send_email': 'CustomerDeliveryAgent'}


class Adapter(Protocol):
    mode: str

    def execute(self, action: str, item: dict, idempotency_key: str) -> dict:
        """Perform an SOP action, returning verified data or raise an exception.

        Mutations MUST deduplicate using idempotency_key. Status actions return
        ready=False while waiting; invoice downloads include path and fields.
        """
        ...


class OutcomeUnknown(RuntimeError):
    """A mutation may have succeeded; manual reconciliation is required."""


class UnconfiguredAdapter:
    mode = 'live'

    def execute(self, action, item, idempotency_key):
        raise RuntimeError('Live Smart Cert, Flex and email adapters are not configured. '
                           'Configure authenticated adapters or use explicit demo mode.')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _money(value):
    try:
        result = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation as exc:
        raise ValueError('Amount must be a decimal number') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('Amount must be finite and nonnegative')
    return result


def _normalized(row):
    aliases = {'flex_contract_number': 'contract_number', 'contracting_country': 'country',
               'man_days': 'man_days', 'mandays': 'man_days', 'business_unit': 'business_unit',
               'invoice_status': 'invoice_status', 'start_date': 'start_date'}
    result = {}
    for key, value in row.items():
        key = re.sub(r'[^a-z0-9]+', '_', str(key).strip().lower()).strip('_')
        result[aliases.get(key, key)] = value
    result['country'] = rules.country(result.get('country', ''))
    return result


def _date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    value = str(value).strip()
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise ValueError('Start Date must use YYYY-MM-DD') from exc


class InvoiceWorkflow:
    def __init__(self, root, adapter=None, poll_limit=5, business_units=None, policy_path=None, policies=None, actor='system'):
        self.root = Path(root)
        self.adapter = adapter or UnconfiguredAdapter()
        self.poll_limit = max(1, int(poll_limit))
        self.policies = policies if isinstance(policies, CountryPolicies) else CountryPolicies(path=policy_path, data=policies)
        if business_units:
            for name, units in business_units.items():
                canonical = self.policies.canonical(name)
                if canonical in self.policies.data:
                    self.policies.data[canonical]['business_units'] = list(units)
                    self.policies.data[canonical]['business_unit_aliases'] = {}
        self.business_units = {name: set(p['business_units']) for name, p in self.policies.data.items()}
        self.actor = str(actor).strip() or 'system'

    def _path(self, run_id):
        if not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('Invalid run ID')
        return self.root / (run_id + '.json')

    def get(self, run_id):
        with LOCK:
            return read_json(self._path(run_id))

    def list_runs(self):
        with LOCK:
            return [read_json(path) for path in sorted(self.root.glob('*.json'), reverse=True)]

    def _save(self, run):
        run['updated_at'] = _now()
        write_json(self._path(run['id']), run)

    def _event(self, run, item, action, status, detail=None):
        run['events'].append({'at': _now(), 'item_id': item.get('id'),
                              'actor': detail.get('reviewer', self.actor) if isinstance(detail, dict) else self.actor,
                              'agent': AGENTS.get(action, 'WIPIntakeAgent'),
                              'action': action, 'status': status, 'detail': detail})

    def create(self, rows, month, country='Dubai'):
        country = self.policies.canonical(country)
        policy = self.policies.get(country)
        south_africa = country == 'South Africa'
        try:
            date.fromisoformat(month + '-01')
        except (TypeError, ValueError) as exc:
            raise ValueError('Month must use YYYY-MM') from exc
        if self.adapter.mode == 'live' and (not policy['live_enabled'] or not policy['live_confirmation']):
            raise ValueError(f'{country} live policy and attachment templates require confirmation; use demo or assisted mode')
        if country not in self.business_units and not south_africa:
            raise ValueError('Country business units must be configured before processing')
        if not isinstance(rows, list):
            raise ValueError('WIP rows must be a list')
        run = {'id': uuid.uuid4().hex, 'mode': self.adapter.mode, 'country': country,
               'month': month, 'status': 'ready', 'created_at': _now(), 'items': [],
               'excluded_rows': 0, 'duplicate_rows': 0, 'process_version': rules.VERSION,
               'policy': policy, 'control_version': CONTROL_VERSION,
               'events': [], 'errors': []}
        identities = set()
        exact_rows = set()
        for index, source in enumerate(rows):
            row = _normalized(source)
            row['country'] = self.policies.canonical(row.get('country'))
            row['business_unit'] = self.policies.normalize_unit(country, row.get('business_unit'))
            fingerprint = json.dumps(row, sort_keys=True, default=str)
            if policy['deduplicate'] and fingerprint in exact_rows:
                run['duplicate_rows'] += 1
                continue
            exact_rows.add(fingerprint)
            if not self.policies.eligible(country, row):
                run['excluded_rows'] += 1
                continue
            start = _date(row.get('start_date', ''))
            if start[:7] > month or (start[:7] < month and not policy['include_backlog']):
                run['excluded_rows'] += 1
                continue
            contract = str(row.get('contract_number', '')).strip()
            line = str(row.get('line_item_id', '')).strip()
            if not contract:
                raise ValueError(f'WIP row {index + 1}: Flex Contract Number is required')
            # No line ID is acceptable at intake; the adapter must resolve one
            # unambiguously from the work order and report date.
            identity = (contract, line, start)
            if identity in identities:
                raise ValueError(f'Duplicate WIP row for contract {contract}; resolve line items')
            identities.add(identity)
            row.update(contract_number=contract, start_date=start, amount=str(_money(row.get('amount', ''))))
            if policy['split_charges'] and any(not row.get(key) for key in ('service_resource', 'man_days', 'currency')):
                raise ValueError(f'{country} WIP requires Service Resource, Man Days and Currency')
            item = {'id': uuid.uuid4().hex, 'wip': row, 'step': 'contract', 'data': {},
                    'run_id': run['id'], 'policy': policy, 'control_version': CONTROL_VERSION,
                    'process_version': rules.VERSION, 'status': 'ready', 'error': None,
                    'approvals': [], 'attempts': {}}
            run['items'].append(item)
            self._event(run, item, 'wip_intake', 'validated')
        if not run['items']:
            raise ValueError('No eligible audit rows for the selected country and month')
        with LOCK:
            for previous in self.list_runs():
                if previous.get('mode') != run['mode'] or previous.get('country') != country:
                    continue
                prior = {(i['wip']['contract_number'], str(i['wip'].get('line_item_id', '')).strip(), i['wip']['start_date']) for i in previous['items']}
                if any(a[0] == b[0] and a[2] == b[2] and (a[1] == b[1] or not a[1] or not b[1]) for a in identities for b in prior):
                    raise ValueError('WIP invoice already exists in run ' + previous['id'])
            self._save(run)
        return run

    def _validate(self, action, item, data):
        wip = item['wip']
        if not isinstance(data, dict):
            raise ValueError('Adapter response must be an object')
        polling = action in ('charge_status', 'draft_reports', 'final_reports')
        if polling and not isinstance(data.get('ready'), bool):
            raise ValueError('Polling response requires a boolean ready field')
        if 'ready' in data and (not isinstance(data['ready'], bool) or (not polling and not data['ready'])):
            raise ValueError('Invalid ready flag for action')
        if action == 'contract':
            required = ['signed_contract_path', 'client_email', 'amount',
                        'currency', 'start_date', 'line_item_id', 'work_order_address', 'tax_rate_area']
            policy = item.get('policy')
            if policy is None or policy.get('proposal_required'):
                required.append('proposal_number')
            if policy:
                required.extend(policy.get('required_contract_fields', []))
            for field in required:
                if not data.get(field):
                    raise ValueError(f'Signed contract verification missing {field}')
            if not Path(data['signed_contract_path']).is_file():
                raise ValueError('Signed service contract artifact is missing')
            if _money(data['amount']) != _money(wip['amount']):
                raise ValueError('Commercial offer amount does not match WIP amount')
            if wip.get('currency') and data['currency'] != wip['currency']:
                raise ValueError('Contract currency does not match WIP currency')
            if _date(data['start_date']) != wip['start_date']:
                raise ValueError('Work Order Start Date does not match WIP Start Date')
            if item.get('control_version') == CONTROL_VERSION:
                if data.get('work_order_exists') is not True or not data.get('work_order_id'):
                    raise ValueError('Verified Work Order existence and identifier are required')
                if not data.get('work_order_start_date') or _date(data['work_order_start_date']) != wip['start_date']:
                    raise ValueError('Work Order Start Date does not match WIP Start Date')
                if _money(data.get('work_order_amount', '')) != _money(wip['amount']) or data.get('work_order_currency') != data['currency']:
                    raise ValueError('Work Order amount or currency does not match WIP and signed contract')
                if not data.get('audit_type') or not self._audit_matches(wip['audit_type'], data['audit_type']):
                    raise ValueError('Contract audit type does not match WIP and Work Order audit')
                if not data.get('work_order_audit_type') or not self._audit_matches(data['audit_type'], data['work_order_audit_type']):
                    raise ValueError('Work Order audit type does not match signed contract')
            if wip.get('line_item_id') and str(data['line_item_id']) != str(wip['line_item_id']):
                raise ValueError('Contract line item does not match WIP')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', str(data['client_email'])):
                raise ValueError('Invalid customer email in signed contract')
            contacts = data.get('client_emails') or [data['client_email']]
            if (not isinstance(contacts, list) or not contacts or data['client_email'] not in contacts
                    or len(contacts) != len(set(contacts))
                    or any(not isinstance(email, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) for email in contacts)):
                raise ValueError('Customer contacts must be distinct valid email addresses from the signed contract')
            if wip.get('po_number') and str(data.get('po_number', '')) != str(wip['po_number']):
                raise ValueError('Resolved PO does not match the WIP PO')
            if (not item.get('process_version') and not data.get('po_number')
                    and wip.get('comments') and data.get('comments') != wip['comments']):
                raise ValueError('Required WIP comments were not preserved')
            if data.get('po_number') and not data.get('po_address'):
                raise ValueError('PO billing address is missing')
            if item.get('policy', {}).get('audit_date_comment', wip['country'] == 'Dubai') and not data.get('po_number') and not data.get('comments'):
                raise ValueError('No PO: required charge comments are missing')
            if item.get('process_version') == rules.VERSION:
                rules.validate_contract(item, data)
        elif action == 'create_charges':
            if not data.get('invoice_number'):
                raise ValueError('Smart Cert did not return an Invoice Number')
            if _money(data.get('amount', '')) != rules.charge_amount(item):
                raise ValueError('Generated charge amount does not match WIP')
        elif action == 'send_charges' and data.get('sent') is not True:
            raise ValueError('Smart Cert did not confirm sending charges to Flex')
        elif action == 'charge_status' and data.get('ready') and data.get('status') != 'Handled':
            raise ValueError('Charges must be Handled before processing in Flex')
        elif action == 'flex':
            contract = item['data']['contract']
            address = contract.get('po_address') if contract.get('po_number') else contract['work_order_address']
            expected = {'billing_address': address, 'tax_rate_area': contract['tax_rate_area']}
            if contract.get('proposal_number'):
                expected['proposal_number'] = contract['proposal_number']
            if not data.get('order_number') or any(data.get(k) != v for k, v in expected.items()):
                raise ValueError('Flex order, header attachment, billing address or tax verification failed')
            if item.get('policy', {}).get('split_charges', wip['country'] == 'South Africa'):
                if data.get('header_attachment') != contract['header_attachment'] or data.get('detail_attachments') != contract['charge_lines']:
                    raise ValueError('South Africa header or detail attachments do not match approved charge lines')
        elif action in ('request_draft', 'request_final'):
            if not data.get('job_id'):
                raise ValueError('Invoice request did not return a report job ID')
        elif action in ('draft_reports', 'final_reports') and data.get('ready'):
            required = REQUIRED_JOBS if item.get('control_version') == CONTROL_VERSION else REPORTS
            if not required.issubset(set(data.get('reports', []))):
                raise ValueError('Sales Order and EDI Invoice Extraction reports are both required')
        elif action in ('download_draft', 'download_final'):
            if not data.get('path') or not Path(data['path']).is_file():
                raise ValueError('Downloaded invoice artifact is missing')
            contract = item['data']['contract']
            address = contract.get('po_address') if contract.get('po_number') else contract['work_order_address']
            if data.get('billing_address') != address or data.get('tax_rate_area') != contract['tax_rate_area']:
                raise ValueError('Invoice billing address or tax rate area does not match verified contract')
            if (_money(data.get('amount', '')) != rules.charge_amount(item)
                or data.get('currency') != contract['currency']
                or data.get('contract_number') != wip['contract_number']
                or data.get('order_number') != item['data']['flex']['order_number']):
                raise ValueError('Invoice amount, currency, contract or order validation failed')
            if item.get('process_version') == rules.VERSION:
                rules.validate_invoice(item, data)
            if action == 'download_final' and item.get('control_version') == CONTROL_VERSION and not data.get('invoice_number'):
                raise ValueError('Final invoice number is required for filing')
        elif action == 'send_email':
            if not data.get('message_id') or data.get('recipient') != item['data']['contract']['client_email']:
                raise ValueError('Email delivery acknowledgement or customer recipient is invalid')
            contacts = item['data']['contract'].get('client_emails') or [item['data']['contract']['client_email']]
            actual = data.get('recipients') or [data['recipient']]
            if not isinstance(actual, list) or set(actual) != set(contacts) or len(actual) != len(contacts):
                raise ValueError('Delivery recipients must match all verified signed-contract contacts')

    def advance(self, run_id):
        with LOCK:
            run = self.get(run_id)
            if run['mode'] != self.adapter.mode:
                raise ValueError('Run adapter mode cannot be changed')
            for item in run['items']:
                item.setdefault('run_id', run_id)
                if item['status'] == 'reconciliation_required':
                    continue
                item['error'] = None
                while item['step'] != 'completed':
                    action = item['step']
                    if action in ('draft_review', 'final_review'):
                        item['status'] = 'needs_review'
                        break
                    key = hashlib.sha256(f'{run_id}:{item["id"]}:{action}'.encode()).hexdigest()
                    item['status'] = 'running'
                    item['attempts'][action] = item['attempts'].get(action, 0) + 1
                    self._event(run, item, action, 'started', {'idempotency_key': key})
                    self._save(run)
                    try:
                        if action == 'send_email':
                            self._check_artifact(item['data']['contract'])
                            self._check_artifact(item['data']['download_final'])
                        response = None
                        for _ in range(self.poll_limit if action in ('charge_status', 'draft_reports', 'final_reports') else 1):
                            response = self.adapter.execute(action, item, key)
                            self._validate(action, item, response)
                            if response.get('ready', True):
                                break
                        if response.get('ready', True) is False:
                            item['status'] = 'waiting'
                            self._event(run, item, action, 'waiting')
                            self._save(run)
                            break
                        if action in ('contract', 'download_draft', 'download_final'):
                            artifact = response.get('path') or response.get('signed_contract_path')
                            response['sha256'] = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
                        if action == 'download_final' and item.get('control_version') == CONTROL_VERSION:
                            from veritas.assisted import file_invoice
                            manifest = file_invoice(getattr(self.adapter, 'root', self.root), response['path'], {
                                **response, 'country': run['country'], 'customer_name': item['data']['contract']['customer_name']},
                                evidence_roots=getattr(self.adapter, 'evidence_roots', None))
                            response.update(path=manifest['path'], filing=manifest)
                        item['data'][action] = response
                        self._event(run, item, action, 'completed')
                        item['step'] = STEPS[STEPS.index(action) + 1]
                        item['status'] = 'completed' if item['step'] == 'completed' else 'ready'
                        self._save(run)
                    except Exception as exc:
                        manual_task_id = getattr(exc, 'task_id', None) if type(exc).__name__ == 'ManualActionRequired' else None
                        item['status'] = 'waiting_manual' if manual_task_id else 'reconciliation_required' if isinstance(exc, OutcomeUnknown) else 'blocked'
                        if manual_task_id:
                            item['manual_task_id'] = manual_task_id
                        item['error'] = str(exc)
                        self._event(run, item, action, item['status'], str(exc))
                        self._save(run)
                        break
            states = {item['status'] for item in run['items']}
            run['status'] = ('completed' if states == {'completed'} else 'reconciliation_required' if 'reconciliation_required' in states else 'blocked' if 'blocked' in states
                             else 'needs_review' if 'needs_review' in states else 'waiting_manual' if 'waiting_manual' in states else 'waiting')
            run['errors'] = [item['error'] for item in run['items'] if item.get('error')]
            self._save(run)
            return run

    def reconcile(self, run_id, item_id, response, reviewer):
        """Record externally verified outcome; never replay or execute a gateway action."""
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('Reviewer name is required')
        with LOCK:
            run = self.get(run_id)
            if run['mode'] != self.adapter.mode:
                raise ValueError('Run adapter mode cannot be changed')
            item = next((value for value in run['items'] if value['id'] == item_id), None)
            if item is None or item['status'] != 'reconciliation_required':
                raise ValueError('Item is not awaiting reconciliation')
            action = item['step']
            self._validate(action, item, response)
            if response.get('ready', True) is not True:
                raise ValueError('Reconciliation requires a confirmed completed outcome')
            response = dict(response)
            if action in ('contract', 'download_draft', 'download_final'):
                artifact = response.get('path') or response.get('signed_contract_path')
                response['sha256'] = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
            if action == 'download_final' and item.get('control_version') == CONTROL_VERSION:
                from veritas.assisted import file_invoice
                manifest = file_invoice(getattr(self.adapter, 'root', self.root), response['path'], {
                    **response, 'country': run['country'], 'customer_name': item['data']['contract']['customer_name']},
                    evidence_roots=getattr(self.adapter, 'evidence_roots', None))
                response.update(path=manifest['path'], filing=manifest)
            item['data'][action] = response
            item.setdefault('reconciliations', []).append({
                'action': action, 'reviewer': reviewer.strip(), 'at': _now(),
                'response_sha256': hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest()})
            self._event(run, item, action, 'reconciled', item['reconciliations'][-1])
            item['step'] = STEPS[STEPS.index(action) + 1]
            item['status'] = 'completed' if item['step'] == 'completed' else 'ready'
            item['error'] = None
            states = {value['status'] for value in run['items']}
            run['status'] = ('completed' if states == {'completed'} else
                             'reconciliation_required' if 'reconciliation_required' in states else
                             'blocked' if 'blocked' in states else 'ready')
            run['errors'] = [value['error'] for value in run['items'] if value.get('error')]
            self._save(run)
            return run

    @staticmethod
    def _check_artifact(data):
        path = data.get('path') or data.get('signed_contract_path')
        if not path or not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != data.get('sha256'):
            raise ValueError('Reviewed artifact changed or is missing; reconciliation required')

    def approve(self, run_id, reviewer, reviewer_role='reviewer'):
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('Reviewer name is required')
        with LOCK:
            run = self.get(run_id)
            if run['mode'] != self.adapter.mode:
                raise ValueError('Run adapter mode cannot be changed')
            pending = [i for i in run['items'] if i['status'] == 'needs_review']
            if not pending:
                raise ValueError('No invoice is awaiting review')
            if any(i['step'] == 'draft_review' for i in pending):
                required_role = run.get('policy', {}).get('draft_approval_role', 'country_coordinator' if run['country'] == 'South Africa' else 'reviewer')
                if required_role != 'reviewer' and reviewer_role != required_role:
                    raise ValueError(f'{run["country"]} draft requires {required_role} approval before final invoicing')
            for item in pending:
                self._check_artifact(item['data']['contract'])
                self._check_artifact(item['data']['download_draft' if item['step'] == 'draft_review' else 'download_final'])
            for item in pending:
                action = item['step']
                approval = {'stage': action, 'reviewer': reviewer.strip(), 'role': reviewer_role, 'at': _now(),
                            'artifact': item['data']['download_draft' if action == 'draft_review' else 'download_final']['path']}
                item['approvals'].append(approval)
                self._event(run, item, action, 'approved', approval)
                item['step'] = STEPS[STEPS.index(action) + 1]
                item['status'] = 'ready'
            self._save(run)
            return self.advance(run_id)

    @staticmethod
    def _audit_matches(left, right):
        aliases = {'special': 'special audit', 'sv1': 'surveillance audit 1', 'surveillance 1': 'surveillance audit 1'}
        canonical = lambda value: aliases.get(str(value).strip().casefold(), str(value).strip().casefold())
        return canonical(left) == canonical(right)


class DemoAdapter:
    """Synthetic end-to-end execution. Artifacts are labelled text, not real invoices."""
    mode = 'demo'

    def __init__(self, root):
        self.root = Path(root)

    def execute(self, action, item, idempotency_key):
        self.root.mkdir(parents=True, exist_ok=True)
        wip = item['wip']
        number = idempotency_key[:12].upper()
        if action == 'contract':
            path = self.root / (number + '-signed-contract-DEMO.txt')
            path.write_text('DEMO synthetic signed service contract. No real signature.\n', encoding='utf-8')
            data = {'signed_contract_path': str(path.resolve()), 'proposal_number': 'DEMO-P-' + number,
                    'audit_type': wip['audit_type'], 'work_order_audit_type': wip['audit_type'],
                    'work_order_exists': True, 'work_order_id': 'DEMO-WO-' + number,
                    'work_order_start_date': wip['start_date'], 'work_order_amount': wip['amount'],
                    'work_order_currency': wip.get('currency') or 'AED',
                    'client_email': 'customer@example.invalid', 'amount': wip['amount'],
                    'currency': wip.get('currency') or 'AED', 'start_date': wip['start_date'],
                    'line_item_id': wip.get('line_item_id') or 'DEMO-LINE-1',
                    'work_order_address': 'DEMO billing address',
                    'customer_name': 'DEMO Customer',
                    'billing_jurisdiction': wip.get('billing_jurisdiction') or ('Dubai' if wip['country'] == 'Dubai' else wip['country']),
                    'tax_rate_area': ('AE11' if wip.get('billing_jurisdiction') == 'Other emirates' else 'AE10') if wip['country'] == 'Dubai' else 'DEMO-TAX',
                    'tax_rate': '0.05' if wip['country'] == 'Dubai' else wip.get('demo_tax_rate', ''),
                    'po_number': wip.get('po_number'), 'po_address': (wip.get('po_address') or 'DEMO PO billing address') if wip.get('po_number') else None,
                    'comments': rules.audit_comment(wip['start_date'], wip.get('comments', ''))}
            configured_tax = item.get('policy', {}).get('tax_jurisdictions', {}).get(data['billing_jurisdiction'])
            if configured_tax:
                data.update(tax_rate_area=configured_tax['area'], tax_rate=str(configured_tax['rate']))
            if item.get('policy', {}).get('split_charges', wip['country'] == 'South Africa'):
                data.update(standard_name=wip.get('standard_name', ''),
                            auditor_name=wip.get('service_resource', ''),
                            audit_dates=[d.strip() for d in wip.get('audit_dates', '').split(';') if d.strip()])
                data['header_attachment'] = {key: data[key] for key in ('standard_name', 'auditor_name', 'audit_dates')}
                data['header_attachment'].update(audit_type=wip['audit_type'], po_number=wip.get('po_number') or '')
                lines = []
                for kind in ('on_site', 'reporting'):
                    days = rules.decimal(wip.get(kind + '_man_days', ''))
                    price = rules.decimal(wip.get('unit_price', ''))
                    lines.append({'line_item_id': 'DEMO-' + kind, 'kind': kind,
                                  'man_days': str(days), 'unit_price': str(price),
                                  'amount': str(days * price), 'currency': data['currency'],
                                  'description': f'{kind}: {days} man-days x {data["currency"]} {price}'})
                lines.append({'line_item_id': 'DEMO-management', 'kind': 'annual_project_management',
                              'amount': str(rules.decimal(wip.get('annual_project_management_fee', ''))),
                              'currency': data['currency'], 'description': 'Annual project management fee'})
                data['charge_lines'] = lines
            return data
        if action == 'create_charges':
            return {'invoice_number': 'DEMO-I-' + number, 'amount': str(rules.charge_amount(item))}
        if action == 'send_charges':
            return {'sent': True}
        if action == 'charge_status':
            return {'ready': True, 'status': 'Handled'}
        if action == 'flex':
            contract = item['data']['contract']
            return {'order_number': 'DEMO-O-' + number, 'proposal_number': contract['proposal_number'],
                    'header_attachment': contract.get('header_attachment'),
                    'detail_attachments': contract.get('charge_lines'),
                    'billing_address': contract.get('po_address') or contract['work_order_address'],
                    'tax_rate_area': contract['tax_rate_area']}
        if action in ('request_draft', 'request_final'):
            return {'job_id': 'DEMO-JOB-' + number}
        if action in ('draft_reports', 'final_reports'):
            return {'ready': True, 'reports': sorted(REPORTS | REQUIRED_JOBS)}
        if action in ('download_draft', 'download_final'):
            contract = item['data']['contract']
            net = rules.charge_amount(item)
            from decimal import ROUND_HALF_UP
            tax = (net * rules.decimal(contract['tax_rate'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            data = {'amount': str(net), 'net_amount': str(net), 'tax_amount': str(tax),
                    'invoice_number': item['data']['create_charges']['invoice_number'],
                    'country': wip['country'],
                    'gross_amount': str(net + tax), 'tax_rate': contract['tax_rate'],
                    'customer_name': contract['customer_name'],
                    'audit_dates': contract.get('audit_dates', [contract['start_date']]),
                    'currency': contract['currency'],
                    'contract_number': wip['contract_number'], 'order_number': item['data']['flex']['order_number'],
                    'billing_address': item['data']['flex']['billing_address'],
                    'tax_rate_area': item['data']['flex']['tax_rate_area']}
            from veritas.assisted import render_demo_invoice
            return render_demo_invoice(self.root, data, 'draft' if action == 'download_draft' else 'final')
        if action == 'send_email':
            return {'message_id': 'DEMO-NOT-SENT-' + number,
                    'recipient': item['data']['contract']['client_email'], 'simulated': True}
        raise ValueError('Unsupported adapter action: ' + action)


def main():
    from veritas.invoice_cli import main as cli_main
    cli_main()


if __name__ == '__main__':
    main()
