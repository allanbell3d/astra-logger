"""Small native-cron input adapter; SQLite review state, no model/scheduler."""
from pathlib import Path
import json,os,shlex,sys
from astra.review import ReviewStore,packed,digest


def prepare_job(role, manifest, store, prune=True):
    root=Path(manifest['root'])
    store.sync_sources(root/'enriched')
    if prune:store.prune_evidence()
    if role=='daily':
        status=store.status()
        if not any(status[k] for k in ('pending_triage','pending_rca','blocked_batches','findings_evidence_expired')) and not store.get_history(limit=1)['findings_history']:
            return {'wakeAgent':False,'reason':'no review history or pending work'}
        packet={'kind':'daily','host':manifest['host'],'status':status,'history':store.get_history(limit=5),'retention':store.retention_report(root/'enriched')}
        fingerprint=digest(packet)
        store.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT)')
        previous=store.db.execute("SELECT value FROM runtime_meta WHERE key='daily_completed'").fetchone()
        if previous and previous[0]==fingerprint:return {'wakeAgent':False,'reason':'daily review unchanged'}
        with store.db:store.db.execute("INSERT OR REPLACE INTO runtime_meta VALUES('daily_prepared',?)",(fingerprint,))
        packet.update(wakeAgent=True)
    else:
        packet=store.prepare(role,max_bytes=24000)
        if not packet['wakeAgent']:return packet
    helper=Path.home()/'.hermes/scripts/astra_review.py'
    packet['helper']=manifest.get('helper') or ('python3 '+shlex.quote(str(helper))+' --root '+shlex.quote(str(root/'enriched'))+' --state '+shlex.quote(str(store.path)))
    if role=='triage':
        skeleton=root/'review-results'/f"triage-{packet['batch']}-template.json"
        skeleton.parent.mkdir(parents=True,exist_ok=True)
        skeleton.write_text(json.dumps([{'id':x['id'],'decision':None,'reason':''} for x in packet['items']],indent=2)+'\n')
        packet['result_template']=str(skeleton)
        packet['result_path']=str(root/'review-results'/f"triage-{packet['batch']}.json")
    lane_dir={'rca-a':'rca-a','rca-b':'rca-b','rca':'rca-a'}.get(role,'rca')
    packet['result_dir']=str(root/'review-results');(root/'review-results').mkdir(parents=True,exist_ok=True)
    packet['archive_dir']=str(root/lane_dir);(root/lane_dir).mkdir(parents=True,exist_ok=True)
    packet['shared_dir']=str(Path(manifest['shared'])/lane_dir)
    packet['commissioning']=bool(manifest.get('commissioning'))
    if manifest.get('case_file'):packet['case_file']=manifest['case_file']
    known=root/'triage-known-issues.md'
    if role=='triage' and packet.get('wakeAgent') and known.is_file():
        text=known.read_text(encoding='utf-8')
        if len(text.encode('utf-8'))>8192:
            raise ValueError('Owner known-issue reference exceeds 8KiB bound')
        packet['owner_known_issues']={'source':str(known),'text':text,
            'scope':'Owner historical dispositions, not proof of current repair. Match exact incident context; new material impact may warrant RCA. Uncertain questions in this reference are not confirmations.'}
    if len(packed(packet).encode())>32768:raise ValueError('Review packet exceeds 32KiB safety bound')
    return packet


