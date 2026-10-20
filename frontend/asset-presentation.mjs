const FAMILIES={dbnsfp:'dbNSFP',alphamissense:'AlphaMissense',spliceai:'SpliceAI',cadd:'CADD',revel:'REVEL',exomiser:'Exomiser',hpo:'Human Phenotype Ontology',ensembl:'Ensembl',mane:'MANE',clingen:'ClinGen',vep:'Ensembl VEP',reference:'Reference genome',clinvar:'ClinVar',gnomad:'gnomAD',giab:'GIAB',omim:'OMIM',dbsnp:'dbSNP'};
function meta(asset){const raw=asset.metadata||asset.manifest||{};if(typeof raw!=='string')return raw;try{return JSON.parse(raw);}catch{return {};}}
export function assetFormat(asset){return asset.format||meta(asset).format||'';}
export function isIndexAsset(asset){return assetFormat(asset)==='INDEX'||/\.(?:bai|crai|tbi|csi|fai|gzi|dict|amb|ann|bwt(?:\.2bit\.64)?|pac|sa|bt2l?|ht2l?)$/i.test(asset.name||'');}
export function isPrimaryAsset(asset){
 if(isIndexAsset(asset)||asset.presentation?.primary_eligible===false)return false;
 const family=asset.family||meta(asset).family,name=asset.name||'';
 if(family==='vep')return asset.presentation?.primary_eligible===true;
 if(family==='exomiser'&&(/^(?:test-analysis|readme|licen[cs]e|exomiser-cli-)/i.test(name)||/\.(?:ya?ml|properties|conf|ini)$/i.test(name)))return false;
 return true;
}
export function assetPresentation(asset){
 const m=meta(asset),p=asset.presentation||{},raw=asset.name||asset.id||'Unnamed dataset',name=raw.replaceAll('\\','/').split('/').at(-1),family=asset.family||m.family||'',format=assetFormat(asset);
 const release=p.release||asset.release||m.release||m.source_identity?.release||m.version||name.match(/(?:dbnsfp[._-]?)(\d+(?:\.\d+)*[ac]?)/i)?.[1]||name.match(/(?:^|[_\-.])v(\d+(?:\.\d+)+)/i)?.[1]||'';
 const assembly=p.assembly||asset.assembly||m.assembly||(/(?:hg38|grch38)/i.test(name)?'GRCh38':/(?:hg19|grch37)/i.test(name)?'GRCh37':/(?:chm13|t2t)/i.test(name)?'T2T-CHM13':'');
 const scope=p.contig?`Contig ${p.contig}`:m.scope||m.source_identity?.scope||name.match(/(?:^|[._-])(chr(?:[0-9]+|X|Y|M|MT))(?=[._-]|$)/i)?.[1]||'';
 const role=p.role||(isIndexAsset(asset)?'Companion index':/cdna|transcript/i.test(name)?'Transcript sequences':/pep|protein/i.test(name)?'Protein sequences':/cds/i.test(name)?'Coding sequences':format==='FASTA'?'Reference sequences':/VCF|BCF/.test(format)?'Variant records':format==='BED'?'Genomic intervals':format==='FASTQ_PAIR'?'Paired reads':format==='FASTQ'?'Sequence reads':/BAM|CRAM/.test(format)?'Read alignments':/scores|annotation/i.test(name)?'Annotation scores':format==='ARCHIVE'?'Archive':format==='DATABASE'?'Database data':format||'Dataset');
 const title=FAMILIES[family]||name.replace(/(?:\.gz|\.bgz)?$/i,'').replace(/\.(?:vcf|bcf|fa|fasta|fna|fq|fastq|bam|cram|tsv|csv|txt|bed)$/i,'').replace(/[_]+/g,' ');
 const parts=[title,release,assembly,scope,role,FAMILIES[family]?name:''].filter(Boolean);
 return {title:[...new Set(parts)].join(' · '),raw_name:name,format,release,assembly,scope,role,family,primary:isPrimaryAsset(asset),sha256:m.sha256||asset.sha256||'',version_id:m.version_id||asset.version_id||'',id:asset.id};
}
export function assetOptionLabel(asset){const p=assetPresentation(asset);return p.title+(asset.id?' ['+asset.id.slice(-6)+']':'');}
export function compatibleCompanion(asset,field){
 const format=assetFormat(asset);
 if(field==='primary')return isPrimaryAsset(asset);
 if(field==='mate_id')return format==='FASTQ'||/\.(?:f(?:ast)?q)(?:\.gz)?$/i.test(asset.name||'');
 if(field==='dictionary_id')return /\.dict$/i.test(asset.name||'');
 if(field==='index_id')return /\.(?:bai|crai|tbi|csi|fai)$/i.test(asset.name||'');
 return false;
}
