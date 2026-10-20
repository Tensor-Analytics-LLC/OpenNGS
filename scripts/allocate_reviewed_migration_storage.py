"""Audit/apply the single reviewed baseline migration storage allocation.

Default is read-only. --apply is an operator action, not a user entitlement or
funding grant. Registered immutable source plan, owner, bytes and count must all
match the reviewed scope. No job, upload, source or reservation is altered.
"""
import argparse
import json
from pathlib import Path
from backend.config import aws_session

APPROVED_PLAN = 'migration_765d67a4a7ddd10bfd877595'
APPROVED_SHA = '41047a533687c94988ab048ea13d7877067b564b1c636ce6c0bf76e695b20ea4'
APPROVED_BYTES = 473449575292
APPROVED_OBJECTS = 10828
REMOTE = r'''
import json
from backend import db,budget,migration,entitlements
from backend.postgres import close
try:
 row=db.query('SELECT value FROM controls WHERE key=?',('migration:plan:'+EXPECTED['plan_id'],),one=True)
 if not row:raise ValueError('Reviewed migration is not registered')
 registration=json.loads(row['value'])
 if registration['sha256']!=EXPECTED['sha256']:raise ValueError('Registered source plan differs from reviewed checksum')
 plan=migration.load_plan(registration)
 if plan['total_bytes']!=EXPECTED['bytes'] or len(plan['objects'])!=EXPECTED['objects']:raise ValueError('Migration scope differs from reviewed allocation')
 owner=plan['owner_id'];admin=db.query('SELECT role FROM users WHERE id=?',(owner,),one=True)
 if not admin or admin['role']!='admin':raise ValueError('Immutable migration owner is not the reviewed administrator')
 before=entitlements.research(owner)
 if APPLY:
  budget.allocate_baseline_storage(owner,plan['plan_id'],'Reviewed exact source inventory already included within the 600 GiB baseline fixed forecast; no additional execution access or funding granted')
 if entitlements.research(owner)!=before:raise RuntimeError('Unexpected entitlement change')
 with db.tx() as c:
  allocations=budget.baseline_storage(owner,c)
  accounting={'allocated_baseline_bytes':sum(int(a['bytes']) for a in allocations),
              'verified_stored_baseline_bytes':budget.baseline_stored_bytes(owner,c),
              'physical_tenant_bytes':entitlements.storage_exposure(c,owner),
              'explore_output_usage_bytes':entitlements.output_storage_exposure(c,owner,'explore'),
              'baseline_capacity_bytes':int(min(600,budget.policy()['max_storage_gib'])*1024**3)}
 print(json.dumps({'applied':APPLY,'plan_id':plan['plan_id'],'plan_sha256':registration['sha256'],
   'accounting':accounting,'entitlement_changed':False,'cost':budget.snapshot(),
   'migration':migration.status(owner)},sort_keys=True))
finally:close()
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    session=aws_session()
    stack=session.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]
    host=next(x['OutputValue'] for x in stack['Outputs'] if x['OutputKey']=='HostId')
    expected={'plan_id':APPROVED_PLAN,'sha256':APPROVED_SHA,'bytes':APPROVED_BYTES,'objects':APPROVED_OBJECTS}
    script="set -euo pipefail\nset -a; source /var/lib/openngs/runtime.env; set +a\ncd /opt/openngs\n/opt/openngs/runtime/bin/python - <<'STORAGE_ALLOCATION'\nEXPECTED="+repr(expected)+'\nAPPLY='+repr(args.apply)+'\n'+REMOTE+'\nSTORAGE_ALLOCATION'
    response=session.client('ssm').send_command(InstanceIds=[host],DocumentName='AWS-RunShellScript',
        Parameters={'commands':[script],'executionTimeout':['120']},Comment='OpenNGS reviewed migration baseline accounting '+('apply' if args.apply else 'read-only audit'))
    result={'command_id':response['Command']['CommandId'],'apply':args.apply,'plan_id':APPROVED_PLAN}
    Path('.state/migration/baseline-allocation-command.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':main()
