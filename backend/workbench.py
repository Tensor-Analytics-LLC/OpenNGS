"""Tenant-scoped, disk-indexed tertiary exploration; no inferred clinical classifications."""
import collections
import concurrent.futures
import csv
import hashlib
import io
import itertools
import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from . import data,db
from .config import DATA,aws_session
from .scientific import number

POOL=concurrent.futures.ThreadPoolExecutor(max_workers=2,thread_name_prefix='results')
LOCK=threading.RLock();JOBS={}
VARIANTS={'VCF','BGZIP_VCF','INDEXED_VCF','BCF'}

def owned(uid,aid):
    from . import retention
    retention.lease(uid,aid,seconds=900)
    row=db.query('SELECT * FROM artifacts WHERE id=? AND user_id=?',(aid,uid),one=True) or db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(aid,uid),one=True)
    if not row:raise ValueError('Artifact not found in this workspace')
    from .migration import resolve_asset
    row=resolve_asset(uid,row)
    row['metadata']=json.loads(row['metadata'])
    return row

def object_args(uid,row,field=None):
    """Authorize immutable raw object access without invoking a genomic parser."""
    path=row['metadata'].get(field) if field else row['path']
    if not isinstance(path,str) or not path.startswith('s3://'):
        raise ValueError('A verified private cloud object is required')
    bucket,key=path[5:].split('/',1)
    from .migration import authorize_source
    proof=authorize_source(uid,path)
    if (bucket!=data.settings(uid)['s3_bucket'] and not proof) or not key.startswith('tenants/'+uid+'/'):
        raise ValueError('Track is outside the private tenant cache')
    args={'Bucket':bucket,'Key':key}
    version=row['metadata'].get(field+'_version_id') if field else row['metadata'].get('version_id')
    for secondary in row['metadata'].get('secondary_objects',[]):
        if secondary.get('uri')==path:version=secondary.get('version_id') or version
    if version:args['VersionId']=version
    if proof:
        if version and version!=proof['VersionId']:raise ValueError('Migration object version changed')
        args['VersionId']=proof['VersionId']
    return args

def source(uid,row,field=None):
    from .config import CLOUD
    if CLOUD:
        trusted=False
        if row.get('run_id'):
            run=db.query("SELECT r.graph FROM runs r JOIN demo_tasks d ON d.run_id=r.id WHERE r.id=? AND r.user_id=? AND d.state='published'",(row['run_id'],uid),one=True)
            trusted=bool(run and json.loads(run['graph']).get('_execution',{}).get('access')=='explore')
        if not trusted:raise ValueError('Native parsing of personal research files requires the isolated workbench profile, which is not yet published. Cloud pipeline execution and raw authorized exports remain available; this view will not parse untrusted files on the application host.')
    path=row['metadata'].get(field) if field else row['path']
    if not path:raise ValueError('Required secondary file is not bound: '+str(field))
    if path.startswith('s3://'):
        bucket,key=path[5:].split('/',1);settings=data.settings(uid)
        if bucket!=settings['s3_bucket'] or not key.startswith('tenants/'+uid+'/'):raise ValueError('Track is outside the private tenant cache')
        args={'Bucket':bucket,'Key':key}
        if not field and row['metadata'].get('version_id'):args['VersionId']=row['metadata']['version_id']
        return aws_session().client('s3',config=__import__('botocore.config',fromlist=['Config']).Config(signature_version='s3v4')).generate_presigned_url('get_object',Params=args,ExpiresIn=300)
    return str(data.permitted_path(uid,path))

