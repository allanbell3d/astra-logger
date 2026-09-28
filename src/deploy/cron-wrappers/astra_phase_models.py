#!/usr/bin/env python3
"""ASTRA phase-models v4 — apply owner playbook sections to config.yaml at pipeline moments.

OWNER RULES (binding):
  * models.yaml is OWNER-owned. This script READS it, never writes it.
  * models.yaml is parsed as section-delimited chunks: bare `default:` / `triage:` /
    `rca:` lines are delimiters; content after each is config-style YAML at column 0
    (same indentation as config.yaml). Each chunk is parsed separately.
  * A BLOCK is a column-0 key plus everything indented under it.
  * Every block PRESENT in the section REPLACES the same-named block in config
    wholesale. Blocks ABSENT from the section are left untouched — NOTHING is
    ever silently removed.
  * TEXT-SPLICE WRITE: replaced blocks are spliced as RAW TEXT from models.yaml
    over the config's block line range. Comments, quoting style and multiline
    scalars come out exactly as the owner wrote them; untouched regions of
    config.yaml stay byte-identical. No YAML re-serialization happens, ever.
  * Unknown/extra keys are applied verbatim. This engine does not validate
    their downstream meaning or safety in Hermes.
  * Fail loud ONLY for real structural breakage: bad YAML anywhere, section not a
    mapping, block not column-0 text, managed entry present but wrong type.
    NEVER over model names or values — those belong to the owner.
  * Any failure leaves config.yaml byte-identical. Verify the spliced text in
    memory (full parse + per-block deep-equal + no-vanished-blocks) before any
    write; write = temp file + atomic os.replace. Timestamped backups; never pruned.
  * Concurrency: flock guard; a second apply while one is mid-flight refuses.

Historical session observation (Hermes source + agent.log 2026-09-21;
revalidate the configured runtime before relying on this during deployment):
  a session snapshots config.yaml ONCE at spawn (agent_init._init_fallback_chain);
  failover walks the in-memory chain and NEVER re-reads config from disk (429 ->
  fallback switch measured at 93 ms, 120 subsequent calls stayed on the chain).
  So restoring AFTER a session has emitted its first `agent.turn_context` log
  line (model loaded) cannot affect that session.

Commands:
  apply <phase>   replace config blocks with section blocks (phase: default|triage|rca)
  apply <phase> --dry   verify only, write nothing
  restore         undo: restore config.yaml byte-exact from THIS apply's backup
                  (the pre-apply snapshot recorded at last successful write).
                  Not "newest bak-phase-* on disk" — leftover/stale files are
                  ignored. Returns to whatever the owner's resting config WAS.
                  Does NOT apply the playbook default section (`apply default`).
  arm-restore <phase> PREFIX [PREFIX...] [--ttl S]
                  apply <phase> (rca covers both lanes), record watched session
                  prefixes + failsafe deadline (default 60s), spawn a detached
                  watcher; it byte-restores as soon as every PREFIX has a live
                  session in agent.log (`agent.turn_context` line newer than arm
                  time = model loaded), or at the deadline, whichever first.
                  A newer arm-restore supersedes an older watcher (generation
                  counter); the loser exits without touching config.
  show <phase>    print the section as YAML (read-only)
  current         print live config model/fallback summary (read-only)

Run with the profile venv python (needs PyYAML). HERMES_HOME selects the profile.
"""
import sys, os, re, json, shutil, fcntl, datetime, subprocess, time, stat
import yaml

HERMES_HOME = os.environ.get('HERMES_HOME') or os.path.expanduser(
    '~/.hermes/profiles/astra-dell-health-monitor')
MODELS = os.path.join(HERMES_HOME, 'models.yaml')
CONFIG = os.path.join(HERMES_HOME, 'config.yaml')
LOCK = os.path.join(HERMES_HOME, 'logs', 'phase-models.lock')
LOG = os.path.join(HERMES_HOME, 'logs', 'phase-models.log')
AGENT_LOG = os.path.join(HERMES_HOME, 'logs', 'agent.log')
ARM_FILE = os.path.join(HERMES_HOME, 'logs', 'phase-models.arm.json')
LAST_BACKUP = os.path.join(HERMES_HOME, 'logs', 'phase-models.last-backup')
SECTIONS = ('default', 'triage', 'rca')
DEFAULT_TTL = 60  # retained failsafe; current runtime load/restore ordering needs validation


class PhaseFail(Exception):
    """Structural refusal — config untouched. str(e) is the reason."""


def fail(msg):
    logline(f'FAIL: {msg}')
    raise PhaseFail(msg)


