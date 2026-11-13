import argparse
import csv
import shutil
from pathlib import Path

from veritas.engine import FIELDS, process
from veritas.uc.intake import extract_zip
from veritas.uc.storage import write_json
from veritas.uc.hashing import sha256_file


def export_csv(results, path):
    with Path(path).open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(['filename', *FIELDS, 'unresolved_fields', 'review_required_fields'])
        for result in results:
            data = result.to_dict() if hasattr(result, 'to_dict') else result
            values = [data['fields'][k]['value'] if data['fields'][k] else '' for k in FIELDS]
            # Prevent formula execution when opening exports in spreadsheet software.
            safe = lambda s: "'"+s if str(s).startswith(('=', '+', '-', '@')) else s
            writer.writerow([safe(data['filename']), *map(safe, values),
                             ', '.join(k for k in FIELDS if not data['fields'][k]),
                             ', '.join(k for k in FIELDS if data['fields'][k] and data['fields'][k].get('needs_review'))])


def run_batch(source, output, use_gpt=False, model=None, progress=print, refresh=False):
    source, output = Path(source), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == '.zip':
        root = extract_zip(source, output/'intake'/sha256_file(str(source)))
        paths = sorted(p for p in root.rglob('*') if p.suffix.lower() == '.pdf')
    elif source.is_dir():
        paths = sorted(p for p in source.rglob('*') if p.suffix.lower() == '.pdf')
    else:
        paths = [source]
    if not paths:
        raise ValueError('No PDF documents found.')
    results = []
    for path in paths:
        if path.suffix.lower() != '.pdf':
            raise ValueError('Choose a PDF, ZIP of PDFs, or folder.')
        try:
            result = process(path, use_gpt, model, progress, refresh=refresh)
            data = result.to_dict()
            digest = sha256_file(str(path))
            saved_source = output/'documents'/(digest+'.pdf')
            saved_source.parent.mkdir(parents=True, exist_ok=True)
            if not saved_source.exists(): shutil.copyfile(path, saved_source)
            data.update(source_sha256=digest, source_path=str(saved_source.resolve()))
            results.append(data)
        except Exception as exc:
            from veritas.engine import Result
            result = Result(path.name)
            result.warnings.append(f'Document processing failed: {type(exc).__name__}: {exc}')
            results.append(result.to_dict())
        # Publish each completed document so partial batches remain usable.
        write_json(output/'results.json', results)
    export_csv(results, output/'results.csv')
    usages = [{'filename': r['filename'], **r['usage']} for r in results if r.get('usage') and r['usage'].get('total_tokens') is not None]
    write_json(output/'token_usage.json', {
        'documents': usages,
        'documents_with_usage': len(usages),
        'documents_without_usage': len(results)-len(usages),
        'totals': {key: sum(u.get(key) or 0 for u in usages)
                   for key in ('input_tokens', 'output_tokens', 'total_tokens')},
        'cloud_totals': {key: sum(u.get(key) or 0 for u in usages if u.get('provider') in ('azure', 'openai'))
                         for key in ('input_tokens', 'output_tokens', 'total_tokens')},
    })
    return results


def main():
    parser = argparse.ArgumentParser(description='UC-based local audit proposal extraction')
    parser.add_argument('source', nargs='?', help='PDF, ZIP, or folder; omit to open desktop app')
    parser.add_argument('--output', default='outputs')
    parser.add_argument('--gpt', action='store_true', help='Allow sending document text/images to OpenAI')
    parser.add_argument('--model', default=None)
    parser.add_argument('--refresh', action='store_true', help='Ignore saved extraction results; GPT calls still require --gpt')
    parser.add_argument('--fallback', choices=['azure', 'off'], default='off', help='Local OCR and cache by default; Azure requires explicit selection')
    args = parser.parse_args()
    if args.source:
        import os
        os.environ['VERITAS_PROVIDER'] = 'azure'
        enabled = args.gpt or args.fallback != 'off'
        results = run_batch(args.source, args.output, enabled, args.model, refresh=args.refresh)
        print(f'Saved {len(results)} document(s) to {args.output}')
    else:
        from veritas.desktop import launch
        launch()


if __name__ == '__main__':
    main()
