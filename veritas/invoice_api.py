"""Local HTTP integration for durable invoice runs (same origin as Veritas)."""
import json
import threading
import uuid
import hashlib
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs
from veritas.invoice_lock import exclusive

LOCK = threading.Lock()


def handle(handler, route, output, size=None):
    """Return True if this route was handled."""
    if route.path != '/api/invoicing' and not route.path.startswith('/api/invoicing/'):
        return False
    from veritas.invoicing import DemoAdapter, InvoiceWorkflow
    from veritas.invoice_gateway import configured_adapter
    from veritas.wip import load_wip
    query = parse_qs(route.query)
    mode = query.get('mode', ['live'])[0]
    try:
        if mode not in ('demo', 'assisted', 'live'):
            raise ValueError('mode must be demo, assisted or live')
        root = Path(output) / 'invoicing' / mode
        if mode == 'assisted':
            from veritas.assisted import AssistedAdapter
            adapter = AssistedAdapter(root)
        else:
            adapter = DemoAdapter(root / 'artifacts') if mode == 'demo' else configured_adapter()
        policy_path = Path(output) / 'operations' / 'countries.json'
        workflow = InvoiceWorkflow(root, adapter, policy_path=policy_path if policy_path.exists() else None)
        parts = route.path.rstrip('/').split('/')[3:]
        with LOCK, exclusive(root):
            if size is None:
                if len(parts) == 2 and parts[1] == 'artifact':
                    run = workflow.get(parts[0])
                    item = next((i for i in run['items'] if i['id'] == query.get('item', [''])[0]), None)
                    stage = query.get('stage', [''])[0]
                    if item is None or stage not in ('contract', 'download_draft', 'download_final'):
                        raise ValueError('Unknown invoice artifact')
                    artifact = item['data'].get(stage, {})
                    path = Path(artifact.get('path') or artifact.get('signed_contract_path') or '').resolve()
                    if (not any(path.is_relative_to(base.resolve()) for base in (root, Path(output) / 'operations' / 'documents'))
                            or path.suffix.lower() not in ('.txt', '.pdf')):
                        raise ValueError('Artifact is not available in this workspace')
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != artifact.get('sha256'):
                        raise ValueError('Artifact changed since verification')
                    handler.reply(200, content, 'application/pdf' if path.suffix.lower() == '.pdf' else 'text/plain; charset=utf-8')
                    return True
                if len(parts) > 1:
                    raise ValueError('Invalid invoice run path')
                result = workflow.list_runs() if not parts else workflow.get(parts[0])
            elif parts == ['seed-demo']:
                if mode != 'demo':
                    raise ValueError('Sample data can only be loaded in demo mode')
                handler.rfile.read(size)
                existing = workflow.list_runs()
                result = []
                for name, country in (('wip-demo.csv', 'Dubai'), ('wip-south-africa-demo.csv', 'South Africa')):
                    rows = load_wip(Path(__file__).resolve().parent.parent / 'scripts' / name)
                    for row in rows:
                        row['contract_number'] = 'UI-APP-' + row['contract_number']
                    match = next((r for r in existing if any(i['wip']['contract_number'] == rows[0]['contract_number'] for i in r['items'])), None)
                    if match is None:
                        match = workflow.create(rows, rows[0]['start_date'][:7], country)
                        match = workflow.advance(match['id'])
                    result.append(match)
                # Representative, isolated examples also populate reporting and
                # exception screens. Approvals here are explicitly simulated.
                for scenario in ('COMPLETED', 'EXCEPTION'):
                    contract = 'UI-APP-' + scenario + '-001'
                    match = next((r for r in existing if any(i['wip']['contract_number'] == contract for i in r['items'])), None)
                    if match is None:
                        rows = load_wip(Path(__file__).resolve().parent.parent / 'scripts' / 'wip-demo.csv')
                        rows[0]['contract_number'] = contract
                        if scenario == 'EXCEPTION':
                            class MismatchDemo(DemoAdapter):
                                def execute(self, action, item, key):
                                    response = super().execute(action, item, key)
                                    if action == 'contract':
                                        response['amount'] = str(Decimal(item['wip']['amount']) + 1)
                                    return response
                            example = InvoiceWorkflow(root, MismatchDemo(root / 'artifacts'), actor='Demo simulator')
                        else:
                            example = InvoiceWorkflow(root, DemoAdapter(root / 'artifacts'), actor='Demo simulator')
                        match = example.advance(example.create(rows, rows[0]['start_date'][:7], 'Dubai')['id'])
                        if scenario == 'COMPLETED':
                            match = example.approve(match['id'], 'Simulated draft reviewer')
                            match = example.approve(match['id'], 'Simulated final reviewer')
                    result.append(match)
            elif parts == ['import']:
                workflow.actor = query.get('actor', ['demo operator' if mode == 'demo' else ''])[0].strip()
                if not workflow.actor:
                    raise ValueError('Operator name is required for importing WIP')
                suffix = Path(query.get('name', [''])[0]).suffix.lower()
                if suffix not in ('.xlsx', '.csv'):
                    raise ValueError('Choose a WIP XLSX or CSV file')
                body = handler.rfile.read(size)
                if len(body) != size:
                    raise ValueError('Incomplete upload')
                folder = root / 'intake'
                folder.mkdir(parents=True, exist_ok=True)
                source = folder / (uuid.uuid4().hex + suffix)
                source.write_bytes(body)
                result = workflow.create(load_wip(source), query.get('month', [''])[0],
                                         query.get('country', ['Dubai'])[0])
            elif len(parts) == 2 and parts[1] in ('advance', 'approve', 'reconcile'):
                payload = json.loads(handler.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object')
                workflow.actor = str(payload.get('actor') or payload.get('reviewer') or ('demo operator' if mode == 'demo' else '')).strip()
                if not workflow.actor:
                    raise ValueError('Operator name is required')
                if parts[1] == 'advance':
                    result = workflow.advance(parts[0])
                elif parts[1] == 'approve':
                    result = workflow.approve(parts[0], payload.get('reviewer', ''), payload.get('reviewer_role', 'reviewer'))
                else:
                    result = workflow.reconcile(parts[0], payload.get('item_id'),
                                                payload.get('response'), payload.get('reviewer', ''))
            else:
                handler.reply(404, {'error': 'Unknown invoice operation'})
                return True
        handler.reply(200, result)
    except (ValueError, KeyError, RuntimeError, FileNotFoundError) as exc:
        handler.reply(400, {'error': str(exc)})
    return True
