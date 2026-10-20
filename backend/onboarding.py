"""Import declarative contracts without executing fetched source or trusting its claims."""
import http.client
import ipaddress
import json
import re
import shlex
import socket
import ssl
import time
from urllib.parse import urlsplit,urlunsplit,quote
import yaml
from . import registry,db,security

def request_public(url,method='GET',headers=None,body=None,maximum=2*1024**2,timeout=15):
    started=time.monotonic();timeout=max(.1,min(30,float(timeout)))
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in [None,443] or parsed.fragment:raise ValueError('Only public HTTPS URLs on port 443 are accepted')
    addresses={x[4][0] for x in socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):raise ValueError('Private, local and metadata-network destinations are prohibited')
    # Pin the checked address for this connection; preserve original TLS SNI and certificate verification.
    class PinnedTLS(http.client.HTTPSConnection):
        def connect(self):
            sock=socket.create_connection((sorted(addresses)[0],443),timeout=timeout)
            self.sock=ssl.create_default_context().wrap_socket(sock,server_hostname=parsed.hostname)
    connection=PinnedTLS(parsed.hostname,timeout=timeout)
    try:
        connection.request(method,urlunsplit(('', '',parsed.path or '/',parsed.query,'')),body=body,headers={'Host':parsed.hostname,'User-Agent':'OpenNGS-contract-import/1',**(headers or {})})
        response=connection.getresponse()
        if 300<=response.status<400:raise ValueError('Redirects are not followed; supply the final public specification URL')
        if response.status>=400:raise ValueError('Remote endpoint returned HTTP '+str(response.status))
        parts=[];size=0
        while True:
            remaining=timeout-(time.monotonic()-started)
            if remaining<=0:raise TimeoutError('Remote request exhausted its wall-time budget')
            if connection.sock:connection.sock.settimeout(remaining)
            block=response.read1(min(65536,maximum+1-size))
            if not block:break
            parts.append(block);size+=len(block)
            if size>maximum:raise ValueError('Remote response exceeds its bounded size limit')
        return b''.join(parts)
    finally:connection.close()

def semantic(name,format=None):
    value=(str(format or '')+' '+name).lower()
    for token,kind in [('cram','CRAM'),('bam','BAM'),('sam','SAM'),('fastq','FASTQ'),('vcf','VCF'),('variants','VCF'),('bcf','BCF'),('fasta','FASTA'),('.fa','FASTA'),('reference','FASTA'),('bigwig','BIGWIG'),('bed','BED'),('gtf','GTF'),('gff','GFF3'),('h5ad','H5AD'),('json','REPORT'),('csv','CSV'),('tsv','TSV')]:
        if token in value:return kind
    return 'ANY'

def local_ref(doc,value):
    seen=set()
    while isinstance(value,dict) and '$ref' in value:
        ref=value['$ref']
        if not ref.startswith('#/') or ref in seen:raise ValueError('Only acyclic document-local schema references are supported')
        seen.add(ref);value=doc
        for part in ref[2:].split('/'):value=value[part.replace('~1','/').replace('~0','~')]
    return value

def fields(items):
    if isinstance(items,dict):return [(k,v if isinstance(v,dict) else {'type':v}) for k,v in items.items()]
    return [(x.get('id','').split('#')[-1].split('/')[-1],x) for x in items or []]

def scalar(key,schema,description=''):
    typ=schema.get('type','string');default=schema.get('default')
    if isinstance(typ,list):typ=next((x for x in typ if x!='null'),'string')
    kind='boolean' if typ in ['boolean','bool'] else 'number' if typ in ['integer','int','long','float','double','number'] else 'select' if schema.get('enum') else 'text'
    if default is None:default=False if kind=='boolean' else 0 if kind=='number' else schema['enum'][0] if kind=='select' else ''
    extra={}
    if kind=='select':extra['options']=schema['enum']
    for source,target in [('minimum','min'),('maximum','max')]:
        if source in schema:extra[target]=schema[source]
    return registry.param(key,schema.get('title',key.replace('_',' ').title()),default,kind,schema.get('description',description),**extra)

def parse_yaml(content):
    if sum(1 for _ in yaml.scan(content))>100000:raise ValueError('Specification is too complex')
    if any(isinstance(event,yaml.events.AliasEvent) for event in yaml.parse(content)):raise ValueError('YAML aliases are not accepted in imported contracts')
    try:return yaml.safe_load(content)
    except yaml.YAMLError as e:raise ValueError('Invalid YAML/JSON contract') from e

