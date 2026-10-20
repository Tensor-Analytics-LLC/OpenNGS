"""Qualify exact-version retention against a newly created, isolated AWS canary.

Only this script's administrative fixture uses a synthetic age/cutoff. Normal
retention and its 24-hour grace period are unchanged. Source data is preserved.
Run after publish_retention.py; --enable activates the reviewed cleanup policy
only after all assertions pass. The invocation/result remain in deployment
evidence and the audit chain.
"""
import argparse
import json
from pathlib import Path
from backend.config import aws_session

REMOTE = r'''
import hashlib,json,os,time,uuid
from backend import db,retention,entitlements
from backend.config import aws_session
from backend.postgres import close
uid=CONFIG['uid']
actor=db.query("SELECT id FROM users WHERE role='admin' ORDER BY created LIMIT 1",one=True)['id']
assert db.query('SELECT id FROM users WHERE id=?',(uid,),one=True)
s3=aws_session().client('s3');bucket=os.environ['NGS_BUCKET']
rid='retention_canary_'+uuid.uuid4().hex;aid='artifact_'+rid
prefix=f'tenants/{uid}/runs/{rid}/'
key=prefix+'outputs/qualification.txt'
source=f'tenants/{uid}/datasets/{rid}/source-preservation.txt'
put={'Bucket':bucket,'ServerSideEncryption':'aws:kms','SSEKMSKeyId':os.environ['NGS_KMS_KEY'],'BucketKeyEnabled':True}
first=s3.put_object(**put,Key=key,Body=b'exact-version-one\n')['VersionId']
body=b'exact-version-two\n'
second=s3.put_object(**put,Key=key,Body=body)['VersionId']
original=s3.put_object(**put,Key=source,Body=b'preserved retention qualification source\n')['VersionId']
assert first!=second and all(v and v!='null' for v in [first,second,original])
now=time.time();finished=now-(entitlements.limits(uid)['retention_days']+3)*retention.DAY
graph={'name':'Administrative retention qualification; no scientific analysis','nodes':[],'edges':[],
 '_execution':{'access':'research-pro','allowance':{'retention_days':7},'cloud_settings':{'s3_bucket':bucket}}}
with db.tx() as c:
 c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,finished,reserved) VALUES(?,?,?,'succeeded',?,?,?,0)",(rid,uid,rid,db.encode(graph),finished-1,finished))
 c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)',(aid,uid,rid,'retention-canary','qualification.txt','s3://'+bucket+'/'+key,'REPORT',len(body),'sha256:'+hashlib.sha256(body).hexdigest(),db.encode({'version_id':second,'administrative_canary':True}),finished))
 c.execute("INSERT INTO retention_plans VALUES(?,?,'inventory',?,?,?,?,?,NULL,0,?,?,NULL)",(rid,uid,bucket,prefix,finished,now+60,'{}',now,now))
 db.audit(c,actor,'retention.qualification.started',rid,{'synthetic_clock_fixture':True,'exact_prefix':prefix,'source_preservation_key':source,'production_grace_unchanged':True})
retention.pin(uid,rid,True,'Protect isolated retention qualification fixture')
pinned=retention.process_one(run_id=rid)
assert pinned['state']=='protected' and 'pinned' in pinned['reason']
retention.pin(uid,rid,False,'Release isolated retention qualification fixture')
with db.tx() as c:c.execute('UPDATE retention_plans SET lease_until=0 WHERE run_id=?',(rid,))
retention.lease(uid,aid,60)
leased=retention.process_one(run_id=rid)
assert leased['state']=='protected' and 'read' in leased['reason']
with db.tx() as c:
 c.execute('DELETE FROM retention_leases WHERE user_id=? AND run_id=?',(uid,rid))
 c.execute('UPDATE retention_plans SET lease_until=0 WHERE run_id=?',(rid,))
inventory=retention.process_one(run_id=rid)
assert inventory['state']=='inventory' and inventory['versions']==2,inventory
deleted=retention.process_one(run_id=rid)
assert deleted['state']=='deleted' and deleted['versions_deleted']==2,deleted
remaining=s3.list_object_versions(Bucket=bucket,Prefix=prefix)
assert not remaining.get('Versions') and not remaining.get('DeleteMarkers')
preserved=s3.head_object(Bucket=bucket,Key=source,VersionId=original)
assert preserved['VersionId']==original
assert db.query('SELECT id FROM retention_tombstones WHERE id=?',(aid,),one=True)
assert db.query('SELECT id FROM runs WHERE id=?',(rid,),one=True)
assert db.query('SELECT id FROM artifacts WHERE id=?',(aid,),one=True) is None
try:retention.ensure_available(uid,aid)
except ValueError:expired_read_blocked=True
else:raise AssertionError('Expired artifact was still available')
record={'run_id':rid,'versions_deleted':2,'pin_protected':True,'read_lease_protected':True,
 'source_version_preserved':original,'audit_and_run_preserved':True,'tombstone_preserved':True,
 'expired_read_blocked':True,'synthetic_age_fixture':True,'production_grace_hours':retention.GRACE//3600}
with db.tx() as c:db.audit(c,actor,'retention.qualification.passed',rid,record)
if CONFIG['enable']:
 retention.configure(actor,True,'Enable user-requested derived-output cleanup after live exact-version and source-preservation qualification')
record['automatic_cleanup_enabled']=retention.enabled()
print(json.dumps(record))
close()
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    private=json.loads(Path('.state/research-validation.json').read_text())
    config={'uid':private['user_id'],'enable':args.enable}
    session=aws_session()
    outputs={x['OutputKey']:x['OutputValue'] for x in session.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']}
    script="set -euo pipefail\nset -a; source /var/lib/openngs/runtime.env; set +a\ncd /opt/openngs\n/opt/openngs/runtime/bin/python - <<'RETENTION_QUALIFICATION'\nCONFIG="+repr(config)+'\n'+REMOTE+'\nRETENTION_QUALIFICATION'
    result=session.client('ssm').send_command(InstanceIds=[outputs['HostId']],DocumentName='AWS-RunShellScript',
        Parameters={'commands':[script],'executionTimeout':['180']},Comment='OpenNGS isolated exact-version retention qualification')
    record={'command_id':result['Command']['CommandId'],'host':outputs['HostId'],'enable_after_pass':args.enable}
    Path('evidence/deployment/retention-qualification-command.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))


if __name__=='__main__':
    main()
