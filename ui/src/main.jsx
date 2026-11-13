import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
import Invoicing from './invoicing';
import OperationsPage,{SuiteNavigation} from './operations';

const labels = {audit_type:'Audit type',amount:'Proposal amount',man_days:'Man days',proposal_number:'Proposal number',client_email_id:'Client email'};
const keys = Object.keys(labels);
async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
function ProposalApp(){
  const [state,setState]=useState({results:[],busy:false,message:'Connecting to Veritas…'});
  const [selected,setSelected]=useState(0), [values,setValues]=useState({}), [error,setError]=useState(''), [saving,setSaving]=useState(false), [uploading,setUploading]=useState(false), [filter,setFilter]=useState(''), [dirty,setDirty]=useState(false);
  const input=useRef();
  const current=state.results[selected];
  const busy=state.busy||uploading;
  async function refresh(){try{const next=await request('/api/state');setState(next);}catch(e){setError(e.message);}}
  useEffect(()=>{refresh();const timer=setInterval(refresh,1800);return()=>clearInterval(timer);},[]);
  const snapshot=JSON.stringify(current);
  useEffect(()=>{setValues(Object.fromEntries(keys.map(k=>[k,current?.fields[k]?.value||''])));setDirty(false);},[selected,snapshot]);
  async function upload(file){
    if(!file||busy)return;
    if(dirty&&!window.confirm('Discard unsaved edits and process a new document?'))return;
    setError('');setUploading(true);
    try{await request('/api/upload?name='+encodeURIComponent(file.name),{method:'POST',body:file});setSelected(0);await refresh();}catch(e){setError(e.message);}finally{setUploading(false);if(input.current)input.current.value='';}
  }
  async function save(){setSaving(true);setError('');try{await request('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:selected,values})});setDirty(false);await refresh();}catch(e){setError(e.message);}finally{setSaving(false);}}
  const reviewCount=state.results.filter(r=>keys.some(k=>!r.fields[k]||r.fields[k].needs_review)).length;
  function select(index){if(!dirty||window.confirm('Discard unsaved edits?'))setSelected(index);}
  return <div className="app">
    <aside className="sidebar"><a className="brand" href="/" aria-label="Veritas home"><span className="mark">V</span>veritas<span className="dot">.</span></a><div className="workspace">AUDIT WORKSPACE</div><div className="nav active"><span>▤</span> Proposal review <small>{state.results.length}</small></div><div className="sidebar-bottom"><span className="status-dot"/> Local workspace<p>Documents stay on this computer.<br/>Cloud fallback is off.</p></div></aside>
    <main><header><div className="breadcrumb">Workspace <span>/</span> Proposal review</div><span className="local-badge"><span className="status-dot"/> Local engine</span></header>
    <section className="page"><div className="title-row"><div><div className="eyebrow">DOCUMENT INTELLIGENCE</div><h1>Clarity in every proposal.</h1><p>Extract the details. Check the evidence. Review with confidence.</p></div><button className="primary" disabled={busy} onClick={()=>input.current.click()}>＋ Add document</button></div>
    <input ref={input} type="file" accept=".pdf,.zip" hidden onChange={e=>upload(e.target.files[0])}/>
    <div className="stats"><div><span>Documents</span><strong>{String(state.results.length).padStart(2,'0')}</strong><small>In your workspace</small></div><div><span>Needs review</span><strong className="amber">{String(reviewCount).padStart(2,'0')}</strong><small>Check against the source</small></div><div><span>Reviewed</span><strong>{String(state.results.length-reviewCount).padStart(2,'0')}</strong><small>All five fields confirmed</small></div><div className="engine"><span>Extraction mode</span><strong>Local first <i>ON</i></strong><small>PDF text + OCR · no cloud requests</small></div></div>
    {error&&<div role="alert" className="error">{error}<button onClick={()=>setError('')}>Dismiss</button></div>}
    <div className="activity" role="status"><span className={'status-dot '+(busy?'pulse':'')}/>{uploading?'Uploading document…':state.message}{state.error&&<span className="failure"> · {state.error}</span>}</div>
    <div className="review-layout"><section className="documents card"><div className="panel-heading"><h2>Documents</h2><span>{state.results.length} total</span></div><label className="search"><span>⌕</span><input aria-label="Search documents" placeholder="Find a document…" value={filter} onChange={e=>setFilter(e.target.value)}/></label><div className="document-list">{state.results.map((r,i)=>({r,i})).filter(({r})=>r.filename.toLowerCase().includes(filter.toLowerCase())).map(({r,i})=><button key={i} className={'document '+(i===selected?'selected':'')} onClick={()=>select(i)}><span className="pdf-icon">PDF</span><span className="doc-info"><b>{r.filename}</b><small>{keys.filter(k=>r.fields[k]).length}/5 fields extracted</small><span className={'tag '+(keys.some(k=>!r.fields[k]||r.fields[k].needs_review)?'needs':'done')}>{keys.some(k=>!r.fields[k]||r.fields[k].needs_review)?'Needs review':'Reviewed'}</span></span><span className="chevron">›</span></button>)}{!state.results.length&&<div className="empty-list">Your documents will appear here.</div>}</div><button className="dropzone" disabled={busy} onClick={()=>input.current.click()} onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();upload(e.dataTransfer.files[0]);}}><span>↥</span><b>Drop a document here</b><small>or click to browse · PDF or ZIP · up to 100 MB</small></button></section>
    <section className="editor card">{current?<><div className="panel-heading"><div><div className="eyebrow">PROPOSAL DETAILS</div><h2 className="filename">{current.filename}</h2></div><a className="secondary" href={'/api/document?index='+selected} target="_blank" rel="noreferrer">Open original ↗</a></div><div className="review-note"><span>ⓘ</span><div><b>A quick human check makes the difference.</b><p>Confirm values against the original PDF before saving your review.</p></div></div><div className="fields">{keys.map(k=><div className="field" key={k}><label htmlFor={k}>{labels[k]}{(!current.fields[k]||current.fields[k].needs_review)&&<small>Check value</small>}</label><input id={k} value={values[k]||''} placeholder="Unresolved — enter a value" disabled={busy} onChange={e=>{setValues({...values,[k]:e.target.value});setDirty(true);}}/><details><summary>Source evidence {current.fields[k]?.page?'· page '+current.fields[k].page:''}</summary><p>{current.fields[k]?.evidence||'No source quote recorded. Confirm this field in the original document.'}</p>{!current.fields[k]&&(current.candidates?.[k]||[]).slice(0,6).map((c,i)=><p key={i}>Page {c.page}: {c.value}</p>)}</details></div>)}</div>{!!current.warnings?.length&&<details className="warnings"><summary>{current.warnings.length} extraction notes</summary>{current.warnings.map((w,i)=><p key={i}>{w}</p>)}</details>}<div className="editor-footer"><span>{dirty?'Unsaved changes':'Review all five fields before saving'}</span><button className="primary" disabled={busy||saving} onClick={save}>{saving?'Saving…':'✓ Save review'}</button></div></>:<div className="welcome"><div className="welcome-icon">▤</div><div className="eyebrow">FROM DOCUMENT TO DECISION</div><h2>Your next review starts here.</h2><p>Add an audit proposal to extract its key details<br/>and review each field alongside the source evidence.</p><button className="primary" disabled={busy} onClick={()=>input.current.click()}>＋ Add your first document</button><span>Five key fields. One clear view.</span></div>}</section></div>
    <footer><span>VERITAS <span className="footer-divider">/</span> Evidence behind every field.</span><div>{state.results.length>0&&<><a href="/api/export.json" download="results.json">Export JSON ↓</a><a href="/api/export.csv" download="results.csv">Export CSV ↓</a></>}</div></footer></section></main></div>;
}
function App(){
  const [page,setPage]=useState(window.location.hash);
  const [mode,setMode]=useState(()=>{const saved=localStorage.getItem('veritas.workspace');return ['demo','assisted','live'].includes(saved)?saved:'demo';});
  const [actor,setActor]=useState(()=>localStorage.getItem('veritas.operator')||'');
  useEffect(()=>{const change=()=>setPage(window.location.hash);window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);},[]);
  useEffect(()=>localStorage.setItem('veritas.workspace',mode),[mode]);
  useEffect(()=>localStorage.setItem('veritas.operator',actor),[actor]);
  const active=page.slice(1)||'invoicing';
  return <><SuiteNavigation mode={mode} setMode={setMode} actor={actor} setActor={setActor} page={active}/>{active==='proposals'?<><a className="invoice-return" href="#invoicing">← Invoicing workspace</a><ProposalApp/></>:['tasks','exceptions','reports','settings','evidence'].includes(active)?<OperationsPage key={active+mode} page={active} mode={mode} actor={actor}/>:<Invoicing key={mode} mode={mode} actor={actor}/>}</>;
}
createRoot(document.getElementById('root')).render(<App/>);