def inspect(payload):
    kind=payload.get('kind','auto');url=str(payload.get('url','')).strip();content=payload.get('content','');warnings=[]
    if not isinstance(content,str) or len(content)>1500000:raise ValueError('Contract text exceeds the import ceiling')
    if url and not content:
        if 'github.com/' in url and '/blob/' in url:url=url.replace('https://github.com/','https://raw.githubusercontent.com/').replace('/blob/','/')
        if re.fullmatch(r'[a-zA-Z0-9_-]+',url) and kind=='bioconda':
            metadata=json.loads(request_public('https://api.anaconda.org/package/bioconda/'+url));return {'kind':'bioconda','draft':{'name':metadata.get('name',url),'image':'','inputs':[],'outputs':[],'output_files':{},'parameters':[],'command':[],'license':'Review required','docs':metadata.get('home')},'warnings':['Package metadata is not an executable contract. Select an immutable container and introspect its help or import a declarative wrapper.','Conda package license: '+str(metadata.get('license','unreported'))],'metadata':{'versions':metadata.get('versions',[])[-10:]}}
        if not url.startswith('https://'):
            if not re.fullmatch(r'[A-Za-z0-9./_:@-]{1,300}',url):raise ValueError('Provide an HTTPS spec, package name or OCI image URI')
            return {'kind':'oci','draft':{'name':url.split('/')[-1].split(':')[0],'image':url,'inputs':[registry.port('data','ANY')],'outputs':[registry.port('result','ANY')],'output_files':{},'parameters':[],'command':[],'license':'Review required'},'warnings':['OCI images do not describe scientific ports. Resolve the digest, then use isolated help introspection or import a CWL wrapper. No fetched binary has been run.']}
        if re.fullmatch(r'https://github.com/[^/]+/[^/]+/?',url):raise ValueError('Select a specific CWL, WDL or nf-core module file from this repository; a repository homepage does not specify an executable contract')
        content=request_public(url).decode('utf8')
    if not content.strip():raise ValueError('Provide a specification URL or declarative text / help output')
    if kind=='auto':
        if 'openapi:' in content or '"openapi"' in content or '"swagger"' in content:kind='openapi'
        elif 'cwlVersion' in content:kind='cwl'
        elif re.search(r'\btask\s+\w+\s*\{',content):kind='wdl'
        elif re.search(r'\bprocess\s+\w+\s*\{',content):kind='nextflow'
        else:kind='help'
    draft={'name':payload.get('name') or 'Imported tool','image':payload.get('image',''),'license':'Review required','inputs':[],'outputs':[],'output_files':{},'parameters':[],'command':[],'docs':url or None,'resources':{'threads':2,'memory_mb':4096,'timeout_seconds':120}}
    if kind=='cwl':
        doc=parse_yaml(content)
        if not isinstance(doc,dict) or doc.get('class')!='CommandLineTool':raise ValueError('Import a CWL CommandLineTool; Workflow graphs must first be decomposed into their tool contracts')
        draft['name']=doc.get('label',draft['name']);requirements=doc.get('requirements',{})
        reqs=requirements if isinstance(requirements,dict) else {r.get('class'):r for r in requirements}
        hints=doc.get('hints',{});hints=hints if isinstance(hints,dict) else {r.get('class'):r for r in hints}
        docker=reqs.get('DockerRequirement') or hints.get('DockerRequirement') or {};draft['image']=docker.get('dockerPull',draft['image'])
        if 'ShellCommandRequirement' in reqs or 'InlineJavascriptRequirement' in reqs:raise ValueError('Shell/JavaScript requirements require a separately reviewed runtime; source is never executed during import')
        base=doc.get('baseCommand',[]);base=[base] if isinstance(base,str) else base
        if not base or any(not isinstance(a,str) for a in base):raise ValueError('CWL needs a literal baseCommand')
        draft['command']=[base];bindings=[]
        for key,schema in fields(doc.get('inputs',{})):
            typ=schema.get('type','string');optional=False
            if isinstance(typ,list):optional='null' in typ;typ=next((x for x in typ if x!='null'),'string')
            if isinstance(typ,str) and typ.endswith('?'):optional=True;typ=typ[:-1]
            binding=schema.get('inputBinding')
            if isinstance(typ,dict) or typ in ['Directory','Any'] or (isinstance(typ,str) and typ.endswith('[]')):raise ValueError('This CWL profile supports scalar File/primitive inputs. Arrays, directories and expressions need an explicit runtime contract.')
            if typ=='File':draft['inputs'].append(registry.port(key,semantic(key,schema.get('format')),not optional));token='{in.'+key+'}'
            else:draft['parameters'].append(scalar(key,{**schema,'type':typ}));token='{param.'+key+'}'
            if binding:
                if set(binding)-{'position','prefix','separate'}:raise ValueError('Dynamic CWL input bindings are outside the safe literal profile')
                bindings.append({'key':key,'file':typ=='File','optional':optional,'position':binding.get('position',0),'prefix':binding.get('prefix',''),'separate':binding.get('separate',True),'boolean':typ in ['boolean','bool']})
        draft['bindings']=sorted(bindings,key=lambda b:b['position'])
        for argument in doc.get('arguments',[]):
            if not isinstance(argument,str) or '$(' in argument or '${' in argument:raise ValueError('Only literal CWL arguments are supported')
            draft['command'][0].append(argument)
        for key,schema in fields(doc.get('outputs',{})):
            glob=schema.get('outputBinding',{}).get('glob')
            if schema.get('type')=='stdout':glob=doc.get('stdout');draft['capture_stdout']=True
            if not isinstance(glob,str) or '$(' in glob or '${' in glob:raise ValueError('Output glob must be a literal relative file path')
            fmt='REPORT' if schema.get('type')=='stdout' else semantic(glob,schema.get('format'));draft['outputs'].append(registry.port(key,fmt));draft['output_files'][key]={'file':glob,'type':fmt}
        resources=reqs.get('ResourceRequirement') or hints.get('ResourceRequirement') or {}
        for key,target in [('coresMin','threads'),('ramMin','memory_mb')]:
            if isinstance(resources.get(key),(int,float)):draft['resources'][target]=int(resources[key])
        warnings.append('Port formats inferred from declared format/name; review assemblies, indices and scientific semantics. Only the safe literal CWL subset is imported.')
    elif kind=='openapi':
        doc=parse_yaml(content)
        if not isinstance(doc,dict) or not ('openapi' in doc or 'swagger' in doc):raise ValueError('Expected an OpenAPI / Swagger object')
        server=(doc.get('servers') or [{}])[0].get('url') or ('https://'+doc.get('host','')+doc.get('basePath',''))
        operations=[]
        for path,item in doc.get('paths',{}).items():
            for method,operation in item.items():
                if method not in ['get','post']:continue
                params=[];bindings=[]
                for raw in item.get('parameters',[])+operation.get('parameters',[]):
                    entry=local_ref(doc,raw);schema=local_ref(doc,entry.get('schema',entry))
                    if entry.get('in') not in ['path','query']:continue
                    key=entry['name'];params.append(scalar(key,schema,entry.get('description','')));bindings.append({'key':key,'in':entry['in'],'required':entry.get('required',False)})
                schemes=doc.get('components',{}).get('securitySchemes',doc.get('securityDefinitions',{}));requirements=operation.get('security',doc.get('security',[]));auth=None
                for requirement in requirements:
                    for name in requirement:
                        scheme=local_ref(doc,schemes.get(name,{}))
                        if scheme.get('type')=='http' and scheme.get('scheme','').lower()=='bearer':auth={'kind':'bearer','header':'Authorization'}
                        elif scheme.get('type')=='apiKey' and scheme.get('in')=='header':auth={'kind':'header','header':scheme['name']}
                unsupported_auth=bool(requirements) and not auth
                request_body=local_ref(doc,operation.get('requestBody',{}));json_body='application/json' in request_body.get('content',{}) or any(p.get('in')=='body' for p in operation.get('parameters',[]))
                body_schema=local_ref(doc,request_body.get('content',{}).get('application/json',{}).get('schema',{}))
                for key,raw_schema in body_schema.get('properties',{}).items():
                    schema=local_ref(doc,raw_schema)
                    if schema.get('type') not in ['string','number','integer','boolean']:continue
                    parameter_key='body_'+key
                    params.append(scalar(parameter_key,schema));bindings.append({'key':parameter_key,'name':key,'in':'body','required':key in body_schema.get('required',[])})
                operations.append({'id':operation.get('operationId',method+' '+path),'name':operation.get('summary',operation.get('operationId',path)),'method':method.upper(),'url':server.rstrip('/')+path,'parameters':params,'api_bindings':bindings,'auth':auth,'json_input':json_body,'unsupported_auth':unsupported_auth})
        if not operations:raise ValueError('No GET/POST operation contracts found')
        chosen=next((o for o in operations if o['id']==payload.get('operation_id')),operations[0])
        draft.update(name=chosen['name'],image=None,api={k:v for k,v in chosen.items() if k not in ['parameters','name','id']},parameters=chosen['parameters']+[registry.param('allow_external_processing','Allow sending bound data to this reviewed API',False,'boolean')],inputs=[registry.port('data','REPORT',False)] if chosen['json_input'] else [],outputs=[registry.port('response','REPORT')],credential='customapi' if chosen['auth'] else None,license='Paid API BYOK' if chosen['auth'] else 'Review required')
        if chosen['unsupported_auth']:warnings.append('OAuth/mTLS/unsupported authentication requires a dedicated broker; this operation cannot be published through the scalar adapter.')
        warnings.extend(['Review the exact endpoint, data processing agreement and request schema before enabling external processing.','Automatic parsing does not validate vendor licensing or clinical correctness.'])
        return {'kind':kind,'draft':draft,'operations':[{k:o[k] for k in ['id','name','method','url']} for o in operations],'warnings':warnings}
    elif kind in ['nextflow','wdl']:
        # Parsing source is discovery, never shell execution. Complex workflow DSLs cannot safely be translated by regex.
        name=re.search(r'\b(?:process|task)\s+(\w+)',content);draft['name']=name[1] if name else draft['name']
        image=re.search(r'(?:container\s+|docker\s*:\s*)["\']([^"\']+)',content)
        if image:draft['image']=image[1]
        for name in re.findall(r'\b(?:File|path)\s*\(?\s*(\w+)',content)[:50]:draft['inputs'].append(registry.port(name,semantic(name)))
        return {'kind':kind,'draft':draft,'warnings':['DSL source inventoried without execution. Review and supply explicit argv/output bindings, or import the corresponding CWL tool. Scatter, shell interpolation and workflow orchestration are not silently guessed.'],'source_preview':content[:12000]}
    else:
        for line in content.splitlines():
            match=re.match(r'\s*(?:-\w[, ]+)?(--[A-Za-z][A-Za-z0-9_-]*)(?:[ =]+([A-Z][A-Z0-9_<>{}|-]*))?\s*(.*)',line)
            if not match:continue
            flag,arg,description=match.groups();key=flag[2:].replace('-','_');draft['parameters'].append(registry.param(key,flag,False if not arg else '', 'boolean' if not arg else 'text',description[:500]));draft.setdefault('bindings',[]).append({'key':key,'file':False,'prefix':flag,'separate':True,'boolean':not arg})
            if len(draft['parameters'])>=200:break
        warnings.append('Help text supplies flags, not trustworthy input/output semantics. Review file ports, binary command and output contracts before publication.')
    return {'kind':kind,'draft':draft,'warnings':warnings}

