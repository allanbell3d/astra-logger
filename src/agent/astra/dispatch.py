"""Narrow adapter from review eligibility to native Hermes cron dispatch.
No schedule loop, model call or acknowledgement lives here.
Dual-lane: manifest ids may carry 'rca' (lane A) and 'rca-b' (lane B); each is
gated by its own status key and re-armed on the same triage approval, staggered.
"""
from datetime import datetime, timezone, timedelta


def dispatch_once(status, ids, get_job, trigger_job, rearm_oneshot, now=None):
    now = now or datetime.now(timezone.utc)
    roles = []
    lane_a_id = ids.get('rca-a') or ids.get('rca')
    if lane_a_id:
        roles.append(('rca-a', 'dispatch_rca_a'))
    if ids.get('rca-b'):
        roles.append(('rca-b', 'dispatch_rca_b'))
    results = []
    for key, gate_key in roles:
        if not status.get(gate_key):
            continue
        jid = lane_a_id if key == 'rca-a' else ids[key]
        job = get_job(jid)
        if not job or job.get('schedule', {}).get('kind') != 'once':
            raise ValueError('RCA must be an existing native one-shot card')
        if job.get('state') == 'paused' and job.get('paused_reason') != 'ASTRA trigger-only standby':
            results.append({'dispatched': False, 'role': key, 'reason': 'RCA paused by operator'})
            continue
        if job.get('enabled') or job.get('state') == 'running':
            results.append({'dispatched': False, 'role': key, 'reason': 'RCA already armed/running'})
            continue
        rearm_oneshot(jid, (now + timedelta(seconds=5 + 5 * len(results))).isoformat())
        results.append({'dispatched': True, 'role': key, 'job': jid})
    if not results:
        return {'dispatched': False, 'reason': 'no eligible early work'}
    if len(results) == 1:
        return results[0]
    return {'dispatched': any(r.get('dispatched') for r in results), 'lanes': results}
