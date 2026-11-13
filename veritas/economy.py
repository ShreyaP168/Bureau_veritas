"""Local field resolution and strictly bounded cloud context."""
import re

VERSION = 'economy-1'
MAX_TEXT_CHARS = 3500
MAX_IMAGE_PAGES = 2


def enrich_local(result, pages):
    def set_field(key, value, page, evidence):
        result.fields[key] = {'value': value, 'page': page.number, 'evidence': evidence,
                              'source': page.method, 'needs_review': True}

    for page in pages:
        for match in re.finditer(r'\b[A-Z0-9_%+-]+(?:\.[ \t]*[A-Z0-9_%+-]+)+@[A-Z0-9.-]+\.[A-Z]{2,}\b', page.text, re.I):
            if re.search(r'\.[ \t]+', match.group()):
                set_field('client_email_id', re.sub(r'\s+', '', match.group()), page, match.group())

    # Cover descriptions can wrap; legal notes are never contracted audit types.
    if pages:
        cover = pages[0]
        flattened = re.sub(r'\s+', ' ', cover.text)
        match = re.search(r'contract\s+for\s+(.{1,200}?)(?=\s+issued\s+to|\s+for\s+standards|$)', flattened, re.I)
        if match and re.search(r'certification|audit', match.group(), re.I):
            set_field('audit_type', match.group().strip(), cover, match.group())
        elif re.search(r'proposal for re[- ]?certification', flattened, re.I):
            set_field('audit_type', 'Recertification', cover, 'Proposal for Re-certification')
        elif re.search(r'proposal for certification', flattened, re.I):
            set_field('audit_type', 'Certification', cover, 'Proposal for Certification')
    for page in pages if result.fields['audit_type'] is None else []:
        match = re.search(r'(?:contract\s+for\s+[^\n]+|proposal\s+for\s+re[- ]?certification|Bureau Veritas hypothesis[^\n]*re[- ]?certification)', page.text, re.I)
        if match:
            set_field('audit_type', 'Recertification', page, match.group())
            break
    if result.fields['audit_type'] is None:
        for page in pages:
            if re.search(r'commercial offer|certification scope|charge.out rate', page.text, re.I):
                commercial = re.split(r'\bNote\s*1\s*[:.]?', page.text, maxsplit=1, flags=re.I)[0]
                match = re.search(r'\bre[- ]?certification\s+audit\b', commercial, re.I)
                if match:
                    set_field('audit_type', match.group(), page, match.group())
                    break
    if result.fields['audit_type'] is None and pages:
        cover = pages[0]
        if re.search(r'proposal for certification', cover.text, re.I):
            set_field('audit_type', 'Certification', cover, 'Proposal for Certification')

    refs = [c for c in result.candidates['proposal_number'] if re.search(r'\d', c['value'])]
    # Preserve multiple SF numbers without picking one or merging unrelated stages.
    sf = [c for c in refs if re.search(r'SF\s*NO', c['evidence'], re.I)]
    if sf:
        values = list(dict.fromkeys(c['value'] for c in sf))
        result.fields['proposal_number'] = {**sf[0], 'value': '; '.join(values), 'needs_review': True}
    elif refs and result.fields['proposal_number'] is None:
        unique = {c['value'].lower(): c for c in refs}
        if len(unique) == 1:
            result.fields['proposal_number'] = {**next(iter(unique.values())), 'needs_review': True}

    if result.fields['amount'] is None:
        # Currency amounts on a commercial table remain separate labelled rows.
        selected = []
        for page in pages:
            if not re.search(r'certification scope|commercial offer|charge.out rate', page.text, re.I):
                continue
            for candidate in result.candidates['amount']:
                if candidate['page'] == page.number and re.search(r'\b(?:audit|reporting|fees and expenses)\b', candidate['evidence'], re.I):
                    if candidate['evidence'] not in [c['evidence'] for c in selected]:
                        selected.append(candidate)
        if selected:
            result.fields['amount'] = {**selected[0], 'value': '; '.join(c['evidence'] for c in selected), 'needs_review': True}
    if result.fields['man_days'] is None:
        values = {c['value'].lower(): c for c in result.candidates['man_days']}
        if len(values) == 1:
            result.fields['man_days'] = {**next(iter(values.values())), 'needs_review': True}
    return result


