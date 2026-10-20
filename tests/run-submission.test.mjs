import test from 'node:test';
import assert from 'node:assert/strict';
import {createRunSubmissions,submissionFingerprint} from '../frontend/run-submission.mjs';

const graph=()=>({name:'Private sample label',nodes:[{id:'reads',tool:'sample.fastq',x:10,y:20,params:{patient:'not-for-storage'}}],edges:[],maximum_output_bytes:1024});
function storage(){const values=new Map();return {getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k),values};}
const missing=()=>Object.assign(new Error('Submission not found'),{status:404});
const accepted=(owner,operation,graph)=>({run:{id:'run_real',user_id:owner,operation_id:operation,graph},created:true});

test('Lost HTTP response recovers the committed run without a second paid submission',async()=>{
 const store=storage();let committed,posts=0;
 const api=async(path,request)=>{
  if(request){posts++;committed=accepted('owner',request.headers['Idempotency-Key'],request.body.graph);throw new TypeError('Connection lost after commit');}
  assert.equal(path,'/run-submissions/'+committed.run.operation_id);return {...committed,created:false};
 };
 const result=await createRunSubmissions({owner:'owner',storage:store,api,randomId:()=> 'identity123'}).submit(graph());
 assert.equal(result.run.id,'run_real');assert.equal(posts,1);assert.equal(store.values.size,0);
});

test('Unknown outcome survives controller replacement and preserves the identical request key',async()=>{
 const store=storage(),keys=[];let online=false;
 const api=async(path,request)=>{
  if(!request)throw missing();
  keys.push(request.headers['Idempotency-Key']);
  if(!online)throw new TypeError('Network unavailable');
  return accepted('owner',keys.at(-1),request.body.graph);
 };
 await assert.rejects(createRunSubmissions({owner:'owner',storage:store,api,randomId:()=> 'original123'}).submit(graph()),e=>e.pendingSubmission);
 assert.equal(store.values.size,1);assert.ok(![...store.values.values()][0].includes('not-for-storage'));assert.ok(![...store.values.values()][0].includes('Private sample'));
 online=true;const moved=graph();moved.name='Renamed';moved.nodes[0].x=800;
 await createRunSubmissions({owner:'owner',storage:store,api,randomId:()=> 'mustNotUseThis'}).submit(moved);
 assert.deepEqual(keys,['op_original123','op_original123']);assert.equal(store.values.size,0);
});

test('Scientific edits and output allowances get distinct identities; layout changes do not',async()=>{
 const original=graph(),moved=graph();moved.nodes[0].selected=true;moved.nodes[0].y=55;
 assert.equal(await submissionFingerprint(original),await submissionFingerprint(moved));
 moved.nodes[0].params.depth=30;assert.notEqual(await submissionFingerprint(original),await submissionFingerprint(moved));
 const smaller=graph();smaller.maximum_output_bytes=512;assert.notEqual(await submissionFingerprint(original),await submissionFingerprint(smaller));
});

test('An accepted request can reconnect before a new quota estimate or submission',async()=>{
 const store=storage();let operation,calls=[];
 const offline=createRunSubmissions({owner:'owner',storage:store,randomId:()=> 'identity123',api:async(path,request)=>{if(request)operation=request.headers['Idempotency-Key'];throw new TypeError('Disconnected');}});
 await assert.rejects(offline.submit(graph()));
 const fresh=createRunSubmissions({owner:'owner',storage:store,api:async(path,request)=>{calls.push(path);assert.equal(request,undefined);return accepted('owner',operation,graph());}});
 assert.equal((await fresh.recover(graph())).run.id,'run_real');
 assert.deepEqual(calls,['/run-submissions/op_identity123']);assert.equal(store.values.size,0);
 assert.equal(await fresh.recover(graph()),null);assert.equal(calls.length,1);
});

test('Failed confirmation cannot adopt another tenant or a different scientific graph',async()=>{
 for(const invalid of ['owner','graph']){
  const store=storage(),api=async(path,request)=>{
   if(!request)throw missing();
   const response=accepted('owner',request.headers['Idempotency-Key'],request.body.graph);
   if(invalid==='owner')response.run.user_id='foreign';else response.run.graph.maximum_output_bytes=42;
   return response;
  };
  await assert.rejects(createRunSubmissions({owner:'owner',storage:store,api,randomId:()=> 'identity123'}).submit(graph()),e=>e.pendingSubmission);
  assert.equal(store.values.size,1);
 }
});

test('A quota rejection retains its useful reason and storage failure prevents submission',async()=>{
 let posts=0;const store=storage(),rejected=Object.assign(new Error('Monthly allowance exhausted'),{status:403});
 await assert.rejects(createRunSubmissions({owner:'owner',storage:store,randomId:()=> 'identity123',api:async(path,request)=>{if(!request)throw missing();posts++;throw rejected;}}).submit(graph()),e=>e===rejected);
 assert.equal(posts,1);
 await assert.rejects(createRunSubmissions({owner:'owner',storage:{...storage(),setItem(){throw new Error('Storage disabled');}},api:async()=>{posts++;}}).submit(graph()),/Storage disabled/);
 assert.equal(posts,1);
});

test('Concurrent same-page calls share one immutable scientific snapshot',async()=>{
 const store=storage(),source=graph();let posts=0,release;
 const gate=new Promise(resolve=>{release=resolve;});
 const controller=createRunSubmissions({owner:'owner',storage:store,randomId:()=> 'identity123',api:async(path,request)=>{posts++;await gate;return accepted('owner',request.headers['Idempotency-Key'],request.body.graph);}});
 const first=controller.submit(source),second=controller.submit(source);source.nodes[0].params.patient='edited-after-click';release();
 const [a,b]=await Promise.all([first,second]);assert.equal(posts,1);assert.equal(a.run.id,b.run.id);assert.equal(a.run.graph.nodes[0].params.patient,'not-for-storage');
});
