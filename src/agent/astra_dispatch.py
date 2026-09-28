#!/usr/bin/env python3
"""Capture-triggered adapter to STOCK Hermes cron; never calls a model."""
from pathlib import Path
import json,os,subprocess,sys,time,importlib.util
sys.path.insert(0,str(Path.home()/'.hermes/scripts'))
from astra.review import ReviewStore
from astra.runtime import reconcile_results
from astra.dispatch import dispatch_once
from cron.jobs import get_job,trigger_job,rearm_oneshot

TTL=60  # phase-models failsafe; restore the pre-apply snapshot at most 60s after arm
LEDGER=None  # dedup ledger path; set in main() — one Discord send per unique notice, ever

def _discord_send(channel, message):
    """Send via THIS profile's own Discord identity (ASTRA bot token in profile .env).

    The `hermes send` CLI path is unusable here (Discord plugin not registered in
    CLI mode) and the global bot identity would leak into the channel. Direct API
    with the profile token guarantees the notification carries the ASTRA bot id,
    so the owner can lock the channel to invited bots only.
    """
    import re as _re, urllib.request
    tok=None
    try:
        env_text=open(Path(os.environ['HERMES_HOME'])/'.env').read()
        m=_re.search(r'^DISCORD_BOT_TOKEN=(.*)$',env_text,_re.M)
        if m:tok=m.group(1).strip().strip('"').strip("'")
    except OSError:pass
    if not tok:
        print('discord send skipped: no DISCORD_BOT_TOKEN in profile .env',file=sys.stderr)
        return False
    req=urllib.request.Request(f'https://discord.com/api/v10/channels/{channel}/messages',
        data=json.dumps({'content':message}).encode(),
        headers={'Authorization':f'Bot {tok}','Content-Type':'application/json',
                 'User-Agent':'DiscordBot (https://github.com/astra-pipeline, v0.5.4)'})
    try:
        urllib.request.urlopen(req,timeout=15);return True
    except Exception as e:
        print(f'discord send failed: {e}',file=sys.stderr);return False

def _already_sent(message):
    """Owner anti-spam rule: a repeated identical notice must not re-send every tick."""
    if not LEDGER:return False
    import hashlib
    key=hashlib.sha256(message.encode()).hexdigest()
    try:
        return key in Path(LEDGER).read_text().splitlines()
    except FileNotFoundError:
        return False

def _mark_sent(message):
    """Record a notice only after Discord accepted it."""
    if not LEDGER:return
    import hashlib
    key=hashlib.sha256(message.encode()).hexdigest()
    with open(LEDGER,'a') as f:f.write(key+'\n')

def _phase_models():
    p=Path(os.environ['HERMES_HOME'])/'scripts'/'astra_phase_models.py'
    spec=importlib.util.spec_from_file_location('astra_phase_models',p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m

def _arm_and_watch(pm,prefixes):
    # Inline (NOT a detached child): this unit runs with KillMode=control-group,
    # so any detached watcher would be reaped when the oneshot exits. The loop
    # adds <=TTL+2s to the unit runtime (TimeoutStartSec=120).
    pm.apply_phase('rca')   # idempotent: skips write if already equal; creates the restore backup
    gen=int(time.time()*1000)
    pm._arm_write(gen,'rca',prefixes,time.time()+TTL)
    print(f'phase-models: armed gen={gen} prefixes={prefixes} ttl={TTL}s',flush=True)
    pm._watch(gen,TTL,time.time(),prefixes)

def main():
    global LEDGER
    profile=Path(os.environ['HERMES_HOME'])
    m=json.loads((profile/'astra-jobs.json').read_text())
    if not m.get('enabled'):return
    root=Path(m['root']);s=ReviewStore(root/'review.sqlite')
    try:
        s.sync_sources(root/'enriched');s.prune_evidence()
        jobs={role:get_job(jid) for role,jid in m['jobs'].items()}
        LEDGER=str(root/'.notices.sent')
        for notice in reconcile_results(s,jobs,manifest=m):
            message=f"ASTRA host={m['host']} agent={profile.name} "+notice
            print(message,flush=True)
            if _already_sent(message):
                print('notice suppressed (already delivered): same state, no repeat',flush=True)
                continue
            # Existing native delivery path, once per completed run; no inference.
            if not _discord_send(m['deliver'].split(':',1)[1],message):
                print('ASTRA failure-notice delivery failed',file=sys.stderr)
            else:
                _mark_sent(message)
        s.promote_urgent()
        res=dispatch_once(s.status(),m['jobs'],get_job,trigger_job,rearm_oneshot)
        print(json.dumps(res))
        lanes=res.get('lanes') or ([res] if res.get('dispatched') and res.get('job') else [])
        prefixes=[f"cron_{r['job']}_" for r in lanes if r.get('dispatched')]
        if prefixes:
            try:
                # apply rca BEFORE the +5s/+10s card fires spawn; watch both sessions,
                # restore default when both are alive (or at the failsafe deadline)
                _arm_and_watch(_phase_models(),prefixes)
            except Exception as e:
                reason=f'phase-models refused apply rca: {e}; config untouched, sessions spawn on resting'
                print(reason,file=sys.stderr)
                try:
                    # owner-visible refusal: same native channel as the notices above
                    _discord_send(m['deliver'].split(':',1)[1],
                                  f'ASTRA {profile.name}: phase-models apply rca REFUSED — config untouched. {e}')
                except Exception as se:
                    print(f'refusal delivery failed: {se}',file=sys.stderr)
    finally:s.close()
if __name__=='__main__':main()
