import test from 'node:test';
import assert from 'node:assert/strict';
import {setImmediate as turn} from 'node:timers/promises';
import {installExtensions} from '../frontend/extensions.js';

// Minimal DOM surface: exercise the real workbench event handlers and polling,
// without replacing their lifecycle with a copy of the implementation.
function fixture(t,respond){
 const elements=new Map(),controls=[];
 class Element{
  constructor(id=''){this.id=id;this.hidden=false;this.disabled=false;this.style={};this.dataset={};this.classList={toggle(){}};}
  set innerHTML(value){this.html=value;for(const match of value.matchAll(/id="([^"]+)"/g))if(!elements.has(match[1]))elements.set(match[1],new Element(match[1]));}
  get innerHTML(){return this.html||'';}
  setAttribute(name,value){this[name]=value;}
  querySelectorAll(selector){if(selector==='[data-variant]')return [];return controls.filter(el=>selector.split(',').some(s=>s.startsWith('#')?el.id===s.slice(1):Object.hasOwn(el.dataset,s.replace(/^\[data-|\]$/g,''))));}
  showModal(){this.open=true;}
  close(){this.open=false;}
 }
 for(const key of ['filter','export','tab']){const el=new Element();el.dataset[key]=key==='tab'?'variants':'test';controls.push(el);}
 for(const id of ['includeMissing','clearFilters']){const el=new Element(id);elements.set(id,el);controls.push(el);}
 for(const id of ['resultsButton','publishButton'])elements.set(id,new Element(id));
 const original=globalThis.document;
 globalThis.document={createElement:()=>new Element(),body:{append(){}},querySelector:selector=>elements.get(selector.slice(1))};
 t.after(()=>{globalThis.document=original;});
 const requests=[],errors=[];
 const viewer=installExtensions({api:async(path,options)=>{requests.push({path,options});return respond(path,options);},state:{user:{role:'admin'}},esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),toast:(value,error)=>{if(error)errors.push(value);},guarded:fn=>fn,closeModal(){}});
 t.after(()=>elements.get('closeWorkbench')?.onclick());
 return {viewer,elements,controls,requests,errors};
}

test('private preparation shows meaningful progress, disables incomplete index actions, and closing only stops polling',async t=>{
 const f=fixture(t,async path=>{assert.equal(path,'/results/private/index');return {request_id:'viewer_private',state:'running',rows:0,note:'The isolated worker is preparing authorized data.'};});
 const opened=f.viewer.open('private');await turn();
 assert.equal(f.elements.get('resultsStatus').textContent,'Preparing variants in an isolated worker');
 assert.match(f.elements.get('resultsPanel').innerHTML,/isolated worker is preparing authorized data/);
 assert.doesNotMatch(f.elements.get('resultsPanel').innerHTML,/0 alleles|\d+%/);
 assert.equal(f.elements.get('resultsPanel')['aria-busy'],'true');
 assert.ok(f.controls.every(el=>el.disabled));
 await f.controls.find(el=>Object.hasOwn(el.dataset,'tab')).onclick();
 assert.equal(f.requests.length,1,'unprepared tabs must not query results');
 f.elements.get('closeWorkbench').onclick();await opened;
 assert.equal(f.requests.length,1,'closing must not submit a cancellation or another preparation');
 assert.ok(f.requests[0].options.signal.aborted);
 assert.deepEqual(f.errors,[]);
});

test('local indexing polls without a remote request ID, then enables querying and exports only when ready',async t=>{
 const f=fixture(t,async path=>{
  if(path==='/results/local/index')return {state:'building',rows:0};
  if(path==='/results/local/status')return {state:'ready',rows:0};
  assert.equal(path,'/results/local/query');return {matched:0,total:0,rows:[]};
 });
 const opened=f.viewer.open('local');await turn();
 assert.equal(f.elements.get('resultsStatus').textContent,'Building the variant index');
 assert.ok(f.controls.every(el=>el.disabled));
 assert.deepEqual(f.requests.map(r=>r.path),['/results/local/index']);
 await opened;
 assert.deepEqual(f.requests.map(r=>r.path),['/results/local/index','/results/local/status','/results/local/query']);
 assert.ok(f.controls.every(el=>!el.disabled));
 assert.equal(f.elements.get('resultsPanel')['aria-busy'],'false');
 assert.equal(f.elements.get('matchedCount').textContent,'0 / 0 alleles');
 assert.deepEqual(f.errors,[]);
});

test('failed preparation explains the failure and exposes an explicit retry without querying missing results',async t=>{
 const f=fixture(t,async()=>({request_id:'viewer_failed',state:'failed',error:'Worker could not validate the source index'}));
 await f.viewer.open('failed');
 assert.equal(f.elements.get('retryResultsIndex').hidden,false);
 assert.match(f.elements.get('resultsPanel').innerHTML,/Worker could not validate the source index/);
 assert.ok(f.controls.every(el=>el.disabled));
 assert.equal(f.requests.length,1);
 assert.deepEqual(f.errors,['Worker could not validate the source index']);
});
