"""Validated, data-only country policies shared by intake and matching."""
from copy import deepcopy
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / 'config' / 'countries.json'
CONTROL_VERSION = 'application-2026-11'


class CountryPolicies:
    def __init__(self, path=None, data=None):
        self.path = Path(path) if path else DEFAULT_PATH
        source = data if data is not None else json.loads(self.path.read_text(encoding='utf-8'))
        self.data = self.validate(source)

    @staticmethod
    def validate(source):
        if not isinstance(source, dict) or not source or len(source) > 100:
            raise ValueError('Country policies must be a nonempty object with at most 100 countries')
        result = deepcopy(source)
        aliases = set()
        for name, policy in result.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(policy, dict):
                raise ValueError('Each country policy needs a name and object')
            allowed = {'aliases', 'business_units', 'business_unit_aliases', 'statuses', 'audit_types',
                       'include_backlog', 'deduplicate', 'live_enabled', 'live_confirmation',
                       'draft_approval_role', 'proposal_required', 'split_charges',
                       'audit_date_comment', 'tax_jurisdictions', 'required_contract_fields'}
            if set(policy) - allowed:
                raise ValueError('Unknown country policy setting: ' + ', '.join(sorted(set(policy) - allowed)))
            for field in ('aliases', 'business_units', 'statuses', 'audit_types', 'required_contract_fields'):
                value = policy.setdefault(field, [])
                if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
                    raise ValueError(f'{name}: {field} must be a list of nonempty strings')
            for alias in [name, *policy['aliases']]:
                key = alias.strip().casefold()
                if key in aliases:
                    raise ValueError('Country aliases must be unique: ' + alias)
                aliases.add(key)
            for field in ('include_backlog', 'deduplicate', 'live_enabled', 'proposal_required', 'split_charges', 'audit_date_comment'):
                value = policy.setdefault(field, False)
                if not isinstance(value, bool):
                    raise ValueError(f'{name}: {field} must be boolean')
            policy.setdefault('live_confirmation', '')
            policy.setdefault('draft_approval_role', 'reviewer')
            if not isinstance(policy['draft_approval_role'], str) or not policy['draft_approval_role'].strip():
                raise ValueError('A draft approval role is required')
            if not isinstance(policy['live_confirmation'], str):
                raise ValueError('Live policy confirmation must be text')
            mapping = policy.setdefault('business_unit_aliases', {})
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) or v not in policy['business_units'] for k, v in mapping.items()):
                raise ValueError('Business-unit aliases must map to configured business units')
            taxes = policy.setdefault('tax_jurisdictions', {})
            if not isinstance(taxes, dict):
                raise ValueError('Tax jurisdictions must be an object')
            for jurisdiction, tax in taxes.items():
                from decimal import Decimal, InvalidOperation
                if not isinstance(jurisdiction, str) or not isinstance(tax, dict) or not tax.get('area'):
                    raise ValueError('Each tax jurisdiction requires a tax area')
                try:
                    rate = Decimal(str(tax['rate']))
                except (KeyError, InvalidOperation):
                    raise ValueError('Tax jurisdiction rate is required') from None
                if not rate.is_finite() or not 0 <= rate <= 1:
                    raise ValueError('Tax rates must be between zero and one')
        return result

    def export(self):
        return deepcopy(self.data)

    def canonical(self, value):
        key = str(value).strip().casefold()
        for name, policy in self.data.items():
            if key in {v.strip().casefold() for v in [name, *policy['aliases']]}:
                return name
        return str(value).strip()

    def get(self, country):
        name = self.canonical(country)
        if name not in self.data:
            raise ValueError('Country business units and policy must be configured before processing')
        return deepcopy(self.data[name])

    def normalize_unit(self, country, value):
        policy = self.get(country)
        text = str(value).strip()
        mapping = {k.casefold(): v for k, v in policy['business_unit_aliases'].items()}
        mapping.update({v.casefold(): v for v in policy['business_units']})
        return mapping.get(text.casefold(), text)

    def eligible(self, country, row):
        policy = self.get(country)
        if self.canonical(row.get('country')) != self.canonical(country):
            return False
        for column, field in (('business_unit', 'business_units'), ('invoice_status', 'statuses'), ('audit_type', 'audit_types')):
            value = self.normalize_unit(country, row.get(column)) if column == 'business_unit' else str(row.get(column, '')).strip()
            if policy[field] and value.casefold() not in {v.casefold() for v in policy[field]}:
                return False
        for field in ('already_invoiced', 'fully_invoiced', 'invoiced'):
            if str(row.get(field, '')).strip().casefold() in {'true', 'yes', '1', 'fully invoiced', 'invoiced'}:
                return False
        return True
