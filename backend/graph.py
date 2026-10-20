import copy
import hashlib
import json
import math
import re
import shlex
from collections import deque
from . import db,registry,data,security,compiler
from .config import CLOUD

def graph_digest(graph):
    scientific={'nodes':[{k:v for k,v in n.items() if k not in ['x','y','selected']} for n in graph['nodes']],'edges':graph['edges']}
    if 'maximum_output_bytes' in graph:scientific['maximum_output_bytes']=graph['maximum_output_bytes']
    return 'sha256:'+hashlib.sha256(db.encode(scientific).encode()).hexdigest()

def adapter_path(source,target):
    if registry.compatible(source,target):return []
    queue=deque([(source,[]) ]);seen={source}
    for_limit=0
    while queue and for_limit<100:
        current,path=queue.popleft();for_limit+=1
        for n in registry.NODES:
            if n['group']!='Adapters' or len(n['inputs'])!=1 or len(n['outputs'])!=1:continue
            if not registry.compatible(current,n['inputs'][0]['type']):continue
            output=n['outputs'][0]['type']
            if output in seen:continue
            route=path+[n['id']]
            if registry.compatible(output,target):return route
            seen.add(output);queue.append((output,route))
    return None

def validate_shape(graph):
    if not isinstance(graph,dict) or not isinstance(graph.get('nodes'),list) or not isinstance(graph.get('edges'),list):raise ValueError('Graph must have node and edge arrays')
    if len(graph['nodes'])>2000 or len(graph['edges'])>12000:raise ValueError('Graph exceeds the supported editing envelope')
    if len(db.encode(graph))>1800000:raise ValueError('Graph payload is too large')
    if 'maximum_output_bytes' in graph and (type(graph['maximum_output_bytes'])!=int or not 0<graph['maximum_output_bytes']<=2**53):raise ValueError('Output reservation must be a positive exact byte count within the supported graph envelope')
    ids=set()
    for n in graph['nodes']:
        if not isinstance(n,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(n.get('id',''))):raise ValueError('Invalid node ID')
        if n['id'] in ids:raise ValueError('Duplicate node ID')
        ids.add(n['id'])
        if not isinstance(n.get('tool'),str):raise ValueError('Node tool is required')
        for k in ['x','y']:
            if not isinstance(n.get(k,0),(int,float)) or not math.isfinite(n.get(k,0)) or abs(n.get(k,0))>1e7:raise ValueError('Invalid node coordinates')
        if not isinstance(n.get('params',{}),dict) or not isinstance(n.get('resources',{}),dict):raise ValueError('Parameters and resources must be objects')
    eids=set()
    for e in graph['edges']:
        if not isinstance(e,dict) or any(not isinstance(e.get(k),str) for k in ['id','source','sourcePort','target','targetPort']):raise ValueError('Invalid edge')
        if e['id'] in eids:raise ValueError('Duplicate edge ID')
        eids.add(e['id'])
    return graph

def output_type(n,spec,uid,port_key):
    p=next((x for x in spec['outputs'] if x['key']==port_key),None)
    if not p:return None
    if n['tool']=='asset.file':
        aid=n.get('params',{}).get('asset_id')
        row=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(aid,uid),one=True)
        if row:
            from .migration import resolve_asset
            row=resolve_asset(uid,row)
        if not row:row=db.query('SELECT format FROM artifacts WHERE id=? AND user_id=?',(aid,uid),one=True)
        return row['format'] if row else 'UNBOUND'
    return p['type']

