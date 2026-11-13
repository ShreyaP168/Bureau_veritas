import re
SUMMARY_FIELDS = set()

def _squash(text):
    return re.sub(r'\W+', '', text or '').lower()


def _tokens(text):
    return re.findall(r'[a-z0-9]+', (text or '').lower())


def verify_quotes(block, evidence, selected):
    """Ground each field: its quote must really be on the cited page. A contiguous match counts as
    exact. Because label and value often sit on different lines of extracted text, a quote whose every
    word and number occurs on the cited page also counts, marked "tokens". Anything else is ungrounded."""
    by_page = {number: _squash(text) for number, text in selected}
    words = {number: set(_tokens(text)) for number, text in selected}
    pooled = ''.join(by_page.values())
    pooled_words = set().union(*words.values()) if words else set()
    groundings = {}
    for field, item in (evidence or {}).items():
        if not isinstance(item, dict) or not isinstance(item.get('quote'), str):
            continue
        quote, tokens = _squash(item['quote']), _tokens(item['quote'])
        page = item.get('page') if isinstance(item.get('page'), int) and item.get('page') in by_page else None
        haystack, vocabulary = (by_page[page], words[page]) if page else (pooled, pooled_words)
        method = 'exact' if len(quote) >= 4 and quote in haystack else 'tokens' if tokens and all(t in vocabulary for t in tokens) else None
        groundings[field] = dict(grounded=method is not None, method=method, page=page, snippet=item['quote'][:160])
    filled = []
    for key, value in block.items():
        if key in SUMMARY_FIELDS:
            continue
        if isinstance(value, dict):
            filled += [f'{key}.{k}' for k, v in value.items() if v not in (None, '', [], {})]
        elif value not in (None, '', [], {}):
            filled.append(key)
    for field in filled:
        groundings.setdefault(field, dict(grounded=False, method=None, page=None, snippet=None))
    return groundings, filled
