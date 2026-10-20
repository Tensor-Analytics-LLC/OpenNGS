import test from 'node:test';
import assert from 'node:assert/strict';
import {catalogueCategories,filterCatalogue,nodeAvailability,compatibleCandidates,acceptsAsset} from '../frontend/catalogue-view.mjs';

const tool=(id,group='Annotation')=>({id,name:id,group,description:'Scientific contract',license:'FOSS',inputs:[{key:'variants',type:'VCF'}],outputs:[],availability:{state:'Unavailable',reason:'Runtime not yet published'}});

test('Categories include every actual registry group and dedicated asset views remain searchable',()=>{
 const nodes=[tool('VEP'),tool('gnomAD','Databases'),tool('GRCh38','References'),tool('custom instrument','Future modality')];
 assert.deepEqual(catalogueCategories(nodes),['All','Annotation','Databases','Future modality','References']);
 assert.deepEqual(filterCatalogue(nodes,{library:'databases',term:'gnomad'}).map(n=>n.id),['gnomAD']);
 assert.equal(filterCatalogue(nodes,{library:'all',category:'Annotation'}).length,1);
 assert.equal(filterCatalogue(nodes,{term:'unavailable'}).length,4);
});

test('Missing verification never becomes a Ready badge',()=>{
 assert.equal(nodeAvailability({builtin:true,license:'FOSS'}).state,'Unavailable');
 assert.equal(nodeAvailability({...tool('A'),availability:{state:'Ready'},quarantined:'CVE review'}).state,'Unavailable');
});

test('Compatible suggestions include all 24 matching contracts, including assets and commercial nodes',()=>{
 const source={id:'source',name:'Source',outputs:[{key:'variants',type:'VCF'}],inputs:[]};
 const targets=Array.from({length:24},(_,i)=>tool('tool '+i,i===0?'Assets':i===1?'Commercial':'Annotation'));
 const matches=compatibleCandidates(source,[source,...targets]);
 assert.equal(matches.length,24);
 assert.ok(matches.some(x=>x.spec.group==='Commercial'));
 assert.ok(matches.some(x=>x.spec.group==='Assets'));
 assert.ok(matches.every(x=>x.adapters.length===0));
});

test('Database inspector exposes every supported database file format',()=>{
 for(const format of ['DATABASE','INDEXED_VCF','VCF','BGZIP_VCF','TSV','CSV','ARCHIVE'])
  assert.equal(acceptsAsset({id:'database.asset'},{format}),true,format);
 assert.equal(acceptsAsset({id:'database.asset'},{format:'INDEX'}),false);
 assert.equal(acceptsAsset({id:'database.asset'},{format:'VCF',name:'clinvar.vcf.gz.tbi'}),false);
 assert.equal(acceptsAsset({id:'database.asset'},{format:'FASTQ'}),false);
 assert.equal(acceptsAsset({id:'resource.clinvar',asset_family:'clinvar',outputs:[{type:'VCF'}]},{format:'BGZIP_VCF',family:'clinvar'}),true);
 assert.equal(acceptsAsset({id:'resource.clinvar',asset_family:'clinvar',outputs:[{type:'VCF'}]},{format:'VCF',family:'gnomad'}),false);
});
