// Persist request identities, never genomic parameters, filenames or credentials.
function canonical(value){
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
 return JSON.stringify(value);
}
export async function submissionFingerprint(graph){
 const scientific={nodes:graph.nodes.map(n=>Object.fromEntries(Object.entries(n).filter(([k])=>!['x','y','selected'].includes(k)))),edges:graph.edges};
 if(Object.hasOwn(graph,'maximum_output_bytes'))scientific.maximum_output_bytes=graph.maximum_output_bytes;
 const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(canonical(scientific)));
 return [...new Uint8Array(bytes)].map(n=>n.toString(16).padStart(2,'0')).join('');
}
export function createRunSubmissions({owner,api,storage=localStorage,locks=globalThis.navigator?.locks,randomId=()=>crypto.randomUUID().replaceAll('-','')}){
 if(!owner)throw new Error('Sign in before submitting a workflow.');
 const active=new Map(),prefix='openngs.run-submission.v1.'+owner+'.';
 const lock=async fn=>{
  // Return from the critical section even when browser storage fails.
  const invoke=()=>{try{return {ok:true,value:fn()};}catch(error){return {ok:false,error};}};
  const result=await (locks?locks.request(prefix,{mode:'exclusive'},invoke):invoke());
  if(!result.ok)throw result.error;
  return result.value;
 };
 function stored(fingerprint){
   const prior=storage.getItem(prefix+fingerprint);
   if(prior){
    const record=JSON.parse(prior);
    if(record.owner!==owner||record.fingerprint!==fingerprint||!/^op_[a-zA-Z0-9_-]{8,96}$/.test(record.operation))throw new Error('The saved submission identity needs review. Open execution history.');
    return record;
   }
   return null;
 }
 async function claim(fingerprint){
  return lock(()=>{
   const prior=stored(fingerprint);if(prior)return prior;
   const record={owner,fingerprint,operation:'op_'+randomId(),created:Date.now()};
   storage.setItem(prefix+fingerprint,JSON.stringify(record));
   return record;
  });
 }
 async function accept(answer,record){
  const run=answer?.run;
  if(!run?.id||run.user_id!==owner||run.operation_id!==record.operation||await submissionFingerprint(run.graph)!==record.fingerprint)throw new Error('The returned workflow does not match the saved submission. Open execution history.');
  // A failed cleanup retains the same identity: it can replay accepted work,
  // but cannot turn an uncertain response into a second paid execution.
  await lock(()=>{try{const key=prefix+record.fingerprint,current=JSON.parse(storage.getItem(key)||'null');if(current?.operation===record.operation)storage.removeItem(key);}catch{}});
  return answer;
 }
 async function perform(snapshot,fingerprint){
  const record=await claim(fingerprint);
  try{return await accept(await api('/runs',{method:'POST',headers:{'Idempotency-Key':record.operation},body:{graph:snapshot}}),record);}
  catch(original){
   try{return await accept(await api('/run-submissions/'+encodeURIComponent(record.operation)),record);}
   catch(lookupError){
    if(lookupError.status===404&&original.status>=400&&original.status<500)throw original;
    const error=new Error('Submission could not be confirmed. Retry this unchanged workflow to reconnect, or check execution history.');
    error.cause=original;error.pendingSubmission=true;throw error;
   }
  }
 }
 return {async recover(graph){
  const fingerprint=await submissionFingerprint(JSON.parse(JSON.stringify(graph))),record=await lock(()=>stored(fingerprint));
  if(!record)return null;
  try{return await accept(await api('/run-submissions/'+encodeURIComponent(record.operation)),record);}
  catch(error){if(error.status===404)return null;throw error;}
 },async submit(graph){
  const snapshot=JSON.parse(JSON.stringify(graph)),fingerprint=await submissionFingerprint(snapshot);
  if(active.has(fingerprint))return active.get(fingerprint);
  const pending=perform(snapshot,fingerprint);active.set(fingerprint,pending);
  try{return await pending;}finally{if(active.get(fingerprint)===pending)active.delete(fingerprint);}
 }};
}
