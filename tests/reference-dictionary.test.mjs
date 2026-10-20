import test from 'node:test';
import assert from 'node:assert/strict';
import {adapterPath,analyze,compatible} from '../frontend/graph-core.mjs';

test('Sequence dictionaries cannot be suggested or wired as calling intervals',()=>{
 const definitions=[
  {id:'reference.dictionary',name:'Build reference dictionary',group:'Adapters',inputs:[],outputs:[{key:'dictionary',type:'REFERENCE_DICTIONARY'}]},
  {id:'intervals.bed',name:'IntervalList → BED',group:'Adapters',inputs:[{key:'intervals',type:'INTERVALLIST',required:true}],outputs:[{key:'intervals',type:'BED'}]}
 ];
 assert.equal(compatible('REFERENCE_DICTIONARY','INTERVALLIST'),false);
 assert.equal(compatible('REFERENCE_DICTIONARY','BED|INTERVALLIST'),false);
 assert.equal(adapterPath('REFERENCE_DICTIONARY','BED',definitions),null);
 const result=analyze({nodes:[{id:'dictionary',tool:'reference.dictionary'},{id:'converter',tool:'intervals.bed'}],edges:[{id:'bad',source:'dictionary',sourcePort:'dictionary',target:'converter',targetPort:'intervals'}]},definitions);
 const issue=result.issues.find(item=>item.code==='FORMAT_MISMATCH');
 assert.ok(issue);
 assert.equal(issue.edge_id,'bad');
 assert.equal(issue.adapters,null);
});
