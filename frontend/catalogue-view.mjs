import {adapterPath} from './graph-core.mjs';
import {isPrimaryAsset} from './asset-presentation.mjs';
const STATES=new Set(['Ready','Provisioning','Requires entitlement','Unavailable']);
const DATABASE_FORMATS=new Set(['DATABASE','INDEXED_VCF','VCF','BGZIP_VCF','TSV','CSV','ARCHIVE']);

export function acceptsAsset(node,asset){
 if(!isPrimaryAsset(asset))return false;
 if(node.id==='reference.asset'||node.group==='References')return asset.format==='FASTA';
 if(node.id==='database.asset')return DATABASE_FORMATS.has(asset.format);
 if(node.asset_family){
  if(asset.family!==node.asset_family)return false;
  if(node.outputs?.[0]?.type==='VCF')return ['VCF','BGZIP_VCF','INDEXED_VCF'].includes(asset.format);
  return asset.format!=='INDEX';
 }
 return true;
}

export function nodeAvailability(node){
 const value=node.availability||{};
 const state=node.quarantined?'Unavailable':STATES.has(value.state)?value.state:'Unavailable';
 return {state,reason:node.quarantined?'Quarantined: '+node.quarantined:value.reason||node.support_note||'Execution readiness has not been verified for this deployment.',
  tier:value.tier||'Allowance checks apply',validation:value.validation||'Representative scientific validation has not been established.',
  blockers:Array.isArray(value.blockers)?value.blockers:state==='Ready'?[]:[{kind:'runtime',code:'READINESS',message:value.reason||'Readiness not verified.'}]};
}

export const blockerLabels={licence:'Licence review',credential:'Vault key',allowance:'Compute allowance',dependency:'Data dependency',runtime:'Runtime',validation:'Validation',security:'Quarantined'};
export function blockerSummary(node){const info=nodeAvailability(node);return info.blockers.length?[...new Set(info.blockers.map(b=>blockerLabels[b.kind]||b.kind))].join(' · '):'Ready';}

export function catalogueCategories(nodes){
 return ['All',...[...new Set(nodes.map(n=>n.group))].sort((a,b)=>a.localeCompare(b))];
}

export function filterCatalogue(nodes,{library='all',category='All',term='',status='',blocker='',license='',tier=''}={}){
 const query=term.trim().toLowerCase();
 return nodes.filter(node=>{
  if(library==='references'&&node.group!=='References')return false;
  if(library==='databases'&&node.group!=='Databases')return false;
  if(library==='all'&&category!=='All'&&node.group!==category)return false;
  const availability=nodeAvailability(node);
  if(status&&availability.state!==status)return false;
  if(blocker&&!availability.blockers.some(b=>b.kind===blocker))return false;
  if(license&&node.license!==license)return false;
  if(tier&&availability.tier!==tier)return false;
  return !query||[node.name,node.id,node.group,node.description,node.asset_family,node.license,node.spdx,availability.state,availability.reason].join(' ').toLowerCase().includes(query);
 });
}

export function compatibleCandidates(spec,definitions,wire=null){
 const result=[],upstream=wire?.direction==='input',ports=wire?[wire.port]:spec.outputs;
 for(const other of definitions){
  if(other.id===spec.id)continue;
  for(const source of ports){
   for(const target of (upstream?other.outputs:other.inputs)){
    const adapters=adapterPath(upstream?target.type:source.type,upstream?source.type:target.type,definitions);
    if(adapters!==null){result.push({spec:other,port:target.key,sourcePort:source.key,adapters,upstream});break;}
   }
   if(result.at(-1)?.spec===other)break;
  }
 }
 return result.sort((a,b)=>a.adapters.length-b.adapters.length||a.spec.name.localeCompare(b.spec.name));
}
