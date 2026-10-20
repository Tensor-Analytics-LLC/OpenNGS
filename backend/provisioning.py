"""Reconcile funded entitlements into a fixed, published AWS tenant template."""
import json,math,os,re,time
from . import db,entitlements,budget
from .config import aws_session

CORE_TOOLS={'fastqc','fastp','bwa-mem2','minimap2','samtools','bcftools','freebayes','picard','gatk-bqsr','gatk-apply-bqsr','haplotypecaller','mutect2','gatk.mt.call','gatk.mt.filter','mito.extract','mito.realign','mito.liftover','bcftools.annotate'}
def profile(uid):
    row=db.query('SELECT value FROM controls WHERE key=?',('research-profile:'+uid,),one=True)
    return json.loads(row['value']) if row else {'state':'Unconfigured','reason':'Funded tenant capacity has not been provisioned'}

def save(uid,value):
    with db.tx() as c:c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('research-profile:'+uid,db.encode(value)))

def deepvariant_routes(outputs):
    if not any(key.startswith('DeepVariant') for key in outputs):return {}
    from .deepvariant_runtime import FAMILY,published
    release=published()
    if (not release or outputs.get('DeepVariantImage')!=release['image']
            or outputs.get('DeepVariantSource')!=release['source_sha256']
            or json.loads(outputs.get('DeepVariantTools','null'))!=release['tools']
            or json.loads(outputs.get('DeepVariantModels','null'))!=release['models']
            or not outputs.get('DeepVariantDefinition')):
        raise ValueError('DeepVariant tenant definition must match the exact published image, WGS model and execution proof')
    return {'deepvariant':{'image':release['image'],'definition':outputs['DeepVariantDefinition'],
        'family':FAMILY,'source_grants_version':1,'qualified_models':release['models'],
        'caller_version':release['version'],'upstream_image':release['upstream_image']}}

def vep_routes(outputs):
    if not any(key.startswith('VEP') for key in outputs):return {}
    from .vep_runtime import FAMILY,published
    release=published()
    if (not release or outputs.get('VEPImage')!=release['image']
            or outputs.get('VEPSource')!=release['source_sha256']
            or json.loads(outputs.get('VEPTools','null'))!=release['tools']
            or outputs.get('VEPCacheVersion')!=str(release['cache_version'])
            or outputs.get('VEPAssembly')!=release['assembly']
            or outputs.get('VEPCacheManifest')!=release['cache_manifest_sha256']
            or not outputs.get('VEPDefinition')):
        raise ValueError('VEP tenant definition must match the exact published image, cache release and all five execution controls')
    return {'vep':{'image':release['image'],'definition':outputs['VEPDefinition'],
        'family':FAMILY,'source_grants_version':1,'cache_version':release['cache_version'],
        'assembly':release['assembly'],'cache_manifest_sha256':release['cache_manifest_sha256']}}

def format_routes(outputs):
    if not any(key.startswith('Formats') for key in outputs):return {}
    from .format_contract import FAMILY
    from .runtime_catalog import published_formats
    release=published_formats()
    if (not release or outputs.get('FormatsImage')!=release['image']
            or outputs.get('FormatsSource')!=release['source_sha256']
            or outputs.get('FormatsBaseImage')!=release['base_image']
            or json.loads(outputs.get('FormatsTools','null'))!=release['tools']
            or not outputs.get('FormatsDefinition')):
        raise ValueError('Format tenant definition must match the exact published image, source, base and all eight execution controls')
    return {tool:{'image':release['image'],'definition':outputs['FormatsDefinition'],
        'family':FAMILY,'source_grants_version':1,'source_sha256':release['source_sha256'],
        'base_image':release['base_image'],'version':release['version']} for tool in release['tools']}

def _vep_object(value):
    if (not isinstance(value,dict) or not re.fullmatch(r's3://[^/]+/.+',value.get('uri',''))
            or value.get('version_id') in [None,'','null']
            or not isinstance(value.get('version_id'),str)
            or not re.fullmatch('[a-f0-9]{64}',value.get('sha256',''))
            or type(value.get('bytes')) is not int or value['bytes']<=0):
        raise ValueError('VEP bundle requires immutable cloud object versions, sizes and full SHA-256 identities')
    return {key:value[key] for key in ['uri','version_id','sha256','bytes']}

