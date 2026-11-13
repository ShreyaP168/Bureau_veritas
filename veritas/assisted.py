"""Evidence-driven manual portal tasks and local PDF storage.

Assisted mode never logs into a portal, creates a financial document there, or
sends email. A person completes each external step and records its evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone, date, timedelta
import hashlib
import json
from pathlib import Path
import re
import threading

from veritas.uc.storage import read_json, write_json

LOCK = threading.RLock()
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


def _issued_date(metadata):
    # The application's supplied client timezone is Asia/Calcutta. Callers can
    # supply the actual legal invoice date explicitly in metadata.
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    return date.fromisoformat(str(metadata.get('issued_date') or today)).isoformat()


def _identifier(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{' + str(length) + r'}', value):
        raise ValueError('Invalid evidence identifier')
    return value


def _slug(value):
    return re.sub(r'[^A-Za-z0-9_-]+', '-', str(value)).strip('-')[:70] or 'unknown'


def _additional_candidates(pages):
    """Return source quotes for country-specific facts, never asserted values."""
    patterns = {
        'currency': r'\b(?:USD|ZAR|AED)\b',
        'standard_name': r'\bISO\s*\d{4,5}(?::\d{4})?\b',
        'on_site_man_days': r'\b(?:on[- ]?site|onsite)\s*(?:(?:man[- ]?days?|mandates|days?)\s*[:=-]?\s*)?(\d+(?:\.\d+)?)',
        'reporting_man_days': r'\breporting\s*(?:(?:man[- ]?days?|mandates|days?)\s*[:=-]?\s*)?(\d+(?:\.\d+)?)',
        'annual_project_management_fee': r'\bannual\s+project\s+management(?:\s+fee)?\s*[:=-]?\s*((?:(?:USD|ZAR|AED)\s*)?[\d,]+(?:\.\d{1,2})?)',
    }
    found = {name: [] for name in patterns}
    for page in pages:
        for name, pattern in patterns.items():
            for match in re.finditer(pattern, page.text, re.I):
                candidate = {'value': match.group(1) if match.lastindex else match.group(0),
                             'page': page.number, 'evidence': match.group(0),
                             'source': page.method, 'needs_review': True}
                if candidate not in found[name]:
                    found[name].append(candidate)
    return found


class ManualActionRequired(RuntimeError):
    def __init__(self, task_id):
        self.task_id = task_id
        super().__init__('A person must complete and confirm this portal action before continuing')


INSTRUCTIONS = {
    'contract': 'Open SmartCert Service Contract > More > Documents. Download the signed proposal. Verify report, contract and Work Order facts, then upload the contract PDF and record the verified values. Do not infer missing amounts, dates or tax treatment.',
    'create_charges': 'In SmartCert select the verified contract line items, including all reporting and fee lines. Add the PO or required audit-date comment. Create charges once, then record the generated invoice number and amount.',
    'send_charges': 'Open the generated SmartCert invoice, verify the amount and send those charges to Flex. Confirm this exact invoice was sent; do not create another charge.',
    'charge_status': 'Refresh the existing SmartCert Charges tab until the same invoice status is Handled. Record the observed status.',
    'flex': 'Find the Flex sales order by contract and generated charge. Verify the header/detail attachments, billing address, tax area and currency against the signed contract. Record the generated order number.',
    'request_draft': 'Submit the draft invoice using Literal order number and the contract as Subledger. Record the report job ID.',
    'draft_reports': 'Wait for the draft Print, Sales Order Batch and EDI Invoice Extraction jobs for this order to complete. Record completed reports only.',
    'download_draft': 'Download the draft PDF, upload it to this workspace and verify its customer, address, audit dates, net amount, tax and total. Record the PDF path returned by upload.',
    'request_final': 'After the recorded draft approval, submit the final invoice once for this order and contract. Record its report job ID.',
    'final_reports': 'Wait for the final Print, Sales Order Batch and EDI Invoice Extraction jobs for this order to complete. Record completed reports only.',
    'download_final': 'Download the final invoice PDF, upload it to this workspace and verify all invoice details. Record the PDF path returned by upload; retain its real invoice number for filing.',
    'send_email': 'After final review, send the verified final PDF to the customer contacts on the signed contract using your approved mail system. Record its message ID and actual recipient. This application does not send email in assisted mode.',
}


def _template(action, item):
    wip, data = item.get('wip', {}), item.get('data', {})
    contract = data.get('contract', {})
    currency = contract.get('currency') or wip.get('currency') or ''
    amount = wip.get('amount') or ''
    if action == 'contract':
        result = {'signed_contract_path': '', 'proposal_number': '', 'client_email': '', 'client_emails': [],
                  'customer_name': '', 'amount': amount, 'currency': currency,
                  'audit_type': wip.get('audit_type', ''), 'start_date': wip.get('start_date', ''),
                  'work_order_id': '', 'work_order_exists': False, 'line_item_id': wip.get('line_item_id', ''),
                  'work_order_start_date': '', 'work_order_amount': '', 'work_order_currency': '',
                  'work_order_audit_type': '',
                  'work_order_address': '', 'tax_rate_area': '', 'tax_rate': '',
                  'billing_jurisdiction': '', 'po_number': wip.get('po_number') or '',
                  'po_address': '', 'comments': wip.get('comments') or ''}
        if wip.get('country') == 'South Africa':
            result.update(standard_name='', auditor_name=wip.get('service_resource', ''),
                          audit_dates=[], header_attachment={}, charge_lines=[])
        return result
    if action == 'create_charges':
        return {'invoice_number': '', 'amount': ''}
    if action == 'send_charges':
        return {'sent': False}
    if action == 'charge_status':
        return {'ready': False, 'status': ''}
    if action == 'flex':
        result = {'order_number': '', 'proposal_number': contract.get('proposal_number', ''),
                  'billing_address': '', 'tax_rate_area': contract.get('tax_rate_area', '')}
        if wip.get('country') == 'South Africa':
            result.update(header_attachment={}, detail_attachments=[])
        return result
    if action in ('request_draft', 'request_final'):
        return {'job_id': ''}
    if action in ('draft_reports', 'final_reports'):
        return {'ready': False, 'reports': []}
    if action in ('download_draft', 'download_final'):
        return {'path': '', 'invoice_number': '', 'amount': '', 'net_amount': '', 'tax_amount': '',
                'gross_amount': '', 'currency': currency, 'tax_rate': contract.get('tax_rate', ''),
                'customer_name': contract.get('customer_name', ''), 'audit_dates': [],
                'contract_number': wip.get('contract_number', ''),
                'order_number': data.get('flex', {}).get('order_number', ''),
                'billing_address': '', 'tax_rate_area': contract.get('tax_rate_area', '')}
    if action == 'send_email':
        return {'message_id': '', 'recipient': contract.get('client_email', ''),
                'recipients': contract.get('client_emails') or [contract.get('client_email', '')]}
    raise ValueError('Unsupported assisted action')


class AssistedAdapter:
    mode = 'assisted'

    def __init__(self, root, evidence_roots=None):
        self.root = Path(root).resolve()
        self.tasks = self.root / 'manual_tasks'
        self.evidence_roots = [self.root, *[Path(value).resolve() for value in (evidence_roots or [])]]

    def _path(self, key):
        return self.tasks / (_identifier(key) + '.json')

    def get_task(self, key):
        with LOCK:
            return read_json(self._path(key))

    def list_tasks(self):
        with LOCK:
            return sorted((read_json(p) for p in self.tasks.glob('*.json')),
                          key=lambda task: task['created_at'], reverse=True)

    def execute(self, action, item, idempotency_key):
        if action not in INSTRUCTIONS:
            raise ValueError('Unsupported assisted action')
        with LOCK:
            path = self._path(idempotency_key)
            if path.exists():
                task = read_json(path)
                if task['action'] != action or task['item_id'] != item['id']:
                    raise ValueError('Manual task key does not match this action and item')
                if task['status'] == 'submitted':
                    return json.loads(json.dumps(task['response']))
            else:
                template = _template(action, item)
                fields = []
                for name, value in template.items():
                    kind = ('boolean' if isinstance(value, bool) else 'array' if isinstance(value, list)
                            else 'object' if isinstance(value, dict) else 'string')
                    fields.append({'name': name, 'label': name.replace('_', ' '), 'type': kind})
                task = {'id': idempotency_key, 'action': action, 'run_id': item.get('run_id'),
                        'item_id': item['id'], 'status': 'pending', 'created_at': _now(),
                        'instructions': INSTRUCTIONS[action], 'wip': item.get('wip', {}),
                        'data': item.get('data', {}), 'response_template': template,
                        'fields': fields, 'required_fields': list(template), 'history': []}
                write_json(path, task)
            raise ManualActionRequired(idempotency_key)

    def submit(self, key, response, actor, reason='', attachments=None):
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError('A named operator is required')
        if not isinstance(reason, str):
            raise ValueError('Evidence note must be text')
        if not isinstance(response, dict):
            raise ValueError('Confirmed action response must be an object')
        if not isinstance(attachments or [], list):
            raise ValueError('Attachments must be a list of uploaded file paths')
        with LOCK:
            path = self._path(key)
            task = read_json(path)
            if task['status'] == 'submitted' and task['response'] == response:
                return task
            if task['status'] != 'pending' and not reason.strip():
                raise ValueError('Correcting submitted evidence requires a reason')
            for field in ('path', 'signed_contract_path'):
                if response.get(field):
                    self._verify_local_pdf(response[field])
            verified_attachments = []
            for attachment in attachments or []:
                local = self._verify_local_pdf(attachment)
                verified_attachments.append({'path': str(local), 'sha256': hashlib.sha256(local.read_bytes()).hexdigest()})
            task['history'].append({'at': _now(), 'actor': actor.strip(), 'reason': reason.strip(),
                                    'previous_response': task.get('response'),
                                    'response_sha256': hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest(),
                                    'attachments': verified_attachments})
            task.update(status='submitted', response=response, submitted_at=_now(), actor=actor.strip())
            write_json(path, task)
            return task

    def _verify_local_pdf(self, value):
        if not isinstance(value, str):
            raise ValueError('Evidence path must be text')
        local = Path(value).resolve()
        if (not any(local.is_relative_to(root) for root in self.evidence_roots)
                or local.suffix.lower() != '.pdf' or not local.is_file()):
            raise ValueError('Use a PDF uploaded into this assisted workspace')
        if not local.read_bytes().startswith(b'%PDF-'):
            raise ValueError('Evidence is not a PDF')
        return local


class DocumentStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def list_documents(self):
        return sorted((read_json(p) for p in (self.root / 'document_metadata').glob('*.json')),
                      key=lambda doc: doc['uploaded_at'], reverse=True)

    def get_document(self, identifier):
        return read_json(self.root / 'document_metadata' / (_identifier(identifier) + '.json'))

    def import_extraction(self, source, result, actor='Workspace import'):
        """Reuse an existing checked document hash and its reviewable extraction."""
        source = Path(source)
        body = source.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if len(body) > MAX_PDF_BYTES or digest != result.get('source_sha256') or not body.startswith(b'%PDF-'):
            raise ValueError('Existing proposal source does not match its saved document hash')
        import pymupdf
        with pymupdf.open(stream=body, filetype='pdf') as document:
            if document.needs_pass or not 0 < len(document) <= MAX_PDF_PAGES:
                raise ValueError('Existing proposal must be an unlocked PDF under the page limit')
            pages = len(document)
        with LOCK:
            metadata_path = self.root / 'document_metadata' / (digest + '.json')
            if metadata_path.exists():
                return read_json(metadata_path)
            target = self.root / 'documents' / (digest + '.pdf')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            metadata = {'id': digest, 'name': result.get('filename') or source.name,
                        'path': str(target.resolve()), 'sha256': digest, 'page_count': pages,
                        'uploaded_at': _now(), 'uploaded_by': actor,
                        'upload_history': [{'actor': actor, 'at': _now()}],
                        'fields': result.get('fields', {}), 'candidates': result.get('candidates', {}),
                        'warnings': result.get('warnings', []), 'extraction_source': 'existing_proposal_workspace',
                        'extraction_mode': 'reused_existing',
                        'requires_review': any(not value or value.get('needs_review', True) for value in result.get('fields', {}).values())}
            write_json(metadata_path, metadata)
            return metadata

    def save_pdf(self, body, name, actor=''):
        if not isinstance(actor, str):
            raise ValueError('Uploader name must be text')
        if not isinstance(body, bytes) or not body or len(body) > MAX_PDF_BYTES:
            raise ValueError('Upload a PDF under 25 MB')
        if not body.startswith(b'%PDF-') or Path(str(name)).suffix.lower() != '.pdf':
            raise ValueError('Choose a PDF document')
        import pymupdf
        try:
            with pymupdf.open(stream=body, filetype='pdf') as document:
                if document.needs_pass or not 0 < len(document) <= MAX_PDF_PAGES:
                    raise ValueError('Upload an unlocked PDF with 1 to 200 pages')
        except pymupdf.FileDataError:
            raise ValueError('PDF data could not be read') from None
        digest = hashlib.sha256(body).hexdigest()
        target = self.root / 'documents' / (digest + '.pdf')
        metadata_path = self.root / 'document_metadata' / (digest + '.json')
        with LOCK:
            if metadata_path.exists():
                metadata = read_json(metadata_path)
                if actor.strip():
                    metadata.setdefault('upload_history', []).append({'actor': actor.strip(), 'at': _now(),
                                                                      'name': Path(str(name).replace('\\', '/')).name})
                    write_json(metadata_path, metadata)
                return metadata
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            from veritas.engine import read_pages, extract_local
            from veritas.economy import enrich_local
            warnings, fields, candidates, fact_candidates = [], {}, {}, {}
            try:
                pages = read_pages(target, cache_dir=self.root / 'ocr_cache')
                result = extract_local(Path(str(name)).name, pages)
                enrich_local(result, pages)
                fields, candidates = result.fields, result.candidates
                fact_candidates = _additional_candidates(pages)
                warnings = result.warnings
                page_count = len(pages)
            except Exception as exc:
                # Preserve the PDF for manual review even if local OCR is unavailable.
                warnings = [f'Local extraction unavailable ({type(exc).__name__}); review the uploaded PDF manually.']
                with pymupdf.open(target) as document:
                    page_count = len(document)
            metadata = {'id': digest, 'name': Path(str(name).replace('\\', '/')).name,
                        'path': str(target), 'original_pdf': str(target), 'sha256': digest,
                        'uploaded_at': _now(), 'pages': page_count, 'fields': fields,
                        'uploaded_by': actor.strip(),
                        'upload_history': [{'actor': actor.strip(), 'at': _now(),
                                            'name': Path(str(name).replace('\\', '/')).name}],
                        'candidates': candidates, 'warnings': warnings,
                        'fact_candidates': fact_candidates,
                        'extraction_mode': 'local_only', 'requires_review': True}
            write_json(metadata_path, metadata)
            return metadata


def render_demo_invoice(root, metadata, stage):
    if stage not in ('draft', 'final'):
        raise ValueError('Demo invoice stage must be draft or final')
    import pymupdf
    root = Path(root).resolve()
    folder = root / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    country = _slug(metadata.get('country', 'demo'))
    number = _slug(metadata.get('invoice_number') or metadata.get('order_number', 'demo'))
    issued = _issued_date(metadata)
    identity = hashlib.sha256(json.dumps(metadata, sort_keys=True, default=str).encode()).hexdigest()[:12]
    target = folder / f'DEMO_{country}_{number}_{issued}_{identity}_{stage}.pdf'
    text = '\n'.join(['DEMO ONLY - NOT A TAX INVOICE', f'{stage.upper()} INVOICE SAMPLE', '',
                      *[f'{key.replace("_", " ").title()}: {value}' for key, value in metadata.items()],
                      '', 'SYNTHETIC DATA. NO PORTAL INVOICE ISSUED. NO EMAIL SENT.'])
    if not target.exists():
        with pymupdf.open() as document:
            chunks = [text[index:index + 3000] for index in range(0, len(text), 3000)]
            for chunk in chunks:
                page = document.new_page()
                page.insert_textbox(pymupdf.Rect(45, 45, 550, 780), chunk, fontsize=9, color=(.1,.2,.15))
                page.insert_text((90, 440), 'DEMO SAMPLE', fontsize=55, color=(.85,.85,.85), overlay=False)
            document.save(target)
    return {**metadata, 'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'demo': True, 'stage': stage}


def file_invoice(root, source, metadata, evidence_roots=None):
    """File a verified local PDF; never replace a different invoice at the same name."""
    root, source = Path(root).resolve(), Path(source).resolve()
    allowed = [root, *[Path(value).resolve() for value in (evidence_roots or [])]]
    if not any(source.is_relative_to(folder) for folder in allowed) or source.suffix.lower() != '.pdf' or not source.is_file():
        raise ValueError('Invoice filing requires a PDF in this workspace')
    required = ('invoice_number', 'country', 'customer_name')
    if any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in required):
        raise ValueError('Invoice number, country and customer name are required for filing')
    issued = _issued_date(metadata)
    body = source.read_bytes()
    if not body.startswith(b'%PDF-'):
        raise ValueError('Invoice is not a valid PDF')
    digest = hashlib.sha256(body).hexdigest()
    if metadata.get('sha256') and metadata['sha256'] != digest:
        raise ValueError('Invoice changed before filing')
    folder = root / 'filed' / _slug(metadata['country']) / issued[:7]
    folder.mkdir(parents=True, exist_ok=True)
    filename = '_'.join((_slug(metadata['invoice_number']), issued, _slug(metadata['customer_name']))) + '.pdf'
    target = folder / filename
    with LOCK:
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('A different invoice is already filed under this name')
        if not target.exists():
            target.write_bytes(body)
        manifest = {'invoice_number': metadata['invoice_number'], 'country': metadata['country'],
                    'customer_name': metadata['customer_name'], 'issued_date': issued,
                    'source_path': str(source), 'path': str(target), 'sha256': digest,
                    'filed_at': _now(), 'filename': filename}
        write_json(target.with_suffix('.json'), manifest)
        return manifest
