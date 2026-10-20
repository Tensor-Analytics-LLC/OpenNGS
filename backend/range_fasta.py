"""Bounded FAI arithmetic for uncompressed private S3 FASTA region previews.

No whole-reference download, shell, arbitrary URL fetch or automatic indexing.
Authorization is performed by workbench.source before constructing this reader.
"""
import re

class S3Fasta:
    def __init__(self,s3,bucket,key,index_key,version_id=None):
        self.s3,self.bucket,self.key,self.version_id=s3,bucket,key,version_id
        if key.endswith(('.gz','.bgz','.bgzf')):raise ValueError('BGZF reference preview needs the isolated GZI-aware workbench profile')
        response=s3.get_object(Bucket=bucket,Key=index_key)
        try:body=response['Body'].read(4*1024**2+1)
        finally:response['Body'].close()
        if len(body)>4*1024**2:raise ValueError('FAI exceeds the reference preview index limit')
        self.entries={}
        for line in body.decode('ascii').splitlines():
            fields=line.split('\t')
            if len(fields)!=5 or not re.fullmatch(r'[^\s\x00-\x1f]{1,256}',fields[0]):raise ValueError('Invalid FASTA index row')
            length,offset,bases,width=map(int,fields[1:])
            if fields[0] in self.entries or length<=0 or offset<0 or not 0<bases<=width<=bases+2:raise ValueError('Invalid FASTA index coordinates')
            self.entries[fields[0]]=(length,offset,bases,width)
            if len(self.entries)>100000:raise ValueError('Too many preview reference contigs')
        if not self.entries:raise ValueError('Reference index is empty')
        self.references=tuple(self.entries)

    def get_reference_length(self,chrom):return self.entries[chrom][0]

    def fetch(self,chrom,start,end):
        length,offset,bases,width=self.entries[chrom]
        if not 0<=start<end<=length or end-start>2000:raise ValueError('Reference preview must span 1–2000 existing bases')
        first=offset+(start//bases)*width+start%bases
        last=offset+((end-1)//bases)*width+(end-1)%bases
        if last-first+1>6000:raise ValueError('Reference byte range exceeds the preview limit')
        args={'Bucket':self.bucket,'Key':self.key,'Range':f'bytes={first}-{last}'}
        if self.version_id:args['VersionId']=self.version_id
        response=self.s3.get_object(**args)
        try:body=response['Body'].read(last-first+2)
        finally:response['Body'].close()
        if len(body)!=last-first+1:raise ValueError('Reference byte range is incomplete')
        sequence=body.replace(b'\r',b'').replace(b'\n',b'')
        if len(sequence)!=end-start or not re.fullmatch(b'[ACGTURYSWKMBDHVNacgturyswkmbdhvn.-]+',sequence):raise ValueError('FASTA content does not match its bound index')
        return sequence.decode('ascii')

    def __enter__(self):return self
    def __exit__(self,*exc):return False