def _read_text(path):
    """Read text with no newline translation (CRLF stays CRLF)."""
    with open(path, 'r', encoding='utf-8', newline='') as f:
        return f.read()


def _write_text_tmp(tmp, text):
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _write_bytes_tmp(tmp, data):
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _chunk_models(txt):
    """Split models.yaml text on bare section-delimiter lines.

    Line endings inside a section are preserved (no CRLF→LF rewrite).
    Duplicate `default:` / `triage:` / `rca:` headers refuse.
    """
    chunks, cur, buf = {}, None, []
    for line in txt.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        if re.match(r'^(default|triage|rca):$', raw):
            if cur is not None:
                if cur in chunks:
                    fail(f'duplicate "{cur}:" section header in models.yaml')
                chunks[cur] = ''.join(buf)
            cur, buf = raw[:-1], []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        if cur in chunks:
            fail(f'duplicate "{cur}:" section header in models.yaml')
        chunks[cur] = ''.join(buf)
    return chunks


def parse_models(path=MODELS):
    """Chunk-parse models.yaml on bare section delimiters; parse each chunk as YAML."""
    if not os.path.exists(path):
        fail(f'{path} not found')
    chunks = _chunk_models(_read_text(path))
    if not chunks:
        fail('no `default:` / `triage:` / `rca:` header lines found in models.yaml')
    secs = {}
    for name, chunk in chunks.items():
        try:
            data = yaml.safe_load(chunk) if chunk.strip() else {}
        except yaml.YAMLError as e:
            fail(f'bad YAML in models.yaml section "{name}": {e}')
        if not isinstance(data, dict):
            fail(f'section "{name}" is not a mapping (got {type(data).__name__})')
        secs[name] = data
    return secs


def raw_section_blocks(phase):
    """Verbatim text lines of each column-0 block in a models.yaml section."""
    if not os.path.exists(MODELS):
        fail(f'{MODELS} not found')
    chunks = _chunk_models(_read_text(MODELS))
    if phase not in chunks:
        fail(f'no "{phase}:" section in models.yaml')
    order, blocks, _ = split_block_lines(chunks[phase].splitlines(keepends=True))
    _refuse_dup_keys(order, f'models.yaml section "{phase}"')
    return blocks


def type_check(phase, sec):
    """Structural-only validation. Values and model names are NEVER validated here."""
    for key in ('model', 'agent', 'auxiliary', 'delegation', 'plugins', 'memory'):
        if key in sec and not isinstance(sec[key], dict):
            fail(f'{phase}.{key} must be a mapping, got {type(sec[key]).__name__}')
    if 'fallback_providers' in sec and not isinstance(sec['fallback_providers'], list):
        fail(f'{phase}.fallback_providers must be a list, got {type(sec["fallback_providers"]).__name__}')


def split_block_lines(lines):
    """Split config-style text into column-0 blocks.

    A block = a column-0 `key:` line plus every following line until the next
    column-0 line (key, comment or anything else non-indented) or EOF.
    Returns (order, blocks, ranges):
      order  — keys in file order
      blocks — key -> verbatim lines (each ending with \\n)
      ranges — key -> (start_idx, end_idx_exclusive) into `lines`
    """
    order, blocks, ranges = [], {}, {}
    cur_key, start = None, None
    for i, ln in enumerate(lines):
        at_col0 = bool(re.match(r'\S', ln))
        if at_col0:
            stripped = ln.strip()
            is_comment = stripped.startswith('#')
            is_list_item = stripped.startswith('-')
            is_key = (not is_comment and not is_list_item and stripped and ':' in ln)
            if is_key:
                if cur_key is not None:
                    ranges[cur_key] = (start, i)
                cur_key = ln.split(':', 1)[0].strip()
                start = i
                order.append(cur_key)
                blocks[cur_key] = [ln if ln.endswith('\n') else ln + '\n']
                continue
            if is_list_item and cur_key is not None:
                blocks[cur_key].append(ln if ln.endswith('\n') else ln + '\n')
                continue
            # column-0 comment / anything else: block boundary
            if cur_key is not None:
                ranges[cur_key] = (start, i)
                cur_key = None
        elif cur_key is not None:
            blocks[cur_key].append(ln if ln.endswith('\n') else ln + '\n')
    if cur_key is not None:
        ranges[cur_key] = (start, len(lines))
    return order, blocks, ranges


def _refuse_dup_keys(order, where):
    seen, dups = set(), []
    for k in order:
        if k in seen and k not in dups:
            dups.append(k)
        seen.add(k)
    if dups:
        fail(f'duplicate column-0 key(s) in {where}: {dups} — refusing')


