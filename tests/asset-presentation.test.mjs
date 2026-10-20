import test from 'node:test';
import assert from 'node:assert/strict';
import {assetPresentation,assetOptionLabel,isIndexAsset,compatibleCompanion} from '../frontend/asset-presentation.mjs';
import {acceptsAsset,blockerSummary,nodeAvailability} from '../frontend/catalogue-view.mjs';

test('A verified annotation release has a readable label without losing raw identity',()=>{
 const asset={id:'asset_abcdef',name:'dbNSFP5.3.1a_grch38.gz',family:'dbnsfp',format:'DATABASE',metadata:{sha256:'a'.repeat(64),version_id:'immutable-v1'}};
 const view=assetPresentation(asset);
 assert.equal(view.release,'5.3.1a');assert.equal(view.assembly,'GRCh38');
 assert.match(view.title,/dbNSFP · 5.3.1a · GRCh38/);
 assert.equal(view.raw_name,asset.name);assert.equal(view.version_id,'immutable-v1');
 assert.match(assetOptionLabel(asset),/\[abcdef\]$/);
});

test('Index companions cannot be selected as primary even if an old format label is wrong',()=>{
 for(const name of ['clinvar.vcf.gz.tbi','reference.fa.fai','GRCh38.dict','reads.bam.bai','genome.1.bt2']){
  const item={name,format:'DATABASE'};
  assert.equal(isIndexAsset(item),true,name);
  assert.equal(acceptsAsset({id:'database.asset'},item),false,name);
  assert.equal(compatibleCompanion(item,'primary'),false,name);
 }
 assert.equal(compatibleCompanion({name:'reads_2.fastq.gz',format:'FASTQ'},'mate_id'),true);
 assert.equal(compatibleCompanion({name:'reads.bam'},'mate_id'),false);
 assert.equal(compatibleCompanion({name:'reference.dict'},'dictionary_id'),true);
 assert.equal(compatibleCompanion({name:'reference.fa.fai'},'dictionary_id'),false);
 assert.equal(compatibleCompanion({name:'reference.fa.fai'},'index_id'),true);
});

test('Dataset API manifests are rendered consistently and declared metadata takes precedence',()=>{
 const asset={id:'ds_a',name:'sample.fa',manifest:JSON.stringify({format:'FASTA',assembly:'GRCh37',release:'2024.1'}),family:'reference'};
 assert.equal(assetPresentation(asset).assembly,'GRCh37');
 assert.equal(assetPresentation(asset).release,'2024.1');
 assert.equal(assetPresentation(asset).role,'Reference sequences');
 assert.equal(assetPresentation({...asset,metadata:{assembly:'GRCh38',format:'FASTA'}}).assembly,'GRCh38');
});

test('Licence, key, allowance and dependency blockers remain independent',()=>{
 const node={availability:{state:'Requires entitlement',blockers:[
  {kind:'licence',message:'Review rights'}, {kind:'credential',message:'Bind key'},
  {kind:'allowance',message:'Needs funding'}, {kind:'dependency',message:'Missing index'}]}};
 assert.equal(nodeAvailability(node).blockers.length,4);
 assert.equal(blockerSummary(node),'Licence review · Vault key · Compute allowance · Data dependency');
 assert.equal(blockerSummary({availability:{state:'Ready',blockers:[]}}),'Ready');
});
