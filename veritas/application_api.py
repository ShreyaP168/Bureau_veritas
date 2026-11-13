"""Local application endpoints for operations, evidence and assisted actions."""
import hashlib
import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs

from veritas.invoice_lock import exclusive
from veritas.uc.storage import write_json, read_json

LOCK = threading.RLock()


def handle(handler, route, output, size=None):
    if not route.path.startswith('/api/application/'):
        return False
    from veritas.operations import Operations
    from veritas.assisted import AssistedAdapter, DocumentStore
    from veritas.invoicing import InvoiceWorkflow
    from veritas.country_policies import CountryPolicies
    output = Path(output)
    query = parse_qs(route.query)
    mode = query.get('mode', ['assisted'])[0]
    endpoint = route.path[len('/api/application/'):].strip('/')
    try:
        if mode not in ('demo', 'assisted', 'live'):
            raise ValueError('Unknown workspace mode')
        service = Operations(output)
        evidence_root = output / 'operations' / 'documents'
        documents = DocumentStore(evidence_root)
        policy_path = output / 'operations' / 'countries.json'
        with LOCK:
            if size is None:
                if endpoint == 'dashboard':
                    result = service.get_dashboard(mode, query.get('month', [''])[0])
                elif endpoint == 'export.csv':
                    handler.reply(200, service.export_csv(mode, query.get('month', [''])[0]), 'text/csv; charset=utf-8')
                    return True
                elif endpoint == 'settings':
                    result = service.get_settings()
                elif endpoint == 'readiness':
                    result = service.readiness()
                elif endpoint == 'cases':
                    result = service.list_cases(mode)
                elif endpoint == 'policies':
                    result = CountryPolicies(path=policy_path if policy_path.exists() else None).export()
                elif endpoint == 'documents':
                    result = documents.list_documents()
                elif endpoint == 'document':
                    document = documents.get_document(query.get('id', [''])[0])
                    path = Path(document['path']).resolve()
                    if not path.is_relative_to(evidence_root.resolve()):
                        raise ValueError('Document is outside the evidence store')
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != document['sha256']:
                        raise ValueError('Evidence file changed')
                    handler.reply(200, content, 'application/pdf')
                    return True
                elif endpoint == 'tasks':
                    result = AssistedAdapter(output / 'invoicing' / 'assisted').list_tasks() if mode == 'assisted' else []
                else:
                    raise ValueError('Unknown application endpoint')
            elif endpoint == 'documents':
                actor = query.get('actor', [''])[0].strip()
                if not actor:
                    raise ValueError('Operator name is required for evidence upload')
                if size > 25 * 1024 * 1024:
                    raise ValueError('PDF must be no larger than 25 MB')
                content = handler.rfile.read(size)
                if len(content) != size:
                    raise ValueError('Incomplete PDF upload')
                result = documents.save_pdf(content, query.get('name', ['document.pdf'])[0], actor=actor)
            else:
                payload = json.loads(handler.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object')
                if endpoint == 'settings':
                    result = service.save_settings(payload)
                elif endpoint == 'cases':
                    result = service.update_case(mode, payload.get('run_id'), payload.get('item_id'),
                                                 {key:value for key,value in payload.items() if key not in ('run_id', 'item_id')})
                elif endpoint == 'policies':
                    actor = str(payload.get('actor', '')).strip()
                    if not actor:
                        raise ValueError('Actor is required')
                    if not isinstance(payload.get('policies'), dict):
                        raise ValueError('A country policies object is required')
                    policies = CountryPolicies(data=payload.get('policies'))
                    with exclusive(output / 'operations'):
                        from datetime import datetime, timezone
                        history_path = output / 'operations' / 'policy-history.json'
                        history = read_json(history_path) if history_path.exists() else []
                        history.append({'actor': actor, 'at': datetime.now(timezone.utc).isoformat(),
                                        'previous': CountryPolicies(path=policy_path if policy_path.exists() else None).export(),
                                        'updated': policies.export()})
                        write_json(history_path, history)
                        write_json(policy_path, policies.export())
                    result = policies.export()
                elif endpoint.startswith('tasks/') and endpoint.endswith('/submit'):
                    task_id = endpoint.split('/')[1]
                    if not re.fullmatch(r'[a-f0-9]{64}', task_id):
                        raise ValueError('Invalid task ID')
                    root = output / 'invoicing' / 'assisted'
                    adapter = AssistedAdapter(root, evidence_roots=[evidence_root])
                    with exclusive(root):
                        task = adapter.get_task(task_id)
                        workflow = InvoiceWorkflow(root, adapter, policy_path=policy_path if policy_path.exists() else None)
                        workflow.actor = str(payload.get('actor', '')).strip()
                        if not workflow.actor:
                            raise ValueError('Actor is required')
                        run = workflow.get(task['run_id'])
                        item = next(i for i in run['items'] if i['id'] == task['item_id'])
                        if item['step'] != task['action']:
                            raise ValueError('This task is no longer the active workflow step')
                        response = payload.get('response')
                        if not isinstance(response, dict):
                            raise ValueError('Action results must be an object')
                        for field in ('path', 'signed_contract_path'):
                            if response.get(field):
                                path = Path(response[field]).resolve()
                                if not path.is_relative_to(evidence_root.resolve()):
                                    raise ValueError('Upload the PDF to the evidence library and select it for this task')
                        try:
                            workflow._validate(task['action'], item, response)
                        except (ValueError, TypeError, KeyError) as exc:
                            item['status'] = 'blocked'
                            item['error'] = str(exc)
                            item.setdefault('validation_attempts', []).append({'action': task['action'], 'actor': workflow.actor,
                                                                             'reason': str(exc), 'response': response})
                            run['status'] = 'blocked'
                            run['errors'] = [i['error'] for i in run['items'] if i.get('error')]
                            workflow._event(run, item, task['action'], 'mismatch', {'reason': str(exc), 'reviewer': workflow.actor})
                            workflow._save(run)
                            raise ValueError('Evidence mismatch routed to Exceptions: ' + str(exc)) from None
                        adapter.submit(task_id, response, workflow.actor, payload.get('reason', ''), payload.get('attachments'))
                        result = workflow.advance(task['run_id'])
                elif endpoint == 'fetch-wip':
                    from veritas.invoice_gateway import configured_adapter
                    actor = str(payload.get('actor', '')).strip()
                    if not actor:
                        raise ValueError('Actor is required')
                    adapter = configured_adapter()
                    countries = CountryPolicies(path=policy_path if policy_path.exists() else None)
                    country, month = countries.canonical(payload.get('country')), payload.get('month')
                    from datetime import date
                    import uuid
                    date.fromisoformat(str(month) + '-01')
                    response = adapter.execute('fetch_wip', {'wip': {'country': country, 'start_date': str(month) + '-01'}, 'data': {}, 'policy': countries.get(country)},
                                               uuid.uuid4().hex + uuid.uuid4().hex)
                    if not isinstance(response.get('rows'), list) or len(response['rows']) > 100000:
                        raise ValueError('Gateway did not return a bounded WIP report')
                    root = output / 'invoicing' / 'live'
                    with exclusive(root):
                        workflow = InvoiceWorkflow(root, adapter, policy_path=policy_path if policy_path.exists() else None)
                        workflow.actor = actor
                        result = workflow.create(response['rows'], month, country)
                else:
                    raise ValueError('Unknown application operation')
        handler.reply(200, result)
    except (ValueError, KeyError, TypeError, RuntimeError, FileNotFoundError, StopIteration) as exc:
        handler.reply(400, {'error': str(exc) or 'Invalid application request'})
    return True
