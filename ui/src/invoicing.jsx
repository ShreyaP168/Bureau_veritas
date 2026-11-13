import React, {useEffect, useRef, useState} from 'react';
import './invoicing.css';

const api = '/api/invoicing';
async function request(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
const post = body => ({method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
const words = value => (value || '').replaceAll('_', ' ');
const money = (value, currency='') => value === undefined ? '—' : `${currency} ${Number(value).toLocaleString(undefined, {minimumFractionDigits:2,maximumFractionDigits:2})}`;
const stages = [
  ['WIP intake', ['contract']],
  ['Contract & charges', ['create_charges','send_charges','charge_status']],
  ['Flex order', ['flex']],
  ['Draft invoice', ['request_draft','draft_reports','download_draft','draft_review']],
  ['Final invoice', ['request_final','final_reports','download_final','final_review']],
  ['Delivery', ['send_email','completed']],
];

export default function Invoicing({mode='demo', actor=''}) {
  const [runs,setRuns] = useState([]), [selected,setSelected] = useState('');
  const [loading,setLoading] = useState(true), [busy,setBusy] = useState(false), [error,setError] = useState('');
  const [country,setCountry] = useState('All'), [search,setSearch] = useState('');
  const [countries,setCountries] = useState(['Dubai','South Africa']);
  const [importCountry,setImportCountry] = useState('Dubai');
  const [month,setMonth] = useState(() => {const now=new Date();return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;});
  const [reviewer,setReviewer] = useState(''), [role,setRole] = useState('reviewer'), [checked,setChecked] = useState(false);
  const input = useRef();
  const run = runs.find(r=>r.id === selected);
  const items = runs.flatMap(r=>r.items);
  const pending = run?.items.filter(i=>i.status==='needs_review') || [];
  const draftPending = pending.some(i=>i.step==='draft_review');
  const requiredRole=run?.policy?.draft_approval_role||(run?.country==='South Africa'?'country_coordinator':'reviewer');
  const coordinator = draftPending && requiredRole!=='reviewer';
  const signature = pending.map(i=>i.id+i.step).join(',');
  useEffect(()=>{setChecked(false);setRole('reviewer');},[selected,signature]);
  useEffect(()=>{request('/api/application/policies').then(p=>setCountries(Object.keys(p))).catch(e=>setError(e.message));},[]);
  async function refresh() {
    const next = await request(api+'?mode='+mode);
    setRuns(next);
    setSelected(previous=>next.some(r=>r.id===previous)?previous:(next.find(r=>r.status==='needs_review')||next[0])?.id||'');
  }
  useEffect(()=>{
    let stopped=false;
    async function start() {
      try {if(mode==='demo')await request(api+'/seed-demo?mode=demo',post({}));if(!stopped)await refresh();}
      catch(e){if(!stopped)setError(e.message);}
      finally {if(!stopped)setLoading(false);}
    }
    start();
    const timer=setInterval(()=>{if(!stopped)refresh().catch(e=>setError(e.message));},5000);
    return ()=>{stopped=true;clearInterval(timer);};
  },[]);
  async function act(action) {
    if(!run)return;
    setBusy(true);setError('');
    try {
      await request(`${api}/${run.id}/${action}?mode=${mode}`,post(action==='approve'?{reviewer,reviewer_role:role,actor:reviewer}:{actor}));
      setChecked(false);await refresh();
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function upload(file) {
    if(!file)return;
    setBusy(true);setError('');
    try {
      const query = new URLSearchParams({mode,month,country:importCountry,name:file.name,actor});
      const created = await request(api+'/import?'+query,{method:'POST',body:file});
      setSelected(created.id);setCountry('All');setSearch('');await refresh();
    }catch(e){setError(e.message);}finally{setBusy(false);if(input.current)input.current.value='';}
  }
  const visible=runs.filter(r=>(country==='All'||r.country===country)&&r.items.some(i=>`${i.wip.contract_number} ${r.country} ${i.wip.audit_type}`.toLowerCase().includes(search.toLowerCase())));
  const artifact=(item,stage)=>`${api}/${run.id}/artifact?${new URLSearchParams({mode,item:item.id,stage})}`;
  return <div className="app invoice-app">
    <aside className="sidebar">
      <a className="brand" href="#invoicing"><span className="mark">V</span>veritas<span className="dot">.</span></a>
      <div className="workspace">AUDIT WORKSPACE</div>
      <a className="nav active" href="#invoicing">Invoicing <small>{items.length}</small></a>
      <a className="nav" href="#proposals">Proposal review</a>
      <a className="nav" href="#tasks">Operator tasks</a><a className="nav" href="#exceptions">Exceptions</a><a className="nav" href="#reports">Reports</a><a className="nav" href="#settings">Configuration</a>
      <div className="sidebar-bottom"><span className="status-dot"/> {words(mode)} workspace<p>{mode==='demo'?'Synthetic contracts and invoices.':mode==='assisted'?'External actions need an operator.':'Gateway connection required.'}<br/>{mode==='demo'?'No portal actions or emails.':'Every confirmation is recorded.'}</p></div>
    </aside>
    <main><header><div className="breadcrumb">Workspace <span>/</span> Invoicing</div><span className="local-badge">{mode.toUpperCase()} MODE</span></header>
      <section className="page">
        <div className="title-row"><div><div className="eyebrow">DUBAI & SOUTH AFRICA</div><h1>From audit to invoice.</h1><p>Follow each contract, review the evidence, and move billing forward.</p></div><a className="secondary" href="#proposals">Review proposal PDFs</a></div>
        <div className="demo-banner"><b>{mode==='demo'?'Simulation workspace.':mode==='assisted'?'Assisted processing.':'Connected processing.'}</b> {mode==='demo'?'SmartCert, Flex and email are simulated. Uploaded WIP files also run in demo mode.':mode==='assisted'?'Import a real WIP report, upload signed evidence, and confirm external actions in Operator tasks. The app does not log in to portals or send email in this mode.':'SmartCert/Flex automation requires a configured, tested gateway. Unavailable connections block execution.'}</div>
        <div className="stats">
          <div><span>Audit items</span><strong>{items.length}</strong><small>Across {runs.length} runs</small></div>
          <div><span>Awaiting review</span><strong className="amber">{items.filter(i=>i.status==='needs_review').length}</strong><small>Check the draft or final invoice</small></div>
          <div><span>Completed</span><strong>{items.filter(i=>i.status==='completed').length}</strong><small>{mode==='demo'?'Simulated delivery only':'Confirmed through delivery'}</small></div>
          <div className="engine"><span>Live connections</span><strong>Not configured</strong><small>Dubai and South Africa demo available</small></div>
        </div>
        {error&&<div className="error" role="alert"><span>{error}</span><button onClick={()=>setError('')}>Dismiss</button></div>}
        <section className="import-bar card" aria-label="Import WIP">
          <div><b>Import WIP</b><small>Excel or CSV · {mode} processing</small></div>
          <label>Country<select value={importCountry} onChange={e=>setImportCountry(e.target.value)}>{countries.map(name=><option key={name}>{name}</option>)}</select></label>
          <label>Audit month<input type="month" value={month} onChange={e=>setMonth(e.target.value)}/></label>
          <button className="primary" disabled={busy||loading||!month||(mode!=='demo'&&!actor.trim())} onClick={()=>input.current.click()}>Choose WIP file</button>
          {mode==='live'&&<button className="secondary" disabled={busy||!actor.trim()} onClick={async()=>{setBusy(true);setError('');try{await request('/api/application/fetch-wip?mode=live',post({actor,country:importCountry,month}));await refresh();}catch(e){setError(e.message);}finally{setBusy(false);}}}>Pull from SmartCert</button>}
          <input hidden ref={input} type="file" accept=".csv,.xlsx" onChange={e=>upload(e.target.files[0])}/>
        </section>
        <div className="activity" role="status"><span className={'status-dot '+(busy||loading?'pulse':'')}/>{loading?'Loading workspace…':busy?'Processing workflow…':`${runs.length} saved ${mode} runs · changes are saved automatically`}{mode!=='demo'&&!actor.trim()&&' · Enter your operator name above to start.'}</div>
        <div className="invoice-layout">
          <section className="card invoice-runs">
            <div className="panel-heading"><h2>Invoice runs</h2><button className="secondary" disabled={busy} onClick={()=>refresh().catch(e=>setError(e.message))}>Refresh</button></div>
            <label className="search"><input aria-label="Find contract" placeholder="Find a contract or audit…" value={search} onChange={e=>setSearch(e.target.value)}/></label>
            <div className="country-tabs" aria-label="Filter by country">{['All',...countries].map(c=><button key={c} className={country===c?'selected':''} onClick={()=>setCountry(c)}>{c}</button>)}</div>
            <div className="document-list">{visible.map(r=><button className={'document '+(selected===r.id?'selected':'')} key={r.id} onClick={()=>setSelected(r.id)}>
              <span className="country-icon">{r.country==='Dubai'?'AE':'ZA'}</span>
              <span className="doc-info"><b>{r.items[0].wip.contract_number}</b><small>{r.country} · {r.month} · {r.items.length} item{r.items.length===1?'':'s'}</small><span className={'tag '+(r.status==='completed'?'done':'needs')}>{words(r.status)}</span></span>
            </button>)}{!visible.length&&<div className="empty-list">{loading?'Loading sample runs…':search||country!=='All'?'No matching runs. Try another filter.':'Import a WIP file to start a run.'}</div>}</div>
          </section>
          <section className="card invoice-detail">{run?<>
            <div className="panel-heading"><div><div className="eyebrow">{run.country.toUpperCase()} · {run.month}</div><h2 className="filename">{run.items[0].wip.contract_number}</h2></div><span className={'tag '+(run.status==='completed'?'done':'needs')}>{words(run.status)}</span></div>
            {run.items.map(item=>{
              const c=item.data.contract, invoice=item.data.download_final||item.data.download_draft;
              const currentStage=stages.findIndex(([,steps])=>steps.includes(item.step));
              return <div className="invoice-item" key={item.id}>
                <ol className="workflow-steps">{stages.map(([title],index)=><li key={title} className={item.step==='completed'||index<currentStage?'complete':index===currentStage?'current':''}><span>{item.step==='completed'||index<currentStage?'✓':index+1}</span>{title}</li>)}</ol>
                <dl className="invoice-facts">
                  <div><dt>Audit type</dt><dd>{item.wip.audit_type}</dd></div>
                  <div><dt>Audit start date</dt><dd>{item.wip.start_date}</dd></div>
                  <div><dt>WIP audit amount</dt><dd>{money(item.wip.amount,c?.currency||item.wip.currency)}</dd></div>
                  <div><dt>Current step</dt><dd>{words(item.step)}</dd></div>
                  {c&&<><div><dt>Customer</dt><dd>{c.customer_name}</dd></div><div><dt>Proposal number</dt><dd>{c.proposal_number}</dd></div><div><dt>Recipient</dt><dd>{c.client_email}</dd></div><div><dt>Billing address</dt><dd>{c.po_address||c.work_order_address}</dd></div><div><dt>Tax rate area</dt><dd>{c.tax_rate_area} · {Number(c.tax_rate)*100}%</dd></div><div><dt>PO / charge comments</dt><dd>{c.po_number||c.comments||'No PO'}</dd></div></>}
                </dl>
                {c?.charge_lines&&<div className="charge-table"><h3>Charge lines</h3><table><thead><tr><th>Charge</th><th>Man-days</th><th>Amount</th></tr></thead><tbody>{c.charge_lines.map(line=><tr key={line.line_item_id}><td>{words(line.kind)}</td><td>{line.man_days||'—'}</td><td>{money(line.amount,line.currency)}</td></tr>)}</tbody></table></div>}
                {invoice&&<div className="invoice-totals"><div><small>Net amount</small><b>{money(invoice.net_amount,invoice.currency)}</b></div><div><small>VAT</small><b>{money(invoice.tax_amount,invoice.currency)}</b></div><div><small>Total</small><b>{money(invoice.gross_amount,invoice.currency)}</b></div></div>}
                <div className="artifact-links">{[['contract','Open signed contract'],['download_draft','Open draft invoice'],['download_final','Open final invoice']].filter(([stage])=>item.data[stage]).map(([stage,title])=><a className="secondary" key={stage} href={artifact(item,stage)} target="_blank" rel="noreferrer">{title}{mode==='demo'?' sample':''} ↗</a>)}</div>
                {item.error&&<div role="alert" className="error">{item.error}</div>}
                {!!item.approvals.length&&<details className="invoice-history"><summary>{item.approvals.length} recorded approval{item.approvals.length===1?'':'s'}</summary>{item.approvals.map((a,i)=><p key={i}>{words(a.stage)} · {a.reviewer} · {words(a.role)} · {new Date(a.at).toLocaleString()}</p>)}</details>}
              </div>;
            })}
            {pending.length>0?<div className="approval-panel">
              <h3>{coordinator?`${words(requiredRole)} review`:draftPending?'Draft invoice review':'Final invoice review'}</h3>
              <p>{draftPending?'Open the draft and check the customer, address, audit dates, charge lines and totals. Approval creates the final invoice sample.':'Open the final invoice and verify the recipient and totals. Approval simulates customer delivery.'}</p>
              <div className="reviewer-fields"><label>Reviewer name<input value={reviewer} onChange={e=>setReviewer(e.target.value)} placeholder="Enter your name"/></label>{coordinator&&<label>Review role<select value={role} onChange={e=>setRole(e.target.value)}><option value="reviewer">Select required role</option><option value={requiredRole}>{words(requiredRole)}</option></select></label>}</div>
              <label className="review-check"><input type="checkbox" checked={checked} onChange={e=>setChecked(e.target.checked)}/>I checked all {pending.length} pending invoice sample{pending.length===1?'':'s'} and their supporting details.</label>
              <button className="primary" disabled={busy||!reviewer.trim()||!checked||(coordinator&&role!==requiredRole)} onClick={()=>act('approve')}>{draftPending?'Approve draft & continue':mode==='demo'?'Approve final & simulate delivery':'Approve final & continue to delivery'}</button>
            </div>:<div className="editor-footer"><span>{run.status==='completed'?(mode==='demo'?'Demo complete. No email was sent.':'All recorded stages are complete.'):run.status==='waiting_manual'?'An operator needs to complete the next external action.':run.status==='reconciliation_required'?'External outcome needs reconciliation before continuing.':'Advance to the next review or check.'}</span>{run.status==='waiting_manual'?<a className="primary" href="#tasks">Open operator tasks</a>:!['completed','reconciliation_required'].includes(run.status)&&<button className="primary" disabled={busy||(mode!=='demo'&&!actor.trim())} onClick={()=>act('advance')}>{run.status==='blocked'?'Retry current step':'Advance workflow'}</button>}</div>}
            <details className="invoice-history"><summary>Activity history · {run.events.length} events</summary><ol>{run.events.slice().reverse().map((e,i)=><li key={i}><b>{words(e.action)}</b> · {words(e.status)}<small>{e.agent} · {new Date(e.at).toLocaleString()}</small></li>)}</ol></details>
          </>:<div className="welcome"><div className="eyebrow">INVOICING WORKSPACE</div><h2>{loading?'Loading your sample data…':'Select an invoice run.'}</h2><p>Dubai and South Africa samples appear in the run list.</p></div>}</section>
        </div>
        <footer><span>VERITAS / {words(mode)} workspace{mode==='demo'?' · synthetic data':''}</span><a href="#proposals">Go to proposal review →</a></footer>
      </section>
    </main>
  </div>;
}
