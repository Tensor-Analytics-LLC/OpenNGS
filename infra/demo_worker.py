"""One bounded DAG per Fargate sandbox; no task IAM role or user code uploads."""
import glob
import hashlib
import json
import os
import resource
import subprocess
import sys
import tarfile
import time
from pathlib import Path
import httpx
from backend import compiler,graph,portable,registry
from backend.demo import TOOLS

ROOT=Path('/work/job')
def emit(kind,**payload):print(json.dumps({'kind':kind,'at':time.time(),**payload}),flush=True)

def native(spec,node,inputs,work):
    plan=compiler.compile_tool(spec,node,inputs,work);started=time.monotonic()
    mapping=dict(plan['bindings'])
    # Bindings are target -> source because all files are already inside this task.
    mapping={target:source for source,target in plan['bindings']}
    def translate(value):
        value=value.replace('/work/',str(work)+'/') if value!='/work' else str(work)
        for target,source in sorted(mapping.items(),key=lambda x:-len(x[0])):value=value.replace(target,source)
        return value
    for step in plan['steps']:
        argv=[translate(x) for x in step['argv']]
        target=(work/step['stdout']).open('wb') if step.get('stdout') else subprocess.DEVNULL
        try:
            result=subprocess.run(argv,cwd=work,stdout=target,stderr=subprocess.PIPE,timeout=max(1,node['resources']['timeout_seconds']-(time.monotonic()-started)),check=False)
            if result.returncode:raise ValueError(spec['name']+' failed: '+result.stderr[-1500:].decode('utf8','replace'))
        finally:
            if hasattr(target,'close'):target.close()
    outputs={}
    for port,descriptors in plan['outputs'].items():
        outputs[port]=[]
        for d in descriptors:
            files=sorted(work.glob(d['file']))
            if not files:raise ValueError('Required tool output is missing: '+d['file'])
            for p in files:outputs[port].append(portable.artifact(p,d['format'],**d.get('metadata',{})))
    return outputs

def main():
    resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024**2,64*1024**2))
    ROOT.mkdir(parents=True,exist_ok=True)
    with httpx.Client(timeout=60,follow_redirects=False) as client:
        response=client.get(sys.argv[1]);response.raise_for_status()
        if len(response.content)>512*1024:raise ValueError('Manifest exceeds its size limit')
        job=response.json();g=job['graph'];graph.validate_shape(g)
        from backend.demo import validate
        validate(g)
        sample_outputs=json.loads(Path('/opt/studio/demo-data/manifest.json').read_text())['outputs']
        specs=registry.definitions();completed={};events=[];began=time.monotonic()
        for nid in job['order']:
            n=next(n for n in g['nodes'] if n['id']==nid);spec=specs[n['tool']]
            if n['tool'] not in TOOLS:raise ValueError('Tool is outside the hosted scientific contract')
            work=ROOT/nid;work.mkdir()
            n={**n,'params':{**registry.defaults(spec),**n.get('params',{})},'resources':{**spec['resources'],**n.get('resources',{})}}
            inputs={}
            for e in g['edges']:
                if e['target']==nid:inputs.setdefault(e['targetPort'],[]).extend(completed.get(e['source'],{}).get(e['sourcePort'],[]))
            emit('node.started',node=nid,tool=n['tool'])
            start=time.monotonic()
            if inputs:
                from backend.semantics import runtime
                runtime(spec,inputs)
            if n['tool'] in sample_outputs:outputs=sample_outputs[n['tool']]
            elif any(p['required'] and not inputs.get(p['key']) for p in spec['inputs']):outputs={}
            elif spec['builtin']:outputs=portable.execute({'tool':n['tool'],'params':n['params'],'inputs':inputs,'workdir':str(work),'threads':2})
            else:outputs=native(spec,n,inputs,work)
            elapsed=time.monotonic()-start
            if elapsed>n['resources']['timeout_seconds']:raise TimeoutError('Node exceeded its deadline')
            completed[nid]=outputs;events.append({'node':nid,'seconds':elapsed,'tool':n['tool']})
            emit('node.completed',node=nid,seconds=elapsed)
            total=sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
            if total>64*1024**2:raise ValueError('Hosted sample output ceiling exceeded')
        files={};published={}
        def file_ref(raw):
            path=Path(raw).resolve()
            if not (path.is_relative_to(ROOT) or path.is_relative_to(Path('/opt/studio/demo-data'))):raise ValueError('Output path escaped the sandbox data roots')
            if path.is_symlink() or not path.is_file():raise ValueError('Invalid artifact')
            relative='files/'+hashlib.sha256(str(path).encode()).hexdigest()[:24]+'/'+path.name
            files[relative]=path;return relative
        for nid,ports in completed.items():
            published[nid]={}
            for port,items in ports.items():
                published[nid][port]=[]
                for item in items:
                    item=json.loads(json.dumps(item));item['path']=file_ref(item['path'])
                    for field in ['mate','index','reference']:
                        if item.get('metadata',{}).get(field) and Path(str(item['metadata'][field])).is_file():item['metadata'][field]=file_ref(item['metadata'][field])
                    if item.get('metadata',{}).get('index_paths'):item['metadata']['index_paths']=[file_ref(x) for x in item['metadata']['index_paths']]
                    published[nid][port].append(item)
        result={'schema':1,'run':job['run'],'outputs':published,'metrics':{'elapsed_seconds':time.monotonic()-began,'nodes':events,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},'scientific_tool_versions':Path('/opt/studio/tool-versions.tsv').read_text(),'sample_manifest_sha256':hashlib.sha256(Path('/opt/studio/demo-data/manifest.json').read_bytes()).hexdigest()}
        manifest=Path('/work/manifest.json');manifest.write_text(json.dumps(result,allow_nan=False))
        bundle=Path('/work/result.tar.gz')
        if sum(p.stat().st_size for p in files.values())>64*1024**2:raise ValueError('Published output ceiling exceeded')
        with tarfile.open(bundle,'w:gz',dereference=True) as archive:
            archive.add(manifest,arcname='manifest.json')
            for name,path in files.items():archive.add(path,arcname=name)
        with bundle.open('rb') as f:
            upload=client.post(job['output']['url'],data=job['output']['fields'],files={'file':('result.tar.gz',f,'application/gzip')});upload.raise_for_status()
        emit('run.completed',run=job['run'],bytes=bundle.stat().st_size)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        import re
        message='Network transfer failed' if isinstance(exc,httpx.HTTPError) else re.sub(r'https?://\S+','[redacted URL]',str(exc))[:1800]
        emit('run.failed',exception=type(exc).__name__,message=message)
        raise SystemExit(1)
