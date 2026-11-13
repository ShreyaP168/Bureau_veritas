from pathlib import Path
import hashlib
import uuid

import pytest

from veritas.assisted import (AssistedAdapter, ManualActionRequired, DocumentStore,
                              file_invoice, render_demo_invoice)


@pytest.fixture
def workspace():
    root = Path('runtime') / ('assisted-tests-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    return root.resolve()


def item():
    return {'id': 'item-1', 'run_id': 'run-1', 'wip': {'country': 'Dubai',
            'contract_number': 'C-001', 'start_date': '2026-11-01',
            'amount': '1000', 'currency': 'AED', 'audit_type': 'Special Audit'}, 'data': {}}


def test_manual_action_waits_for_named_confirmation_and_is_idempotent(workspace):
    adapter = AssistedAdapter(workspace)
    key = 'a' * 64
    for _ in range(2):
        with pytest.raises(ManualActionRequired) as error:
            adapter.execute('create_charges', item(), key)
        assert error.value.task_id == key
    tasks = adapter.list_tasks()
    assert len(tasks) == 1 and tasks[0]['status'] == 'pending'
    assert tasks[0]['run_id'] == 'run-1'
    assert 'create charges once' in tasks[0]['instructions'].lower()
    response = {'invoice_number': 'EXTERNAL-1', 'amount': '1000'}
    with pytest.raises(ValueError, match='operator'):
        adapter.submit(key, response, '')
    assert adapter.get_task(key)['status'] == 'pending'
    submitted = adapter.submit(key, response, 'Operator A', 'Verified SmartCert charge')
    assert submitted['history'][0]['actor'] == 'Operator A'
    assert adapter.execute('create_charges', item(), key) == response
    assert len(adapter.submit(key, response, 'Operator A')['history']) == 1
    with pytest.raises(ValueError, match='reason'):
        adapter.submit(key, dict(response, invoice_number='EXTERNAL-2'), 'Operator A')


def test_assisted_evidence_must_be_an_uploaded_local_pdf(workspace):
    adapter = AssistedAdapter(workspace)
    key = 'b' * 64
    with pytest.raises(ManualActionRequired):
        adapter.execute('download_draft', item(), key)
    outside = workspace.parent / ('outside-' + uuid.uuid4().hex + '.pdf')
    outside.write_bytes(b'%PDF-1.4\n')
    with pytest.raises(ValueError, match='uploaded'):
        adapter.submit(key, {'path': str(outside)}, 'Operator A')
    assert adapter.get_task(key)['status'] == 'pending'
    with pytest.raises(ValueError, match='identifier'):
        adapter.get_task('../../outside')


def test_upload_uses_local_extraction_and_retains_review_flags(workspace, monkeypatch):
    import pymupdf
    from veritas.engine import Page
    from veritas import engine
    with pymupdf.open() as document:
        document.new_page()
        body = document.tobytes()
    monkeypatch.setattr(engine, 'read_pages', lambda *args, **kwargs: [Page(1,
        'Special audit\nProposal number: DEMO-12345\nEmail: client@example.test\nAED 1000', 'pdf_text')])
    store = DocumentStore(workspace)
    uploaded = store.save_pdf(body, '../../Signed proposal.pdf', actor='Uploader A')
    assert uploaded['name'] == 'Signed proposal.pdf'
    assert uploaded['sha256'] == hashlib.sha256(body).hexdigest()
    assert Path(uploaded['path']).is_relative_to(workspace)
    assert uploaded['extraction_mode'] == 'local_only'
    assert uploaded['requires_review']
    assert uploaded['uploaded_by'] == 'Uploader A'
    assert uploaded['fields']['proposal_number']['value'] == 'DEMO-12345'
    assert len(store.list_documents()) == 1
    duplicate = store.save_pdf(body, 'duplicate.pdf', actor='Uploader B')
    assert duplicate['id'] == uploaded['id']
    assert duplicate['uploaded_by'] == 'Uploader A'
    assert duplicate['upload_history'][-1]['actor'] == 'Uploader B'
    assert len(store.list_documents()) == 1
    with pytest.raises(ValueError, match='PDF'):
        store.save_pdf(b'not pdf', 'fake.pdf')


def test_demo_pdf_and_filing_preserve_hash_and_prevent_replacement(workspace):
    import pymupdf
    metadata = {'country': 'Dubai', 'invoice_number': 'DEMO/INV-001', 'customer_name': 'Demo Customer',
                'issued_date': '2026-11-13', 'net_amount': '1000', 'tax_amount': '50', 'gross_amount': '1050'}
    generated = render_demo_invoice(workspace, metadata, 'final')
    assert generated['demo'] is True
    with pymupdf.open(generated['path']) as document:
        text = ''.join(page.get_text() for page in document)
    assert 'DEMO ONLY' in text and 'NOT A TAX INVOICE' in text
    repeat = render_demo_invoice(workspace, metadata, 'final')
    assert repeat['sha256'] == generated['sha256']
    filed = file_invoice(workspace, generated['path'], generated)
    assert filed['sha256'] == generated['sha256']
    assert 'DEMO-INV-001_2026-11-13_Demo-Customer.pdf' == filed['filename']
    another = render_demo_invoice(workspace, dict(metadata, gross_amount='999'), 'final')
    with pytest.raises(ValueError, match='different invoice'):
        file_invoice(workspace, another['path'], another)
    changed = dict(generated, sha256='0' * 64)
    with pytest.raises(ValueError, match='changed'):
        file_invoice(workspace, generated['path'], changed)


def test_existing_proposal_import_preserves_evidence_without_ocr(workspace, monkeypatch):
    import pymupdf
    from veritas import engine
    def no_extraction(*args, **kwargs):
        raise AssertionError('Import must reuse saved evidence without OCR or model calls')
    monkeypatch.setattr(engine, 'read_pages', no_extraction)
    source = workspace / 'original.pdf'
    with pymupdf.open() as document:
        document.new_page()
        document.save(source)
    result = {'filename': 'Existing proposal.pdf', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'fields': {'amount': {'value': '1500', 'evidence': 'Commercial offer AED 1500', 'needs_review': True}}}
    store = DocumentStore(workspace / 'library')
    imported = store.import_extraction(source, result)
    assert imported['fields'] == result['fields']
    assert imported['requires_review'] is True
    assert len(store.list_documents()) == 1
    assert store.import_extraction(source, result)['id'] == imported['id']
    with pytest.raises(ValueError, match='hash'):
        store.import_extraction(source, dict(result, source_sha256='0' * 64))


def test_trusted_document_store_can_be_attested_and_filed(workspace):
    documents = workspace / 'shared-documents'
    assisted = workspace / 'assisted'
    metadata = {'country': 'Dubai', 'invoice_number': 'TEST-001', 'customer_name': 'Test Customer'}
    pdf = render_demo_invoice(documents, metadata, 'final')
    adapter = AssistedAdapter(assisted, evidence_roots=[documents])
    key = 'c' * 64
    with pytest.raises(ManualActionRequired):
        adapter.execute('download_final', item(), key)
    adapter.submit(key, {'path': pdf['path']}, 'Operator', attachments=[pdf['path']])
    assert adapter.get_task(key)['history'][0]['attachments'][0]['sha256'] == pdf['sha256']
    with pytest.raises(ValueError, match='workspace'):
        file_invoice(assisted, pdf['path'], pdf)
    filed = file_invoice(assisted, pdf['path'], pdf, evidence_roots=[documents])
    assert Path(filed['path']).is_relative_to(assisted)
    assert filed['sha256'] == pdf['sha256']
