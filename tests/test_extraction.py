import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from veritas.engine import Page, Result, extract_local, gpt_fallback, read_pages
from veritas.uc.intake import extract_zip
from veritas.__main__ import export_csv, run_batch


def test_provider_email_is_excluded_and_ocr_value_needs_review():
    result = extract_local('test.pdf', [Page(1, 'Email: buyer@example.com\ncontact@bureauveritas.com\nProposal No: ABC1234', 'tesseract')])
    assert [c['value'] for c in result.candidates['client_email_id']] == ['buyer@example.com']
    assert result.fields['client_email_id']['needs_review']
    assert result.fields['proposal_number']['value'] == 'ABC1234'


def test_multiple_audit_cycles_are_not_summed_or_selected():
    text = 'Recertification 5 man-days AED 2,000\nSurveillance 1 2 man-days AED 1,000\nSF NO: 1234567\nSF NO: 7654321'
    result = extract_local('multi.pdf', [Page(1, text, 'pdf_text')])
    for name in ('audit_type', 'amount', 'man_days', 'proposal_number'):
        assert result.fields[name] is None
        assert len(result.candidates[name]) >= 2


def test_zar_and_proposal_degree_symbol():
    result = extract_local('x.pdf', [Page(1, 'Proposal N°: 7711204\nFees 69850.00 ZAR', 'tesseract')])
    assert result.fields['proposal_number']['value'] == '7711204'
    assert result.candidates['amount'][0]['value'] == '69850.00 ZAR'


def test_zip_traversal_is_rejected_before_any_write(tmp_path):
    archive = tmp_path/'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('valid.pdf', 'test')
        z.writestr('../escape.pdf', 'test')
    with pytest.raises(ValueError): extract_zip(archive, tmp_path/'unpacked')
    assert not (tmp_path/'unpacked/valid.pdf').exists()


def test_text_pdf_is_processed_without_ocr_and_cached(tmp_path):
    import pymupdf
    path = tmp_path/'text.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 40), ('Detailed contract information for audit services.\n'*20)+'Proposal No: PR123456\nEmail: buyer@example.com')
        doc.save(path)
    pages = read_pages(path, tmp_path/'cache')
    assert pages[0].method == 'pdf_text'
    assert read_pages(path, tmp_path/'cache') == pages
    result = extract_local(path.name, pages)
    assert result.fields['proposal_number']['value'] == 'PR123456'


def test_gpt_fallback_uses_images_and_marks_unverified_evidence(tmp_path):
    import pymupdf
    path = tmp_path/'scan.pdf'
    with pymupdf.open() as doc:
        doc.new_page(); doc.save(path)
    calls = []
    def parse(**kwargs):
        calls.append(kwargs)
        schema = kwargs['text_format']
        return SimpleNamespace(output_parsed=schema(audit_type=None, amount={'value': 'AED 100', 'page': 1, 'evidence': 'not present in OCR'}, man_days=None, proposal_number=None, client_email_id=None),
            provider='azure', usage={'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120})
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = gpt_fallback(Result('scan.pdf'), [Page(1, 'source text', 'tesseract')], path, 'configured-model', client)
    assert calls[0]['store'] is False
    assert calls[0]['input'][0]['content'][1]['type'] == 'input_image'
    assert result.fields['amount']['needs_review'] is True
    assert result.fields['amount']['grounding']['grounded'] is False
    assert result.usage['input_tokens'] == 100
    assert result.usage['output_tokens'] == 20
    assert result.usage['total_tokens'] == 120


def test_export_escapes_formulas_and_preserves_missing_fields(tmp_path):
    result = Result('=FORMULA.pdf')
    export_csv([result], tmp_path/'out.csv')
    assert "'=FORMULA.pdf" in (tmp_path/'out.csv').read_text(encoding='utf-8-sig')


def test_batch_continues_after_bad_pdf(tmp_path):
    source = tmp_path/'input'; source.mkdir()
    (source/'bad.pdf').write_text('not a PDF')
    results = run_batch(source, tmp_path/'out')
    assert len(results) == 1
    assert 'failed' in results[0]['warnings'][0]
    assert json.loads((tmp_path/'out/results.json').read_text())[0]['filename'] == 'bad.pdf'


def test_ocr_failure_still_routes_to_gpt_vision(tmp_path, monkeypatch):
    import pymupdf
    import veritas.engine as engine
    path = tmp_path/'scan.pdf'
    with pymupdf.open() as doc:
        doc.new_page(); doc.save(path)
    def unavailable(*args, **kwargs): raise RuntimeError('OCR unavailable')
    calls = []
    def fallback(result, pages, pdf_path, model=None):
        calls.append(pdf_path)
        result.fields['audit_type'] = {'value': 'Recertification', 'source': 'gpt', 'needs_review': True}
    monkeypatch.setattr(engine, 'read_pages', unavailable)
    monkeypatch.setattr(engine, 'gpt_fallback', fallback)
    result = engine.process(path, use_gpt=True)
    assert calls == [path]
    assert result.fields['audit_type']['source'] == 'gpt'
    with pytest.raises(RuntimeError): engine.process(path, use_gpt=False)


def test_split_proposal_reference_does_not_publish_prefix():
    result = extract_local('x.pdf', [Page(2, 'Proposal No.\nCERLB-26- 15353364-9K / Rev.00', 'tesseract')])
    assert result.fields['proposal_number']['value'] == 'CERLB-26-15353364-9K/Rev.00'


def test_source_amount_typo_and_inferred_table_units_are_flagged():
    from veritas.engine import add_quality_warnings
    result = extract_local('test.pdf', [Page(1, 'Total Proposal value: 2,435,0000.00 KES', 'tesseract')])
    result.fields['man_days'] = {'value': '5.5', 'source': 'gpt'}
    add_quality_warnings(result)
    assert any('irregular digit grouping' in w for w in result.warnings)
    assert any('verify units' in w for w in result.warnings)
