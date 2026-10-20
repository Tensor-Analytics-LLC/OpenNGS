import test from 'node:test';
import assert from 'node:assert/strict';
import {awaitViewer,abortableDelay,preparationLabel} from '../frontend/viewer-poll.mjs';

test('preparation displays queue and worker progress and only resolves committed ready output',async()=>{
 const progress=[],states=['staging','running','building','ready'];
 const value=await awaitViewer({request_id:'v_1',state:'queued'},{delay:async()=>{},request:async(path)=>{assert.equal(path,'/viewer/v_1');return {request_id:'v_1',state:states.shift(),id:'asset'};},onProgress:r=>progress.push(r.state)});
 assert.deepEqual(progress,['queued','staging','running','building','ready']);
 assert.equal(value.id,'asset');
 assert.match(preparationLabel({state:'queued'}),/Waiting/);
});

test('failure is not mistaken for a successful inspection',async()=>{
 await assert.rejects(awaitViewer({request_id:'x',state:'failed',error:'Allowance exceeded'},{request:async()=>{assert.fail('must not retry failed preparation');}}),/Allowance exceeded/);
});

test('expired or missing cache stops polling and explains regeneration',async()=>{
 for(const state of ['expired','regenerate']){
  await assert.rejects(awaitViewer({request_id:'x',state,error:'Prepare again from the retained source'},{request:async()=>assert.fail('must not keep polling expired results')}),/Prepare again/);
 }
});

test('closing a view stops polling without a job cancellation request',async()=>{
 const controller=new AbortController();let calls=0;
 await assert.rejects(awaitViewer({request_id:'x',state:'queued'},{request:async()=>{calls++;},signal:controller.signal,delay:async()=>controller.abort()}),{name:'AbortError'});
 assert.equal(calls,0);
});

test('synchronous and cached responses return without polling; a pending delay is abortable',async()=>{
 const value={id:'asset',metadata:{header_inspected:true}};
 assert.equal(await awaitViewer(value,{request:async()=>assert.fail('unnecessary poll')}),value);
 const controller=new AbortController(),waiting=abortableDelay(5000,controller.signal);controller.abort();
 await assert.rejects(waiting,{name:'AbortError'});
});
