"""Build a reviewed sample image in an existing OpenNGS compute stack."""
import hashlib
import json
import zipfile
from pathlib import Path
from backend.config import aws_session

def main():
    root=Path.cwd();session=aws_session();c=session.client('cloudformation')
    deployment=json.loads((root/'evidence/deployment/release.json').read_text())
    archive=root/'evidence/deployment/demo-build.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for directory in ['backend','demo-data']:
            for p in sorted((root/directory).rglob('*')):
                if p.is_file() and '__pycache__' not in p.parts:z.write(p,str(p.relative_to(root)))
        for name in ['infra/demo.Dockerfile','infra/demo_worker.py','requirements.lock']:z.write(root/name,name)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest();tag=digest[:20];key='operations/builds/'+tag+'/source.zip'
    session.client('s3').upload_file(str(archive),deployment['bucket'],key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':deployment['kms']})
    response=c.get_template(StackName='openngs-demo-compute');template=response['TemplateBody']
    if isinstance(template,str):template=json.loads(template)
    template['Resources']['Build']['Properties']['Source']['Location']=deployment['bucket']+'/'+key
    statements=template['Resources']['BuildRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    for statement in statements:
        if 's3:GetObject' in statement['Action']:statement['Resource']='arn:aws:s3:::'+deployment['bucket']+'/'+key
    c.update_stack(StackName='openngs-demo-compute',TemplateBody=json.dumps(template),Parameters=[{'ParameterKey':'ImageTag','ParameterValue':tag},{'ParameterKey':'WorkerImage','UsePreviousValue':True}],Capabilities=['CAPABILITY_IAM'])
    path=root/'evidence/deployment/compute.json';record=json.loads(path.read_text());record.update(source_key=key,image_tag=tag,source_sha256=digest)
    path.write_text(json.dumps(record,indent=2));print(json.dumps({'source_sha256':digest,'image_tag':tag,'status':'reviewed-source-update-started'}))

if __name__=='__main__':main()
