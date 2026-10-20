"""Bounded data interchange across the untrusted genomic parser boundary."""
import json
import math

VERSION = 1
MAX_LINE = 1024 * 1024
COLUMNS = 'chrom pos ref alt gene consequence clinical classification af cadd revel alphamissense spliceai dp gq vaf qual zygosity inheritance oncokb cosmic hpo info vcf'.split()
NUMERIC = {'af', 'cadd', 'revel', 'alphamissense', 'spliceai', 'dp', 'gq', 'vaf', 'qual', 'cosmic'}
SCHEMA = 'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE variants(id INTEGER PRIMARY KEY,' + ','.join(k+' '+('INTEGER' if k=='pos' else 'REAL' if k in NUMERIC else 'TEXT') for k in COLUMNS) + ');'
INDICES = 'CREATE INDEX locus ON variants(chrom,pos,id); CREATE INDEX frequency ON variants(af); CREATE INDEX gene_index ON variants(gene); CREATE INDEX clinical_index ON variants(classification); CREATE INDEX consequence_index ON variants(consequence);'

def decode(line):
    if len(line) > MAX_LINE:
        raise ValueError('Viewer record exceeds its 1 MiB interchange limit; no truncated index is published')
    return json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite viewer value')))

def validate_row(value):
    if not isinstance(value, list) or len(value) != len(COLUMNS):
        raise ValueError('Invalid viewer allele row')
    for key, item in zip(COLUMNS, value):
        if key == 'pos':
            if type(item) is not int or not 1 <= item <= 2**53:
                raise ValueError('Invalid genomic position')
        elif key in NUMERIC:
            if item is not None and (type(item) not in (int, float) or not math.isfinite(item)):
                raise ValueError('Invalid numeric annotation')
        elif not isinstance(item, str) or len(item.encode()) > MAX_LINE:
            raise ValueError('Invalid annotation text')
    info = decode(value[COLUMNS.index('info')])
    if not isinstance(info, dict):
        raise ValueError('Invalid allele evidence')
    return value

def validate_metadata(value):
    if not isinstance(value, dict) or value.get('schema_version') != VERSION:
        raise ValueError('Unknown viewer interchange schema')
    if not isinstance(value.get('header'), str) or not value['header'].startswith('##fileformat=VCF'):
        raise ValueError('Missing VCF provenance header')
    if len(value['header'].encode()) > MAX_LINE or not isinstance(value.get('annotation_schema'), dict):
        raise ValueError('Invalid viewer header')
    return value

def validate_result(operation, value):
    """Reject structural and numeric abuse before UI data enters the app DB."""
    if not isinstance(value, dict):
        raise ValueError('Viewer result must be an object')
    def string(v, maximum=8192):
        if not isinstance(v, str) or len(v) > maximum:
            raise ValueError('Invalid viewer text')
    def numeric(v, nullable=False):
        if nullable and v is None:return
        if type(v) not in (int,float) or not math.isfinite(v):
            raise ValueError('Invalid viewer number')
    def integer(v, minimum=0, maximum=2**53):
        if type(v) is not int or not minimum<=v<=maximum:
            raise ValueError('Invalid viewer integer or coordinate')
    def boolean(v):
        if type(v) is not bool:
            raise ValueError('Invalid viewer boolean')
    def sequence(v, maximum):
        if not isinstance(v,list) or len(v)>maximum:
            raise ValueError('Viewer collection limit exceeded')
        return v
    if operation=='header':
        string(value.get('format'),40)
        metadata=value.get('metadata')
        if not isinstance(metadata,dict):raise ValueError('Invalid inspected metadata')
        if metadata.get('header_inspected') is not True:
            raise ValueError('Header inspection is not complete')
        for key,v in metadata.items():
            if key in {'samples','info_fields','contig_preview'}:
                for item in sequence(v,10000):string(item)
            elif key=='contig_count':integer(v)
            elif key=='header_inspected':
                if type(v) is not bool:raise ValueError('Invalid header inspection state')
            elif key in {'sort_order','contig_style','assembly_verification'}:
                if v is not None:string(v)
            else:raise ValueError('Unknown inspected metadata')
    elif operation=='pileup':
        string(value.get('chrom'));string(value.get('reference'),2000)
        for key in ['start','end']:integer(value.get(key))
        if not 0<=value['start']<value['end'] or value['end']-value['start']>2000:
            raise ValueError('Invalid viewer region')
        span=value['end']-value['start']
        if len(value['reference'])!=span:
            raise ValueError('Reference sequence does not match the displayed region')
        boolean(value.get('truncated'))
        for key in ['coverage','forward','reverse']:
            items=sequence(value.get(key),2000)
            if len(items)!=span:raise ValueError('Mismatched coverage coordinates')
            for item in items:integer(item,0,300)
        if any(total!=forward+reverse for total,forward,reverse in zip(value['coverage'],value['forward'],value['reverse'])):
            raise ValueError('Coverage strand counts disagree')
        for read in sequence(value.get('reads'),300):
            if not isinstance(read,dict):raise ValueError('Invalid read')
            for key in ['name','cigar']:string(read.get(key) or '')
            for key in ['start','end']:integer(read.get(key))
            if read['end']<read['start']:raise ValueError('Invalid read span')
            integer(read.get('mapq'),0,255)
            for key in ['reverse','duplicate']:boolean(read.get(key))
            for base in sequence(read.get('bases'),2000):
                if not isinstance(base,list) or len(base)!=3:raise ValueError('Invalid pileup base')
                integer(base[0],value['start'],value['end']-1);string(base[1],1)
                if len(base[1])!=1:raise ValueError('Invalid pileup base symbol')
                if base[2] is not None:integer(base[2],0,255)
        for key in ['coverage_scope','transport']:string(value.get(key))
    elif operation=='omics':
        string(value.get('method'));integer(value.get('limit'),1,20000)
        if 'embeddings' in value:
            if not isinstance(value['embeddings'],dict) or set(value['embeddings'])-{'X_umap','X_tsne','X_pca'}:
                raise ValueError('Unknown embedding')
            for points in value['embeddings'].values():
                for point in sequence(points,value['limit']):
                    if not isinstance(point,list) or len(point)!=2:raise ValueError('Invalid embedding point')
                    for v in point:numeric(v)
        else:
            for key in ['columns','numeric','matrix_columns','row_labels']:
                for v in sequence(value.get(key),1000):string(v)
            for row in sequence(value.get('matrix'),200):
                for v in sequence(row,50):numeric(v,True)
            for point in sequence(value.get('points'),value['limit']):
                if not isinstance(point,dict):raise ValueError('Invalid point')
                numeric(point.get('x'));numeric(point.get('y'));string(point.get('label'))
                row=point.get('row')
                if not isinstance(row,dict) or len(row)>1000:raise ValueError('Invalid chart row')
                for k,v in row.items():
                    string(k)
                    if v is not None:string(v)
    else:
        raise ValueError('Unsupported typed viewer result')
    return value
