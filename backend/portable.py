"""Bounded subprocess worker. Never imported into an HTTP parser path."""
import collections
import gzip
import hashlib
import itertools
import json
import os
import random
import shutil
import sys
from pathlib import Path

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def artifact(path,fmt,**metadata):
    p=Path(path)
    return {'path':str(p.resolve()),'name':p.name,'format':fmt,'bytes':p.stat().st_size,'digest':'sha256:'+sha(p),'metadata':metadata}

def report(work,name,value):
    p=work/name;p.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    return artifact(p,'REPORT',report=value)

def fasta_fixture(work):
    rng=random.Random(7341);seq=''.join(rng.choices('ACGT',k=20000));p=work/'engineering.fa'
    p.write_text('>engineering_contig\n'+'\n'.join(seq[i:i+80] for i in range(0,len(seq),80))+'\n')
    return p,seq

def paired_fixture(work,prefix='fixture',seed=7341):
    rng=random.Random(seed);_,seq=fasta_fixture(work);paths=[work/f'{prefix}_R{i}.fastq.gz' for i in [1,2]]
    comp=str.maketrans('ACGT','TGCA')
    with gzip.open(paths[0],'wt') as a,gzip.open(paths[1],'wt') as b:
        for i in range(1000):
            start=rng.randrange(0,len(seq)-400);fragment=seq[start:start+350]
            read1=fragment[:100];read2=fragment[-100:].translate(comp)[::-1]
            for mate,read,f in [(1,read1,a),(2,read2,b)]:f.write(f'@{prefix}_{i}/{mate}\n{read}\n+\n'+('I'*len(read))+'\n')
    return artifact(paths[0],'FASTQ_PAIR',mate=str(paths[1]),synthetic=True,reference='engineering_contig',seed=seed)

def download_bounded(url,path,maximum=4*1024*1024):
    import httpx
    with httpx.stream('GET',url,follow_redirects=True,timeout=30) as r:
        r.raise_for_status();total=0
        with path.open('wb') as f:
            for part in r.iter_bytes(65536):
                total+=len(part)
                if total>maximum:raise ValueError('Public fixture exceeds its download ceiling')
                f.write(part)

