"""Acquire an exact approved public-object plan inside AWS, with conditional copies.

No local download, latest-release substitution, deletion or SSD dependency. The
checkpoint records each immutable destination version and AWS SHA-256 checksum.
Readiness of an assembly bundle still requires matching indices and validation.
"""
import base64,json,time
from pathlib import Path
from backend.config import aws_session

def main():
    s=aws_session();client=s.client('s3');release=json.loads(Path('evidence/deployment/release.json').read_text())
    plan=json.loads(Path('evidence/deployment/reference-plan.json').read_text());path=Path('evidence/deployment/reference-acquisition.json')
    result=json.loads(path.read_text()) if path.exists() else {'schema':1,'objects':[],'completed':False}
    total=sum(x['bytes'] for x in plan);monthly=total/1024**3*.023
    if total>6*1024**3 or monthly>.15:raise ValueError('This reviewed acquisition permits at most 6 GiB and $0.15/month of new reference storage')
    for item in plan:
        prior=next((x for x in result['objects'] if x['source_bucket']==item['bucket'] and x['source_key']==item['key'] and x['source_etag']==item['etag']),None)
        if prior:
            head=client.head_object(Bucket=release['bucket'],Key=prior['key'],VersionId=prior['version_id'],ChecksumMode='ENABLED')
            if head['ContentLength']==item['bytes'] and head.get('ChecksumSHA256')==prior['checksum_sha256']:continue
            raise ValueError('Previously acquired immutable reference changed')
        key='operations/checkpoints/references/'+item['bucket']+'/'+item['key']
        copied=client.copy_object(Bucket=release['bucket'],Key=key,CopySource={'Bucket':item['bucket'],'Key':item['key']},CopySourceIfMatch=item['etag'],ServerSideEncryption='aws:kms',SSEKMSKeyId=release['kms'],ChecksumAlgorithm='SHA256',MetadataDirective='REPLACE',Metadata={'source-etag':item['etag'].strip('"'),'source-bucket':item['bucket']})
        version=copied['VersionId'];head=client.head_object(Bucket=release['bucket'],Key=key,VersionId=version,ChecksumMode='ENABLED')
        if head['ContentLength']!=item['bytes'] or not head.get('ChecksumSHA256'):raise ValueError('Destination integrity metadata is incomplete')
        result['objects'].append({'source_bucket':item['bucket'],'source_key':item['key'],'source_etag':item['etag'],'source_version':item['version'],'bucket':release['bucket'],'key':key,'version_id':version,'bytes':head['ContentLength'],'checksum_sha256':head['ChecksumSHA256'],'checksum_type':head.get('ChecksumType','FULL_OBJECT'),'acquired':time.time()})
        path.write_text(json.dumps(result,indent=2));print(json.dumps({'acquired':item['key'],'bytes':item['bytes'],'version_pinned':True}),flush=True)
    result.update(completed=True,total_bytes=total,monthly_s3_usd=monthly,scope='Exact bytes copied with source ETag precondition; full aligner/annotation/benchmark bundle validation remains separate')
    path.write_text(json.dumps(result,indent=2));client.put_object(Bucket=release['bucket'],Key='operations/checkpoints/references/acquisition-v1.json',Body=json.dumps(result).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=release['kms'])
    print(json.dumps({'complete':True,'bytes':total,'monthly_s3_usd':monthly}))

if __name__=='__main__':main()
