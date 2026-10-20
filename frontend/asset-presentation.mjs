const FAMILIES={dbnsfp:'dbNSFP',alphamissense:'AlphaMissense',spliceai:'SpliceAI',cadd:'CADD',revel:'REVEL',exomiser:'Exomiser',hpo:'Human Phenotype Ontology',ensembl:'Ensembl',mane:'MANE',clingen:'ClinGen',vep:'Ensembl VEP',reference:'Reference genome',clinvar:'ClinVar',gnomad:'gnomAD',giab:'GIAB',omim:'OMIM',dbsnp:'dbSNP'};
function meta(asset){const raw=asset.metadata||asset.manifest||{};if(typeof raw!=='string')return raw;try{return JSON.parse(raw);}catch{return {};}}
export function assetFormat(asset){return asset.format||meta(asset).format||'';}
export function isIndexAsset(asset){return assetFormat(asset)==='INDEX'||/\.(?:bai|crai|tbi|csi|fai|gzi|dict|amb|ann|bwt(?:\.2bit\.64)?|pac|sa|bt2l?|ht2l?)$/i.test(asset.name||'');}
export function assetPresentation(asset){
 const m=meta(asset),raw=asset.name||asset.id||'Unnamed dataset',name=raw.split('/').at(-1),family=asset.family||m.family||'',format=assetFormat(asset);
 const release=asset.release||m.release||m.source_identity?.release||m.version||name.match(/(?:dbnsfp[._-]?)(\d+(?:\.\d+)*[ac]?)/i)?.[1]||name.match(/(?:^|[_\-.])v(\d+(?:\.\d+)+)/i)?.[1]||'';
 const assembly=asset.assembly||m.assembly||(/(?:hg38|grch38)/i.test(name)?'GRCh38':/(?:hg19|grch37)/i.test(name)?'GRCh37':/(?:chm13|t2t)/i.test(name)?'T2T-CHM13':'');
 const scope=m.scope||m.source_identity?.scope||name.match(/(?:^|[._-])(chr(?:[0-9]+|X|Y|M|MT))(?=[._-]|$)/i)?.[1]||'';
 const role=isIndexAsset(asset)?'Companion index':/cdna|transcript/i.test(name)?'Transcript sequences':/pep|protein/i.test(name)?'Protein sequences':/cds/i.test(name)?'Coding sequences':format==='FASTA'?'Reference sequences':/VCF|BCF/.test(format)?'Variant records':format==='BED'?'Genomic intervals':format==='FASTQ_PAIR'?'Paired reads':format==='FASTQ'?'Sequence reads':/BAM|CRAM/.test(format)?'Read alignments':/scores|annotation/i.test(name)?'Annotation scores':format==='ARCHIVE'?'Database bundle':format==='DATABASE'?'Database data':format||'Dataset';
 const title=FAMILIES[family]||name.replace(/(?:\.gz|\.bgz)?$/i,'').replace(/\.(?:vcf|bcf|fa|fasta|fna|fq|fastq|bam|cram|tsv|csv|txt|bed)$/i,'').replace(/[_]+/g,' ');
 const parts=[title,release,assembly,scope,role].filter(Boolean);
 return {title:[...new Set(parts)].join(' · '),raw_name:raw,format,release,assembly,scope,role,family,sha256:m.sha256||asset.sha256||'',version_id:m.version_id||asset.version_id||'',id:asset.id};
}
export function assetOptionLabel(asset){const p=assetPresentation(asset);return p.title+(asset.id?' ['+asset.id.slice(-6)+']':'');}
export function compatibleCompanion(asset,field){
 const format=assetFormat(asset);
 if(field==='primary')return !isIndexAsset(asset);
 if(field==='mate_id')return format==='FASTQ'||/\.(?:f(?:ast)?q)(?:\.gz)?$/i.test(asset.name||'');
 if(field==='dictionary_id')return /\.dict$/i.test(asset.name||'');
 if(field==='index_id')return /\.(?:bai|crai|tbi|csi|fai)$/i.test(asset.name||'');
 return false;
}
