"""Record-aware format conversions and contig naming; no coordinate liftover."""
import gzip
import json
from pathlib import Path
import re

TOOLS={'vcf.bcf','bcf.vcf','contig.rename','contig.rename.bed','gtf.bed','bed.gtf'}
VERSION='1.0.0'
NAME=re.compile(r'^[^\s,;=<>\[\]"\\]+$')
BND=re.compile(r'(?P<bracket>[\[\]])(?P<mate>[^\[\]]+):(?P<position>[0-9]+)(?P=bracket)')


def bed_header(line):
    # Directive syntax is distinct from BED rows on contigs named track/browser.
    return line.startswith('#') or bool(re.match(
        r'^(?:track(?:[ \t]+[A-Za-z][A-Za-z0-9_]*=|[ \t]*\r?$)|'
        r'browser(?:[ \t]+(?:position|hide|dense|pack|squish|full|pix)\b|[ \t]*\r?$))',line))


def rename_contig_header(line,names):
    # Quoted metadata may contain both commas and the text ID=. Consume whole
    # quoted strings before matching the actual unquoted ID header attribute.
    return re.sub(r'"(?:\\.|[^"\\])*"|(?P<id>(?<=[<,])ID=)(?P<name>[^,>]+)',
                  lambda match:'ID='+names[match['name']] if match['id'] else match[0],line)


def contig_name(name):
    if not isinstance(name,str) or not NAME.fullmatch(name):
        raise ValueError('Contig names must be nonempty tokens without VCF/header delimiters')
    return name


def renamer(params):
    style=params.get('style','chr')
    if style not in {'chr','numeric','mapping'}:raise ValueError('Unknown contig naming style')
    mapping=json.loads(params.get('mapping','{}'))
    if not isinstance(mapping,dict):raise ValueError('Contig mapping must be a JSON object')
    for key,value in mapping.items():contig_name(key);contig_name(value)
    def rename(name):
        contig_name(name)
        if style=='mapping':return mapping.get(name,name)
        if style=='numeric':return 'MT' if name in {'chrM','M','MT'} else contig_name(name.removeprefix('chr'))
        return 'chrM' if name in {'M','MT','chrM'} else name if name.startswith('chr') else 'chr'+name
    return rename


def header_schema(header):
    return {'contigs':set(header.contigs),'info':set(header.info),'format':set(header.formats),'filter':set(header.filters)}


def validate_record(record,schema):
    # HTSlib may synthesize missing declarations while parsing a malformed VCF.
    # Never let those inferred tags silently become a partial BCF dictionary.
    if record.contig not in schema['contigs']:raise ValueError('VCF records require explicit contig header declarations')
    if set(record.info)-schema['info'] or set(record.format)-schema['format'] or set(record.filter)-schema['filter']-{'PASS','.'}:
        raise ValueError('VCF INFO, FORMAT and FILTER fields require explicit header declarations')


def bed_fields(line):
    fields=line.rstrip('\r\n').split('\t')
    if len(fields)<3:raise ValueError('BED requires at least three tab-separated columns')
    contig_name(fields[0])
    try:start,end=int(fields[1]),int(fields[2])
    except ValueError as error:raise ValueError('BED coordinates must be integers') from error
    if start<0 or end<start:raise ValueError('Invalid zero-based half-open BED coordinates')
    if len(fields)>5 and fields[5] not in {'+','-','.'}:raise ValueError('BED strand must be +, - or .')
    if len(fields) in {10,11}:raise ValueError('BED block fields must include count, sizes and starts together')
    blocks=[(start,end)]
    if len(fields)>=12:
        try:
            count=int(fields[9]);sizes=[int(x) for x in fields[10].rstrip(',').split(',')]
            offsets=[int(x) for x in fields[11].rstrip(',').split(',')]
        except ValueError as error:raise ValueError('Invalid BED12 block integers') from error
        if count<1 or len(sizes)!=count or len(offsets)!=count:raise ValueError('BED12 block count differs from its arrays')
        blocks=[]
        for size,offset in zip(sizes,offsets):
            left,right=start+offset,start+offset+size
            if size<1 or offset<0 or right>end or blocks and left<blocks[-1][1]:
                raise ValueError('BED12 blocks must be positive, ordered, nonoverlapping and inside the interval')
            blocks.append((left,right))
        if blocks[0][0]!=start or blocks[-1][1]!=end:
            raise ValueError('BED12 first and last blocks must match the outer interval boundaries')
    return fields,start,end,blocks


def text_input(path):
    return gzip.open(path,'rt') if str(path).endswith(('.gz','.bgz')) else open(path)