def validate_vep_bundle(value):
    """A bounded transcript shard is never evidence that the full cache exists."""
    from . import vep_runtime as vep
    release=vep.published()
    if (not isinstance(value,dict) or value.get('state')!='Ready'
            or value.get('verification')!='complete-vep-cache-cloud-v1'
            or value.get('cache_version')!=vep.CACHE_VERSION or value.get('assembly')!=vep.ASSEMBLY
            or not release or value.get('image')!=release['image']
            or not re.fullmatch(r'u_[a-f0-9]{32}',value.get('owner_id',''))):
        raise ValueError('The complete original VEP 115 GRCh38 bundle has not been verified in its published cloud runtime')
    archive=_vep_object(value.get('archive'));reference=_vep_object(value.get('reference'));index=_vep_object(value.get('index'))
    owner_prefix='/tenants/'+value['owner_id']+'/datasets/'
    if owner_prefix not in archive['uri']:
        raise ValueError('VEP original cache must retain its private migration owner')
    vep.cache_binding({'name':vep.CACHE_ARCHIVE_NAME,'path':archive['uri'],'bytes':archive['bytes'],
        'sha256':archive['sha256'],'assembly':vep.ASSEMBLY,'metadata':{'version_id':archive['version_id']},'state':'Ready'})
    complete=value.get('complete_cache',{})
    if complete!={'members':vep.CACHE_MEMBER_COUNT,'extracted_bytes':vep.CACHE_EXTRACTED_BYTES,
                   'member_manifest_sha256':vep.CACHE_MANIFEST_SHA256,'info_sha256':vep.CACHE_INFO_SHA256}:
        raise ValueError('VEP full-cache verification must hash every exact original archive member')
    checks=value.get('reference_verification',{})
    if (checks.get('fasta_sha256')!=reference['sha256'] or checks.get('fai_sha256')!=index['sha256']
            or checks.get('all_fai_fields_validated') is not True
            or type(checks.get('contigs')) is not int or checks['contigs']<=0):
        raise ValueError('VEP bundle requires a complete matching FASTA and FAI verification')
    proof=value.get('execution',{})
    if (proof.get('status')!='SUCCEEDED' or not proof.get('job_id')
            or type(proof.get('scratch_bytes')) is not int or proof['scratch_bytes']<80*1024**3
            or proof.get('all_members_verified') is not True):
        raise ValueError('VEP complete-bundle cloud execution proof is required')
    _vep_object(proof.get('artifact'))
    return value

def vep_bundle():
    row=db.query('SELECT value FROM controls WHERE key=?',('reference-bundle:vep-cache',),one=True)
    try:return validate_vep_bundle(json.loads(row['value'])) if row else None
    except (ValueError,TypeError,KeyError):return None

def require_vep_bundle(cache,reference):
    from .vep_runtime import cache_binding
    cache_binding(cache)
    bundle=vep_bundle()
    if not bundle:raise ValueError('The complete original VEP cache and matching FASTA/FAI are still awaiting cloud bundle verification')
    def identity(asset):
        meta=asset.get('metadata',{})
        return {'uri':asset.get('path'),'version_id':asset.get('version_id') or meta.get('version_id'),
            'sha256':asset.get('sha256') or meta.get('sha256') or asset.get('digest','').removeprefix('sha256:'),'bytes':asset.get('bytes')}
    meta=reference.get('metadata',{})
    if (identity(cache)!=_vep_object(bundle['archive']) or identity(reference)!=_vep_object(bundle['reference'])
            or meta.get('assembly',reference.get('assembly'))!='GRCh38'
            or meta.get('index')!=bundle['index']['uri'] or meta.get('index_sha256')!=bundle['index']['sha256']):
        raise ValueError('VEP inputs must match the exact verified cache, GRCh38 FASTA and FAI bundle')
    secondaries=meta.get('secondary_objects',[])
    index_version=next((r.get('version_id') for r in secondaries if r.get('uri')==meta.get('index')),None)
    if index_version!=bundle['index']['version_id']:
        raise ValueError('VEP FAI must retain its exact verified cloud version')
    return bundle

def ensure_curated(uid,actor_id):
    from . import curated_cloud
    try:
        for source in curated_cloud.demo.SOURCES:curated_cloud.outputs(uid,source)
        return
    except ValueError:
        pass
    actor=db.query("SELECT id,role,frozen FROM users WHERE id=? AND role='admin' AND frozen=0",(actor_id,),one=True)
    if not actor:raise ValueError('An active granting administrator is needed to stage the reviewed sample release')
    curated_cloud.publish(actor,uid)

