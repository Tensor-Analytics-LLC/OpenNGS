"""Reconcile funded entitlements into a fixed, published AWS tenant template."""
import json,math,os,time
from . import db,entitlements,budget
from .config import aws_session

CORE_TOOLS={'fastqc','fastp','bwa-mem2','minimap2','samtools','bcftools','freebayes','picard','gatk-bqsr','gatk-apply-bqsr','haplotypecaller','mutect2','gatk.mt.call','gatk.mt.filter','mito.extract','mito.realign','mito.liftover','bcftools.annotate'}
def profile(uid):
    row=db.query('SELECT value FROM controls WHERE key=?',('research-profile:'+uid,),one=True)
    return json.loads(row['value']) if row else {'state':'Unconfigured','reason':'Funded tenant capacity has not been provisioned'}

def save(uid,value):
    with db.tx() as c:c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('research-profile:'+uid,db.encode(value)))

def once():
    url=os.getenv('NGS_RESEARCH_TEMPLATE_URL');role=os.getenv('NGS_RESEARCH_CFN_ROLE')
    if not url or not role:return False
    client=aws_session().client('cloudformation')
    for grant in db.query("SELECT * FROM research_allowances WHERE state='active' ORDER BY updated"):
        uid=grant['user_id'];p=profile(uid);a=json.loads(grant['body']);name='openngs-pro-'+uid[2:]
        budget.reserve_storage_capacity(uid,a)
        if p.get('grant_version')==grant['version'] and p.get('state') in ['Ready','Blocked'] and p.get('template_url')==url:continue
        if p.get('state')=='Provisioning':
            stack=client.describe_stacks(StackName=name)['Stacks'][0];status=stack['StackStatus']
            if status.endswith('_IN_PROGRESS'):continue
            if status not in ['CREATE_COMPLETE','UPDATE_COMPLETE']:
                save(uid,{**p,'state':'Blocked','grant_version':grant['version'],'reason':'Tenant stack '+status+'; preserved for operator reconciliation'});continue
            out={x['OutputKey']:x['OutputValue'] for x in stack['Outputs']}
            save(uid,{'state':'Ready','grant_version':p['grant_version'],'bucket':out['Bucket'],'queue':out['Queue'],'definition':out['Definition'],'image':out['Image'],'max_scratch_mb':int(out['ScratchMiB']),'region':'us-east-1','tools':sorted(CORE_TOOLS),'spot_only':True,'stack':name,'instance_profile':out.get('InstanceProfile'),'template_url':url})
            with db.tx() as c:db.audit(c,'capacity-service','research.capacity.ready',uid,{'stack':name,'grant_version':p['grant_version'],'scale_to_zero':True})
            continue
        # This published profile has a finite CPU fleet and cannot invent GPU or
        # high-scratch support. Larger allowances still retain their own data.
        cpu=min(16,a['max_vcpus']);fleet=cpu*min(4,a['max_concurrent_jobs']);fleet=next(v for v in [2,4,8,16,32,64] if v>=fleet)
        kwargs={'StackName':name,'TemplateURL':url,'RoleARN':role,'Parameters':[{'ParameterKey':'TenantId','ParameterValue':uid},{'ParameterKey':'StorageId','ParameterValue':uid[2:]},{'ParameterKey':'FleetVCPU','ParameterValue':str(fleet)},{'ParameterKey':'MonthlyBudget','ParameterValue':str(a['monthly_budget_usd'])}],'Capabilities':['CAPABILITY_IAM'],'Tags':[{'Key':'Project','Value':'OpenNGS-Pro'},{'Key':'TenantId','Value':uid}]}
        try:
            if p.get('stack'):client.update_stack(**kwargs)
            else:client.create_stack(**kwargs,DisableRollback=True)
        except client.exceptions.ClientError as e:
            if 'No updates are to be performed' not in str(e):raise
        save(uid,{**p,'state':'Provisioning','template_url':url,'stack':name,'grant_version':grant['version'],'reason':'Creating private storage and an idle-free, bounded Spot execution queue'})
        with db.tx() as c:db.audit(c,'capacity-service','research.capacity.provisioning',uid,{'grant_version':grant['version'],'fleet_vcpus':fleet,'funded_monthly_usd':a['monthly_budget_usd']})
    return True

if __name__=='__main__':
    while True:
        try:once()
        except Exception as e:print(db.encode({'event':'research.provisioning.retry','exception':type(e).__name__}),flush=True)
        time.sleep(15)