def execute(job):
    import pysam
    work=Path(job['workdir']);work.mkdir(parents=True,exist_ok=True)
    tool=job['tool'];params=job.get('params',{});inputs=job.get('inputs',{})
    if tool=='results.prepare':
        from .remote_viewer_worker import execute as viewer_execute
        return viewer_execute(job)
    from .assays import TOOLS as ASSAY_TOOLS,execute as execute_assay
    if tool in ASSAY_TOOLS:return execute_assay(job)
    bundled=Path(__file__).resolve().parents[1]/'demo-data'
    if tool.startswith(('sample.','reference.tiny')) and (bundled/'manifest.json').is_file() and os.getenv('NGS_USE_BUNDLED_SAMPLES','1')=='1':
        frozen=json.loads((bundled/'manifest.json').read_text())
        if tool in frozen['outputs']:
            for entry in frozen['files']:
                if entry['path'].startswith(tool+'/'):
                    p=(bundled/entry['path']).resolve()
                    if not p.is_relative_to(bundled.resolve()) or sha(p)!=entry['sha256']:raise ValueError('Bundled sample digest mismatch')
                    target=work/Path(entry['path']).relative_to(tool);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
            return json.loads(json.dumps(frozen['outputs'][tool]).replace('/opt/studio/demo-data/'+tool,str(work.resolve())))
    def first(key):
        rows=inputs.get(key,[])
        if not rows:raise ValueError('Required input missing: '+key)
        return rows[0]
    def path(key):return first(key)['path']
    def vcf(path):return pysam.VariantFile(path)
    from .scientific import TOOLS,execute as scientific_execute
    if tool in TOOLS:return scientific_execute(job)
    if tool=='sample.clinvar':
        local=Path('/Volumes/Extreme SSD/decode/reference-data/grch38/clinvar/clinvar.vcf.gz')
        if not local.exists():raise ValueError('The configured public ClinVar snapshot is not present. Bind a permitted ClinVar SSD asset or sync the published release first.')
        p=work/'ClinVar_GRCh38_1000.vcf'
        with vcf(str(local)) as src,pysam.VariantFile(str(p),'w',header=src.header) as dest:
            for record in itertools.islice(src,1000):dest.write(record)
        return {'variants':[artifact(p,'VCF',assembly='GRCh38',public=True,source='https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/',subset='First 1000 source records',source_snapshot_mtime=local.stat().st_mtime)]}
    if tool=='sample.alignments':
        reference,seq=fasta_fixture(work);pysam.faidx(str(reference));p=work/'engineering.bam';rng=random.Random(7341)
        with pysam.AlignmentFile(str(p),'wb',header={'HD':{'VN':'1.6','SO':'coordinate'},'SQ':[{'SN':'engineering_contig','LN':len(seq)}]}) as dest:
            for i in range(100):
                start=900+i;read=pysam.AlignedSegment();read.query_name='engineering_'+str(i);read.query_sequence=seq[start:start+100];read.flag=16 if i%2 else 0;read.reference_id=0;read.reference_start=start;read.mapping_quality=60;read.cigar=[(0,100)];read.query_qualities=pysam.qualitystring_to_array('I'*100);dest.write(read)
        pysam.index(str(p));metadata={'assembly':'engineering','synthetic':True,'contig_style':'custom'}
        return {'alignment':[artifact(p,'INDEXED_BAM',**metadata,index=str(p)+'.bai')],'reference':[artifact(reference,'FASTA',**metadata,index=str(reference)+'.fai')]}
    if tool=='sample.fastq':return {'reads':[paired_fixture(work)]}
    if tool=='sample.somatic':return {'tumor':[paired_fixture(work,'tumor',7351)],'normal':[paired_fixture(work,'normal',7341)]}
    if tool=='reference.tiny':
        p,_=fasta_fixture(work);pysam.faidx(str(p));return {'reference':[artifact(p,'FASTA',assembly='engineering',index=str(p)+'.fai',synthetic=True)]}
    if tool=='sample.rna':
        commit='cad884e7fbe5617dcd55bdd07d9ddf65b54febe4';base=f'https://raw.githubusercontent.com/nf-core/test-datasets/{commit}/testdata/prokaryotic/'
        paths=[]
        for name in ['sample_R1_1.fq.gz','sample_R1_2.fq.gz']:
            p=work/name;download_bounded(base+name,p);paths.append(p)
        return {'reads':[artifact(paths[0],'FASTQ_PAIR',mate=str(paths[1]),source=base,commit=commit,public=True,organism='prokaryotic test data',mate_sha256=sha(paths[1]))]}
    if tool=='sample.giab':
        local=Path('/Volumes/Extreme SSD/decode/reference-data/validation/giab/HG002_GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz')
        remote='https://giab.s3.us-east-1.amazonaws.com/release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz'
        source=str(local) if local.exists() else remote
        p=work/'HG002_chr22_1000.vcf'
        with vcf(source) as src,pysam.VariantFile(str(p),'w',header=src.header) as out:
            chrom='chr22' if 'chr22' in src.header.contigs else '22'
            for rec in itertools.islice(src.fetch(chrom),1000):out.write(rec)
        return {'variants':[artifact(p,'VCF',assembly='GRCh38',source=remote,local_source=local.exists(),benchmark='GIAB NIST v4.2.1',subset='first 1000 chr22 records; not a complete benchmarking region',public=True)]}
    if tool=='qc.summary':
        reads=first('reads');paths=[reads['path']]
        if reads.get('metadata',{}).get('mate'):paths.append(reads['metadata']['mate'])
        results=[];limit=params.get('max_reads',100000)
        for source in paths:
            count=bases=gc=ns=qsum=q30=0;lengths=collections.Counter();per_cycle_sum=[];per_cycle_count=[]
            opener=gzip.open if source.endswith(('.gz','.bgz')) else open
            with opener(source,'rt') as f:
                while True:
                    header=f.readline(1000002)
                    if not header:break
                    seq=f.readline(1000002).strip().upper();plus=f.readline(1000002);quality=f.readline(1000002).rstrip('\r\n')
                    if not header.startswith('@') or not plus.startswith('+') or len(seq)!=len(quality):raise ValueError(f'Invalid FASTQ record at record {count+1}')
                    if len(seq)>1000000:raise ValueError('FASTQ read length exceeds the preview safety bound')
                    count+=1;bases+=len(seq);gc+=seq.count('G')+seq.count('C');ns+=seq.count('N');lengths[len(seq)]+=1
                    while len(per_cycle_sum)<len(quality):per_cycle_sum.append(0);per_cycle_count.append(0)
                    for i,char in enumerate(quality):
                        q=ord(char)-33
                        if not 0<=q<=93:raise ValueError('Unsupported FASTQ quality encoding; Phred+33 required')
                        qsum+=q;q30+=q>=30;per_cycle_sum[i]+=q;per_cycle_count[i]+=1
                    if limit and count>=limit:break
            results.append({'file':Path(source).name,'reads':count,'bases':bases,'gc_percent':round(100*gc/max(bases,1),4),'n_percent':round(100*ns/max(bases,1),4),'mean_phred':round(qsum/max(bases,1),4),'q30_percent':round(100*q30/max(bases,1),4),'lengths':dict(lengths),'cycle_quality':[round(a/max(b,1),2) for a,b in zip(per_cycle_sum,per_cycle_count)],'bounded':bool(limit and count>=limit)})
        return {'report':[report(work,'read-quality.json',{'method':'Studio streaming FASTQ summary','mates':results,'total_reads':sum(x['reads'] for x in results),'phred_encoding':33,'not_a_fastqc_replacement':True})]}
    if tool in ['vcf.stats','vcf.filter']:
        with vcf(path('variants')) as src:
            if tool=='vcf.stats':
                counts=collections.Counter();chroms=collections.Counter();filters=collections.Counter();count=0
                for rec in src:
                    count+=1;chroms[rec.contig]+=1
                    for alt in rec.alts or []:counts['SNV' if len(rec.ref)==len(alt)==1 else 'indel' if not alt.startswith('<') else 'symbolic']+=1
                    for f in list(rec.filter) or ['unfiltered']:filters[f]+=1
                return {'report':[report(work,'variant-statistics.json',{'records':count,'allele_classes':dict(counts),'chromosomes':dict(chroms),'filters':dict(filters),'samples':list(src.header.samples),'source_sha256':first('variants').get('digest')})]}
            p=work/'filtered.vcf';kept=total=0
            with pysam.VariantFile(str(p),'w',header=src.header) as out:
                for rec in src:
                    total+=1
                    if params.get('chromosome') and rec.contig!=params['chromosome']:continue
                    if (rec.qual or 0)<params.get('minimum_quality',0):continue
                    if params.get('pass_only',True) and set(rec.filter)-{'PASS','.'}:continue
                    out.write(rec);kept+=1
            return {'variants':[artifact(p,'VCF',**{k:v for k,v in first('variants').get('metadata',{}).items() if k in ['assembly','public']},filtered_records=kept,input_records=total)]}
    if tool=='sam.bam':
        p=work/'converted.bam'
        with pysam.AlignmentFile(path('alignment'),'r') as src,pysam.AlignmentFile(str(p),'wb',header=src.header) as out:
            for rec in src:out.write(rec)
        return {'alignment':[artifact(p,'BAM')]}
    if tool=='bam.sort':
        p=work/'sorted.bam';pysam.sort('-@',str(job.get('threads',1)),'-m','128M','-T',str(work/'sorttmp'),'-o',str(p),path('alignment'))
        return {'alignment':[artifact(p,'SORTED_BAM',**{k:v for k,v in first('alignment').get('metadata',{}).items() if k in ['assembly','public','synthetic']})]}
    if tool=='bam.index':
        src=Path(path('alignment'));p=work/'indexed.bam'
        # Hard links avoid a second large copy within the same data filesystem.
        try:os.link(src,p)
        except OSError:
            shutil.copyfile(src,p)
        args=['-c'] if params.get('csi') else []
        pysam.index(*args,str(p));idx=str(p)+('.csi' if args else '.bai')
        return {'alignment':[artifact(p,'INDEXED_BAM',index=idx,**{k:v for k,v in first('alignment').get('metadata',{}).items() if k in ['assembly','public','synthetic']})]}
    if tool=='bam.stats':
        source=path('alignment');mode='r' if source.endswith('.sam') else 'rb';counts=collections.Counter()
        with pysam.AlignmentFile(source,mode) as src:
            for rec in src.fetch(until_eof=True):
                counts['records']+=1;counts['mapped']+=not rec.is_unmapped;counts['duplicates']+=rec.is_duplicate;counts['secondary']+=rec.is_secondary;counts['supplementary']+=rec.is_supplementary
        return {'report':[report(work,'alignment-statistics.json',dict(counts))]}
    if tool=='vcf.bgzip':
        src=path('variants');plain=work/'input.vcf'
        if src.endswith(('.gz','.bgz')):
            with vcf(src) as data,pysam.VariantFile(str(plain),'w',header=data.header) as out:
                for rec in data:out.write(rec)
            src=str(plain)
        p=work/'variants.vcf.gz';pysam.tabix_compress(src,str(p),force=True)
        return {'variants':[artifact(p,'BGZIP_VCF',**{k:v for k,v in first('variants').get('metadata',{}).items() if k in ['assembly','public']})]}
    if tool=='vcf.index':
        p=work/'indexed.vcf.gz'
        try:os.link(path('variants'),p)
        except OSError:
            shutil.copyfile(path('variants'),p)
        pysam.tabix_index(str(p),preset='vcf',force=True)
        return {'variants':[artifact(p,'INDEXED_VCF',index=str(p)+'.tbi',**{k:v for k,v in first('variants').get('metadata',{}).items() if k in ['assembly','public']})]}
    if tool=='provider.variants':
        result=[]
        with vcf(path('variants')) as src:
            for rec in src:
                for alt in rec.alts or []:
                    if alt.startswith(('<','*')) or '[' in alt or ']' in alt:raise ValueError('Symbolic/structural variants require the corresponding provider adapter')
                    chrom=rec.contig.removeprefix('chr');ref=rec.ref;variant=alt;start=rec.pos
                    while ref and variant and ref[0]==variant[0]:ref=ref[1:];variant=variant[1:];start+=1
                    while ref and variant and ref[-1]==variant[-1]:ref=ref[:-1];variant=variant[:-1]
                    if not ref and not variant:continue
                    end=start+len(ref)-1
                    location=str(start) if start==end else f'{start}_{end}'
                    change=f'{location}{ref}>{variant}' if len(ref)==len(variant)==1 else f'{start-1}_{start}ins{variant}' if not ref else location+'del' if not variant else location+'delins'+variant
                    result.append({'hgvsg':chrom+':g.'+change,'chromosome':chrom,'position':rec.pos,'ref':rec.ref,'alt':alt})
                    if len(result)>=params.get('limit',25):break
                if len(result)>=params.get('limit',25):break
        (work/'variants-for-provider.json').write_text(json.dumps(result));return {}
    if tool=='mt.qc':
        contig=params.get('contig','chrM');sites=[];observed=eligible=0
        with pysam.FastaFile(path('reference')) as ref,pysam.AlignmentFile(path('alignment'),'rb') as bam:
            if contig not in ref.references or contig not in bam.references:raise ValueError('Mitochondrial contig must exist in both alignment and reference')
            if ref.get_reference_length(contig)!=bam.get_reference_length(contig) or ref.get_reference_length(contig)>30000:raise ValueError('Reference/contig length mismatch or non-mitochondrial region')
            for column in bam.pileup(contig,truncate=True,stepper='samtools',max_depth=200000,min_base_quality=params.get('minimum_base_quality',20),min_mapping_quality=params.get('minimum_mapping_quality',30)):
                observed+=1;counts=collections.Counter()
                for pile in column.pileups:
                    if pile.is_del or pile.is_refskip or pile.query_position is None:continue
                    read=pile.alignment
                    if read.is_secondary or read.is_supplementary or read.is_duplicate or read.is_qcfail:continue
                    base=read.query_sequence[pile.query_position].upper()
                    if base in 'ACGT':counts[base]+=1
                depth=sum(counts.values())
                if depth<params.get('minimum_depth',50):continue
                eligible+=1;reference=ref.fetch(contig,column.reference_pos,column.reference_pos+1).upper()
                alternatives={b:{'reads':n,'fraction':n/depth} for b,n in counts.items() if b!=reference and n/depth>=params.get('minimum_fraction',.01)}
                if alternatives:sites.append({'contig':contig,'position':column.reference_pos+1,'reference':reference,'depth':depth,'base_counts':dict(counts),'alternate_fractions':alternatives})
        return {'report':[report(work,'mitochondrial-allele-fractions.json',{'method':'Quality-filtered pileup base-count QC','observed_positions':observed,'eligible_positions':eligible,'sites':sites,'parameters':params,'clinical_validation':False,'limitations':['No NUMT classification','No strand-bias model','No circular/rotated-reference reconciliation','No indel heteroplasmy model']})]}
    if tool in ['bed.intervals','intervals.bed','utility.scatter']:
        source=Path(path('intervals'));opener=gzip.open if source.name.endswith('.gz') else open
        if tool=='utility.scatter':
            shards=max(1,min(32,int(params.get('shards',4))));paths=[work/f'part-{i:03}.bed' for i in range(shards)];handles=[p.open('w') for p in paths]
            try:
                with opener(source,'rt') as f:
                    for i,line in enumerate(l for l in f if l.strip() and not l.startswith(('#','track','browser'))):handles[i%shards].write(line)
            finally:
                for f in handles:f.close()
            return {'shards':[artifact(p,'BED',shard=i,shards=shards) for i,p in enumerate(paths)]}
        p=work/('regions.interval_list' if tool=='bed.intervals' else 'regions.bed')
        with opener(source,'rt') as f,p.open('w') as out:
            contigs={}
            if tool=='bed.intervals':
                with pysam.FastaFile(path('reference')) as ref:contigs=dict(zip(ref.references,ref.lengths))
                out.write('@HD\tVN:1.6\tSO:coordinate\n')
                for name,length in contigs.items():out.write(f'@SQ\tSN:{name}\tLN:{length}\n')
            for i,line in enumerate(f):
                if not line.strip() or line.startswith(('@','#','track','browser')):continue
                cols=line.rstrip().split('\t')
                if len(cols)<3:raise ValueError('Interval record requires at least three columns')
                start,end=int(cols[1]),int(cols[2])
                if tool=='bed.intervals':
                    if cols[0] not in contigs or start<0 or end<=start or end>contigs[cols[0]]:raise ValueError('BED interval conflicts with the reference dictionary')
                    out.write(f'{cols[0]}\t{start+1}\t{end}\t{cols[5] if len(cols)>5 and cols[5] in ["+","-"] else "+"}\t{cols[3] if len(cols)>3 else "interval_"+str(i)}\n')
                else:
                    if start<1 or end<start:raise ValueError('Invalid one-based interval')
                    out.write(f'{cols[0]}\t{start-1}\t{end}\n')
        return {'intervals':[artifact(p,'INTERVALLIST' if tool=='bed.intervals' else 'BED')]}
    if tool in ['fastq.split','fastq.merge']:
        item=first('reads');source=Path(item['path']);opener=lambda p:gzip.open(p,'rt') if str(p).endswith('.gz') else open(p)
        def records(f):
            while header:=f.readline():
                rows=[header,f.readline(),f.readline(),f.readline()]
                if not rows[2].startswith('+') or len(rows[1].strip())!=len(rows[3].strip()):raise ValueError('Invalid FASTQ')
                yield rows
        def same(a,b):return a[0].split()[0].removesuffix('/1').removesuffix('/2')==b[0].split()[0].removesuffix('/1').removesuffix('/2')
        if tool=='fastq.split':
            a=work/'reads_R1.fastq.gz';b=work/'reads_R2.fastq.gz'
            with opener(source) as f,gzip.open(a,'wt') as fa,gzip.open(b,'wt') as fb:
                it=iter(records(f))
                for one in it:
                    two=next(it,None)
                    if two is None or not same(one,two):raise ValueError('Interleaved mates are incomplete or mismatched')
                    fa.writelines(one);fb.writelines(two)
            return {'reads':[artifact(a,'FASTQ_PAIR',mate=str(b))]}
        mate=item.get('metadata',{}).get('mate')
        if not mate:raise ValueError('Paired input has no mate binding')
        p=work/'interleaved.fastq.gz'
        with opener(source) as f,opener(mate) as g,gzip.open(p,'wt') as out:
            for one,two in itertools.zip_longest(records(f),records(g)):
                if one is None or two is None or not same(one,two):raise ValueError('Mates differ in count or identifiers')
                out.writelines(one);out.writelines(two)
        return {'reads':[artifact(p,'INTERLEAVED_FASTQ')]}
    if tool in ['utility.gather','utility.export']:return {('collection' if tool=='utility.gather' else 'published'):[x for rows in inputs.values() for x in rows]}
    if tool=='utility.condition':
        item=first('data');size=item['bytes']
        return {'accepted':[item] if params.get('minimum_bytes',1)<=size<=params.get('maximum_bytes',1000000000) else []}
    if tool in ['alphamissense','spliceai','cadd','revel']:
        variants=path('variants');database=Path(path('database'));rows=[];limit=10000
        if str(database).endswith(('.vcf.gz','.vcf.bgz')) and (Path(str(database)+'.tbi').exists() or Path(str(database)+'.csi').exists()):
            with vcf(variants) as src,pysam.VariantFile(str(database)) as annotations:
                for rec in itertools.islice(src,limit):
                    if rec.contig not in annotations.header.contigs:continue
                    for hit in annotations.fetch(rec.contig,rec.start,rec.stop):
                        if rec.pos==hit.pos and rec.ref==hit.ref and set(rec.alts or [])&set(hit.alts or []):rows.append({'contig':rec.contig,'position':rec.pos,'ref':rec.ref,'alts':rec.alts,'annotations':{k:list(v) if isinstance(v,tuple) else v for k,v in hit.info.items()}})
        else:
            # Unindexed score tables are deliberately bounded. Production-sized
            # tables should be indexed once, rather than scanned on every run.
            wanted=set()
            with vcf(variants) as src:
                for rec in itertools.islice(src,limit):
                    for alt in rec.alts or []:wanted.add((rec.contig.removeprefix('chr'),str(rec.pos),rec.ref,alt))
            opener=gzip.open if str(database).endswith(('.gz','.bgz')) else open
            with opener(database,'rt') as f:
                header=None
                for i,line in enumerate(f):
                    if i>2000000:raise ValueError('Unindexed score table exceeds 2M-line scan ceiling; prepare an indexed score table')
                    if line.startswith('##'):continue
                    cols=line.lstrip('#').rstrip().split('\t')
                    if header is None:header=cols;continue
                    if len(cols)<4:continue
                    names=[x.lower().lstrip('#') for x in header]
                    def col(options):
                        for option in options:
                            if option in names:return names.index(option)
                        raise ValueError('Score-table columns are unsupported; prepare a canonical chrom/pos/ref/alt table')
                    chromcol=col(['chrom','chr','chromosome']);poscol=col((['grch38_pos','hg38_pos'] if params.get('assembly')=='GRCh38' else ['hg19_pos','grch37_pos'])+['pos','position']);refcol=col(['ref','reference']);altcol=col(['alt','alternate'])
                    key=(cols[chromcol].removeprefix('chr'),cols[poscol],cols[refcol],cols[altcol])
                    if key in wanted:rows.append(dict(zip(header,cols)))
        retained=[];threshold=params.get('threshold',.5)
        for row in rows:
            annotations=row.get('annotations',row);lower={k.lower():v for k,v in annotations.items()};score=None
            for key in {'alphamissense':['am_pathogenicity'],'cadd':['phred'],'revel':['revel','revel_score']}.get(tool,[]):
                if key in lower:
                    try:score=float(lower[key]);break
                    except (ValueError,TypeError):pass
            if tool=='spliceai' and 'spliceai' in lower:
                values=lower['spliceai'] if isinstance(lower['spliceai'],list) else [lower['spliceai']]
                score=max((float(v.split('|')[i]) for v in values for i in range(2,6)),default=None)
            if score is None:raise ValueError('Matched score-table row has no supported numeric score field')
            if score>=threshold:retained.append({**row,'selected_score':score})
        return {'annotation':[report(work,tool+'-lookup.json',{'method':'Exact allele score-table lookup','source_database_sha256':first('database').get('digest'),'matches':retained,'threshold':threshold,'matched_before_threshold':len(rows),'max_input_records':limit,'prediction_inference':False})]}
    raise ValueError('No portable implementation for '+tool)

def main():
    job=json.loads(Path(sys.argv[1]).read_text())
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        maximum=int(job.get('max_file_bytes',2*1024**3));resource.setrlimit(resource.RLIMIT_FSIZE,(maximum,maximum))
        if sys.platform.startswith('linux'):
            memory=int(job.get('memory_mb',4096))*1024**2;resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
    except (ImportError,ValueError,OSError):
        # External process watermarks remain active on hosts without rlimits.
        pass
    try:
        result=execute(job);Path(job['workdir'],'result.json').write_text(json.dumps(result,allow_nan=False))
        print('Operation completed; validated output manifest written.',flush=True)
    except Exception as e:
        print(type(e).__name__+': '+str(e)[:500],file=sys.stderr,flush=True);raise SystemExit(1)

if __name__=='__main__':main()