def once():
    url=os.getenv('NGS_RESEARCH_TEMPLATE_URL');role=os.getenv('NGS_RESEARCH_CFN_ROLE')
    if not url or not role:return False
    client=aws_session().client('cloudformation')
    for grant in db.query("SELECT * FROM research_allowances WHERE state='active' ORDER BY updated"):
        uid=grant['user_id'];p=profile(uid);a=json.loads(grant['body']);name='openngs-pro-'+uid[2:]
        budget.reserve_storage_capacity(uid,a)
        if p.get('grant_version')==grant['version'] and p.get('state') in ['Ready','Blocked'] and p.get('template_url')==url:
            if p['state']=='Ready':ensure_curated(uid,grant['actor'])
            continue
        if p.get('state')=='Provisioning':
            stack=client.describe_stacks(StackName=name)['Stacks'][0];status=stack['StackStatus']
            if status.endswith('_IN_PROGRESS'):continue
            if status not in ['CREATE_COMPLETE','UPDATE_COMPLETE']:
                save(uid,{**p,'state':'Blocked','grant_version':grant['version'],'reason':'Tenant stack '+status+'; preserved for operator reconciliation'});continue
            out={x['OutputKey']:x['OutputValue'] for x in stack['Outputs']}
            routes={}
            if out.get('ExtendedImage') and out.get('ExtendedDefinition'):
                from .runtime_catalog import EXTENDED_TOOLS,ADJUNCT_TOOLS,PORTABLE_TOOLS
                extra=json.loads(out.get('ExtendedTools','[]'))
                if not isinstance(extra,list) or any(t not in EXTENDED_TOOLS for t in extra):raise ValueError('Published stack contains unreviewed runtime tools')
                routes={t:{'image':out['ExtendedImage'],'definition':out['ExtendedDefinition'],'family':'extended-cpu-v1','source_grants_version':1} for t in extra}
                core=json.loads(out.get('RefreshedCoreTools','[]'))
                if not isinstance(core,list) or any(t not in CORE_TOOLS for t in core):raise ValueError('Published stack contains unreviewed core refresh routes')
                routes.update({t:{'image':out['ExtendedImage'],'definition':out['ExtendedDefinition'],'family':'core-cpu-refresh-v1','source_grants_version':1} for t in core})
                adjunct=json.loads(out.get('AdjunctTools','[]'))
                if not isinstance(adjunct,list) or any(t not in ADJUNCT_TOOLS for t in adjunct):raise ValueError('Published stack contains unreviewed additional native contracts')
                routes.update({t:{'image':out['ExtendedImage'],'definition':out['ExtendedDefinition'],'family':'extended-core-contracts-v1','source_grants_version':1} for t in adjunct})
                portable=json.loads(out.get('PortableTools','[]'))
                if not isinstance(portable,list) or any(t not in PORTABLE_TOOLS for t in portable):raise ValueError('Published stack contains unreviewed portable routes')
                routes.update({t:{'image':out['ExtendedImage'],'definition':out['ExtendedDefinition'],'family':'portable-refresh-v1','source_grants_version':1} for t in portable})
                # The internal viewer contract is not a public scientific capability;
                # its own request/admission path independently requires verification.
                routes['results.prepare']={'image':out['ExtendedImage'],'definition':out['ExtendedDefinition'],'family':'extended-cpu-v1','source_grants_version':1,'internal':True}
            if out.get('SecondaryImage') and out.get('SecondaryDefinition'):
                from .secondary_runtime import TOOLS as SECONDARY_TOOLS
                secondary=json.loads(out.get('SecondaryTools','[]'))
                if not isinstance(secondary,list) or any(t not in SECONDARY_TOOLS for t in secondary):raise ValueError('Unreviewed secondary runtime route')
                routes.update({t:{'image':out['SecondaryImage'],'definition':out['SecondaryDefinition'],'family':'secondary-cpu-v1','source_grants_version':1} for t in secondary})
            routes.update(deepvariant_routes(out))
            routes.update(vep_routes(out))
            routes.update(format_routes(out))
            save(uid,{'state':'Ready','grant_version':p['grant_version'],'bucket':out['Bucket'],'queue':out['Queue'],'definition':out['Definition'],'image':out['Image'],'max_scratch_mb':int(out['ScratchMiB']),'region':'us-east-1','tools':sorted(CORE_TOOLS|set(routes)),'runtime_profiles':routes,'spot_only':True,'stack':name,'instance_profile':out.get('InstanceProfile'),'template_url':url})
            with db.tx() as c:db.audit(c,'capacity-service','research.capacity.ready',uid,{'stack':name,'grant_version':p['grant_version'],'scale_to_zero':True})
            ensure_curated(uid,grant['actor'])
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
