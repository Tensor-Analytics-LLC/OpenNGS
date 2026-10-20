"""Verify the builder, snapshot its reviewed runtime, then terminate it."""
import json,time
from pathlib import Path
from backend.config import aws_session

def main():
    path=Path('evidence/deployment/vm-build.json');r=json.loads(path.read_text());s=aws_session();ec2=s.client('ec2');ssm=s.client('ssm')
    if not r.get('ami'):
        cmd=ssm.send_command(InstanceIds=[r['builder']],DocumentName='AWS-RunShellScript',Parameters={'commands':['test -f /var/lib/openngs-image-ready && cat /var/lib/openngs-image-digest; test ! -f /root/.docker/config.json']},Comment='OpenNGS verify image pin and absence of cached registry credentials')
        for _ in range(30):
            try:v=ssm.get_command_invocation(CommandId=cmd['Command']['CommandId'],InstanceId=r['builder'])
            except ssm.exceptions.InvocationDoesNotExist:time.sleep(1);continue
            if v['Status'] in ['Success','Failed','TimedOut','Cancelled']:break
            time.sleep(1)
        if v['Status']!='Success' or v['StandardOutputContent'].strip()!=r['image']:raise ValueError('Scientific image bake is not verified or still in progress')
        name='openngs-core-'+r['image'].rsplit(':',1)[1][:18]+'-'+str(int(r['started']))
        known=ec2.describe_images(Owners=['self'],Filters=[{'Name':'name','Values':[name]}])['Images']
        if len(known)>1:raise ValueError('Ambiguous private image identity')
        if known:r['ami']=known[0]['ImageId']
        else:
            created=ec2.create_image(InstanceId=r['builder'],Name=name,Description='OpenNGS fresh exclusive scientific worker; reviewed runtime digest pinned',NoReboot=False,TagSpecifications=[{'ResourceType':kind,'Tags':[{'Key':'Project','Value':'OpenNGS'},{'Key':'RuntimeDigest','Value':r['image'].rsplit(':',1)[1]}]} for kind in ['image','snapshot']]);r['ami']=created['ImageId']
        path.write_text(json.dumps(r,indent=2));print(json.dumps({'ami':r['ami'],'state':'snapshotting'}),flush=True)
    for _ in range(120):
        image=ec2.describe_images(ImageIds=[r['ami']])['Images'][0]
        if image['State']=='available':break
        if image['State']=='failed':raise ValueError('AMI snapshot failed; builder lifetime remains bounded')
        time.sleep(5)
    if image['State']!='available':raise ValueError('AMI has not become available; preserve its checkpoint')
    if image.get('Public'):raise ValueError('Scientific AMI must remain private')
    ec2.terminate_instances(InstanceIds=[r['builder']]);r.update(ready=True,builder_termination_requested=True,finished=time.time());path.write_text(json.dumps(r,indent=2));print(json.dumps({'ami':r['ami'],'ready':True,'builder_termination_requested':True}))

if __name__=='__main__':main()
