"""Rules evidenced by the April 2, 2026 process walkthrough.

Example amounts are fixtures, never production defaults. South Africa remains
demo-only until its business-unit, tax and attachment policies are confirmed.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VERSION = 'meeting-2026-04-02'
COUNTRIES = {'a': 'Dubai', 'ae': 'Dubai', 'dubai': 'Dubai', 'za': 'South Africa', 'south africa': 'South Africa'}


def country(value):
    return COUNTRIES.get(str(value).strip().lower(), str(value).strip())


def decimal(value):
    try:
        result = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation:
        raise ValueError('Expected a finite nonnegative decimal') from None
    if not result.is_finite() or result < 0:
        raise ValueError('Expected a finite nonnegative decimal')
    return result


def audit_comment(start, existing=''):
    # Use the verified Work Order date, never today's date or invoice period.
    formatted = date.fromisoformat(start).strftime('%d/%m/%Y')
    text = str(existing).strip()
    if formatted in text or start in text:
        return text
    return f'{text}; Audit date: {formatted}' if text else f'Audit date: {formatted}'


def charge_amount(item):
    contract = item['data'].get('contract', {})
    if item.get('policy', {}).get('split_charges', item['wip']['country'] == 'South Africa'):
        return sum((decimal(line['amount']) for line in contract['charge_lines']), Decimal('0'))
    return decimal(item['wip']['amount'])


def validate_contract(item, data):
    wip = item['wip']
    policy = item.get('policy', {})
    if not data.get('po_number') and policy.get('audit_date_comment', wip['country'] == 'Dubai'):
        if data.get('comments') != audit_comment(data['start_date'], wip.get('comments', '')):
            raise ValueError('No PO: comments must preserve WIP comments and include the verified audit date')
    if not data.get('customer_name'):
        raise ValueError('Signed contract customer name is required')
    if 'tax_rate' not in data or decimal(data['tax_rate']) > 1:
        raise ValueError('A verified tax rate between 0 and 1 is required')
    # This is the walkthrough's Dubai example, not an all-UAE tax rules engine.
    if policy.get('tax_jurisdictions'):
        jurisdiction = data.get('billing_jurisdiction')
        expected = policy['tax_jurisdictions'].get(jurisdiction)
        if not expected:
            raise ValueError('Verified billing jurisdiction must match a configured tax policy')
        if data['tax_rate_area'] != expected['area'] or decimal(data['tax_rate']) != decimal(expected['rate']):
            raise ValueError('Tax area or VAT rate does not match the configured billing jurisdiction')
    elif data.get('billing_jurisdiction') == 'Dubai':
        if data['tax_rate_area'] != 'AE10' or decimal(data['tax_rate']) != Decimal('.05'):
            raise ValueError('Dubai walkthrough requires AE10 and 5% VAT; exceptions require confirmed policy')
    if not policy.get('split_charges', wip['country'] == 'South Africa'):
        return
    required = ('standard_name', 'auditor_name', 'audit_dates', 'header_attachment', 'charge_lines')
    if any(not data.get(field) for field in required):
        raise ValueError('South Africa requires standard, auditor, dates, header and charge line attachments')
    if data['auditor_name'] != wip.get('service_resource'):
        raise ValueError('South Africa auditor must match the WIP service resource')
    if not isinstance(data['audit_dates'], list):
        raise ValueError('Audit dates must be an explicit list of ISO dates')
    dates = [date.fromisoformat(value).isoformat() for value in data['audit_dates']]
    if wip['start_date'] not in dates or len(dates) != len(set(dates)):
        raise ValueError('Audit dates must include Work Order start date without duplicates')
    header = data['header_attachment']
    expected = {'audit_type': wip['audit_type'], 'standard_name': data['standard_name'],
                'auditor_name': data['auditor_name'], 'audit_dates': dates,
                'po_number': data.get('po_number') or ''}
    if header != expected:
        raise ValueError('South Africa header attachment does not match verified contract and Work Order')
    lines = data['charge_lines']
    if not isinstance(lines, list) or not lines:
        raise ValueError('South Africa charge lines are required')
    kinds, identifiers, amount, days = set(), set(), Decimal('0'), Decimal('0')
    for line in lines:
        if not isinstance(line, dict) or any(not line.get(key) for key in ('line_item_id', 'kind', 'description')):
            raise ValueError('Every charge line needs an ID, kind and detail attachment description')
        if line['line_item_id'] in identifiers:
            raise ValueError('Duplicate South Africa charge line')
        identifiers.add(line['line_item_id'])
        kinds.add(line['kind'])
        if line['currency'] != data['currency']:
            raise ValueError('Charge line currencies must match the signed contract')
        value = decimal(line['amount'])
        if line['kind'] in ('on_site', 'reporting'):
            quantity, price = decimal(line['man_days']), decimal(line['unit_price'])
            if quantity * price != value:
                raise ValueError('Charge amount must equal man-days multiplied by unit price')
            amount += value
            days += quantity
        elif line['kind'] != 'annual_project_management':
            raise ValueError('Unconfirmed South Africa fee kind')
    if not {'on_site', 'reporting', 'annual_project_management'}.issubset(kinds):
        raise ValueError('South Africa requires on-site, reporting and annual project management lines')
    if amount != decimal(wip['amount']) or days != decimal(wip.get('man_days', '')):
        raise ValueError('South Africa base amount and combined man-days must reconcile with WIP')


def validate_invoice(item, data):
    contract = item['data']['contract']
    net = charge_amount(item)
    tax = (net * decimal(contract['tax_rate'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    expected = {'net_amount': net, 'tax_amount': tax, 'gross_amount': net + tax,
                'tax_rate': decimal(contract['tax_rate'])}
    if any(decimal(data.get(key, '')) != value for key, value in expected.items()):
        raise ValueError('Invoice net, VAT and gross totals do not reconcile')
    if data.get('customer_name') != contract['customer_name']:
        raise ValueError('Invoice customer name does not match signed contract')
    if data.get('audit_dates') != contract.get('audit_dates', [contract['start_date']]):
        raise ValueError('Invoice audit dates do not match verified Work Order dates')