def _recover_rca_response(batch_id, job_id, not_before=0):
    home=Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes')))
    output_dir=home/'cron'/'output'/str(job_id)
    if not output_dir.is_dir():return None
    paths=sorted(output_dir.glob('*.md'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths[:5]:
        if path.stat().st_mtime < not_before:continue
        text=path.read_text(errors='replace')
        if '## Response' not in text:continue
        prompt,response=text.split('## Response',1)
        if batch_id not in prompt:continue
        report=response.strip()
        if report and report!='[SILENT]':return report
    return None


def reconcile_results(store, jobs, now=None, manifest=None):
    """Do not equate a native successful prose reply with review completion."""
    from datetime import datetime
    import time
    now=time.time() if now is None else now
    notices=[]
    if getattr(store,'delivery_config',None):
        from astra.delivery import reconcile_best_effort
        delivery_sync=reconcile_best_effort(store)
        if not delivery_sync.get('ok'):
            # Existing notice routing/ledger handles a stable actionable error;
            # expected dashboard downtime is represented as DEGRADED, not a crash.
            notices.append('function=delivery sync deferred; '+delivery_sync['error'])
    store.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT)')
    for role,job in jobs.items():
        if not job or not job.get('last_run_at') or job.get('state')=='running':continue
        key='checked:'+job['id'];stamp=job['last_run_at']
        old=store.db.execute('SELECT value FROM runtime_meta WHERE key=?',(key,)).fetchone()
        kinds=('rca','rca-a') if role in ('rca','rca-a') else (role,)
        marks=','.join('?' for _ in kinds)
        running=store.db.execute(
            f"SELECT COUNT(*) FROM batches WHERE kind IN ({marks}) AND status='running'",kinds
        ).fetchone()[0]
        if old and old[0]==stamp and not (
            role in ('rca','rca-a','rca-b') and running and job.get('last_status')=='ok'
        ):continue
        completed=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
        with store.db:
            for batch in store.db.execute(f"SELECT * FROM batches WHERE kind IN ({marks}) AND status='running'",kinds).fetchall():
                started=store.db.execute('SELECT value FROM runtime_meta WHERE key=?',('lease_started:'+batch['id'],)).fetchone()
                lease_start=float(started[0]) if started else batch['created']
                if completed < lease_start:continue
                rejected_key=f"rejected:{batch['id']}:{batch['attempts']}:{stamp}"
                if store.db.execute('SELECT 1 FROM runtime_meta WHERE key=?',(rejected_key,)).fetchone():continue
                if role in ('rca','rca-a','rca-b') and job.get('last_status')=='ok':
                    report=_recover_rca_response(batch['id'],job['id'],not_before=lease_start)
                    if report:
                        shared=Path((manifest or {}).get('shared',store.path.parent/'shared'/'rca'))/('rca-b' if role=='rca-b' else 'rca-a')
                        try:
                            store.complete_rca(batch['id'],report,store.path.parent/('rca-b' if role=='rca-b' else 'rca-a'),shared,now=now)
                        except ValueError as exc:
                            # Contract-invalid report: count the attempt instead of crashing dispatch.
                            # Next attempt gives the worker agent a second chance, then blocks.
                            attempts=batch['attempts']
                            status='blocked' if attempts>=2 else 'running'
                            store.db.execute('UPDATE batches SET status=?,attempts=?,lease_until=? WHERE id=?',(status,attempts,now,batch['id']))
                            store.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES(?,?)',(rejected_key,str(exc)))
                            notices.append(f"function=rca report rejected by contract; batch={batch['id']} attempts={attempts}/2 state={status}. error={exc}")
                            continue
                        continue
                status='blocked' if batch['attempts']>=2 else 'running'
                store.db.execute('UPDATE batches SET status=?,lease_until=? WHERE id=?',(status,now,batch['id']))
                notices.append(f"function={role} analysis incomplete; batch={batch['id']} attempts={batch['attempts']}/2 state={status}. Evidence remains unresolved; no repair executed.")
            if job.get('last_delivery_error') or job.get('last_delivery_unverified'):
                notices.append(f"function={role} notification failed/unverified; job={job['id']}. Analysis and report state retained separately.")
            if role=='daily' and job.get('last_status')=='ok':
                store.db.execute("INSERT OR REPLACE INTO runtime_meta SELECT 'daily_completed',value FROM runtime_meta WHERE key='daily_prepared'")
            store.db.execute('INSERT OR REPLACE INTO runtime_meta VALUES(?,?)',(key,stamp))
    return notices


def pre_script(role):
    profile=Path(os.environ['HERMES_HOME']).resolve()
    manifest=json.loads((profile/'astra-jobs.json').read_text())
    if not manifest.get('enabled'):
        print('{"wakeAgent":false,"reason":"ASTRA disabled by operator"}');return
    store=ReviewStore(Path(manifest['root'])/'review.sqlite')
    try:
        packet=prepare_job(role,manifest,store)
        if role in ('triage','rca','rca-a','rca-b') and packet.get('wakeAgent'):
            # Hermes runs this pre-script before loading the job's effective config.
            # Keep phase diagnostics off stdout: stdout is the agent's JSON packet.
            import contextlib,importlib.util
            path=profile/'scripts'/'astra_phase_models.py'
            spec=importlib.util.spec_from_file_location('astra_phase_models',path)
            phase=importlib.util.module_from_spec(spec);spec.loader.exec_module(phase)
            phase_name='triage' if role=='triage' else 'rca'
            job_role='rca-a' if role=='rca' and 'rca-a' in manifest['jobs'] else role
            with contextlib.redirect_stdout(sys.stderr):
                phase.arm_restore(phase_name,[f"cron_{manifest['jobs'][job_role]}_"],60)
        print(packed(packet))
    finally:store.close()