def deep_equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def logline(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(f'{datetime.datetime.now().isoformat(timespec="seconds")} {msg}\n')


def _acquire_lock():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lockf = open(LOCK, 'w')
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail('another phase-models apply is mid-flight (lock held)')
    return lockf


def _write_last_backup(path):
    os.makedirs(os.path.dirname(LAST_BACKUP), exist_ok=True)
    tmp = LAST_BACKUP + '.tmp'
    with open(tmp, 'w') as f:
        f.write(path)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LAST_BACKUP)


def _read_last_backup():
    try:
        p = open(LAST_BACKUP).read().strip()
    except FileNotFoundError:
        return None
    return p or None


def _clear_last_backup():
    try:
        os.unlink(LAST_BACKUP)
    except FileNotFoundError:
        pass


def _validate_backup_path(src):
    if not src or not os.path.isfile(src):
        fail('no last-apply backup recorded — nothing to restore from')
    if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(CONFIG)):
        fail('last-apply backup path is not beside config.yaml — refusing')
    if not os.path.basename(src).startswith('config.yaml.bak-phase-'):
        fail(f'last-apply backup name is not a phase backup: {os.path.basename(src)}')
    return src


def _chmod_like(src, dest):
    os.chmod(dest, stat.S_IMODE(os.stat(src).st_mode))


def apply_phase(phase, dry=False, arm=None):
    if phase not in SECTIONS:
        fail(f'unknown phase "{phase}" (use: {"|".join(SECTIONS)})')
    secs = parse_models()
    sec = secs.get(phase) or {}
    if not sec:
        fail(f'no content under "{phase}:" in models.yaml — nothing to apply')
    type_check(phase, sec)
    sec_text = raw_section_blocks(phase)

    lockf = _acquire_lock()  # concurrency guard: one apply at a time, ever
    try:
        cfg_text = _read_text(CONFIG)
        cfg_lines = cfg_text.splitlines(keepends=True)
        cfg_order, cfg_blocks, cfg_ranges = split_block_lines(cfg_lines)
        _refuse_dup_keys(cfg_order, 'config.yaml')
        live = yaml.safe_load(cfg_text) or {}
        if not isinstance(live, dict):
            fail('live config.yaml does not parse to a mapping — refusing')

        def _finish_arm():
            if not arm:
                return None
            since = time.time()
            data = {'generation': arm['generation'], 'phase': phase,
                    'prefixes': arm['prefixes'],
                    'deadline_epoch': since + arm['ttl'], 'armed_epoch': since}
            _arm_write_unlocked(data)
            return data

        # idempotence: skip only when listed blocks are already byte-identical
        # (semantic-equal but different quoting/comments still splices)
        if all(k in cfg_blocks and ''.join(cfg_blocks[k]) == ''.join(sec_text[k])
               for k in sec):
            print(f'SKIP "{phase}": config already matches section — no write')
            logline(f'skip {phase} (already applied)')
            return None if dry else _finish_arm()

        existing = _read_last_backup()
        if existing and os.path.isfile(existing):
            fail('active unrestored apply recorded — restore first '
                 '(refusing overlapping apply/arm)')

        # TEXT SPLICE: overwrite each listed block's line range with the
        # section's verbatim lines; untouched regions never move a byte.
        # Ranges refer to the ORIGINAL file, so all splices are collected
        # first and applied against the original line list in one pass —
        # never incrementally (an incremental splice would shift the
        # indices of every later range once lengths differ).
        splices = []
        appended = []
        for k in sec:
            if k not in sec_text:
                fail(f'section block "{k}" is not a column-0 block in models.yaml '
                     f'— refusing (indented content under "{phase}:"?)')
            if k in cfg_ranges:
                s, e = cfg_ranges[k]
                splices.append((s, e, sec_text[k]))
            else:
                appended.append(k)
        splices.sort(key=lambda t: t[0])
        new_lines = []
        pos = 0
        for s, e, rep in splices:
            if s < pos:
                fail(f'internal: overlapping block ranges at line {s} — refusing')
            new_lines.extend(cfg_lines[pos:s])
            new_lines.extend(rep)
            pos = e
        new_lines.extend(cfg_lines[pos:])
        if appended:
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines[-1] += '\n'
            for k in appended:
                new_lines.append('\n')
                new_lines.extend(sec_text[k])

        out = ''.join(new_lines)

        # in-memory verify BEFORE any write: full parse, per-block equality,
        # and no pre-existing block may vanish
        try:
            rebuilt = yaml.safe_load(out)
        except yaml.YAMLError as e:
            first = str(e).splitlines()[0] if str(e) else 'unknown'
            fail(f'post-splice output does not parse — refusing to write: {first}')
        if not isinstance(rebuilt, dict):
            fail('post-splice parse did not yield a mapping — refusing to write')
        for k in sec:
            if not deep_equal(rebuilt.get(k), sec[k]):
                fail(f'post-splice verify failed for block "{k}" — refusing to write')
        vanished = [k for k in live if k not in rebuilt]
        if vanished:
            fail(f'post-splice verify: pre-existing blocks vanished: {vanished} — refusing')

        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        if dry:
            print(f'DRY ok: {len(sec)} blocks would be applied for phase "{phase}": {sorted(sec)}')
            return None

        orig_mode = stat.S_IMODE(os.stat(CONFIG).st_mode)
        backup = f'{CONFIG}.bak-phase-{phase}-{ts}'
        n = 0
        while os.path.exists(backup):
            n += 1
            backup = f'{CONFIG}.bak-phase-{phase}-{ts}-{n}'
        shutil.copy2(CONFIG, backup)
        _write_last_backup(os.path.abspath(backup))

        tmp = f'{CONFIG}.tmp-phase-{ts}'
        _write_text_tmp(tmp, out)
        if not isinstance(yaml.safe_load(out), dict):
            os.unlink(tmp)
            fail('final parse check failed — temp discarded')
        os.chmod(tmp, orig_mode)
        os.replace(tmp, CONFIG)

        print(f'APPLIED "{phase}" -> {len(sec)} blocks replaced: {sorted(sec)}')
        print(f'backup: {os.path.basename(backup)} (untouched blocks byte-identical)')
        logline(f'apply {phase} blocks={sorted(sec)} backup={os.path.basename(backup)} pid={os.getpid()}')
        return _finish_arm()
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def _arm_write_unlocked(data):
    """Write ARM_FILE. Caller must already hold the phase-models flock."""
    os.makedirs(os.path.dirname(ARM_FILE), exist_ok=True)
    tmp = f'{ARM_FILE}.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ARM_FILE)