def preflight(graph,user,capabilities=None):
    validate_shape(graph)
    from .demo import validate as validate_demo
    validate_demo(graph,user['id'])
    specs=registry.definitions(user['id']);nodes={n['id']:n for n in graph['nodes']};issues=[];warnings=[];links=[]
    order=[];degree={k:0 for k in nodes};adj={k:[] for k in nodes};incoming={k:[] for k in nodes};settings=data.settings(user['id'])
    def issue(code,message,node=None,fix=None):issues.append({'code':code,'message':message,'node_id':node,'fix':fix})
    if not nodes:issue('EMPTY_GRAPH','Add a sample or asset and an analysis node.')
    if user['frozen']:issue('TENANT_FROZEN','This workspace is frozen. An administrator must release it.')
    global_freeze=db.query("SELECT value FROM controls WHERE key='global_freeze'",one=True)
    if global_freeze and global_freeze['value']=='true':issue('GLOBAL_FREEZE','Execution admission is frozen by the administrator.')
    for e in graph['edges']:
        if e['source'] not in nodes or e['target'] not in nodes:issue('DANGLING_EDGE','A connection refers to a deleted node.',fix={'action':'remove_edge','edge_id':e['id']});continue
        a=nodes[e['source']];b=nodes[e['target']];sa=specs.get(a['tool']);sb=specs.get(b['tool'])
        if not sa or not sb:continue
        source=output_type(a,sa,user['id'],e['sourcePort']);target=next((p for p in sb['inputs'] if p['key']==e['targetPort']),None)
        if not source or not target:issue('UNKNOWN_PORT','A connection uses a nonexistent port.',e['target'],{'action':'remove_edge','edge_id':e['id']});continue
        incoming[e['target']].append(e);degree[e['target']]+=1;adj[e['source']].append(e['target'])
        if source=='UNBOUND':issue('ASSET_REQUIRED','Select an existing asset in the source node.',a['id'])
        elif not registry.compatible(source,target['type']):
            route=adapter_path(source,target['type'])
            issue('FORMAT_MISMATCH',f"{sa['name']} emits {source}; {sb['name']} requires {target['type']}.",b['id'],{'action':'insert_adapters','edge_id':e['id'],'tools':route} if route else None)
        links.append((e['source'],e['target']))
    q=deque(k for k,v in degree.items() if v==0)
    while q:
        k=q.popleft();order.append(k)
        for v in adj[k]:
            degree[v]-=1
            if degree[v]==0:q.append(v)
    if len(order)!=len(nodes):issue('CYCLE','The pipeline contains a cycle. Remove a feedback connection; analyses execute as a DAG.')
    reservation=0;licenses={};reference_builds=set()
    migrated_inputs=False
    if CLOUD:
        source_ids=[n.get('params',{}).get('asset_id') for n in graph['nodes'] if n.get('params',{}).get('asset_id')]
        if source_ids:
            marks=','.join('?' for _ in source_ids)
            migrated_inputs=bool(db.query('SELECT asset_id FROM migration_assets WHERE user_id=? AND (asset_id IN ('+marks+') OR canonical_asset_id IN ('+marks+')) LIMIT 1',(user['id'],*source_ids,*source_ids),one=True))
    for n in graph['nodes']:
        spec=specs.get(n['tool'])
        if not spec:issue('UNKNOWN_TOOL','This node is not present in the published registry.',n['id']);continue
        if n.get('execution_profile') not in [None,'default','exclusive-vm']:issue('EXECUTION_PROFILE','Choose a published default or exclusive-VM execution profile.',n['id'])
        if spec.get('quarantined'):issue('QUARANTINED',spec['quarantined'],n['id'])
        licenses[spec['license']]=licenses.get(spec['license'],0)+1
        for p in spec['inputs']:
            es=[e for e in incoming[n['id']] if e['targetPort']==p['key']]
            if p['required'] and not es:issue('MISSING_INPUT',f"Connect the {p['key']} input of {spec['name']}.",n['id'])
            if not p.get('many') and len(es)>1:issue('MULTIPLE_INPUTS',f"{p['key']} accepts one connection. Use Gather for a collection.",n['id'])
        params={**registry.defaults(spec),**n.get('params',{})}
        if spec['id'] in compiler.REQUIRES_PUBLISHED or spec['id']=='custom':
            published=specs.get(params.get('published_tool'))
            if not published or not published.get('approved'):
                issue('RUNTIME_CONTRACT',f"Publish and select the exact {spec['name']} runtime contract. This node will not launch guessed vendor/model commands.",n['id'])
            elif {(p['key'],p['type']) for p in published['inputs']}!={(p['key'],p['type']) for p in spec['inputs']} or {(p['key'],p['type']) for p in published['outputs']}!={(p['key'],p['type']) for p in spec['outputs']}:
                issue('CONTRACT_PORTS','Published runtime input/output contracts differ. Add the published custom node directly or publish matching ports.',n['id'])
        if spec.get('api') and not params.get('allow_external_processing'):issue('EXTERNAL_PROCESSING','Explicitly allow processing by this reviewed API endpoint.',n['id'])
        if spec['id']=='oncokb' and not params.get('allow_external_processing'):issue('EXTERNAL_PROCESSING','Explicitly allow external OncoKB processing after checking the intended data and provider agreement.',n['id'])
        for p in spec['parameters']:
            value=params.get(p['key'])
            if p.get('required') and not str(value or '').strip():issue('PARAMETER_REQUIRED',f"{p['label']} is required before execution.",n['id'])
            if p['kind']=='number' and (type(value) not in [int,float] or not math.isfinite(value) or ('min'in p and value<p['min']) or ('max'in p and value>p['max'])):issue('PARAMETER_RANGE',f"{p['label']} is outside its supported range.",n['id'])
            if p['kind']=='boolean' and type(value)!=bool:issue('PARAMETER_TYPE',f"{p['label']} must be a toggle.",n['id'])
            if p['kind']=='select' and value not in p['options']:issue('PARAMETER_CHOICE',f"Choose a supported {p['label']}.",n['id'])
            if p['kind'] in ['text','asset'] and (not isinstance(value,str) or len(value)>8192):issue('PARAMETER_TYPE',f"Invalid {p['label']}.",n['id'])
        try:
            flags=shlex.split(n.get('advanced',''))
            if len(flags)>200 or len(n.get('advanced',''))>8192:raise ValueError()
        except (ValueError,TypeError):issue('CLI_ARGUMENTS','Advanced arguments must be valid bounded argv, without unmatched quotes.',n['id'])
        resources={**spec['resources'],**n.get('resources',{})}
        for k,low,high in [('threads',1,2 if user['tier']=='trial' else 64),('memory_mb',128,8192 if user['tier']=='trial' else 524288),('timeout_seconds',1,settings['max_node_seconds'])]:
            if type(resources[k])!=int or not low<=resources[k]<=high:issue('RESOURCE_LIMIT',f'{k} must be between {low} and {high} for this workspace.',n['id'])
        scratch=resources.get('scratch_mb',2048)
        from .entitlements import limits
        scratch_ceiling=limits(user['id'])['max_scratch_mb'] if CLOUD else settings['max_upload_gib']*1024
        if type(scratch)!=int or not 128<=scratch<=scratch_ceiling:issue('RESOURCE_LIMIT','Scratch must fit the assigned execution scratch allowance.',n['id'])
        if all(type(resources[k])==int for k in ['threads','timeout_seconds']):reservation+=max(0,resources['threads'])*max(0,resources['timeout_seconds'])
        if n['tool'] in ['asset.file','reference.asset','database.asset'] or spec.get('asset_family'):
            aid=params.get('asset_id');asset=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(aid,user['id']),one=True) or db.query('SELECT * FROM artifacts WHERE id=? AND user_id=?',(aid,user['id']),one=True)
            if asset:
                from .migration import resolve_asset
                try:asset=resolve_asset(user['id'],asset)
                except ValueError as error:issue('DATA_NOT_STAGED',str(error),n['id'])
                if json.loads(asset['metadata']).get('migration_companions_pending'):issue('DATA_NOT_STAGED','Exact companion files are still awaiting AWS migration verification.',n['id'])
            if not asset:issue('ASSET_REQUIRED','Select a verified cloud dataset or an existing run output.',n['id'])
            elif n['tool']=='reference.asset' and asset.get('assembly') and params.get('assembly')!=asset['assembly']:issue('ASSEMBLY_MISMATCH','Selected assembly differs from the indexed reference asset.',n['id'])
            elif CLOUD and asset and not asset['path'].startswith('s3://'):issue('DATA_NOT_STAGED','This dataset is not yet verified in cloud storage. Wait for preparation to complete.',n['id'])
            if asset and json.loads(asset['metadata']).get('migration_companions_pending'):
                issue('MIGRATION_COMPANIONS','The primary data is verified, but one or more exact companion files are still migrating. Wait for the complete input bundle.',n['id'])
            if asset:
                from .migration import require_scientific_asset
                try:
                    require_scientific_asset(user['id'], asset)
                except ValueError as error:issue('DATA_QUALIFICATION_REQUIRED',str(error),n['id'])
                from .asset_rights import require as require_data_rights
                try:require_data_rights(user['id'],asset)
                except ValueError as error:issue('DATA_RIGHTS_REQUIRED',str(error),n['id'])
        if n['tool']=='reference.asset':reference_builds.add(params.get('assembly'))
        if spec['license'] in ['Academic','Review required','Commercial BYOL'] and spec['id'] not in settings['rights_approved']:
            issue('RIGHTS_REQUIRED',f"{spec['name']} needs an explicit rights approval; a platform trial is not a third-party licence.",n['id'])
        if spec.get('credential') and not security.credential_for(user['id'],spec['credential'],spec['id']):issue('CREDENTIAL_REQUIRED',f"Add a {spec['credential']} credential in Vault and bind it to this node.",n['id'])
        if capabilities:
            if spec.get('resource')=='gpu' and settings['backend']!='aws-batch':issue('GPU_PROFILE','Select an approved GPU-capable cloud job profile.',n['id'])
            elif not spec['builtin'] and not spec.get('api') and spec['id'] not in ['oncokb','franklin']:
                if settings['backend']=='portable':issue('NATIVE_BACKEND',f"{spec['name']} requires Docker or AWS Batch. Portable mode runs the built-in operations.",n['id'])
                if settings['backend']=='docker' and not capabilities.get('docker'):issue('DOCKER_UNAVAILABLE','Docker is not available. Start a runtime or select a configured AWS backend.',n['id'])
                if settings['backend']!='hosted-demo' and not settings['image_approvals'].get(spec['id']) and not spec.get('approved') and not (specs.get(params.get('published_tool')) or {}).get('approved'):issue('IMAGE_APPROVAL',f"Resolve and approve an immutable image digest for {spec['name']}.",n['id'])
            if settings['backend']=='aws-batch' and (not settings['batch_queue'] or not settings['batch_job_definition'] or not settings['s3_bucket'] or not settings['kms_key_arn']):issue('AWS_PROFILE','Configure the us-east-1 Batch queue, published worker revision, private cache and KMS key.',n['id'])
            cloud_task=settings['backend']=='aws-batch' and not spec.get('asset_family') and n['tool'] not in ['asset.file','reference.asset','database.asset','oncokb','franklin','utility.export','utility.gather','utility.condition']
            if cloud_task and spec.get('resource')!='gpu':
                capacity=settings.get('research_profile',{})
                runtime_profile=capacity.get('runtime_profiles',{}).get(spec['id'])
                from .cloud import require_migration_runtime
                try:require_migration_runtime(runtime_profile,migrated_inputs)
                except ValueError as error:issue('MIGRATION_RUNTIME_PENDING',str(error),n['id'])
                if spec['id']=='results.prepare' and not runtime_profile:issue('VIEWER_RUNTIME','The isolated private-results runtime has not been published for this tenant.',n['id'])
                if capacity and capacity.get('state')!='Ready':issue('CAPACITY_PROVISIONING',capacity.get('reason','Funded execution capacity is provisioning'),n['id'])
                if capacity and not spec['builtin'] and spec['id'] not in capacity.get('tools',[]):issue('RUNTIME_NOT_PUBLISHED','The pinned research runtime has no reviewed executable profile for this tool.',n['id'])
                use_vm=scratch>capacity.get('max_scratch_mb',184320) or n.get('execution_profile')=='exclusive-vm'
                if use_vm:
                    from . import vm,entitlements
                    published=vm.configuration()
                    try:vm.runtime_route(capacity,runtime_profile,migrated_inputs,published)
                    except ValueError as error:issue('VM_RUNTIME_MISMATCH',str(error),n['id'])
                    if not all(published.values()) or not capacity.get('instance_profile'):issue('VM_PROFILE_UNPUBLISHED','A published private VM image and tenant instance profile are required for this scratch request; no scientific input will be reduced.',n['id'])
                    try:
                        machine=vm.select(resources);allowance=entitlements.limits(user['id'])
                        if machine['vcpus']>allowance['max_vcpus'] or machine['memory_mb']>allowance['max_memory_mb'] or machine['scratch_gib']*1024>allowance['max_scratch_mb']:issue('VM_ALLOCATION','The exclusive machine CPU, RAM or minimum scratch allocation exceeds this funded allowance.',n['id'])
                        reservation+=machine['vcpus']*(resources['timeout_seconds']+900)-resources['threads']*resources['timeout_seconds']
                    except ValueError as e:issue('VM_PROFILE',str(e),n['id'])
                    continue
                cpu=resources['threads'];memory=resources['memory_mb'];profiles={1:(2048,8192,1024),2:(4096,16384,1024),4:(8192,30720,1024),8:(16384,61440,4096),16:(32768,122880,8192)}
                if cpu not in profiles:issue('FARGATE_RESOURCES','This CPU lane accepts 1, 2, 4, 8 or 16 vCPU profiles.',n['id'])
                else:
                    low,high,step=profiles[cpu]
                    if not low<=memory<=high or (memory-low)%step:issue('FARGATE_RESOURCES',f'{cpu} vCPU requires {low}–{high} MiB in {step} MiB increments.',n['id'],{'action':'set_resources','node_id':n['id'],'memory_mb':low})
            if spec['id']=='franklin':issue('PROVIDER_CONTRACT','Franklin integration requires the endpoint/schema from your Genoox contract. Publish its reviewed adapter before sending data.',n['id'])
    from .semantics import check as semantic_check
    semantic_issues,semantic_warnings,descriptors=semantic_check(graph,user['id'],specs,order)
    issues.extend(semantic_issues);warnings.extend(semantic_warnings)
    if any(n['tool']=='mutect2' for n in graph['nodes']):
        for n in graph['nodes']:
            if n['tool']=='mutect2':
                inputs={e['targetPort']:e['source'] for e in incoming[n['id']]}
                if inputs.get('tumor')==inputs.get('normal') and inputs.get('tumor'):issue('SAMPLE_ROLES','Tumor and normal must be different, explicitly identified samples.',n['id'])
    if not CLOUD and reservation>user['quota_seconds']-user['used_seconds']-user['reserved_seconds']:issue('QUOTA_EXCEEDED','Worst-case CPU-time reservation exceeds the remaining quota. Reduce limits or request an audited quota increase.')
    if CLOUD:
        from .entitlements import limits
        allowance=limits(user['id'])
        if len(graph['nodes'])>allowance['max_nodes']:issue('TIER_NODES','Graph exceeds the assigned node allowance')
        for n in graph['nodes']:
            if n['tool'] not in specs:continue
            from .demo import SOURCES
            if allowance['access']=='research-pro' and n['tool'] in SOURCES:
                from .curated_cloud import outputs as curated_outputs
                try:curated_outputs(user['id'],n['tool'])
                except ValueError as error:issue('CURATED_CLOUD_SOURCE',str(error),n['id'])
            r={**specs[n['tool']]['resources'],**n.get('resources',{})}
            if r['threads']>allowance['max_vcpus'] or r['memory_mb']>allowance['max_memory_mb'] or r.get('scratch_mb',2048)>allowance['max_scratch_mb'] or r['timeout_seconds']>allowance['max_node_seconds']:issue('TIER_RESOURCES','Requested resources exceed the assigned allowance',n['id'])
            if specs[n['tool']].get('resource')=='gpu' and allowance['max_gpu']<1:issue('TIER_GPU','GPU use requires an explicitly funded GPU allowance',n['id'])
        if sum({**specs[n['tool']]['resources'],**n.get('resources',{})}['timeout_seconds'] for n in graph['nodes'] if n['tool'] in specs)>allowance['max_job_seconds']:issue('TIER_RUNTIME','Worst-case graph duration exceeds the job allowance')
    if len(graph['nodes'])>500:warnings.append('This graph exceeds the initial 500-visible-node performance target. Collapse or split analysis groups.')
    return {'ok':not issues,'issues':issues,'warnings':warnings,'order':order,'reservation_cpu_seconds':reservation,'graph_digest':graph_digest(graph),'licenses':licenses,'backend':settings['backend'],'semantic_context':descriptors}

