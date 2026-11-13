import json
from pathlib import Path

from veritas.engine import FIELDS, Page, Result, process
from veritas.economy import MAX_IMAGE_PAGES, MAX_TEXT_CHARS, VERSION, enrich_local, select_context
from veritas.uc.hashing import sha256_file
from veritas.uc.storage import write_json


def test_context_excludes_boilerplate_and_caps_cloud_payload():
    pages = [Page(1, 'Commercial offer\nMan day Amount\n'+'TOTAL 4.0 18500\n'*500+'Note 1: generic terms', 'tesseract')]
    pages += [Page(n, 'General legal terms and conditions\n'*200, 'tesseract') for n in range(2, 40)]
    text, images = select_context(pages, ['man_days'])
    assert len(text) <= MAX_TEXT_CHARS
    assert len(images) <= MAX_IMAGE_PAGES
    assert images == [1]
    assert 'General legal' not in text


def test_local_audit_cover_wins_over_boilerplate():
    result = Result('contract.pdf')
    enrich_local(result, [Page(1, 'BV CONTRACT for QMS RECERTIFICATION & EMS CERTIFICATION', 'tesseract'),
                         Page(2, 'A special audit may be required.', 'tesseract')])
    assert result.fields['audit_type']['value'] == 'CONTRACT for QMS RECERTIFICATION & EMS CERTIFICATION'
    assert result.fields['audit_type']['needs_review']


def test_certification_cover_is_not_overridden_by_recertification_legal_note():
    result = Result('initial.pdf')
    enrich_local(result, [Page(1, 'Proposal for Certification\nissued to: A client', 'tesseract'),
        Page(9, 'Commercial offer\nNote 1: review during Stage 2/ Recertification audit.', 'tesseract')])
    assert result.fields['audit_type']['value'] == 'Certification'


def test_wrapped_cover_keeps_both_standards():
    result = Result('wrapped.pdf')
    enrich_local(result, [Page(1, 'BV CONTRACT for QMS\nRECERTIFICATION & EMS CERTIFICATION\nissued to: buyer', 'tesseract')])
    assert result.fields['audit_type']['value'] == 'CONTRACT for QMS RECERTIFICATION & EMS CERTIFICATION'


def test_cache_hit_makes_no_ocr_or_cloud_call(tmp_path, monkeypatch):
    import veritas.engine as engine
    monkeypatch.setenv('VERITAS_PROVIDER', 'azure')
    path = tmp_path/'source.pdf'; path.write_bytes(b'same-source')
    result = Result('original.pdf')
    result.fields = {k: {'value': 'recorded', 'source': 'gpt', 'needs_review': True} for k in FIELDS}
    result.usage = {'provider': 'azure', 'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}
    cache = tmp_path/'cache'
    write_json(cache/(sha256_file(str(path))+'-'+VERSION+'.json'), {'result': result.to_dict(), 'usage': result.usage})
    def forbidden(*args, **kwargs): raise AssertionError('No OCR or Azure expected')
    monkeypatch.setattr(engine, 'read_pages', forbidden)
    monkeypatch.setattr(engine, 'gpt_fallback', forbidden)
    cached = process(path, use_gpt=True, cache_dir=cache)
    assert cached.usage['total_tokens'] == 0
    assert cached.processing['original_usage']['total_tokens'] == 120
    assert cached.fields['amount']['needs_review']


def test_review_flag_does_not_trigger_cloud(tmp_path, monkeypatch):
    import veritas.engine as engine
    path = tmp_path/'new.pdf'; path.write_bytes(b'new-source')
    local = Result('new.pdf')
    local.fields = {k: {'value': 'local', 'source': 'tesseract', 'needs_review': True} for k in FIELDS}
    monkeypatch.setattr(engine, 'read_pages', lambda *a, **kw: [Page(1, 'text', 'tesseract')])
    monkeypatch.setattr(engine, 'extract_local', lambda *a: local)
    def forbidden(*args, **kwargs): raise AssertionError('Review flags must not cause paid calls')
    monkeypatch.setattr(engine, 'gpt_fallback', forbidden)
    result = process(path, use_gpt=True, refresh=True, cache_dir=tmp_path/'cache')
    assert result.usage['total_tokens'] == 0


def test_changed_source_cannot_use_existing_cache(tmp_path, monkeypatch):
    import veritas.engine as engine
    path = tmp_path/'file.pdf'; path.write_bytes(b'first')
    cache = tmp_path/'cache'
    write_json(cache/(sha256_file(str(path))+'-'+VERSION+'.json'), {'result': Result('old.pdf').to_dict()})
    path.write_bytes(b'changed')
    calls = []
    monkeypatch.setattr(engine, 'read_pages', lambda *a, **kw: calls.append(True) or [Page(1, 'text', 'pdf_text')])
    process(path, cache_dir=cache)
    assert calls
