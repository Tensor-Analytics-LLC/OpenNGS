import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

globalThis.document={querySelector:()=>({})};
const source=await readFile(new URL('../frontend/operations.js',import.meta.url),'utf8');
const {mergeBindingChoices}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

test('Primary and companion selections survive disjoint search and pagination results',()=>{
 const primary={id:'primary',name:'reads.fastq',state:'Ready'};
 const mate={id:'mate',name:'reads_2.fastq',state:'Ready'};
 const index={id:'index',name:'reference.fai',state:'Ready'};
 let choices=mergeBindingChoices([primary,mate],[]);
 choices=mergeBindingChoices([index],[choices.find(f=>f.id==='primary'),choices.find(f=>f.id==='mate')]);
 assert.deepEqual(choices.map(f=>f.id),['index','primary','mate']);
 choices=mergeBindingChoices([],[choices.find(f=>f.id==='primary'),choices.find(f=>f.id==='index')]);
 assert.deepEqual(choices.map(f=>f.id),['primary','index']);
});

test('Selectors exclude unverified data and do not duplicate selected files on matching pages',()=>{
 const selected={id:'selected',state:'Ready'};
 assert.deepEqual(mergeBindingChoices([selected,{id:'pending',state:'Verifying'}],[selected,undefined]),[selected]);
});
