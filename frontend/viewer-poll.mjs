/** Poll a durable preparation request; closing its view never cancels accepted work. */
export function abortableDelay(milliseconds,signal){
 return new Promise((resolve,reject)=>{
  if(signal?.aborted)return reject(signal.reason||new DOMException('View closed','AbortError'));
  const finish=()=>{signal?.removeEventListener('abort',abort);resolve();};
  const timer=setTimeout(finish,milliseconds);
  const abort=()=>{clearTimeout(timer);signal?.removeEventListener('abort',abort);reject(signal.reason||new DOMException('View closed','AbortError'));};
  signal?.addEventListener('abort',abort,{once:true});
 });
}

export async function awaitViewer(initial,{request,signal,onProgress=()=>{},delay=abortableDelay,interval=1800}){
 let value=initial;
 while(true){
  if(signal?.aborted)throw signal.reason||new DOMException('View closed','AbortError');
  if(['failed','cancelled','interrupted','expired','regenerate'].includes(value.state))throw new Error(value.error||'Results preparation did not complete.');
  onProgress(value);
  if(!value.request_id||value.state==='ready')return value;
  await delay(interval,signal);
  if(signal?.aborted)throw signal.reason||new DOMException('View closed','AbortError');
  value=await request('/viewer/'+encodeURIComponent(value.request_id),{signal});
 }
}

export function preparationLabel(value){
 return ({queued:'Waiting for an available worker',staging:'Staging authorized data',starting:'Starting the isolated worker',submitted:'Worker requested',running:'Inspecting the genomic data',succeeded:'Validating the prepared result',building:'Validating the prepared result',ingesting:'Validating the prepared result',ready:'Header inspection complete',failed:'Header inspection failed'})[value.state]||String(value.state||'Requesting header inspection');
}