def _arm_write(gen, phase, prefixes, deadline):
    """Record armed state under the same flock that guards config writes."""
    lockf = _acquire_lock()
    try:
        data = {'generation': gen, 'phase': phase, 'prefixes': prefixes,
                'deadline_epoch': deadline, 'armed_epoch': time.time()}
        _arm_write_unlocked(data)
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def arm_restore(phase, prefixes, ttl):
    """Apply the phase, record watched prefixes, spawn the detached restore watcher.

    Apply + arm file write happen under one flock so a restore cannot sneak
    into the gap. Generation check on restore also happens under that flock.
    """
    if phase not in ('triage', 'rca'):
        fail(f'arm-restore phase must be triage or rca (got "{phase}")')
    if not prefixes:
        fail('arm-restore needs at least one session-id prefix')
    gen = int(time.time() * 1000)
    data = apply_phase(phase, arm={'generation': gen, 'prefixes': prefixes, 'ttl': ttl})
    since = (data or {}).get('armed_epoch') or time.time()
    logline(f'arm gen={gen} phase={phase} prefixes={prefixes} ttl={ttl}')
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), '_watch',
         str(gen), str(ttl), str(since), *prefixes],
        env=dict(os.environ), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    print(f'ARMED gen={gen} phase={phase} prefixes={prefixes} ttl={ttl}s '
          f'(restore fires when all sessions are alive, or at deadline)')


def restore_from_backup(dry=False, expected_gen=None):
    """Undo: return config.yaml to the exact pre-apply bytes of THIS apply.

    Uses logs/phase-models.last-backup (written at apply), never the newest
    bak-phase-* glob. Extra keys added by apply are dropped — that is the undo.
    Backup must parse to a mapping. Mode bits of config.yaml are preserved.
    """
    lockf = _acquire_lock()
    try:
        if expected_gen is not None:
            try:
                arm = json.load(open(ARM_FILE))
            except Exception:
                arm = {}
            if arm.get('generation') != expected_gen:
                logline(f'watch gen={expected_gen}: superseded '
                        f'(armed gen={arm.get("generation")}) — not restoring')
                return False
        src = _validate_backup_path(_read_last_backup())
        raw = open(src, 'rb').read()
        try:
            data = yaml.safe_load(raw.decode('utf-8'))
        except yaml.YAMLError as e:
            fail(f'backup {os.path.basename(src)} does not parse: {e}')
        if not isinstance(data, dict):
            fail(f'backup {os.path.basename(src)} does not parse to a mapping — refusing')
        if dry:
            print(f'DRY ok: would restore byte-exact from {os.path.basename(src)}')
            return True
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        orig_mode = stat.S_IMODE(os.stat(CONFIG).st_mode)
        tmp = f'{CONFIG}.tmp-restore-{ts}'
        _write_bytes_tmp(tmp, raw)
        os.chmod(tmp, orig_mode)
        os.replace(tmp, CONFIG)
        _clear_last_backup()
        try:
            os.unlink(ARM_FILE)
        except FileNotFoundError:
            pass
        print(f'RESTORED config.yaml byte-exact from {os.path.basename(src)}')
        logline(f'restore from {os.path.basename(src)} pid={os.getpid()}')
        return True
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def _restore_if_armed(gen):
    """Restore pre-phase bytes only if this watcher's generation is still the armed one.

    Generation check and restore run under the same flock (see restore_from_backup).
    """
    return restore_from_backup(expected_gen=gen)


