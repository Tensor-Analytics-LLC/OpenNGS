"""Observed provider allocation, priced conservatively; not a final AWS invoice."""
import json,math
from . import db

def fargate(job,vcpus,memory_mb):
    created=job.get('createdAt');stopped=job.get('stoppedAt')
    if not created or not stopped or stopped<created:return None
    # Includes queue/pull/cleanup rather than assuming container runtime is the
    # whole billing interval. Spot is priced at Linux On-Demand upper rates.
    seconds=max(60,math.ceil((stopped-created)/1000))
    scratch=job.get('container',{}).get('ephemeralStorage',{}).get('sizeInGiB',20)
    price=seconds/3600*(vcpus*.04048+memory_mb/1024*.004445+max(0,scratch-20)*.000111)
    return {'provider':'Fargate','observed_seconds_including_queue':seconds,'vcpus':vcpus,'memory_mb':memory_mb,'physical_scratch_gib':scratch,'upper_pretax_usd':round(price,8),'scope':'Observed submit-to-stop interval at conservative On-Demand rates; Spot savings and invoice reconciliation remain separate'}

def exclusive(instance,profile,observed_at):
    launch=instance.get('LaunchTime')
    if not launch:return None
    seconds=max(60,math.ceil(observed_at-launch.timestamp()))
    price=seconds/3600*(profile['hourly_usd']+(20+profile['scratch_gib'])*.08/730)
    return {'provider':'exclusive-vm','observed_seconds':seconds,'instance_type':profile['instance_type'],'scratch_gib':profile['scratch_gib'],'upper_pretax_usd':round(price,8),'scope':'Observed launch-to-confirmed-termination allocation; standard credits, encrypted root/scratch, conservative tariff'}

def record(aid,usage):
    if not usage:return
    with db.tx() as c:
        row=c.execute('SELECT metrics FROM attempts WHERE id=?',(aid,)).fetchone()
        if not row:raise ValueError('Metering attempt is not registered')
        metrics=json.loads(row['metrics']);metrics['provider_usage']=usage
        c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode(metrics),aid))

def scientific_cost(rid,publication_usd=.10):
    total=publication_usd
    for row in db.query('SELECT provider_id,metrics FROM attempts WHERE run_id=?',(rid,)):
        if not row['provider_id']:continue
        usage=json.loads(row['metrics']).get('provider_usage')
        if not usage:return None  # Unknown allocations keep the original reservation.
        total+=usage['upper_pretax_usd']
    return total