def execute(job):
    import pysam
    from .portable import artifact
    tool=job['tool'];params=job.get('params',{});inputs=job.get('inputs',{});work=Path(job['workdir'])
    work.mkdir(parents=True,exist_ok=True)
    def source(port):
        rows=inputs.get(port,[])
        if len(rows)!=1:raise ValueError('Format operators require exactly one input artifact per port')
        return rows[0]
    def metadata(port):
        return {key:value for key,value in source(port).get('metadata',{}).items()
                if key in {'assembly','contig_style','reference_digest','public','sample_id'}}
    if tool in {'vcf.bcf','bcf.vcf'}:
        item=source('variants');out=work/('converted.bcf' if tool=='vcf.bcf' else 'converted.vcf')
        with pysam.VariantFile(item['path']) as src:
            schema=header_schema(src.header)
            with pysam.VariantFile(str(out),'wb' if tool=='vcf.bcf' else 'w',header=src.header) as dest:
                for record in src:validate_record(record,schema);dest.write(record)
        return {'variants':[artifact(out,'BCF' if tool=='vcf.bcf' else 'VCF',**metadata('variants'),
            transformation='HTSlib encoding conversion; records, alleles, samples and genotype fields retained; no normalization')]}
    if tool in {'contig.rename','contig.rename.bed'}:
        item=source('data');rename=renamer(params);seen={}
        def named(name):
            result=rename(name)
            if result in seen and seen[result]!=name:raise ValueError('Contig mapping creates a many-to-one collision')
            seen[result]=name
            return result
        out=work/('renamed.bed' if tool=='contig.rename.bed' else 'renamed.vcf')
        if tool=='contig.rename.bed':
            if item.get('format')!='BED':raise ValueError('BED contig rename requires a BED artifact')
            with text_input(item['path']) as src,out.open('w') as dest:
                for line in src:
                    if not line.strip() or bed_header(line):dest.write(line);continue
                    fields,_,_,_=bed_fields(line);fields[0]=named(fields[0]);dest.write('\t'.join(fields)+'\n')
        else:
            with pysam.VariantFile(item['path']) as src,out.open('w') as dest:
                schema=header_schema(src.header);names={name:named(name) for name in src.header.contigs}
                for line in str(src.header).splitlines():
                    if line.startswith('##contig='):
                        line=rename_contig_header(line,names)
                    dest.write(line+'\n')
                for record in src:
                    validate_record(record,schema);fields=str(record).rstrip('\r\n').split('\t');fields[0]=names[record.contig]
                    def mate(match):
                        name=match['mate']
                        if name not in names:raise ValueError('Breakend mate contigs require explicit header declarations')
                        return match['bracket']+names[name]+':'+match['position']+match['bracket']
                    alts=[]
                    for alt in fields[4].split(','):
                        if '[' in alt or ']' in alt:
                            matches=list(BND.finditer(alt))
                            if len(matches)!=1 or alt.count('[')+alt.count(']')!=2:
                                raise ValueError('Malformed or unsupported breakend mate notation')
                            match=matches[0]
                            if not ((match.start()==0 and match.end()<len(alt)) or (match.start()>0 and match.end()==len(alt))):
                                raise ValueError('A breakend mate must precede or follow its replacement sequence')
                            alt=BND.sub(mate,alt)
                        alts.append(alt)
                    fields[4]=','.join(alts)
                    # These explicit partner-contig conventions are names. Other
                    # annotation strings remain untouched rather than guessed.
                    infos=[]
                    for entry in fields[7].split(';'):
                        key,sep,value=entry.partition('=')
                        if key in {'CHR2','CHROM2'} and sep:
                            values=value.split(',')
                            if any(name not in names for name in values):raise ValueError('Partner contig fields require declared contigs')
                            entry=key+'='+','.join(names[name] for name in values)
                        infos.append(entry)
                    fields[7]=';'.join(infos);dest.write('\t'.join(fields)+'\n')
        return {'data':[artifact(out,'BED' if tool=='contig.rename.bed' else 'VCF',**{
            **metadata('data'),'contig_style':params.get('style','chr')},
            transformation='Contig names only; coordinates and assembly unchanged; not liftover',liftover=False)]}
    if tool in {'gtf.bed','bed.gtf'}:
        item=source('intervals');out=work/('features.bed' if tool=='gtf.bed' else 'intervals.gtf')
        with text_input(item['path']) as src,out.open('w') as dest:
            feature_number=0
            for line in src:
                if tool=='gtf.bed' and line.rstrip('\r\n')=='##FASTA':break
                if not line.strip() or line.startswith('#') or (tool=='bed.gtf' and bed_header(line)):continue
                fields=line.rstrip('\r\n').split('\t')
                if tool=='gtf.bed':
                    if len(fields)!=9:raise ValueError('GTF/GFF3 requires exactly nine columns')
                    contig_name(fields[0])
                    start,end=int(fields[3]),int(fields[4])
                    if start<1 or end<start:raise ValueError('Invalid one-based closed feature coordinates')
                    if fields[6] not in {'+','-','.'}:raise ValueError('Feature strand must be +, - or . for BED conversion')
                    if fields[7] not in {'0','1','2','.'}:raise ValueError('Feature phase must be 0, 1, 2 or .')
                    if params.get('feature','exon') and fields[2]!=params.get('feature','exon'):continue
                    dest.write(f'{fields[0]}\t{start-1}\t{end}\t{fields[2]}\t0\t{fields[6]}\n')
                else:
                    fields,start,end,blocks=bed_fields(line)
                    if end<=start:raise ValueError('A zero-length BED site cannot become a nonempty GTF feature')
                    strand=fields[5] if len(fields)>5 else '.';feature_number+=1
                    for left,right in blocks:
                        dest.write(f'{fields[0]}\tOpenNGS\texon\t{left+1}\t{right}\t.\t{strand}\t.\tgene_id "interval_{feature_number}"; transcript_id "interval_{feature_number}";\n')
        return {'intervals':[artifact(out,'BED' if tool=='gtf.bed' else 'GTF',**metadata('intervals'),
            transformation='One-based closed and zero-based half-open feature conversion; no liftover',
            conversion_scope='GTF/GFF3 source/score/phase/attributes and BED names/scores/custom columns are not retained; BED12 blocks become separate generated exon features',
            inferred_transcript_biology=False)]}
    raise ValueError('Unknown reviewed format operator')