def _watch(gen, ttl, since, prefixes):
    """Detached: tail agent.log until every prefix shows a live session, then restore.

    readline() at EOF returns '' and picks up later appends on the next call —
    a plain, reliable grow-file tail. Matching requires the log timestamp to be
    newer than `since`, so an older line can never satisfy the watch.
    """
    deadline = time.time() + ttl
    pending = set(prefixes)
    since_dt = datetime.datetime.fromtimestamp(since)
    rx = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO \[([A-Za-z0-9_]+)\] agent\.turn_context')
    f = None
    try:
        f = open(AGENT_LOG, errors='replace')
    except FileNotFoundError:
        logline(f'watch gen={gen}: agent.log missing, failsafe deadline will fire')
    try:
        while pending and time.time() < deadline:
            line = f.readline() if f else ''
            if not line:
                time.sleep(0.5)
                continue
            m = rx.match(line)
            if not m:
                continue
            try:
                ts = datetime.datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            if ts < since_dt:
                continue
            for p in list(pending):
                if m.group(2).startswith(p):
                    pending.discard(p)
                    logline(f'watch gen={gen}: session alive {m.group(2)} '
                            f'({len(pending)} still pending)')
    finally:
        if f:
            f.close()
    reason = ('all-sessions-alive' if not pending
              else f'deadline-{ttl}s-missed:{sorted(pending)}')
    ok = _restore_if_armed(gen)
    logline(f'watch gen={gen} result={"restored" if ok else "superseded"} reason={reason}')


def show(phase):
    if phase not in SECTIONS:
        fail(f'unknown phase "{phase}"')
    secs = parse_models()
    sec = secs.get(phase)
    if not sec:
        fail(f'no content under "{phase}:"')
    print(yaml.safe_dump(sec, sort_keys=False, default_flow_style=False))


def current():
    cfg = yaml.safe_load(open(CONFIG).read()) or {}
    m = cfg.get('model', {})
    print(f'live model: {m.get("provider")}/{m.get("default")} effort={m.get("reasoning_effort")}')
    fb = cfg.get('fallback_providers', [])
    print(f'live chain ({len(fb)}): ' + ' -> '.join(
        f'{e.get("provider")}/{e.get("model")}' for e in fb))
    print(f'knobs: max_concurrent_sessions={cfg.get("max_concurrent_sessions")} '
          f'max_live_sessions={cfg.get("max_live_sessions")}')
    print(f'top-level entries ({len(cfg)}): {sorted(cfg)}')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    try:
        if cmd == 'apply' and len(sys.argv) == 3:
            apply_phase(sys.argv[2])
        elif cmd == 'apply' and len(sys.argv) == 4 and sys.argv[3] == '--dry':
            apply_phase(sys.argv[2], dry=True)
        elif cmd == 'restore' and len(sys.argv) == 3 and sys.argv[2] == '--dry':
            restore_from_backup(dry=True)
        elif cmd == 'restore':
            restore_from_backup()
        elif cmd == 'arm-restore' and len(sys.argv) >= 4:
            args = sys.argv[2:]
            phase, args = args[0], args[1:]
            ttl = DEFAULT_TTL
            if '--ttl' in args:
                i = args.index('--ttl')
                ttl = int(args[i + 1])
                args = args[:i] + args[i + 2:]
            arm_restore(phase, args, ttl)
        elif cmd == '_watch' and len(sys.argv) >= 6:
            _watch(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5:])
        elif cmd == 'show' and len(sys.argv) == 3:
            show(sys.argv[2])
        elif cmd == 'current':
            current()
        else:
            print(__doc__)
            sys.exit(2)
    except PhaseFail as e:
        print(f'PHASE-MODELS FAIL: {e}', file=sys.stderr)
        print('config.yaml left untouched.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
