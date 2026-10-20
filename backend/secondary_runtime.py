"""Independently qualified optional runtimes; never overwrite existing core routes."""
import json
import re
from . import db

ENVIRONMENTS = {
    'analysis': {'packages':['python=3.12','conda-forge::scanpy=1.12.4','python-igraph=1.0.0','leidenalg=0.12.0'],'tools':['scanpy']},
    'annotation': {'packages':['python=3.12','snpeff=5.4.0c','openjdk=21'],'tools':['snpeff']},
    'happy': {'packages':['python=2.7','hap.py=0.3.15','rtg-tools=3.13'],'tools':['benchmark.happy']},
    'legacy': {'packages':['python=2.7','strelka=2.9.10','manta=1.6.0'],'tools':['strelka2','manta']},
}
TOOLS={tool:family for family,spec in ENVIRONMENTS.items() for tool in spec['tools']}

def validate_release(value):
    if value.get('family')!='secondary-cpu-v1' or value.get('environments')!=ENVIRONMENTS:
        raise ValueError('Secondary scientific recipes must match reviewed exact versions')
    if not re.fullmatch(r'[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/openngs-demo@sha256:[a-f0-9]{64}',value.get('image','')):
        raise ValueError('Secondary worker image must be private and immutable')
    if not re.fullmatch('[a-f0-9]{64}',value.get('source_sha256','')):
        raise ValueError('Secondary source provenance missing')
    proof=value.get('qualification',{});cases=proof.get('cases',[])
    if {c.get('tool') for c in cases}!=set(TOOLS) or len(cases)!=len(TOOLS):
        raise ValueError('Every secondary tool requires one real qualification outcome')
    for case in cases:
        if case.get('status')=='passed':
            family=TOOLS[case['tool']]
            if not case.get('outputs') or not case.get('assertions') or proof.get('environments',{}).get(family,{}).get('status')!='installed':
                raise ValueError('Passing tools need installed environments and actual asserted outputs')
            if not re.fullmatch('[a-f0-9]{64}',proof.get('locks',{}).get(family,'')):
                raise ValueError('Passing environments require a frozen transitive dependency lock')
    passing={c['tool'] for c in cases if c.get('status')=='passed'}
    if 'isolation_qualification' in value:
        isolated=value['isolation_qualification'];checks=isolated.get('cases',[])
        if isolated.get('image')!=value['image'] or not re.fullmatch('[a-f0-9]{64}',isolated.get('source_sha256','')):
            raise ValueError('Isolation evidence must bind the exact image and audit source')
        if {c.get('tool') for c in checks}!=set(TOOLS) or len(checks)!=len(TOOLS):
            raise ValueError('Isolation evidence must contain every reviewed contract')
        if any(c.get('status')=='passed' and (not c.get('outputs') or not c.get('assertions')) for c in checks):
            raise ValueError('Isolation checks require asserted scientific outputs')
        passing&={c['tool'] for c in checks if c.get('status')=='passed'}
    if value.get('tools')!=sorted(passing):
        raise ValueError('Only passing secondary tools may be routed')
    return value

def published():
    row=db.query("SELECT value FROM controls WHERE key='runtime-release:secondary-cpu-v1'",one=True)
    if row:
        try:return validate_release(json.loads(row['value']))
        except (ValueError,TypeError,KeyError):return None
    return None
