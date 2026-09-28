#!/usr/bin/env python3
"""Run one host-local ASTRA increment under a single-writer lock."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys

SCRIPTS = Path.home() / ".hermes" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from astra.raw_pipeline import run_raw_pipeline  # noqa: E402


def main() -> int:
    logs = Path.home() / "logs-watch"
    store_dir = logs / "enriched"
    groups_dir = logs / "groups"
    compressed_dir = logs / "compressed"
    for path in (store_dir, groups_dir, compressed_dir):
        path.mkdir(parents=True, exist_ok=True)
    lock_path = logs / "astra.lock"
    compat = logs / ".compat-enriched.jsonl"
    compressed_file = compressed_dir / "compressed-events.jsonl"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        result = run_raw_pipeline(
            hermes_home=str(Path.home() / ".hermes"),
            host=os.uname().nodename,
            enriched_path=str(compat),
            grouped_path=str(groups_dir / "groups-current.jsonl"),
            state_path=str(logs / "astra-state.json"),
            policy_path=str(SCRIPTS / "astra-severity-policy.json"),
            store_dir=str(store_dir),
            compressed_path=str(compressed_file),
        )
        if compat.exists():
            compat.unlink()
        if result["processed"] or result["backlog_bytes"] or result["errors"] or result.get("dropped_old"):
            print(json.dumps(result, ensure_ascii=False))
        route = Path.home() / '.hermes/scripts/astra-host.json'
        profile_name = json.loads(route.read_text())['profile'] if route.exists() else None
        if profile_name is not None:
            import re
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', profile_name):
                raise ValueError('Invalid ASTRA host profile')
        manifest = Path.home() / '.hermes/profiles' / (profile_name or '__unconfigured__') / 'astra-jobs.json'
        if manifest.exists() and json.loads(manifest.read_text()).get('enabled'):
            # Sync/account and arm native jobs in a bounded systemd-owned adapter.
            # The live profile gateway owns ticking/model execution, not capture.
            import subprocess
            env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
                       DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
            try:
                dispatch = subprocess.run(['systemctl','--user','start','--no-block','astra-health-dispatch.service'],
                                          env=env, timeout=8, capture_output=True, text=True)
                if dispatch.returncode:
                    print('ASTRA native cron dispatch failed: ' + dispatch.stderr[:500], file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f'ASTRA native cron dispatch failed: {type(exc).__name__}', file=sys.stderr)
        return 1 if result["errors"] else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
