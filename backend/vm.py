"""Fresh exclusive VM per high-scratch stage. No shared hosts or reused workers.

RunInstances' client token is the durable launch identity. An independent guest
shutdown timer, application reaper and independent AWS deadline reaper bound launches. Only a published
private AMI, tenant instance profile and isolated subnet can be used.
"""
import json,math,os,shlex,time
from . import db
from .config import aws_session

PROFILES=[{'instance_type':'t3.medium','vcpus':2,'memory_mb':4096,'hourly_usd':.0416},
          {'instance_type':'m7i.large','vcpus':2,'memory_mb':8192,'hourly_usd':.1008},
          {'instance_type':'m7i.xlarge','vcpus':4,'memory_mb':16384,'hourly_usd':.2016},
          {'instance_type':'m7i.2xlarge','vcpus':8,'memory_mb':32768,'hourly_usd':.4032},
          {'instance_type':'m7i.4xlarge','vcpus':16,'memory_mb':65536,'hourly_usd':.8064}]
def select(resources):
    if resources.get('scratch_mb',2048)>2*1024**2:raise ValueError('Published VM scratch limit is 2 TiB')
    p=next((dict(p) for p in PROFILES if p['vcpus']>=resources['threads'] and p['memory_mb']>=resources['memory_mb'] and (not p['instance_type'].startswith('t3.') or resources.get('timeout_seconds',120)<=300)),None)
    if not p:raise ValueError('No reviewed VM profile fits these CPU/RAM requirements')
    p['scratch_gib']=max(32,math.ceil(resources.get('scratch_mb',2048)/1024));return p

def configuration():
    return {k:os.getenv(v,'') for k,v in {'ami':'NGS_VM_AMI','image':'NGS_VM_IMAGE','subnet':'NGS_VM_SUBNET','security_group':'NGS_VM_SECURITY'}.items()}

def userdata(image,bucket,key,resources,profile,log_key):
    q=shlex.quote;seconds=resources['timeout_seconds'];disk_bytes=profile['scratch_gib']*1024**3
    # All values are pinned platform identifiers or preflight-validated integers.
    # No scientific parameter, filename, CLI flag or secret enters this shell.
    return '''#!/bin/bash
set -euo pipefail
shutdown -h +'''+str(math.ceil((seconds+900)/60))+'''
export AWS_DEFAULT_REGION=us-east-1
finish() {
  docker rm -f openngs-stage >/dev/null 2>&1 || true
  aws s3 cp /var/log/openngs-stage.log '''+q('s3://'+bucket+'/'+log_key)+''' --only-show-errors || true
  shutdown -h now
}
trap finish EXIT
exec > /var/log/openngs-stage.log 2>&1
systemctl start docker
test "$(cat /var/lib/openngs-image-digest)" = '''+q(image)+'''
disk=$(python3 - <<'DISK'
import json,subprocess
items=json.loads(subprocess.check_output(['lsblk','-b','-J','-o','NAME,TYPE,SIZE']))['blockdevices']
matches=[x['name'] for x in items if x['type']=='disk' and not x.get('children') and int(x['size'])=='''+str(disk_bytes)+''']
if len(matches)!=1:raise SystemExit('Expected exactly one fresh scratch disk')
print('/dev/'+matches[0])
DISK
)
mkfs.ext4 -F "$disk"
mkdir -p /work
mount -o nodev,nosuid "$disk" /work
mkdir -p /work/job /work/inputs
chown 1000:1000 /work/job /work/inputs
timeout --kill-after=30 '''+str(seconds)+''' docker run --name openngs-stage --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --cpus '''+str(resources['threads'])+''' --memory '''+str(resources['memory_mb'])+'''m --memory-swap '''+str(resources['memory_mb'])+'''m --ulimit core=0 --user 1000:1000 --tmpfs /tmp:rw,nosuid,nodev,size=1g --mount type=bind,source=/work/job,target=/work --mount type=bind,source=/work/inputs,target=/inputs '''+q(image)+''' python /opt/studio/cloud_worker.py '''+q(bucket)+' '+q(key)+'\n'

