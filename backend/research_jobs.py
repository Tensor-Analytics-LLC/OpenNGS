"""Bounded durable dispatcher; remote science never runs on the API server."""
import concurrent.futures,json,time
from . import db,entitlements,graph,provisioning,runner
from .config import TERMINAL

def initialize():
    with db.tx() as c:c.execute('CREATE TABLE IF NOT EXISTS research_dispatch(run_id TEXT PRIMARY KEY,lease_until REAL NOT NULL,state TEXT NOT NULL,recoveries INTEGER NOT NULL DEFAULT 0,error TEXT)')

def claim():
    with db.tx() as c:
        rows=c.execute("SELECT r.* FROM runs r LEFT JOIN research_dispatch d ON d.run_id=r.id WHERE r.state IN ('queued','running','cancelling','interrupted') AND (d.run_id IS NULL OR (d.lease_until<? AND d.state!='action-required')) ORDER BY r.created LIMIT 100",(time.time(),)).fetchall()
        for raw in rows:
            row=dict(raw);snapshot=json.loads(row['graph']).get('_execution',{})
            if snapshot.get('access')!='research-pro':continue
            prior=c.execute('SELECT * FROM research_dispatch WHERE run_id=?',(row['id'],)).fetchone()
            if prior and prior['recoveries']>=3:
                c.execute("UPDATE research_dispatch SET state='action-required',error='Three recovery attempts exhausted; provider resources and reservations require operator reconciliation' WHERE run_id=?",(row['id'],));continue
            active=c.execute("SELECT COUNT(*) FROM research_dispatch d JOIN runs r ON r.id=d.run_id WHERE r.user_id=? AND d.lease_until>? AND r.state NOT IN ('succeeded','failed','cancelled')",(row['user_id'],time.time())).fetchone()[0]
            if active>=snapshot['allowance']['max_concurrent_jobs']:continue
            c.execute("INSERT INTO research_dispatch VALUES(?,?,'running',0,NULL) ON CONFLICT(run_id) DO UPDATE SET lease_until=excluded.lease_until,state='running',recoveries=research_dispatch.recoveries+1",(row['id'],time.time()+90))
            return row

def execute(row):
    g=json.loads(row['graph']);nodes={n['id'] for n in g['nodes']};order=[]
    while nodes:
        ready=sorted(n for n in nodes if all(e['source'] in order for e in g['edges'] if e['target']==n))
        if not ready:raise ValueError('Persisted research DAG contains a cycle')
        order.extend(ready);nodes.difference_update(ready)
    runner.run(row['id'],order)

def main():
    initialize();pool=concurrent.futures.ThreadPoolExecutor(max_workers=2);active={};last_provision=0
    while True:
        if time.monotonic()-last_provision>15:
            last_provision=time.monotonic()
            try:provisioning.once()
            except Exception as e:print(db.encode({'event':'capacity.retry','exception':type(e).__name__}),flush=True)
        for rid,future in list(active.items()):
            if future.done():
                error=None
                try:future.result()
                except Exception as e:error=type(e).__name__
                with db.tx() as c:c.execute("UPDATE research_dispatch SET lease_until=?,state=?,error=? WHERE run_id=?",(time.time()+30,'reconciling' if error else 'finished',error,rid))
                del active[rid]
            else:
                with db.tx() as c:c.execute('UPDATE research_dispatch SET lease_until=? WHERE run_id=?',(time.time()+90,rid))
        if len(active)<2:
            row=claim()
            if row:active[row['id']]=pool.submit(execute,row)
        time.sleep(5)

initialize()
if __name__=='__main__':main()
