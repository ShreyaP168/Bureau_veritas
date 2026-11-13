"""Operations reporting and case management, isolated from financial validation.

Configuration and case notes never override invoice facts or approval gates.
Reviewer identities are local attestations; authentication belongs at the access
layer before this application is exposed beyond its loopback workspace.
"""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
import json
import os
from pathlib import Path
import re
import threading

from veritas.invoice_lock import exclusive
from veritas.uc.storage import read_json, write_json

LOCK = threading.RLock()
MODES = {'demo', 'assisted', 'live'}
EXCEPTIONS = {'blocked', 'exception', 'reconciliation_required'}
DETAIL_KEYS = ('proposal_number_format', 'south_africa_detail_template',
               'south_africa_header_template', 'south_africa_tax_policy',
               'escalation_owner', 'filename_template', 'cce_rce_definition')
BASELINE_KEYS = {'monthly_volume', 'minutes_per_invoice', 'error_rate_percent', 'sla_hours', 'hourly_cost', 'day_rate'}
DEFAULTS = {
    'scope': {'automation': 'assisted', 'countries': ['Dubai', 'South Africa'],
              'email_in_scope': True, 'dubai_draft_review_required': True},
    'client_details': {key: '' for key in DETAIL_KEYS},
    'baseline': {name: {key: None for key in sorted(BASELINE_KEYS)}
                 for name in ('Dubai', 'South Africa')},
    'history': [],
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _mode(value):
    if value not in MODES:
        raise ValueError('mode must be demo, assisted or live')
    return value


def _text(value, label, limit=10000, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{label} must be text under {limit} characters')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{label} is required')
    return value


def _month(value):
    if value:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
            raise ValueError('month must use YYYY-MM')
        date.fromisoformat(value + '-01')
    return value


def _amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Saved financial totals contain an invalid decimal') from None
    if not result.is_finite():
        raise ValueError('Saved financial totals must be finite')
    return result


def _safe_csv(value):
    text = str(value if value is not None else '')
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else text


def _invoice_date(final, issued_at):
    # Prefer the recorded legal invoice date, then the issuance event in the
    # user's Asia/Calcutta timezone. A UTC date prefix can misclassify month end.
    supplied = final.get('issued_date') or (final.get('filing') or {}).get('issued_date')
    if supplied:
        return date.fromisoformat(str(supplied)).isoformat()
    if not issued_at:
        return None
    stamp = datetime.fromisoformat(str(issued_at).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()


class Operations:
    def __init__(self, output):
        self.output = Path(output)
        self.root = self.output / 'operations'

    def _runs(self, mode):
        root = self.output / 'invoicing' / _mode(mode)
        runs, warnings = [], []
        for path in sorted(root.glob('*.json')):
            if not re.fullmatch(r'[a-f0-9]{32}', path.stem):
                continue
            try:
                run = read_json(path)
                if not isinstance(run, dict) or not isinstance(run.get('items'), list):
                    raise ValueError('Invalid saved run')
                runs.append(run)
            except (ValueError, OSError) as exc:
                warnings.append({'run_id': path.stem, 'error': type(exc).__name__})
        return runs, warnings

    def get_dashboard(self, mode, month=''):
        """Queue counts use the billing month; issued values use issuance dates.

        A final invoice exists independently of whether its delivery has
        completed. Values are never summed across unlike currencies.
        """
        _month(month)
        runs, warnings = self._runs(mode)
        counts = {'total': 0, 'processed': 0, 'pending': 0, 'exceptions': 0, 'awaiting_review': 0, 'finalized': 0}
        values, days, months, rows = {}, {}, {}, []
        cases = {c['key']: c for c in self.list_cases(mode)}
        for run in runs:
            for item in run['items']:
                wip, data = item.get('wip', {}), item.get('data', {})
                status = item.get('status', 'pending')
                final = data.get('download_final')
                issued_at, issued_date = None, None
                if final:
                    matching = [e.get('at') for e in run.get('events', [])
                                if e.get('item_id') == item.get('id') and e.get('action') == 'download_final'
                                and e.get('status') in ('completed', 'reconciled') and e.get('at')]
                    issued_at = min(matching) if matching else final.get('issued_at')
                    try:
                        issued_date = _invoice_date(final, issued_at)
                    except (TypeError, ValueError):
                        warnings.append({'run_id': run['id'], 'item_id': item.get('id'),
                                         'error': 'Final invoice has an invalid issuance date; omitted from period totals'})
                    if not issued_date:
                        warnings.append({'run_id': run['id'], 'item_id': item.get('id'),
                                         'error': 'Final invoice has no issuance date; omitted from period totals'})
                case_key = f"{mode}:{run['id']}:{item['id']}"
                case = cases.get(case_key, {})
                item_events = [event for event in run.get('events', []) if event.get('item_id') == item.get('id')]
                last_event = item_events[-1] if item_events else {}
                detail = last_event.get('detail') if isinstance(last_event.get('detail'), dict) else {}
                actor = last_event.get('actor') or detail.get('reviewer') or last_event.get('agent') or 'system'
                in_queue_period = not month or run.get('month') == month
                if in_queue_period:
                    counts['total'] += 1
                    if status == 'completed':
                        counts['processed'] += 1
                    elif status in EXCEPTIONS:
                        counts['exceptions'] += 1
                    else:
                        counts['pending'] += 1
                    counts['awaiting_review'] += status == 'needs_review'
                    counts['finalized'] += bool(final)
                    rows.append({'run_id': run['id'], 'item_id': item['id'], 'country': run.get('country'),
                                 'billing_month': run.get('month'), 'contract_number': wip.get('contract_number'),
                                 'audit_type': wip.get('audit_type'), 'audit_date': wip.get('start_date'),
                                 'status': status, 'step': item.get('step'), 'exception_reason': item.get('error'),
                                 'owner': case.get('owner', ''), 'case_status': case.get('status', ''),
                                 'currency': (final or data.get('contract') or wip).get('currency', ''),
                                 'net_invoiced': final.get('net_amount', final.get('amount', '')) if final else '',
                                 'gross_invoiced': final.get('gross_amount', '') if final else '',
                                 'issued_at': issued_at, 'issued_date': issued_date,
                                 'updated_at': run.get('updated_at'), 'last_actor': actor})
                if final and issued_date and (not month or issued_date[:7] == month):
                    currency = final.get('currency')
                    if not isinstance(currency, str) or not currency:
                        warnings.append({'run_id': run['id'], 'error': 'Final invoice has no currency; omitted from totals'})
                        continue
                    try:
                        net = _amount(final.get('net_amount', final.get('amount')))
                        gross = _amount(final.get('gross_amount'))
                    except ValueError as exc:
                        warnings.append({'run_id': run['id'], 'error': str(exc)})
                        continue
                    for target, key in ((values, currency), (days, (issued_date, currency)),
                                        (months, (issued_date[:7], currency))):
                        bucket = target.setdefault(key, {'count': 0, 'net': Decimal('0'), 'gross': Decimal('0')})
                        bucket['count'] += 1
                        bucket['net'] += net
                        bucket['gross'] += gross
        def serialize(bucket):
            return {'count': bucket['count'], 'net': str(bucket['net']), 'gross': str(bucket['gross'])}
        return {'mode': mode, 'month': month, 'generated_at': _now(), 'counts': counts,
                'values_by_currency': {key: serialize(value) for key, value in sorted(values.items())},
                'daily': [{'date': key[0], 'currency': key[1], **serialize(value)} for key, value in sorted(days.items())],
                'monthly': [{'month': key[0], 'currency': key[1], **serialize(value)} for key, value in sorted(months.items())],
                'items': rows, 'warnings': warnings,
                'period_basis': {'queue': 'billing_month', 'values': 'final_invoice_issuance_date',
                                 'timezone': 'Asia/Calcutta'}}

    def export_csv(self, mode, month=''):
        report = self.get_dashboard(mode, month)
        output = StringIO(newline='')
        columns = ['country', 'billing_month', 'contract_number', 'audit_type', 'audit_date', 'status', 'step',
                   'exception_reason', 'owner', 'case_status', 'currency', 'net_invoiced', 'gross_invoiced',
                   'issued_date', 'issued_at', 'last_actor', 'run_id', 'item_id']
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in report['items']:
            writer.writerow([_safe_csv(row.get(column)) for column in columns])
        return ('\ufeff' + output.getvalue()).encode('utf-8')

    def get_settings(self):
        with LOCK:
            path = self.root / 'settings.json'
            return read_json(path) if path.exists() else copy.deepcopy(DEFAULTS)

    def save_settings(self, payload):
        if not isinstance(payload, dict) or set(payload) - {'actor', 'scope', 'client_details', 'baseline'}:
            raise ValueError('Unknown settings fields; secrets must be configured in the server environment')
        actor = _text(payload.get('actor'), 'Actor', 200, True)
        with LOCK, exclusive(self.root):
            settings = self.get_settings()
            if 'scope' in payload:
                scope = payload['scope']
                if not isinstance(scope, dict) or set(scope) - set(DEFAULTS['scope']):
                    raise ValueError('Unknown scope setting')
                if 'automation' in scope and scope['automation'] not in ('assisted', 'full'):
                    raise ValueError('automation must be assisted or full')
                if 'countries' in scope:
                    names = scope['countries']
                    if not isinstance(names, list) or not names or len(names) > 20:
                        raise ValueError('Select between one and twenty countries')
                    names = [_text(name, 'Country', 100, True) for name in names]
                    if len(set(names)) != len(names):
                        raise ValueError('Duplicate countries')
                for key in ('email_in_scope', 'dubai_draft_review_required'):
                    if key in scope and not isinstance(scope[key], bool):
                        raise ValueError(f'{key} must be true or false')
                settings['scope'].update(copy.deepcopy(scope))
            if 'client_details' in payload:
                details = payload['client_details']
                if not isinstance(details, dict) or set(details) - set(DETAIL_KEYS):
                    raise ValueError('Unknown client detail')
                settings['client_details'].update({key: _text(value, key) for key, value in details.items()})
            if 'baseline' in payload:
                baseline = payload['baseline']
                if not isinstance(baseline, dict) or len(baseline) > 20:
                    raise ValueError('Invalid baseline settings')
                for name, values in baseline.items():
                    _text(name, 'Baseline country', 100, True)
                    if not isinstance(values, dict) or set(values) - BASELINE_KEYS:
                        raise ValueError('Unknown baseline metric')
                    checked = {}
                    for key, value in values.items():
                        if value is None or value == '':
                            checked[key] = None
                            continue
                        if isinstance(value, bool):
                            raise ValueError('Baseline metrics must be numbers')
                        number = _amount(value)
                        if number < 0 or number > Decimal('1000000000') or (key == 'error_rate_percent' and number > 100):
                            raise ValueError('Baseline metric is out of range')
                        checked[key] = str(number)
                    settings['baseline'].setdefault(name, {}).update(checked)
            settings.setdefault('history', []).append({'actor': actor, 'at': _now(),
                                                       'changes': copy.deepcopy({k: v for k, v in payload.items() if k != 'actor'})})
            write_json(self.root / 'settings.json', settings)
            return settings

    def readiness(self):
        settings = self.get_settings()
        gateway = bool(os.getenv('VERITAS_INVOICE_GATEWAY_URL'))
        authentication = bool(os.getenv('VERITAS_INVOICE_GATEWAY_TOKEN'))
        missing = [key for key, value in settings['client_details'].items() if not value]
        policy_path = self.root / 'countries.json'
        if not policy_path.exists():
            policy_path = Path(__file__).resolve().parent.parent / 'config' / 'countries.json'
        policies = read_json(policy_path) if policy_path.exists() else {}
        policy_status = {name: {'live_enabled': bool(policy.get('live_enabled')),
                                'confirmation': policy.get('live_confirmation') or '',
                                'draft_approval_role': policy.get('draft_approval_role'),
                                'tax_policy_configured': bool(policy.get('tax_jurisdictions'))}
                         for name, policy in policies.items()}
        return {'gateway_url_configured': gateway, 'gateway_authentication_configured': authentication,
                'gateway_configured': gateway and authentication, 'integration_tested': False,
                'production_ready': False, 'missing_client_details': missing,
                'missing_access': ['SmartCert/Flex approved gateway and service account'] if not (gateway and authentication) else [],
                'scope': settings['scope'], 'baseline': settings['baseline'],
                'country_policy_status': policy_status,
                'identity': 'Local reviewer attestation; enterprise authentication is not configured',
                'note': 'Stored scope and baseline settings record requirements; they do not activate unconfigured integrations.'}

    def _all_cases(self):
        path = self.root / 'cases.json'
        return read_json(path) if path.exists() else {}

    def list_cases(self, mode):
        _mode(mode)
        with LOCK:
            return sorted((case for case in self._all_cases().values() if case['mode'] == mode),
                          key=lambda case: case['updated_at'], reverse=True)

    def update_case(self, mode, run_id, item_id, payload):
        _mode(mode)
        if not isinstance(payload, dict) or set(payload) - {'actor', 'owner', 'reason', 'note', 'resolution', 'status'}:
            raise ValueError('Unknown case fields')
        actor = _text(payload.get('actor'), 'Actor', 200, True)
        for identifier in (run_id, item_id):
            if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
                raise ValueError('Invalid run or item ID')
        run = read_json(self.output / 'invoicing' / mode / (run_id + '.json'))
        item = next((item for item in run['items'] if item['id'] == item_id), None)
        if item is None:
            raise ValueError('Unknown invoice item')
        fields = {key: _text(value, key, 200 if key == 'owner' else 10000)
                  for key, value in payload.items() if key != 'actor'}
        if fields.get('status') not in (None, 'open', 'in_progress', 'resolved'):
            raise ValueError('Case status must be open, in_progress or resolved')
        with LOCK, exclusive(self.root):
            cases = self._all_cases()
            key = f'{mode}:{run_id}:{item_id}'
            previous = cases.get(key, {})
            if not previous and item['status'] not in EXCEPTIONS:
                raise ValueError('Only exception items can open a case')
            case = copy.deepcopy(previous) if previous else {
                'key': key, 'mode': mode, 'run_id': run_id, 'item_id': item_id,
                'country': run.get('country'), 'contract_number': item['wip'].get('contract_number'),
                'status': 'open', 'owner': '', 'reason': item.get('error') or '',
                'note': '', 'resolution': '', 'created_at': _now(), 'history': []}
            case.update(fields)
            if case['status'] == 'resolved' and not case.get('resolution'):
                raise ValueError('Resolution notes are required before closing a case')
            case['updated_at'] = _now()
            case['history'].append({'actor': actor, 'at': case['updated_at'], 'changes': fields})
            cases[key] = case
            write_json(self.root / 'cases.json', cases)
            return case