def repair_email_ocr(result, pages, pdf_path, cache_dir='outputs/cache'):
    """Retry suspicious split email lines locally; never guess missing punctuation."""
    import json
    from pathlib import Path
    from veritas.uc.hashing import sha256_file
    from veritas.uc.storage import write_json
    email = result.fields.get('client_email_id')
    if email is None:
        return
    # Dot-space separators are handled without inventing punctuation above.
    evidence = email.get('evidence', '')
    label = re.search(r'email\s*[:_]+\s*([^\n@]+)@', evidence, re.I)
    if not label or not re.search(r'[A-Z]\s+[A-Z]', label.group(1), re.I):
        return
    import pymupdf
    import pytesseract
    import shutil, os
    from PIL import Image
    exe = os.getenv('TESSERACT_CMD') or shutil.which('tesseract') or r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    pytesseract.pytesseract.tesseract_cmd = exe
    saved = Path(cache_dir)/(sha256_file(str(pdf_path))+f'-email-{email["page"]}-psm11.json')
    try:
        if saved.exists():
            text = json.loads(saved.read_text(encoding='utf-8'))['text']
        else:
            with pymupdf.open(pdf_path) as doc:
                pix = doc[email['page']-1].get_pixmap(dpi=300, colorspace=pymupdf.csRGB)
                text = pytesseract.image_to_string(Image.frombytes('RGB', (pix.width, pix.height), pix.samples), config='--psm 11', timeout=90)
            write_json(saved, {'text': text})
        matches = re.findall(r'\b[A-Z0-9._%+-]+@\s*[A-Z0-9.-]+\.[A-Z]{2,}\b', text, re.I)
        domain = email['value'].split('@')[-1].lower()
        matches = [m for m in matches if re.sub(r'\s+', '', m).split('@')[-1].lower() == domain]
        if len(set(matches)) == 1:
            result.fields['client_email_id'] = {**email, 'value': re.sub(r'\s+', '', matches[0]), 'evidence': matches[0], 'source': 'tesseract_psm11'}
        else:
            result.fields['client_email_id'] = None
    except (OSError, RuntimeError):
        result.fields['client_email_id'] = None
        result.warnings.append('Split client email requires review; local OCR retry could not resolve it.')


def select_context(pages, missing):
    patterns = {
        'man_days': r'man[ -]?days?|commercial offer|charge.out rate|quantity|\bTOTAL\b',
        'amount': r'total proposal|total cost|\b(?:AED|USD|KES|ZAR)\b',
        'proposal_number': r'proposal\s*(?:no|n°|number)|SF\s*NO|/CER/',
        'client_email_id': r'@|client contact|general contact',
        'audit_type': r'proposal for|contract for|hypothesis|commercial offer|recertification audit',
    }
    snippets = []
    ranked = []
    for page in pages:
        score = 0
        lines = page.text.splitlines()
        indices = set()
        # Exclude boilerplate after commercial notes from field selection.
        for index, line in enumerate(lines):
            if re.match(r'\s*Note\s*1\s*[:.]', line, re.I):
                break
            hits = sum(bool(re.search(patterns[k], line, re.I)) for k in missing)
            if hits:
                score += hits
                indices.update(range(max(0, index-1), min(len(lines), index+3)))
        if score:
            snippet = '\n'.join(lines[i] for i in sorted(indices))[:1100]
            snippets.append((score, page.number, f'PAGE {page.number}\n{snippet}'))
        # Images are reserved for tables or unreadable source pages.
        table = bool(re.search(r'commercial offer|charge.out rate|man[ -]?day\s+amount|total cost for', page.text, re.I))
        if ('man_days' in missing and table) or len(page.text.strip()) < 40:
            ranked.append((score, page.number))
    if not snippets and pages:
        snippets = [(1, pages[0].number, f'PAGE {pages[0].number}\n{pages[0].text[:1100]}')]
    budget, chunks = MAX_TEXT_CHARS, []
    for _, _, snippet in sorted(snippets, reverse=True):
        if budget <= 0:
            break
        piece = ('\n' if chunks else '')+snippet
        chunks.append(piece[:budget]); budget -= len(chunks[-1])
    useful = [(score, n) for score, n in ranked if score > 0]
    image_pages = [n for _, n in sorted(useful or ranked, reverse=True)[:MAX_IMAGE_PAGES]]
    return ''.join(chunks), sorted(image_pages)