def apply_fixes(graph,fixes):
    result=copy.deepcopy(graph);used={n['id'] for n in result['nodes']}
    for fix in fixes:
        if fix.get('action')=='set_resources':
            node=next((n for n in result['nodes'] if n['id']==fix['node_id']),None)
            if node:node.setdefault('resources',{})['memory_mb']=fix['memory_mb']
        if fix.get('action')=='remove_edge':result['edges']=[e for e in result['edges'] if e['id']!=fix['edge_id']]
        elif fix.get('action')=='insert_adapters' and fix.get('tools'):
            edge=next((e for e in result['edges'] if e['id']==fix['edge_id']),None)
            if not edge:continue
            source=next(n for n in result['nodes'] if n['id']==edge['source']);target=next(n for n in result['nodes'] if n['id']==edge['target'])
            result['edges'].remove(edge);previous=edge['source'];previous_port=edge['sourcePort']
            for i,tool in enumerate(fix['tools']):
                spec=registry.BASE[tool];nid=db.ident('n_');fraction=(i+1)/(len(fix['tools'])+1)
                result['nodes'].append({'id':nid,'tool':tool,'x':source.get('x',0)+(target.get('x',500)-source.get('x',0))*fraction,'y':source.get('y',0)+(target.get('y',0)-source.get('y',0))*fraction+100,'params':registry.defaults(spec),'resources':{'threads':1,'memory_mb':1024,'timeout_seconds':30}})
                result['edges'].append({'id':db.ident('e_'),'source':previous,'sourcePort':previous_port,'target':nid,'targetPort':spec['inputs'][0]['key']})
                previous=nid;previous_port=spec['outputs'][0]['key']
            result['edges'].append({**edge,'id':db.ident('e_'),'source':previous,'sourcePort':previous_port})
    return result