def inspect_header(uid,aid):
    from . import remote_viewer
    if remote_viewer.required(uid,owned(uid,aid)):
        return remote_viewer.start(uid,aid,'header')
    import pysam,re
    row=owned(uid,aid);path=source(uid,row);fmt=row['format'];metadata=row['metadata'];names=[]
    if fmt in VARIANTS:
        with pysam.VariantFile(path) as handle:
            names=list(handle.header.contigs);metadata['samples']=list(handle.header.samples)
            metadata['info_fields']=list(handle.header.info)
    elif fmt in ['BAM','SORTED_BAM','INDEXED_BAM','CRAM','INDEXED_CRAM','SAM']:
        with pysam.AlignmentFile(path,'rc' if 'CRAM' in fmt else 'r' if fmt=='SAM' else 'rb') as handle:
            names=list(handle.references);metadata['sort_order']=handle.header.to_dict().get('HD',{}).get('SO')
        if metadata['sort_order']=='coordinate' and 'BAM' in fmt:fmt='SORTED_BAM'
    elif fmt=='FASTA':
        if not Path(path+'.fai').is_file():raise ValueError('Reference inspection requires the existing matching FASTA index; add an explicit indexing step first')
        with pysam.FastaFile(path) as handle:names=list(handle.references)
    else:raise ValueError('Header inspection supports VCF/BCF, alignments and indexed FASTA')
    if names:
        metadata['contig_style']='chr' if all(n.startswith('chr') for n in names) else 'numeric' if all(re.fullmatch(r'(?:\d+|X|Y|MT|M)',n) for n in names) else 'mixed'
        metadata['contig_count']=len(names);metadata['contig_preview']=names[:100]
    if not path.startswith('https://'):
        candidates=[path+'.bai',str(Path(path).with_suffix('.bai')),path+'.crai',path+'.tbi',path+'.csi',path+'.fai']
        indices=[p for p in candidates if Path(p).is_file()]
        if indices:metadata['index']=indices[0];metadata['index_paths']=indices
        if fmt=='SORTED_BAM' and any(p.endswith(('.bai','.csi')) for p in indices):fmt='INDEXED_BAM'
        if fmt=='CRAM' and any(p.endswith('.crai') for p in indices):fmt='INDEXED_CRAM'
    metadata['header_inspected']=True;metadata['assembly_verification']='Declared assembly label; exact sequence identity requires reference MD5/dictionary verification'
    with db.tx() as c:
        for table in ['assets','artifacts']:c.execute(f'UPDATE {table} SET format=?,metadata=? WHERE id=? AND user_id=?',(fmt,db.encode(metadata),aid,uid))
        db.audit(c,uid,'asset.header.inspected',aid,{'format':fmt,'contig_count':len(names)})
    return {'id':aid,'format':fmt,'metadata':metadata}

def identity(uid,row):
    if row['path'].startswith('s3://'):
        bucket,key=row['path'][5:].split('/',1)
        source(uid,row)
        head=aws_session().client('s3').head_object(Bucket=bucket,Key=key)
        stamp=[3,row.get('digest'),head.get('VersionId'),head.get('ETag'),head['ContentLength']]
    else:
        p=Path(source(uid,row));st=p.stat();stamp=[3,str(p),st.st_size,st.st_mtime_ns,row.get('digest')]
    return hashlib.sha256(db.encode(stamp).encode()).hexdigest()

def index_path(uid,aid):
    # IDs are produced by this service; hash again to prevent path interpolation.
    from .viewer_retention import cache_path
    path=cache_path(uid,aid);path.parent.mkdir(parents=True,exist_ok=True)
    return path

def connection(path):
    c=sqlite3.connect(path,timeout=15);c.row_factory=sqlite3.Row
    return c

def text(value):
    if isinstance(value,(list,tuple)):return '|'.join(str(v) for v in value if v is not None)
    return '' if value is None else str(value)

def allele_value(value,i):
    if isinstance(value,(list,tuple)):return value[min(i,len(value)-1)] if value else None
    return value