def execute(uid,rid,aid,node,settings,manifest_key,cancelled):
    from .cloud import UncertainCloudState
    config=node.get('_vm_configuration') or configuration();capacity=node.get('_capacity',{})
    if not all(config.values()) or not capacity.get('instance_profile'):raise ValueError('The exclusive VM profile has not been published for this tenant')
    if config['image']!=node['_approved_image']:raise ValueError('Published VM runtime differs from the frozen node image; no mutable fallback is allowed')
    p=select(node['resources']);ec2=aws_session().client('ec2');prefix=manifest_key.rsplit('/',1)[0]+'/';log_key=prefix+'worker.log'
    script=userdata(config['image'],settings['s3_bucket'],manifest_key,node['resources'],p,log_key)
    row=db.query('SELECT provider_id,started,metrics FROM attempts WHERE id=?',(aid,),one=True);iid=row['provider_id'] if row else None
    if not iid:
        tags=[{'Key':'Project','Value':'OpenNGS-worker'},{'Key':'TenantId','Value':uid},{'Key':'RunId','Value':rid},{'Key':'AttemptId','Value':aid},{'Key':'Deadline','Value':str(int(row['started']+node['resources']['timeout_seconds']+900))}]
        request={'ImageId':config['ami'],'InstanceType':p['instance_type'],'MinCount':1,'MaxCount':1,'ClientToken':aid,'SubnetId':config['subnet'],'SecurityGroupIds':[config['security_group']],'IamInstanceProfile':{'Arn':capacity['instance_profile']},'UserData':script,'InstanceInitiatedShutdownBehavior':'terminate','MetadataOptions':{'HttpTokens':'required','HttpPutResponseHopLimit':2},'BlockDeviceMappings':[{'DeviceName':'/dev/xvda','Ebs':{'DeleteOnTermination':True,'Encrypted':True}},{'DeviceName':'/dev/sdf','Ebs':{'VolumeSize':p['scratch_gib'],'VolumeType':'gp3','Encrypted':True,'DeleteOnTermination':True}}],'TagSpecifications':[{'ResourceType':kind,'Tags':tags} for kind in ['instance','volume','network-interface']]}
        if p['instance_type'].startswith('t3.'):request['CreditSpecification']={'CpuCredits':'standard'}
        stored=json.loads(row['metrics']).get('vm_request')
        if stored:request=stored
        with db.tx() as c:
            prior=c.execute('SELECT metrics FROM attempts WHERE id=?',(aid,)).fetchone();metrics=json.loads(prior['metrics']);metrics.update(submission_intent=True,vm_client_token=aid,vm_request=request)
            c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode(metrics),aid))
        try:response=ec2.run_instances(**request)
        except Exception as e:raise UncertainCloudState('VM launch requires reconciliation by immutable client token '+aid+'; accepted reservation is retained') from e
        iid=response['Instances'][0]['InstanceId']
        with db.tx() as c:c.execute('UPDATE attempts SET provider_id=? WHERE id=?',(iid,aid))
    started=time.monotonic()
    while True:
        try:response=ec2.describe_instances(InstanceIds=[iid]);instance=response['Reservations'][0]['Instances'][0]
        except Exception as e:raise UncertainCloudState('Cannot confirm the exclusive VM state; its fixed client token, deadline and reservation are retained') from e
        state=instance['State']['Name'];tags={x['Key']:x['Value'] for x in instance.get('Tags',[])}
        if tags.get('TenantId')!=uid or tags.get('AttemptId')!=aid:raise ValueError('VM execution identity differs from the frozen attempt')
        db.event(rid,'task.metrics',node['id'],provider='exclusive EC2 VM',provider_id=iid,provider_status=state,elapsed_seconds=round(time.monotonic()-started,1),allocated_vcpus=p['vcpus'],allocated_memory_mb=p['memory_mb'],scratch_gib=p['scratch_gib'])
        if state=='terminated':break
        deadline=int(tags.get('Deadline','0'))
        if cancelled(rid) or time.time()>deadline:
            ec2.terminate_instances(InstanceIds=[iid]);db.event(rid,'log',node['id'],text='Exclusive worker termination requested; waiting for provider confirmation')
        time.sleep(5)
    from . import metering
    metering.record(aid,metering.exclusive(instance,p,time.time()))
    if cancelled(rid):raise InterruptedError('Exclusive worker termination confirmed')
    try:
        s3=aws_session().client('s3');s3.head_object(Bucket=settings['s3_bucket'],Key=prefix+'result.json')
    except Exception as e:raise ValueError('The exclusive VM terminated without a committed output manifest; stage diagnostics are retained privately in S3') from e
    db.event(rid,'log',node['id'],text='Fresh VM terminated; immutable output manifest is ready for validation')
    return iid