def execute_api(uid,spec,node,inputs,work,rid):
    from .portable import report
    from urllib.parse import urlencode
    config=spec['api'];params={**registry.defaults(spec),**node.get('params',{})}
    if not params.get('allow_external_processing'):raise ValueError('External processing consent is required')
    url=config['url'];query={};body_fields={}
    for binding in config.get('api_bindings',[]):
        value=params.get(binding['key'])
        if value in [None,'']:
            if binding.get('required'):raise ValueError('Required API parameter is absent: '+binding['key'])
            continue
        if binding['in']=='path':url=url.replace('{'+binding['key']+'}',quote(str(value),safe=''))
        elif binding['in']=='body':body_fields[binding['name']]=value
        else:query[binding['key']]=str(value)
    if query:url+=('&' if '?' in url else '?')+urlencode(query)
    headers={'Accept':'application/json'};secret=None
    if config.get('auth'):
        row=security.credential_for(uid,'customapi',spec['id'])
        if not row:raise ValueError('Bind a provider API credential to this exact node in Vault')
        secret=security.unwrap(uid,row['id'],spec['id']);auth=config['auth'];headers[auth['header']]=('Bearer ' if auth['kind']=='bearer' else '')+secret
    body=None
    if inputs.get('data'):
        item=inputs['data'][0]
        if item['bytes']>1024**2 or item['path'].startswith('s3://'):raise ValueError('Generic JSON API adapter accepts one locally staged JSON report up to 1 MiB')
        value=json.loads(__import__('pathlib').Path(item['path']).read_text())
        if not isinstance(value,dict):raise ValueError('The generic API request body must be a JSON object')
        body_fields={**value,**body_fields}
    if body_fields:body=json.dumps(body_fields).encode();headers['Content-Type']='application/json'
    with db.tx() as c:db.audit(c,uid,'api.requested',spec['id'],{'run':rid,'host':urlsplit(url).hostname,'method':config['method'],'input_bytes':len(body or b'')})
    try:
        response=request_public(url,config['method'],headers,body,8*1024**2,timeout=node.get('resources',{}).get('timeout_seconds',15))
        if secret:response=response.replace(secret.encode(),b'[REDACTED]')
    finally:secret=None;headers={}
    result=json.loads(response)
    return {'response':[report(work,'api-response.json',{'provider_host':urlsplit(url).hostname,'response':result,'interpretation':'Provider response; not independently validated by OpenNGS'})]}
