"""JSON-over-HTTPS adapter for an organization's approved portal gateway.

This is an integration contract, not a claim about native Smart Cert/Flex APIs.
The gateway performs authenticated portal actions and publishes local artifacts
in the shared artifact directory referenced by its responses.
"""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from veritas.invoicing import UnconfiguredAdapter, AGENTS, OutcomeUnknown, REQUIRED_JOBS
from veritas import invoice_rules as rules


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayAdapter:
    mode = 'live'

    def __init__(self, url, token, timeout=30):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Invoice gateway must be an HTTPS URL without credentials, query or fragment')
        if not token or '\n' in token or '\r' in token:
            raise ValueError('Invoice gateway token is missing or invalid')
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout

    def execute(self, action, item, idempotency_key):
        if (action not in AGENTS and action != 'fetch_wip') or action in ('draft_review', 'final_review'):
            raise ValueError('Unsupported gateway action')
        body = json.dumps({'action': action, 'item': item, 'parameters': parameters(action, item),
                           'idempotency_key': idempotency_key}).encode('utf-8')
        request = Request(self.url + '/execute', data=body, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token,
            'Idempotency-Key': idempotency_key,
        })
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise OutcomeUnknown('Gateway must return a completed action response with HTTP 200; reconcile operation')
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise OutcomeUnknown('Invoice gateway response exceeded size limit; reconcile operation')
        except HTTPError as exc:
            raise OutcomeUnknown(f'Invoice gateway HTTP {exc.code}; reconcile using the recorded idempotency key') from None
        except (URLError, TimeoutError, OSError):
            raise OutcomeUnknown('Invoice gateway unavailable or outcome unknown; reconcile the recorded idempotency key before resuming') from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise OutcomeUnknown('Invoice gateway returned invalid JSON; reconcile operation') from None
        if not isinstance(data, dict):
            raise OutcomeUnknown('Invoice gateway response must be an object; reconcile operation')
        return data


def parameters(action, item):
    """Map approved workflow facts to the gateway's SOP operation inputs."""
    wip, data = item.get('wip', {}), item.get('data', {})
    contract = data.get('contract', {})
    south_africa = wip.get('country') == 'South Africa'
    aliases = item.get('policy', {}).get('aliases', [])
    result = {'country': wip.get('country'), 'country_code': aliases[0] if aliases else ('ZA' if south_africa else 'A' if wip.get('country')=='Dubai' else wip.get('country')),
              'contract_number': wip.get('contract_number')}
    if action == 'fetch_wip':
        result.update(report='WIP not fully invoiced with filters', billing_month=wip.get('start_date', '')[:7],
                      include_uninvoiced_backlog=True)
    elif action == 'contract':
        result.update(document='Signed Service Contract', tab='More > Documents',
                      alternate_document_tab='Quotation', work_orders_tab='More > Work Orders',
                      work_order_start_date=wip.get('start_date'), line_item_id=wip.get('line_item_id'))
    elif action == 'create_charges':
        result.update(line_item_id=contract.get('line_item_id'), po_number=contract.get('po_number'),
                      comments=contract.get('comments'), expected_amount=str(rules.charge_amount(item)))
        if south_africa:
            result['charge_lines'] = contract['charge_lines']
    elif action in ('send_charges', 'charge_status'):
        result.update(invoice_number=data.get('create_charges', {}).get('invoice_number'),
                      expected_status='Handled')
    elif action == 'flex':
        result.update(module='Contract Management & Invoicing', menu='Contract from ZIG',
                      inquiry='Sales Inquiry (S1)',
                      invoice_number=data.get('create_charges', {}).get('invoice_number'),
                      header_attachment=contract.get('proposal_number'),
                      billing_address=contract.get('po_address') if contract.get('po_number') else contract.get('work_order_address'),
                      tax_rate_area=contract.get('tax_rate_area'))
        if south_africa:
            result.update(header_attachment=contract['header_attachment'],
                          detail_attachments=contract['charge_lines'])
    elif action in ('request_draft', 'request_final'):
        result.update(menu='Contract from ZIG',
                      report='Draft Invoices' if action == 'request_draft' else 'Final Invoices',
                      operand='Literal', literal_value=data.get('flex', {}).get('order_number'),
                      subledger=wip.get('contract_number'))
        if south_africa:
            result['report'] = 'Standard Draft' if action == 'request_draft' else 'Standard Invoice'
    elif action in ('draft_reports', 'final_reports'):
        request = 'request_draft' if action == 'draft_reports' else 'request_final'
        result.update(job_id=data.get(request, {}).get('job_id'),
                      required_reports=sorted(REQUIRED_JOBS) if item.get('control_version') else ['Sales Order', 'EDI Invoice Extraction'])
    elif action in ('download_draft', 'download_final'):
        request = 'request_draft' if action == 'download_draft' else 'request_final'
        result.update(job_id=data.get(request, {}).get('job_id'),
                      report='Print Invoices – Contract Billing',
                      order_number=data.get('flex', {}).get('order_number'))
    elif action == 'send_email':
        final = data.get('download_final', {})
        result.update(recipient=contract.get('client_email'), attachment_path=final.get('path'),
                      recipients=contract.get('client_emails') or [contract.get('client_email')],
                      attachment_sha256=final.get('sha256'),
                      order_number=data.get('flex', {}).get('order_number'))
    return result


def configured_adapter():
    url = os.getenv('VERITAS_INVOICE_GATEWAY_URL')
    token = os.getenv('VERITAS_INVOICE_GATEWAY_TOKEN')
    if not url or not token:
        return UnconfiguredAdapter()
    return GatewayAdapter(url, token)
