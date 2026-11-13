import json
import os
import queue
import threading
from datetime import datetime, timezone
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from veritas.__main__ import export_csv, run_batch
from veritas.engine import FIELDS
from veritas.uc.storage import write_json


def launch():
    from veritas.provider import load_uc_settings, default_model, configured, azure_configured, provider_name
    load_uc_settings()
    root = tk.Tk()
    root.title('Veritas | Audit proposal extraction')
    root.geometry('1250x950')
    root.minsize(1000, 750)
    root.configure(bg='#edf2f6')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TButton', padding=8)
    style.configure('Treeview', rowheight=30)
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Veritas · Proposal review', font=('Segoe UI', 23, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Local PDF text → Tesseract OCR → optional Azure fallback  /  source evidence and review').pack(anchor='w', pady=(5, 15))
    controls = ttk.Frame(frame)
    controls.pack(fill='x')
    source = tk.StringVar()
    ttk.Entry(controls, textvariable=source).pack(side='left', fill='x', expand=True)
    ttk.Button(controls, text='Choose PDF / ZIP', command=lambda: source.set(filedialog.askopenfilename(filetypes=[('Documents', '*.pdf *.zip')]) or source.get())).pack(side='left', padx=5)
    ttk.Button(controls, text='Choose folder', command=lambda: source.set(filedialog.askdirectory() or source.get())).pack(side='left')
    settings = ttk.Frame(frame)
    settings.pack(fill='x', pady=12)
    enabled = tk.BooleanVar(value=False)
    ttk.Checkbutton(settings, text='Use model fallback', variable=enabled).pack(side='left')
    provider_choice = tk.StringVar(value=provider_name())
    ttk.Label(settings, text='Provider:').pack(side='left', padx=(15, 5))
    provider_box = ttk.Combobox(settings, textvariable=provider_choice, values=('azure', 'openai'), state='readonly', width=12)
    provider_box.pack(side='left')
    model = tk.StringVar(value=default_model())
    ttk.Label(settings, text='Model:').pack(side='left', padx=(15, 5))
    ttk.Entry(settings, textvariable=model, width=20).pack(side='left')
    def change_provider(_=None):
        os.environ['VERITAS_PROVIDER'] = provider_choice.get()
        load_uc_settings()
        model.set(default_model())
        enabled.set(False)
    provider_box.bind('<<ComboboxSelected>>', change_provider)
    status = tk.StringVar(value='Local OCR and cache enabled. Cloud fallback is off until you enable it.')
    ttk.Label(frame, textvariable=status, wraplength=1050).pack(anchor='w', pady=5)
    tree = ttk.Treeview(frame, columns=('file', 'resolved', 'review'), show='headings', height=5)
    for key, title, width in [('file', 'Document', 650), ('resolved', 'Filled fields', 100), ('review', 'Review', 220)]:
        tree.heading(key, text=title); tree.column(key, width=width)
    tree.pack(fill='x', pady=10)
    editor = ttk.Frame(frame)
    editor.pack(fill='x')
    entries = {}
    for name in FIELDS:
        row = ttk.Frame(editor); row.pack(fill='x', pady=3)
        ttk.Label(row, text=name.replace('_', ' ').title(), width=20).pack(side='left')
        value = tk.Text(row, height=2, wrap='word', font=('Segoe UI', 10), relief='solid', borderwidth=1)
        entries[name] = value
        value.pack(side='left', fill='x', expand=True)
    ttk.Label(frame, text='Evidence and alternatives · verify amounts, currency, audit stage and OCR against the original PDF').pack(anchor='w', pady=(12, 4))
    evidence = tk.Text(frame, height=10, wrap='word', font=('Consolas', 10))
    evidence.pack(fill='both', expand=True)
    scrollbar = ttk.Scrollbar(evidence, command=evidence.yview)
    scrollbar.pack(side='right', fill='y'); evidence.configure(yscrollcommand=scrollbar.set)
    events = queue.Queue()
    results = []
    active_results = Path('outputs/results.json')

    def selected():
        selection = tree.selection()
        return int(selection[0]) if selection else None

    def show(_=None):
        i = selected()
        if i is None: return
        result = results[i]
        for key in FIELDS:
            entries[key].delete('1.0', 'end')
            entries[key].insert('1.0', result['fields'][key]['value'] if result['fields'][key] else '')
        evidence.delete('1.0', 'end')
        lines = ['REVIEW NOTES', *['• '+w for w in result.get('warnings', [])], '', 'SOURCE EVIDENCE']
        usage = result.get('usage') or {}
        lines[0:0] = [f"This run: {usage.get('total_tokens', 'not recorded')} tokens · {usage.get('provider', 'unknown')}", '']
        for key in FIELDS:
            value = result['fields'][key]
            lines.append('\n'+key.replace('_', ' ').title())
            if value:
                lines.append(f"Page {value.get('page', '—')} · {value.get('source', 'unknown')} · "+('Review required' if value.get('needs_review') else 'Reviewed / local text'))
                lines.append(value.get('evidence', 'No source quote recorded.'))
            else:
                lines.append('Unresolved. Alternatives:')
                for candidate in result.get('candidates', {}).get(key, [])[:12]:
                    lines.append(f"  Page {candidate.get('page')}: {candidate.get('value')}")
        evidence.insert('end', '\n'.join(lines))

    tree.bind('<<TreeviewSelect>>', show)

    def save():
        i = selected()
        if i is None: return
        for key, variable in entries.items():
            new = variable.get('1.0', 'end').strip()
            old = results[i]['fields'][key]
            if new != (old['value'] if old else ''):
                results[i].setdefault('review_history', []).append({'field': key, 'previous': old, 'value': new,
                    'reviewed_at': datetime.now(timezone.utc).isoformat()})
                results[i]['fields'][key] = {'value': new, 'source': 'manual', 'needs_review': False} if new else None
            elif old:
                old['needs_review'] = False
                old['reviewed_at'] = datetime.now(timezone.utc).isoformat()
        write_json(active_results, results)
        export_csv(results, active_results.with_suffix('.csv'))
        # Human corrections become the reusable result for these exact bytes.
        data = results[i]
        if all(data['fields'].values()) and data.get('source_sha256') and data.get('source_path'):
            from veritas.uc.hashing import sha256_file
            from veritas.economy import VERSION
            from dataclasses import fields as dataclass_fields
            from veritas.engine import Result
            path = Path(data['source_path'])
            if path.exists() and sha256_file(str(path)) == data['source_sha256']:
                payload = {f.name: data[f.name] for f in dataclass_fields(Result) if f.name in data}
                write_json(Path('outputs/extraction-cache')/(data['source_sha256']+'-'+VERSION+'.json'),
                           {'source_sha256': data['source_sha256'], 'usage': data.get('usage'), 'result': payload})
        tree.item(str(i), values=(results[i]['filename'], f"{sum(bool(v) for v in results[i]['fields'].values())}/5", 'Edited by reviewer'))
        status.set(f'Review saved to {active_results} and its CSV export')
        show()

    def original():
        i = selected()
        if i is not None and results[i].get('source_path'):
            os.startfile(results[i]['source_path'])

    def populate(data):
        results[:] = data
        for item in tree.get_children(): tree.delete(item)
        for i, result in enumerate(results):
            filled = sum(bool(v) for v in result['fields'].values())
            tree.insert('', 'end', iid=str(i), values=(result['filename'], f'{filled}/5', 'Review required'))
        if results: tree.selection_set('0'); show()

    def load():
        nonlocal active_results
        path = filedialog.askopenfilename(initialdir='outputs', filetypes=[('Extraction results', '*.json')])
        if not path: return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(data, list) or any(not isinstance(r, dict) or not all(k in r.get('fields', {}) for k in FIELDS) for r in data):
                raise ValueError('Choose an extraction results JSON file.')
            populate(data)
            active_results = Path(path)
            status.set(f'Loaded {len(data)} documents from {active_results}')
        except (ValueError, OSError) as exc: messagebox.showerror('Load results', str(exc))

    def start():
        nonlocal active_results
        if not Path(source.get()).exists() or not source.get():
            messagebox.showerror('Choose documents', 'Select an existing PDF, ZIP, or folder.'); return
        if enabled.get() and (not configured() or not (model.get() or default_model())):
            messagebox.showerror('Model settings', 'Configure credentials and a model for the selected provider. See README.txt.'); return
        go.configure(state='disabled')
        provider_box.configure(state='disabled')
        active_results = Path('outputs/results.json')
        review.configure(state='disabled')
        path, allow, chosen = source.get(), enabled.get(), model.get() or None
        def work():
            try: events.put(('done', run_batch(path, 'outputs', allow, chosen, lambda s: events.put(('progress', s)))))
            except Exception as exc: events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                kind, data = events.get_nowait()
                if kind == 'progress': status.set(data)
                else:
                    go.configure(state='normal'); review.configure(state='normal')
                    provider_box.configure(state='readonly')
                    if kind == 'error': status.set(data)
                    else:
                        populate(data)
                        status.set('Extraction complete. Review evidence, correct fields, then save. Results are in outputs/.')
        except queue.Empty: pass
        root.after(150, poll)

    actions = ttk.Frame(frame); actions.pack(fill='x', pady=(12, 0))
    go = ttk.Button(actions, text='Extract documents', command=start); go.pack(side='left')
    review = ttk.Button(actions, text='Save reviewed values', command=save); review.pack(side='left', padx=10)
    ttk.Button(actions, text='Open original PDF', command=original).pack(side='left')
    ttk.Button(actions, text='Load results', command=load).pack(side='left', padx=10)
    ttk.Button(actions, text='Open results folder', command=lambda: (Path('outputs').mkdir(exist_ok=True), os.startfile(str(Path('outputs').resolve())))).pack(side='right')
    saved = [p for p in Path('outputs').glob('**/results.json') if 'test-run' not in str(p)]
    if saved:
        active_results = max(saved, key=lambda p: p.stat().st_mtime)
        try:
            populate(json.loads(active_results.read_text(encoding='utf-8')))
            status.set(f'{len(results)} documents loaded from {active_results}. Select a document to review all five fields.')
        except (ValueError, KeyError, TypeError):
            status.set('Saved results could not be loaded. Use Load results or extract documents.')
    root.after(150, poll)
    root.mainloop()
