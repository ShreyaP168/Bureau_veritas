"""Loopback-only HTTP adapter for the React proposal review interface."""
import json
import mimetypes
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from veritas.__main__ import run_batch, export_csv
from veritas.engine import FIELDS
from veritas.uc.storage import write_json

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'outputs' / 'web'
DIST = ROOT / 'ui' / 'dist'
LOCK = threading.Lock()
STATE = {'busy': False, 'message': 'Ready for documents', 'results': [], 'error': None}


def extract(source):
    try:
        def progress(message):
            with LOCK:
                STATE['message'] = str(message)
        results = run_batch(source, OUTPUT, progress=progress)
        with LOCK:
            STATE.update(results=results, message=f'Extracted {len(results)} document(s)')
    except Exception as exc:
        with LOCK:
            STATE.update(error=str(exc), message='Extraction failed')
    finally:
        with LOCK:
            STATE['busy'] = False


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data, content_type='application/json'):
        body = json.dumps(data).encode() if content_type == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path)
        from veritas.application_api import handle as application_handle
        if application_handle(self, route, OUTPUT):
            return
        from veritas.invoice_api import handle
        if handle(self, route, OUTPUT):
            return
        if route.path == '/api/state':
            with LOCK:
                return self.reply(200, STATE)
        if route.path in ('/api/export.json', '/api/export.csv'):
            path = OUTPUT / ('results.csv' if route.path.endswith('.csv') else 'results.json')
            if not path.exists():
                return self.reply(404, {'error': 'No export available yet'})
            return self.reply(200, path.read_bytes(), 'text/csv; charset=utf-8' if path.suffix == '.csv' else 'application/octet-stream')
        if route.path == '/api/document':
            try:
                index = int(parse_qs(route.query).get('index', ['-1'])[0])
                with LOCK:
                    if index < 0:
                        raise ValueError()
                    path = Path(STATE['results'][index]['source_path']).resolve()
                path.relative_to(OUTPUT.resolve())
                return self.reply(200, path.read_bytes(), 'application/pdf')
            except (ValueError, IndexError, KeyError, OSError):
                return self.reply(404, {'error': 'Original document unavailable'})
        path = (DIST / route.path.lstrip('/')).resolve()
        if not path.is_relative_to(DIST.resolve()):
            return self.reply(404, {'error': 'Not found'})
        if not path.is_file():
            path = DIST / 'index.html'
        if not path.exists():
            return self.reply(503, {'error': 'Build the UI first: cd ui && npm run build'})
        return self.reply(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream')

    def do_POST(self):
        # Reject browser cross-origin requests to this local service.
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://127.0.0.1:8765', 'http://localhost:8765', 'http://127.0.0.1:5173', 'http://localhost:5173'):
            return self.reply(403, {'error': 'Origin not allowed'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 100 * 1024 * 1024:
                return self.reply(413, {'error': 'Choose a file under 100 MB'})
            route = urlparse(self.path)
            from veritas.application_api import handle as application_handle
            if application_handle(self, route, OUTPUT, size):
                return
            from veritas.invoice_api import handle
            if handle(self, route, OUTPUT, size):
                return
            if route.path == '/api/upload':
                name = parse_qs(route.query).get('name', [''])[0]
                suffix = Path(name).suffix.lower()
                if suffix not in ('.pdf', '.zip'):
                    return self.reply(400, {'error': 'Choose a PDF or ZIP file'})
                with LOCK:
                    if STATE['busy']:
                        return self.reply(409, {'error': 'Extraction is already running'})
                    STATE.update(busy=True, error=None, message='Reading document…')
                try:
                    folder = OUTPUT / 'uploads' / uuid.uuid4().hex
                    folder.mkdir(parents=True)
                    safe_name = Path(name.replace('\\', '/')).name
                    source = folder / safe_name
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError('Incomplete upload')
                    source.write_bytes(body)
                    threading.Thread(target=extract, args=(source,), daemon=True).start()
                except Exception:
                    with LOCK:
                        STATE['busy'] = False
                    raise
                return self.reply(202, {'ok': True})
            if route.path == '/api/review':
                payload = json.loads(self.rfile.read(size))
                index = int(payload['index'])
                values = payload['values']
                if set(values) != set(FIELDS) or any(not isinstance(v, str) or len(v) > 10000 for v in values.values()):
                    raise ValueError('Invalid review fields')
                with LOCK:
                    if STATE['busy']:
                        return self.reply(409, {'error': 'Wait for extraction to finish'})
                    if index < 0:
                        raise ValueError('Invalid document')
                    result = STATE['results'][index]
                    now = datetime.now(timezone.utc).isoformat()
                    for key, value in values.items():
                        value = value.strip()
                        old = result['fields'].get(key)
                        if value != (old or {}).get('value', ''):
                            result.setdefault('review_history', []).append({'field': key, 'previous': old, 'value': value, 'reviewed_at': now})
                            result['fields'][key] = {'value': value, 'source': 'manual', 'needs_review': False, 'reviewed_at': now} if value else None
                        elif old:
                            old.update(needs_review=False, reviewed_at=now)
                    OUTPUT.mkdir(parents=True, exist_ok=True)
                    write_json(OUTPUT / 'results.json', STATE['results'])
                    export_csv(STATE['results'], OUTPUT / 'results.csv')
                    if all(result['fields'].values()) and result.get('source_sha256') and result.get('source_path'):
                        from dataclasses import fields as dataclass_fields
                        from veritas.engine import Result
                        from veritas.economy import VERSION
                        from veritas.uc.hashing import sha256_file
                        original = Path(result['source_path'])
                        if original.exists() and sha256_file(str(original)) == result['source_sha256']:
                            cached = {f.name: result[f.name] for f in dataclass_fields(Result) if f.name in result}
                            write_json(ROOT / 'outputs' / 'extraction-cache' / (result['source_sha256'] + '-' + VERSION + '.json'),
                                       {'source_sha256': result['source_sha256'], 'usage': result.get('usage'), 'result': cached})
                    STATE['message'] = 'Review saved'
                return self.reply(200, {'ok': True})
            return self.reply(404, {'error': 'Not found'})
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return self.reply(400, {'error': str(exc) or 'Invalid request'})
        except Exception:
            return self.reply(500, {'error': 'Could not complete request. Check the server log.'})


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    saved = OUTPUT / 'results.json'
    if saved.exists():
        STATE['results'] = json.loads(saved.read_text(encoding='utf-8'))
    # The proposal and invoicing evidence screens share the existing documents;
    # preserve their extraction and review flags without new OCR/model calls.
    from veritas.assisted import DocumentStore
    documents = DocumentStore(OUTPUT / 'operations' / 'documents')
    for result in STATE['results']:
        source = Path(result.get('source_path') or '').resolve()
        if source.is_file() and source.is_relative_to(OUTPUT.resolve()):
            try:
                documents.import_extraction(source, result)
            except (ValueError, OSError):
                pass
    print('Veritas React UI: http://127.0.0.1:8765', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()


if __name__ == '__main__':
    main()
