"""Published worker contract v1. Runs inside the approved scientific image."""
import base64,glob,hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path
import boto3

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()

def main():
    bucket,key=sys.argv[1:];s3=boto3.client('s3',region_name='us-east-1')
    response=s3.get_object(Bucket=bucket,Key=key)
    if response['ContentLength']>2*1024*1024:raise ValueError('Manifest too large')
    job=json.loads(response['Body'].read());expected=f"tenants/{job['tenant']}/runs/{job['run']}/{job['attempt']}/"
    if job['prefix']!=expected or key!=expected+'manifest.json' or job['bucket']!=bucket:raise ValueError('Run/tenant manifest binding mismatch')
    Path('/work').mkdir(exist_ok=True);began=time.monotonic();deadline=job['wall_seconds']
    for item in job['inputs']:
        target=Path(item['target'])
        if not target.is_relative_to('/inputs') or '..' in target.parts or item['bucket']!=bucket or not item['key'].startswith(f"tenants/{job['tenant']}/"):raise ValueError('Invalid staging target')
        target.parent.mkdir(parents=True,exist_ok=True);s3.download_file(bucket,item['key'],str(target))
        if target.stat().st_size!=item['bytes'] or digest(target)!=item['sha256']:raise ValueError('Input integrity check failed')
    if job['plan'].get('portable_job'):Path('/work/portable-job.json').write_text(json.dumps(job['plan']['portable_job']))
    secret_fd=None
    if job.get('secret'):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        item=job['secret'];context=item['context']
        if context['tenant']!=job['tenant'] or not hasattr(os,'memfd_create'):raise ValueError('Licence memory-file profile is unavailable')
        dek=boto3.client('kms',region_name='us-east-1').decrypt(CiphertextBlob=base64.b64decode(item['key_envelope']),EncryptionContext=context)['Plaintext']
        blob=base64.b64decode(item['encrypted']);aad=json.dumps(context,sort_keys=True,separators=(',',':')).encode();plain=AESGCM(dek).decrypt(blob[:12],blob[12:],aad)
        secret_fd=os.memfd_create('ngs-license',flags=0);os.write(secret_fd,plain);os.lseek(secret_fd,0,os.SEEK_SET);plain=None;dek=None
    for step in job['plan']['steps']:
        remaining=deadline-(time.monotonic()-began)
        if remaining<=0:raise TimeoutError('Node wall limit')
        target=Path('/work')/step['stdout'] if step.get('stdout') else None
        if target and not target.resolve().is_relative_to('/work'):raise ValueError('Unsafe stdout file')
        with target.open('wb') if target else open(os.devnull,'wb') as out:
            argv=[arg.replace('/run/ngs-secrets/license','/proc/self/fd/'+str(secret_fd)) for arg in step['argv']] if secret_fd is not None else step['argv']
            subprocess.run(argv,cwd='/work',stdout=out if target or secret_fd is not None else None,stderr=subprocess.DEVNULL if secret_fd is not None else None,pass_fds=(secret_fd,) if secret_fd is not None else (),check=True,timeout=remaining)
    if secret_fd is not None:os.close(secret_fd)
    results={};output_contract=job['plan']['outputs']
    if job['plan'].get('portable_job'):
        result_file=Path('/work/result.json')
        if result_file.stat().st_size>8*1024*1024:raise ValueError('Output manifest too large')
        output_contract={port:[{'file':str(Path(i['path']).relative_to('/work')),'format':i['format'],'metadata':i.get('metadata',{})} for i in items] for port,items in json.loads(result_file.read_text()).items()}
    def upload(path):
        if path.is_symlink() or not path.resolve().is_relative_to('/work') or not path.is_file():raise ValueError('Unsafe output')
        h=digest(path);output_key=expected+'outputs/'+h+'/'+path.name
        s3.upload_file(str(path),bucket,output_key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':job['kms_key_arn'],'Metadata':{'sha256':h}})
        return 's3://'+bucket+'/'+output_key,h
    for port,descs in output_contract.items():
        results[port]=[]
        for desc in descs:
            files=sorted(Path('/work').glob(desc['file']))
            if not files:raise ValueError('Missing expected output '+desc['file'])
            for path in files:
                uri,h=upload(path);metadata=dict(desc.get('metadata',{}))
                for field in ['mate','index']:
                    if metadata.get(field):
                        original=metadata[field];secondary=Path('/work')/Path(original).name if not str(original).startswith('/work/') else Path(original)
                        secondary_uri,secondary_digest=upload(secondary);metadata[field]=secondary_uri;metadata[field+'_sha256']=secondary_digest
                results[port].append({'path':uri,'name':path.name,'format':desc['format'],'bytes':path.stat().st_size,'digest':'sha256:'+h,'metadata':metadata})
    s3.put_object(Bucket=bucket,Key=expected+'result.json',Body=json.dumps(results).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=job['kms_key_arn'])

if __name__=='__main__':main()