def build(uid,aid,stamp):
    import pysam
    row=owned(uid,aid);path=index_path(uid,aid);temp=path.with_suffix('.building')
    try:
        temp.unlink(missing_ok=True)
        with connection(temp) as c,pysam.VariantFile(source(uid,row)) as src:
            c.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE variants(id INTEGER PRIMARY KEY,chrom TEXT,pos INTEGER,ref TEXT,alt TEXT,gene TEXT,consequence TEXT,clinical TEXT,classification TEXT,af REAL,cadd REAL,revel REAL,alphamissense REAL,spliceai REAL,dp REAL,gq REAL,vaf REAL,qual REAL,zygosity TEXT,inheritance TEXT,oncokb TEXT,cosmic REAL,hpo TEXT,info TEXT,vcf TEXT);
            ''')
            schema={}
            for key in ['CSQ','ANN']:
                if key in src.header.info:
                    description=src.header.info[key].description
                    if 'Format:' in description:schema[key]=[x.strip().strip("'\"") for x in description.split('Format:')[-1].strip().split('|')]
                    elif key=='ANN':schema[key]=['Allele','Annotation','Annotation_Impact','Gene_Name','Gene_ID','Feature_Type','Feature_ID','Transcript_BioType','Rank','HGVS.c','HGVS.p']
            columns=[r[1] for r in c.execute('PRAGMA table_info(variants)') if r[1]!='id'];sql='INSERT INTO variants('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')'
            count=0;buffer=[];ceiling=data.settings(uid)['max_upload_gib']*1024**3
            for rec in src:
                info={k:list(v) if isinstance(v,tuple) else v for k,v in rec.info.items()};call=next(iter(rec.samples.values()),{});gt=call.get('GT');zygosity='unknown'
                if gt and all(v is not None for v in gt):zygosity='hom_ref' if all(v==0 for v in gt) else 'hom_alt' if len(set(gt))==1 else 'het'
                for i,alt in enumerate(rec.alts or []):
                    allele_zygosity=zygosity
                    if gt and all(v is not None for v in gt):allele_zygosity='hom_ref' if i+1 not in gt else 'hemi_alt' if len(gt)==1 else 'hom_alt' if all(v==i+1 for v in gt) else 'het'
                    annotations=[]
                    for key,names in schema.items():
                        entries=info.get(key,[]);entries=entries if isinstance(entries,list) else [entries]
                        annotations.extend(dict(zip(names,v.split('|'))) for v in entries if v and v.split('|')[0] in [alt,alt.removeprefix(rec.ref[0]) or '-',str(i+1)])
                    def find(*names):
                        for name in names:
                            if name in info:
                                cardinality=src.header.info[name].number
                                return allele_value(info[name],i if cardinality=='A' else i+1) if cardinality in ['A','R'] else info[name]
                            for annotation in annotations:
                                if annotation.get(name) not in [None,'']:return annotation[name]
                        return None
                    clinical=text(find('CLNSIG','ClinVar','CLIN_SIG'));classification=text(find('ACMG','ACMG_CLASS','CLASSIFICATION'))
                    if not classification:
                        # A ClinVar label is displayed with provenance, not promoted to a new ACMG assessment.
                        mapping={'Pathogenic':'P','Likely_pathogenic':'LP','Pathogenic/Likely_pathogenic':'P/LP','Uncertain_significance':'VUS','Likely_benign':'LB','Benign':'B','Benign/Likely_benign':'B/LB'}
                        classification=mapping.get(clinical,'')
                    ad=call.get('AD');af=number(allele_value(call.get('AF'),i))
                    if af is None and ad and len(ad)>i+1 and all(v is not None for v in ad) and sum(ad)>0:af=ad[i+1]/sum(ad)
                    splice=number(find('OPENNGS_SPLICEAI','SpliceAI_DS','SpliceAI_score','spliceai'))
                    if splice is None and info.get('SpliceAI'):
                        values=info['SpliceAI'] if isinstance(info['SpliceAI'],list) else [info['SpliceAI']]
                        scores=[number(s) for v in values if v.split('|')[0]==alt for s in v.split('|')[2:6]];scores=[s for s in scores if s is not None];splice=max(scores) if scores else None
                    gene=text(find('SYMBOL','Gene_Name','GENE','Gene'))
                    if not gene and info.get('GENEINFO'):gene='|'.join(v.split(':')[0] for v in text(info['GENEINFO']).split('|'))
                    consequence=text(find('Consequence','Annotation','CONSEQUENCE'))
                    if not consequence and info.get('MC'):
                        items=info['MC'] if isinstance(info['MC'],list) else [info['MC']];consequence='|'.join(v.split('|')[-1] for v in items)
                    values=[rec.contig,rec.pos,rec.ref,alt,gene,consequence,clinical,classification,number(find('gnomAD_AF','gnomADg_AF','gnomADe_AF')),number(find('OPENNGS_CADD','CADD_PHRED','CADD')),number(find('OPENNGS_REVEL','REVEL','REVEL_score')),number(find('OPENNGS_AM','am_pathogenicity','AlphaMissense','AlphaMissense_score')),splice,number(call.get('DP')),number(call.get('GQ')),af,rec.qual,allele_zygosity,text(find('INHERITANCE','Inheritance')),text(find('OPENNGS_ONCOKB','ONCOKB','ONCOKB_LEVEL')),number(find('COSMIC_COUNT')),text(find('HPO','HPO_TERMS')),db.encode({'INFO':info,'sample':next(iter(rec.samples),None),'genotype':list(gt) if gt else None,'annotation_source':'VCF INFO/CSQ/ANN; no inferred pathogenicity','annotations':annotations}),str(rec)]
                    buffer.append(values);count+=1
                    if len(buffer)>=2000:
                        c.executemany(sql,buffer);buffer=[];c.commit()
                        if temp.stat().st_size>ceiling:raise ValueError('Results index exceeds workspace storage ceiling')
                        with LOCK:JOBS[(uid,aid)]={'state':'building','rows':count}
                    if count>100000000:raise ValueError('Results index exceeds 100M-allele safety ceiling')
            if buffer:c.executemany(sql,buffer)
            c.executescript('CREATE INDEX locus ON variants(chrom,pos,id);CREATE INDEX frequency ON variants(af);CREATE INDEX gene_index ON variants(gene);CREATE INDEX clinical_index ON variants(classification);CREATE INDEX consequence_index ON variants(consequence);')
            for k,v in {'identity':stamp,'rows':count,'header':str(src.header),'assembly':row.get('assembly') or row['metadata'].get('assembly'),'created':time.time(),'schema':schema}.items():c.execute('INSERT INTO meta VALUES(?,?)',(k,db.encode(v)))
            c.commit()
        from . import retention
        with db.tx() as c:
            retention.ensure_available(uid,aid,c)
            temp.replace(path)
        with LOCK:JOBS[(uid,aid)]={'state':'ready','rows':count}
        with db.tx() as c:db.audit(c,uid,'results.indexed',aid,{'rows':count,'identity':stamp})
    except Exception as e:
        temp.unlink(missing_ok=True)
        with LOCK:JOBS[(uid,aid)]={'state':'failed','error':str(e)[:500]}

def start_index(uid,aid):
    row=owned(uid,aid)
    if row['format'] not in VARIANTS:raise ValueError('Choose a VCF/BCF variant artifact')
    from . import remote_viewer
    if remote_viewer.required(uid,row):return remote_viewer.start(uid,aid)
    stamp=identity(uid,row);path=index_path(uid,aid)
    with LOCK:
        if path.exists():
            with connection(path) as c:
                meta={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM meta')}
            if meta['identity']==stamp:return {'state':'ready','rows':meta['rows'],'assembly':meta['assembly']}
        job=JOBS.get((uid,aid))
        if job and job['state']=='building':return job
        JOBS[(uid,aid)]={'state':'building','rows':0};POOL.submit(build,uid,aid,stamp)
    return {'state':'building','rows':0}

def where(filters):
    clauses=[];args=[]
    allowed={'q','gene','chrom','classification','clinical','consequence','inheritance','zygosity','oncokb','hpo','genes','max_af','min_cadd','min_revel','min_alphamissense','min_spliceai','min_dp','min_gq','min_vaf','min_qual','min_cosmic','include_missing'}
    if set(filters)-allowed:raise ValueError('Unknown result filter')
    for key in ['chrom','classification','zygosity']:
        if filters.get(key):clauses.append(key+'=?');args.append(str(filters[key])[:200])
    for key in ['gene','clinical','consequence','inheritance','oncokb','hpo']:
        if filters.get(key):clauses.append(key+' LIKE ?');args.append('%'+str(filters[key])[:200]+'%')
    if filters.get('q'):
        clauses.append('(gene LIKE ? OR chrom LIKE ? OR ref=? OR alt=?)');term=str(filters['q'])[:200];args.extend(['%'+term+'%','%'+term+'%',term,term])
    if filters.get('genes'):
        genes=[s.strip() for s in str(filters['genes']).replace(',', ' ').split() if s.strip()]
        if len(genes)>500:raise ValueError('Gene panel is limited to 500 explicit symbols')
        clauses.append('('+' OR '.join('gene=?' for _ in genes)+')');args.extend(genes)
    for key in ['max_af','min_cadd','min_revel','min_alphamissense','min_spliceai','min_dp','min_gq','min_vaf','min_qual','min_cosmic']:
        if filters.get(key) in [None,'']:continue
        value=number(filters[key])
        if value is None:raise ValueError('Numeric filter must be finite')
        column=key.split('_',1)[1];operator='<=' if key.startswith('max') else '>='
        clauses.append(f'({column}{operator}?'+(f' OR {column} IS NULL)' if filters.get('include_missing') else ')'));args.append(value)
    return (' WHERE '+' AND '.join(clauses) if clauses else ''),args

def query(uid,aid,filters,offset=0,limit=100):
    owned(uid,aid);path=index_path(uid,aid)
    from .viewer_retention import lease_index
    lease_index(uid,aid)
    if not path.exists():raise ValueError('Build the results index first')
    offset=int(offset);limit=int(limit)
    if not 0<=offset<=100000000 or not 1<=limit<=200:raise ValueError('Invalid results window')
    clause,args=where(filters)
    with connection(path) as c:
        count=c.execute('SELECT COUNT(*) FROM variants'+clause,args).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT * FROM variants'+clause+' ORDER BY id LIMIT ? OFFSET ?',[*args,limit,offset])]
        total=json.loads(c.execute("SELECT value FROM meta WHERE key='rows'").fetchone()[0])
    for r in rows:r['details']=json.loads(r.pop('info'));r.pop('vcf')
    return {'rows':rows,'matched':count,'total':total,'offset':offset,'limit':limit,'missing_annotation_policy':'Missing numeric values excluded unless include_missing=true. ClinVar-derived badges are not independent ACMG classifications.'}

def stats(uid,aid,filters):
    owned(uid,aid);clause,args=where(filters)
    from .viewer_retention import lease_index
    lease_index(uid,aid)
    if not index_path(uid,aid).is_file():raise ValueError('Build the results index first')
    with connection(index_path(uid,aid)) as c:
        result={key:[{'label':r[0] or 'unannotated','count':r[1]} for r in c.execute(f'SELECT {key},COUNT(*) FROM variants'+clause+f' GROUP BY {key} ORDER BY COUNT(*) DESC LIMIT 30',args)] for key in ['chrom','classification','consequence','zygosity']}
        result['quality']=[{'label':str(r[0]),'count':r[1]} for r in c.execute('SELECT CAST(qual/10 AS INTEGER)*10,COUNT(*) FROM variants'+clause+(' AND' if clause else ' WHERE')+' qual IS NOT NULL GROUP BY 1 ORDER BY 1',args)]
    return result

def export(uid,aid,filters,fmt):
    owned(uid,aid);clause,args=where(filters);path=index_path(uid,aid)
    from .viewer_retention import lease_index
    lease_index(uid,aid)
    if not path.is_file():raise ValueError('Build the results index first')
    def chunks():
        with connection(path) as c:
            if fmt=='vcf':
                yield json.loads(c.execute("SELECT value FROM meta WHERE key='header'").fetchone()[0]);last=None
                for row in c.execute('SELECT vcf FROM variants'+clause+' ORDER BY id',args):
                    if row[0]!=last:yield row[0];last=row[0]
            else:
                fields=['chrom','pos','ref','alt','gene','consequence','clinical','classification','af','cadd','revel','alphamissense','spliceai','dp','gq','vaf','qual','zygosity','inheritance','oncokb','cosmic','hpo']
                output=io.StringIO();writer=csv.writer(output,delimiter='\t' if fmt=='tsv' else ',');writer.writerow(fields);yield output.getvalue()
                for row in c.execute('SELECT '+','.join(fields)+' FROM variants'+clause+' ORDER BY id',args):
                    output.seek(0);output.truncate();writer.writerow([("'"+str(v)) if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in row]);yield output.getvalue()
    return chunks()

def pdf(uid,aid,filters):
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    from xml.sax.saxutils import escape
    result=query(uid,aid,filters,0,100);buf=io.BytesIO();styles=getSampleStyleSheet();story=[]
    story.append(Paragraph('OpenNGS by Tensor Analytics',styles['Title']))
    story.append(Paragraph('Research variant review report',styles['Heading1']))
    for value in ['Research use only. This report is not a clinical diagnosis, independent ACMG assessment or validated medical interpretation.',f'Artifact: {aid}',f'Matching alleles: {result["matched"]:,} of {result["total"]:,}. Showing up to the first 100; complete results are available as TSV/VCF.','Filters: '+db.encode(filters),'Annotation and inheritance values are imported from the source VCF. Missing annotations are not evidence of benignity.']:
        story.extend([Paragraph(escape(value),styles['BodyText']),Spacer(1,8)])
    rows=[['Locus','Allele','Gene','Source label']]
    for r in result['rows']:rows.append([f'{r["chrom"]}:{r["pos"]}',r['ref'][:16]+'>'+r['alt'][:16],r['gene'][:25],Paragraph(escape(r['classification'] or r['clinical'] or 'Unannotated'),styles['BodyText'])])
    table=Table(rows,repeatRows=1,colWidths=[120,140,120,100]);table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#14243c')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTSIZE',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),6),('LINEBELOW',(0,0),(-1,-1),.25,colors.lightgrey)]));story.append(table)
    SimpleDocTemplate(buf,title='OpenNGS research review',author='Tensor Analytics').build(story)
    return buf.getvalue()

def pileup(uid,alignment_id,reference_id,chrom,start,end):
    from . import remote_viewer
    if remote_viewer.required(uid,owned(uid,alignment_id)) or remote_viewer.required(uid,owned(uid,reference_id)):
        if not 0<=start<end or end-start>2000:raise ValueError('Pileup window must span 1–2000 bases')
        return remote_viewer.start(uid,alignment_id,'pileup',{'chrom':chrom,'start':start,'end':end},reference_id)
    import pysam
    if not 0<=start<end or end-start>2000:raise ValueError('Pileup window must span 1–2000 bases')
    alignment=owned(uid,alignment_id);reference=owned(uid,reference_id)
    if alignment['format'] not in ['INDEXED_BAM','INDEXED_CRAM']:raise ValueError('Select an explicitly indexed BAM/CRAM with bound BAI/CRAI')
    if reference['format']!='FASTA':raise ValueError('Select the matching indexed FASTA')
    am=alignment['metadata'].get('assembly') or alignment.get('assembly');rm=reference['metadata'].get('assembly') or reference.get('assembly')
    if am and rm and am!=rm:raise ValueError('Alignment and reference assembly differ')
    refpath=source(uid,reference);refindex=source(uid,reference,'index') if reference['metadata'].get('index') else refpath+'.fai'
    if reference['path'].startswith('s3://'):
        from .range_fasta import S3Fasta
        source(uid,reference,'index')  # Verify the bound companion has the same authorized tenant scope.
        bucket,key=reference['path'][5:].split('/',1);index_bucket,index_key=reference['metadata']['index'][5:].split('/',1)
        if bucket!=index_bucket:raise ValueError('Reference and FAI must be in the same authorized cache')
        seq=S3Fasta(aws_session().client('s3'),bucket,key,index_key,reference['metadata'].get('version_id'))
    else:seq=pysam.FastaFile(refpath,filepath_index=refindex)
    index=source(uid,alignment,'index')
    with seq,pysam.AlignmentFile(source(uid,alignment),'rc' if alignment['format']=='INDEXED_CRAM' else 'rb',index_filename=index,reference_filename=refpath) as bam:
        if chrom not in bam.references or chrom not in seq.references:raise ValueError('Contig not present in both track and reference; explicit renaming may be required')
        end=min(end,seq.get_reference_length(chrom))
        if start>=end:raise ValueError('Pileup starts beyond the selected contig')
        reference_bases=seq.fetch(chrom,start,end);reads=[];coverage=[0]*(end-start);forward=[0]*(end-start);reverse=[0]*(end-start)
        truncated=False
        for rec in bam.fetch(chrom,start,end):
            if rec.is_unmapped:continue
            if len(reads)>=300:truncated=True;break
            bases=[]
            for q,r in rec.get_aligned_pairs(matches_only=False):
                if r is not None and start<=r<end:
                    if q is not None:
                        coverage[r-start]+=1;(reverse if rec.is_reverse else forward)[r-start]+=1
                    bases.append([r,rec.query_sequence[q] if q is not None and rec.query_sequence else '-',int(rec.query_qualities[q]) if q is not None and rec.query_qualities is not None else None])
            reads.append({'name':rec.query_name,'start':rec.reference_start,'end':rec.reference_end,'reverse':rec.is_reverse,'mapq':rec.mapping_quality,'cigar':rec.cigarstring,'duplicate':rec.is_duplicate,'bases':bases})
    return {'chrom':chrom,'start':start,'end':end,'reference':reference_bases,'reads':reads,'coverage':coverage,'forward':forward,'reverse':reverse,'truncated':truncated,'coverage_scope':'Coverage of rendered reads (maximum 300), not whole-file depth','transport':'HTSlib indexed region fetch; private S3 inputs use expiring range-capable URLs'}

def omics(uid,aid,x=None,y=None):
    from . import remote_viewer
    if remote_viewer.required(uid,owned(uid,aid)):return remote_viewer.start(uid,aid,'omics',{'x':x or '', 'y':y or ''})
    import numpy as np
    row=owned(uid,aid);path=source(uid,row)
    if path.startswith('https://'):raise ValueError('Stage this expression/embedding table into the private SSD cache before charting; genome tracks support remote indexed ranges')
    maximum=20000
    if row['format']=='H5AD':
        import h5py
        with h5py.File(path,'r') as f:
            embeddings={}
            for key in ['X_umap','X_tsne','X_pca']:
                if 'obsm/'+key in f and isinstance(f['obsm/'+key],h5py.Dataset):embeddings[key]=np.asarray(f['obsm/'+key][:maximum,:2],dtype=float).tolist()
        return {'embeddings':embeddings,'method':'Existing AnnData obsm coordinates; no inferred or simulated embedding','limit':maximum}
    opener=open
    with opener(path) as f:
        reader=csv.DictReader(f,delimiter=',' if path.endswith('.csv') else '\t');fields=reader.fieldnames or [];rows=list(itertools.islice(reader,maximum))
    numeric=[key for key in fields if rows and sum(number(r.get(key)) is not None for r in rows[:100])>=max(1,len(rows[:100])*.8)]
    points=[]
    if x and y:
        if x not in fields or y not in fields:raise ValueError('Chart axes must be actual table columns')
        for i,r in enumerate(rows):
            a,b=number(r.get(x)),number(r.get(y))
            if a is not None and b is not None:points.append({'x':a,'y':b,'label':r.get('gene') or r.get('Gene') or r.get('cell') or str(i+1),'row':r})
    matrix=[[number(r.get(k)) for k in numeric[:50]] for r in rows[:200]]
    return {'columns':fields,'numeric':numeric,'points':points,'matrix':matrix,'row_labels':[r.get('gene') or r.get('cell') or str(i+1) for i,r in enumerate(rows[:200])],'matrix_columns':numeric[:50],'limit':maximum,'method':'Direct table values; select raw p-values or adjusted FDR explicitly for volcano axes'}

def run_summary(uid,rid):
    row=db.query('SELECT * FROM runs WHERE id=? AND user_id=?',(rid,uid),one=True)
    if not row:raise ValueError('Run not found')
    reports=[]
    for r in db.query("SELECT * FROM artifacts WHERE user_id=? AND run_id=? AND format='REPORT'",(uid,rid)):
        meta=json.loads(r['metadata']);body=meta.get('report')
        if body is None and not r['path'].startswith('s3://') and r['name'].endswith('.json') and r['bytes']<=2*1024**2:
            try:body=json.loads(Path(source(uid,{**r,'metadata':meta})).read_text())
            except (ValueError,OSError):pass
        reports.append({'id':r['id'],'node_id':r['node_id'],'name':r['name'],'metrics':body})
    return {'run_id':rid,'state':row['state'],'reports':reports,'attempts':db.query('SELECT node_id,state,metrics,error FROM attempts WHERE run_id=?',(rid,)),'method':'OpenNGS run metric aggregation; native MultiQC is a separate catalogue node','clinical_validation':False}
