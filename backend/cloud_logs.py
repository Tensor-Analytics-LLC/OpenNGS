"""Tenant-bound, bounded native log delivery; licence-bearing stages stay silent."""
import json,re,time
from botocore.config import Config
from . import db
from .config import aws_session

def redact(line):
    line=re.sub(r'https?://\S+','[URL redacted]',line)
    line=re.sub(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b','[AWS key redacted]',line)
    return re.sub(r'(?i)((?:api[_ -]?key|password|authorization|secret|token)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',line).replace('\x1b','')[:2000]

def drain(uid,rid,aid,nid,job,sensitive=False):
    if sensitive:return
    stream=job.get('container',{}).get('logStreamName')
    if not stream:return
    if job.get('tags',{}).get('TenantId')!=uid:raise ValueError('Provider log tenant does not match this execution')
    expected='/openngs/pro/'+uid
    group=job.get('container',{}).get('logConfiguration',{}).get('options',{}).get('awslogs-group')
    if group!=expected or not stream.startswith('research/'):raise ValueError('Provider log destination differs from the tenant profile')
    row=db.query('SELECT metrics FROM attempts WHERE id=? AND run_id=?',(aid,rid),one=True)
    metrics=json.loads(row['metrics']);now=time.time()
    if now-metrics.get('log_polled_at',0)<5 or metrics.get('log_bytes',0)>=128*1024:return
    args={'logGroupName':expected,'logStreamName':stream,'startFromHead':True,'limit':100}
    if metrics.get('log_cursor'):args['nextToken']=metrics['log_cursor']
    try:page=aws_session().client('logs',config=Config(connect_timeout=3,read_timeout=3,retries={'total_max_attempts':1})).get_log_events(**args)
    except Exception as e:
        with db.tx() as c:
            current=json.loads(c.execute('SELECT metrics FROM attempts WHERE id=?',(aid,)).fetchone()['metrics'])
            current.update(log_polled_at=now,log_read_status=type(e).__name__)
            c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode(current),aid))
        return
    total=metrics.get('log_bytes',0)
    for event in page.get('events',[]):
        message=event['message'];total+=len(message.encode())
        if total>128*1024:break
        for line in message.splitlines()[:100]:db.event(rid,'log',nid,text=redact(line),source='native-cloud-tool')
    with db.tx() as c:
        current=json.loads(c.execute('SELECT metrics FROM attempts WHERE id=?',(aid,)).fetchone()['metrics'])
        current.update(log_polled_at=now,log_cursor=page.get('nextForwardToken'),log_bytes=total,log_read_status='streaming')
        c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode(current),aid))
