"""Accounted review over existing ASTRA evidence; no model calls or capture logic.

SQLite is the single local acknowledgement store. Inputs are retained only for
existing evidence retention; completed report summaries are investigation history.
"""
from __future__ import annotations
import hashlib
import html
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from collections import Counter
from astra.fingerprint import build_fingerprint, CRON_RUN, COOLDOWN_EXPIRY
from astra.pager import model_function

RCA_REQUIRED_SECTIONS = [
    (r"(?i)\b(observed\s+impact|impact)\b", "Observed Impact"),
    (r"(?i)\b(timeline\s+and\s+evidence|timeline|evidence)\b", "Timeline / Evidence"),
    (r"(?i)\b(facts\s+versus\s+hypotheses|facts\s+vs\s+hypotheses|facts)\b", "Facts vs Hypotheses"),
    (r"(?i)\b(checks\s+and\s+results|checks)\b", "Checks and Results"),
    (r"(?i)\broot\s+cause\b", "Root Cause"),
    (r"(?i)\b(precise\s+proposed\s+fix|proposed\s+fix|fix)\b", "Proposed Fix"),
    (r"(?i)\b(risks\s+and\s+prerequisites|risks|prerequisites)\b", "Risks and Prerequisites"),
    (r"(?i)\brollback\b", "Rollback"),
    (r"(?i)\b(post-repair\s+verification|verification)\b", "Post-Repair Verification"),
]


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


class ReviewStore:
    def __init__(self, path, cooldown_seconds=3600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_seconds = cooldown_seconds
        self.db = sqlite3.connect(self.path, timeout=20)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, gid TEXT NOT NULL, ts TEXT NOT NULL, row_json TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS evidence_group ON evidence(gid);
          CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, decision TEXT, reason TEXT, reviewed_at REAL, rca_done INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, kind TEXT NOT NULL, ids_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, lease_until REAL NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, stamp TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rca_history(id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT NOT NULL, batch_id TEXT NOT NULL, report_path TEXT NOT NULL, shared_path TEXT, completed_at REAL NOT NULL, summary TEXT);
        ''')
        self._migrate_lanes()
        from astra.coverage import Coverage
        self.coverage = Coverage(self)
        from astra.delivery import configured,install_schema
        self.delivery_config=configured()
        if self.delivery_config and self.path.resolve()==(self.delivery_config.records_root/'review.sqlite').resolve():
            install_schema(self)
        else:self.delivery_config=None

    def _migrate_lanes(self):
        # Dual-lane RCA: rca-a (primary) and rca-b (comparison). Legacy rca_done is
        # kept as a maintained mirror of rca_a_done so older readers stay correct.
        cols={r[1] for r in self.db.execute('PRAGMA table_info(findings)')}
        if 'rca_a_done' not in cols:
            self.db.execute('ALTER TABLE findings ADD COLUMN rca_a_done INTEGER NOT NULL DEFAULT 0')
        if 'rca_b_done' not in cols:
            self.db.execute('ALTER TABLE findings ADD COLUMN rca_b_done INTEGER NOT NULL DEFAULT 0')
        hcols={r[1] for r in self.db.execute('PRAGMA table_info(rca_history)')}
        if 'lane' not in hcols:
            self.db.execute("ALTER TABLE rca_history ADD COLUMN lane TEXT NOT NULL DEFAULT 'a'")
        self.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT)')
        seeded=self.db.execute("SELECT value FROM runtime_meta WHERE key='lane_seed_done'").fetchone()
        if not seeded:
            # one-time frontier seed; after this, lanes move independently
            self.db.execute('UPDATE findings SET rca_a_done=rca_done WHERE rca_a_done=0 AND rca_done=1')
            self.db.execute('UPDATE findings SET rca_b_done=rca_done WHERE rca_b_done=0 AND rca_done=1')
            self.db.execute("INSERT OR REPLACE INTO runtime_meta VALUES('lane_seed_done','1')")
        self.db.commit()

    def close(self):
        self.db.close()

    def sync_sources(self, root_dir, now=None):
        root = Path(root_dir)
        scanned_files = 0
        total_ingested = 0
        if not root.is_dir():
            return {'scanned_files': 0, 'ingested_rows': 0}

        # Look for enriched-*.jsonl files in root_dir
        candidates = sorted(root.glob('enriched-*.jsonl'))
        for p in candidates:
            try:
                st = p.stat()
            except OSError:
                continue
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
            row = self.db.execute('SELECT stamp FROM sources WHERE path=?', (str(p),)).fetchone()
            if row and row['stamp'] == stamp:
                continue
            # Stream into the transaction; bad evidence never advances the stamp.
            def records(handle):
                for line in iter(lambda: handle.readline(1048577), ''):
                    if len(line) > 1048576:
                        raise ValueError(f'Oversize evidence row in {p.name}')
                    if line.strip():
                        try:
                            value = json.loads(line)
                        except ValueError:
                            raise ValueError(f'Malformed evidence in {p.name}') from None
                        if not isinstance(value, dict):
                            raise ValueError(f'Non-object evidence in {p.name}')
                        yield value
            with open(p, 'r', encoding='utf-8') as f:
                inserted = self.ingest(records(f), now=now)
            with self.db:
                self.db.execute('INSERT INTO sources(path, stamp) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET stamp=excluded.stamp', (str(p), stamp))
            scanned_files += 1
            total_ingested += inserted

        return {'scanned_files': scanned_files, 'ingested_rows': total_ingested}

    @staticmethod
    def _canonical_key(row):
        c=row.get('astra.classification') or {}
        fp=build_fingerprint(c,row.get('host'),row.get('profile'),row.get('text'))
        return digest([fp['key'],(row.get('astra.severity') or {}).get('label','watch')])

    def _canonical_groups(self):
        # Retained raw evidence stays byte-identical. Only compare exact identities
        # after normalization; do not equate broad cause/provider categories.
        version=(self.db.total_changes,self.db.execute('PRAGMA data_version').fetchone()[0])
        cached=getattr(self,'_canonical_cache',None)
        if cached and cached[0]==version:return cached[1]
        groups={}
        # Reuse only the expensive identity calculation for unchanged latest
        # payloads. Decisions/flags are always refreshed after database changes.
        # Rebuild this bounded map from retained rows so pruning drops old entries.
        previous=cached[2] if cached else {}
        identities={}
        # Rank only evidence identifiers, once. A correlated latest-row lookup
        # sorts each finding's evidence again and repeatedly visits large payloads.
        # Keep finding order and the exact timestamp/id tie-break used by _item.
        for f in self.db.execute("""
                WITH latest AS (
                    SELECT gid,id,ROW_NUMBER() OVER (
                        PARTITION BY gid ORDER BY ts DESC,id DESC) AS rank
                    FROM evidence
                )
                SELECT f.*,e.row_json,e.id AS evidence_id FROM findings f
                JOIN latest l ON l.gid=f.id AND l.rank=1
                JOIN evidence e ON e.id=l.id
                ORDER BY f.rowid
                """):
            raw=f['row_json'];eid=f['evidence_id']
            prior=previous.get(eid)
            if prior is not None and prior[0]==raw:
                key=prior[1]
            else:
                row=json.loads(raw)
                text=str(row.get('text',''))
                key=self._canonical_key(row) if CRON_RUN.search(text) or COOLDOWN_EXPIRY.search(text) else None
            identities[eid]=(raw,key)
            if key is not None:
                finding=dict(f);del finding['evidence_id']
                groups.setdefault(key,[]).append(finding)
        self._canonical_cache=(version,groups,identities)
        return groups

    def _reuse_completed_rca(self, lane):
        # Coverage provenance is independent of which lane actually investigated.
        return self.coverage.refresh()['covered']

    def ingest(self, rows, now=None):
        now = time.time() if now is None else now
        inserted = 0
        with self.db:
            aliases=None
            for row in rows:
                ts = row.get('event_ts') or row.get('ts')
                if not ts or ts == 'UNKNOWN':
                    raise ValueError('Evidence lacks event timestamp; cannot silently omit it')
                identity_material = [row.get('host'),row.get('profile'),row.get('source_path'),row.get('record_id'),row.get('byte_start'),row.get('byte_end'),(row.get('journal') or {}).get('__CURSOR'),ts,row.get('text')]
                identity = row.get('source_identity')
                if isinstance(identity, dict) and identity:
                    # Rotation disambiguation: same text/offsets but a new file
                    # identity is a distinct evidence row. Only present-identity
                    # rows include these fields, so historical eids stay stable.
                    identity_material = identity_material + [identity.get('inode'), identity.get('dev'), identity.get('content_hash')]
                eid = digest(identity_material)
                # Source files are replayed when appended. An already retained
                # evidence id cannot insert or change its finding; avoid repeating
                # fingerprint/history work. Tracked rows retain their update path.
                severity = (row.get('astra.severity') or {}).get('label', 'watch')
                if severity != 'tracked' and self.db.execute('SELECT 1 FROM evidence WHERE id=?',(eid,)).fetchone():
                    continue
                classification = row.get('astra.classification') or {}
                fp = row.get('astra.fingerprint') or build_fingerprint(classification,row.get('host'),row.get('profile'),row.get('text'))
                gid = digest([fp.get('key'),severity])[:20]
                text=str(row.get('text',''))
                key=None
                if CRON_RUN.search(text) or COOLDOWN_EXPIRY.search(text):
                    if aliases is None:aliases=self._canonical_groups()
                    key=self._canonical_key(row)
                    matches=aliases.get(key,[])
                    if matches and len({f['decision'] for f in matches})==1:
                        # Prefer the most complete existing identity; its lane flags,
                        # reasons and history survive. Original evidence is untouched.
                        gid=max(matches,key=lambda f:(f['rca_a_done']+f['rca_b_done'],f['reviewed_at'] or 0))['id']
                    elif not matches:
                        gid=key[:20]
                if severity == 'tracked':
                    self.db.execute('UPDATE findings SET decision=?,rca_done=1 WHERE id=? AND decision IS NULL', ('tracked', gid))
                n=self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)',(eid,gid,ts,packed(row))).rowcount
                inserted += n
                if n:
                    existing = self.db.execute('SELECT * FROM findings WHERE id=?', (gid,)).fetchone()
                    if existing is None:
                        decision='ignore' if severity=='ignore' or row.get('record_role')=='continuation' else None
                        self.db.execute('INSERT INTO findings(id,decision) VALUES(?,?)',(gid,decision))
                    else:
                        # Keep equivalent recurrences attached to their completed or
                        # queued RCA. A new resource/mechanism/severity has a different
                        # gid and is immediately eligible; mere repetition is daily-review
                        # evidence, not another investigation.
                        if existing['rca_done'] or existing['decision'] == 'rca':
                            pass
                        elif existing['reviewed_at'] is not None and (now - existing['reviewed_at']) >= self.cooldown_seconds:
                            self.db.execute('UPDATE findings SET decision=NULL WHERE id=?', (gid,))
                if n and key is not None:
                    current=dict(self.db.execute('SELECT * FROM findings WHERE id=?',(gid,)).fetchone())
                    group=aliases.setdefault(key,[])
                    group[:]=[f for f in group if f['id']!=gid]+[current]
        return inserted

    def _item(self, gid):
        latest=self.db.execute('SELECT * FROM evidence WHERE gid=? ORDER BY ts DESC,id DESC LIMIT 1',(gid,)).fetchone()
        if latest is None:
            raise ValueError(f'Evidence expired/missing for finding {gid}')
        stats=self.db.execute('SELECT COUNT(*) AS n,MIN(ts) AS first,MAX(ts) AS last FROM evidence WHERE gid=?',(gid,)).fetchone()
        row=json.loads(latest['row_json'])
        c=row.get('astra.classification') or {}
        prior=self.db.execute('SELECT decision,reason,reviewed_at,rca_a_done,rca_b_done FROM findings WHERE id=?',(gid,)).fetchone()
        text=str(row.get('text') or '')
        related=[]
        if CRON_RUN.search(text) or COOLDOWN_EXPIRY.search(text):
            for f in self._canonical_groups().get(self._canonical_key(row),[]):
                if f['id']==gid or f['reviewed_at'] is None:continue
                related.append({k:f[k] for k in ('id','decision','reason','reviewed_at','rca_a_done','rca_b_done')})
        return {'id':gid,'host':row.get('host'),'agent':row.get('profile'),
                'function':model_function(c),
                'event':c.get('event'),'cause':c.get('cause'),
                'provider':c.get('provider'),'model':c.get('model'),'component':c.get('component'),
                'severity':(row.get('astra.severity') or {}).get('label','watch'),
                'evidence_rows':stats['n'],'first':stats['first'],'last':stats['last'],
                'sample':text[:240], 'evidence':latest['id'],
                'sample_truncated':len(text)>240, 'sample_original_chars':len(text),
                'prior_status':dict(prior) if prior else {}, 'exact_prior_findings':related[-3:],
                'investigation_coverage':self.coverage.for_finding(gid),
                'coverage_candidates':self.coverage.candidates(gid),
                'repair_state':self.delivery_fact(gid)}

    def get_evidence(self, finding_or_evidence_id, max_bytes=8192):
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 131072:
            raise ValueError('Evidence byte budget must be 1..131072')
        row = self.db.execute('SELECT * FROM evidence WHERE id=?', (finding_or_evidence_id,)).fetchone()
        if not row:
            row = self.db.execute('SELECT * FROM evidence WHERE gid=? ORDER BY ts DESC, id DESC LIMIT 1', (finding_or_evidence_id,)).fetchone()
        if not row:
            raise ValueError(f'Evidence not found for {finding_or_evidence_id}')

        parsed = json.loads(row['row_json'])
        prov = parsed.get('astra.provenance') or {}
        source_path = prov.get('source_path') or parsed.get('source_path')
        byte_start = prov.get('byte_start', parsed.get('byte_start', 0))
        byte_end = prov.get('byte_end', parsed.get('byte_end', 0))
        journal = parsed.get('journal')
        text = parsed.get('text') or ''

        # Source identity verification (parent capture stores inode/dev/content_hash
        # of the record's first raw line at byte_start, without CRLF).
        identity = parsed.get('source_identity')
        identity_report = {'retained': isinstance(identity, dict) and bool(identity), 'verified': None, 'reason': None}
        if identity_report['retained']:
            try:
                size = Path(source_path).stat().st_size if source_path else 0
                ok_size = True
            except OSError:
                ok_size = False
            identity_ok = ok_size
            if identity_ok and identity.get('content_hash'):
                try:
                    with open(source_path, 'rb') as handle:
                        handle.seek(byte_start)
                        first_line = handle.readline(131073)
                except OSError:
                    first_line = b''
                first_line = first_line.rstrip(b'\r\n')
                identity_ok = hashlib.sha256(first_line).hexdigest() == identity['content_hash']
            if identity_ok and 'inode' in identity:
                try:
                    st = os.stat(source_path)
                    identity_ok = (st.st_ino, st.st_dev) == (identity.get('inode'), identity.get('dev'))
                except OSError:
                    identity_ok = False
            identity_report['verified'] = bool(identity_ok)
            if not identity_ok:
                identity_report['reason'] = 'identity_mismatch'
        else:
            identity_report['reason'] = 'not_retained'

        status = 'retained_summary'
        truncated = False
        excerpt = text

        if source_path == 'journald' or journal:
            if journal and ('__CURSOR' in journal or '_BOOT_ID' in journal):
                status = 'journald_entry'
            else:
                status = 'journald_unretained'
            if len(excerpt.encode('utf-8')) > max_bytes:
                excerpt = excerpt.encode('utf-8')[:max_bytes].decode('utf-8', errors='replace')
                truncated = True
        elif identity_report['retained'] and identity_report['verified'] is False:
            status = 'rotated_or_truncated'
            excerpt = text.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')
            truncated = len(text.encode('utf-8')) > max_bytes
        elif source_path and Path(source_path).is_file():
            try:
                file_size = Path(source_path).stat().st_size
                if byte_start > file_size:
                    status = 'rotated_or_truncated'
                    if len(excerpt.encode('utf-8')) > max_bytes:
                        excerpt = excerpt.encode('utf-8')[:max_bytes].decode('utf-8', errors='replace')
                        truncated = True
                else:
                    span = max(0, byte_end - byte_start) if byte_end > byte_start else max_bytes
                    read_len = min(span, max_bytes)
                    with open(source_path, 'rb') as f:
                        f.seek(byte_start)
                        raw_bytes = f.read(read_len)
                    excerpt = raw_bytes.decode('utf-8', errors='replace')
                    if span > max_bytes or len(raw_bytes) < span:
                        truncated = (span > max_bytes)
                    status = 'exact_source'
            except OSError:
                status = 'source_file_inaccessible'
                if len(excerpt.encode('utf-8')) > max_bytes:
                    excerpt = excerpt.encode('utf-8')[:max_bytes].decode('utf-8', errors='replace')
                    truncated = True
        else:
            status = 'source_file_missing'
            if len(excerpt.encode('utf-8')) > max_bytes:
                excerpt = excerpt.encode('utf-8')[:max_bytes].decode('utf-8', errors='replace')
                truncated = True

        return {
            'id': row['id'],
            'finding_id': row['gid'],
            'ts': row['ts'],
            'host': parsed.get('host'),
            'profile': parsed.get('profile'),
            'source_path': source_path,
            'byte_start': byte_start,
            'byte_end': byte_end,
            'journal': journal,
            'source_identity': identity_report,
            'status': status,
            'truncated': truncated,
            'text': excerpt,
            'classification': parsed.get('astra.classification'),
            'severity': (parsed.get('astra.severity') or {}).get('label')
        }

    def prepare(self, kind='triage', now=None, max_bytes=12000):
        now=time.time() if now is None else now
        if kind=='rca': kind='rca-a'
        if kind not in {'triage','rca-a','rca-b'}:
            raise ValueError('Invalid review kind')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            lane = None if kind=='triage' else kind
            coverage=self.coverage.refresh()
            covered=set(coverage['covered']);uncertain=set(coverage['uncertain'])
            lcol = 'rca_done' if lane is None else ('rca_a_done' if lane=='rca-a' else 'rca_b_done')
            RK = "('rca','rca-a','rca-b')"
            if lane is None:
                condition="decision IS NULL OR decision='rca'"
            else:
                condition="decision='rca'"
            pending=self.db.execute(f'SELECT COUNT(*) FROM findings WHERE {condition}').fetchone()[0]
            kinds = ('rca', 'rca-a') if kind == 'rca-a' else (kind,)
            marks = ','.join('?' for _ in kinds)
            active=self.db.execute(f"SELECT * FROM batches WHERE status='running' AND kind IN ({marks}) ORDER BY created LIMIT 1",kinds).fetchone()
            if active and active['lease_until']>now:
                self.db.commit();return {'wakeAgent':False,'reason':'investigation already running'}
            if active and lane is not None and set(json.loads(active['ids_json'])) <= covered:
                # An expired lease covered by another completed investigation is
                # retained as covered, never falsely marked done or re-leased.
                self.db.execute("UPDATE batches SET status='covered' WHERE id=?",(active['id'],))
                active=None
            if active:
                if active['attempts']>=2:
                    self.db.execute("UPDATE batches SET status='blocked' WHERE id=?",(active['id'],))
                    self.db.commit()
                    raise RuntimeError(f"Analysis blocked after two attempts; batch {active['id']} remains unresolved")
                if active['kind'] not in kinds:
                    self.db.commit();return {'wakeAgent':False,'reason':'other role has pending retry'}
                ids=json.loads(active['ids_json']);bid=active['id']
                items={gid:self._item(gid) for gid in ids}
                self.db.execute('UPDATE batches SET attempts=attempts+1,lease_until=? WHERE id=?',(now+600,bid))
            else:
                lane = None if kind=='triage' else kind
                lcol = 'rca_done' if lane is None else ('rca_a_done' if lane=='rca-a' else 'rca_b_done')
                RK = "('rca','rca-a','rca-b')"
                if lane is None:
                    condition="decision IS NULL OR decision='rca'"
                else:
                    condition="decision='rca'"
                candidates=self.db.execute(f'SELECT id,decision FROM findings WHERE {condition} ORDER BY rowid').fetchall()
                candidates=[c for c in candidates if c['id'] not in covered and
                            ((c['decision'] is None or c['id'] in uncertain) if lane is None else c['id'] not in uncertain)]
                pending=len(candidates)
                scope_kinds = ('rca','rca-a','rca-b') if lane is not None else ('triage','rca','rca-a','rca-b')
                kind_list = "(" + ",".join(f"'{k}'" for k in scope_kinds) + ")"
                busy={gid for b in self.db.execute(f"SELECT ids_json FROM batches WHERE status IN ('running','blocked') AND kind IN {kind_list}") for gid in json.loads(b[0])}
                busy=self.coverage.busy(busy)
                # Serious observations first, then recent evidence; old baseline
                # backlog remains pending rather than hiding new operational faults.
                from datetime import datetime
                items={candidate[0]:self._item(candidate[0]) for candidate in candidates if candidate[0] not in busy and self.db.execute('SELECT 1 FROM evidence WHERE gid=? LIMIT 1',(candidate[0],)).fetchone()}
                def priority(candidate):
                    item=items.get(candidate[0])
                    if item is None:return (2,0)
                    text=self.QUOTED_SPAN.sub(' ',item['sample'])
                    serious=any(pattern.search(text) for pattern,_ in self.SERIOUS_PATTERNS)
                    return (0 if serious else 1,-datetime.fromisoformat(item['last'].replace('Z','+00:00')).timestamp())
                candidates.sort(key=priority)
                ids=[]
                for candidate in candidates:
                    gid=candidate[0]
                    if gid in busy or gid not in items:continue
                    item=items[gid]
                    probe={'wakeAgent':True,'batch':'b'*20,'kind':kind,'pending_findings':pending,'items':[items[x] for x in ids]+[item]}
                    if len(packed(probe).encode())>max_bytes:
                        if not ids:raise ValueError('Single finding exceeds configured packet budget')
                        break
                    ids.append(gid)
                    if lane is not None:break
                if not ids:
                    self.db.commit();return {'wakeAgent':False,'reason':'no unreviewed findings'}
                bid=digest([kind,ids,now])[:20]
                self.db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?)',(bid,kind,packed(ids),'running',1,now+600,now))
            self.db.execute("INSERT OR REPLACE INTO runtime_meta VALUES(?,?)",('lease_started:'+bid,str(now)))
            result={'wakeAgent':True,'batch':bid,'kind':kind,'pending_findings':pending,'items':[items[gid] for gid in ids]}
            self.db.commit();return result
        except Exception:
            self.db.rollback();raise

    def complete(self, batch, findings, now=None):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            b=self.db.execute('SELECT * FROM batches WHERE id=?',(batch,)).fetchone()
            if b is None or b['status']!='running' or b['kind']!='triage':
                raise ValueError('Not an active triage batch')
            expected=set(json.loads(b['ids_json']))
            if not isinstance(findings, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) for x in findings):
                raise ValueError('Triage result must be a JSON array of objects with string finding ids')
            counts=Counter(x['id'] for x in findings)
            if len(findings)!=len(expected) or set(counts)!=expected:
                raise ValueError('Result must account for every finding exactly once; '+packed({
                    'missing':sorted(expected-set(counts)), 'extra':sorted(set(counts)-expected),
                    'duplicates':sorted(k for k,v in counts.items() if v>1)}))
            for f in findings:
                if not isinstance(f.get('decision'),str) or f['decision'] not in {'watch','rca','informational','tracked'} or not isinstance(f.get('reason'),str) or len(f['reason'].strip())<12:
                    raise ValueError('Invalid or unexplained triage result')
            for f in findings:
                if f.get('coverage') is not None and f.get('reopen') is not None:raise ValueError('Choose coverage or reopening, not both')
                if f.get('coverage') is not None:
                    ref=f['coverage']
                    if not isinstance(ref,dict) or set(ref)-{'history_id','relationship_history_id','quote'} or type(ref.get('history_id')) is not int:raise ValueError('Invalid investigation coverage reference')
                    self.coverage.link(f['id'],ref['history_id'],ref.get('relationship_history_id'),ref.get('quote'),now=now)
                if f.get('reopen') is not None:
                    ref=f['reopen']
                    if not isinstance(ref,dict) or set(ref)-{'history_id','reason','evidence_id','quote'} or not isinstance(ref.get('reason'),str) or ('history_id' in ref and type(ref['history_id']) is not int):raise ValueError('Invalid reopening evidence')
                    self.coverage.reopen(f['id'],now=now,**ref)
                if f['decision']=='rca' and f['id'] in self.coverage.refresh()['uncertain']:
                    raise ValueError('Related investigation requires validated coverage or a new discriminating evidence reference')
                self.db.execute("UPDATE findings SET decision=?,reason=?,reviewed_at=? WHERE id=?",(f['decision'],f['reason'],now,f['id']))
            self.db.execute("UPDATE batches SET status='done' WHERE id=?",(batch,))
        return {'batch':batch,'status':'reviewed','findings':findings}

    def recover_blocked_batch(self, batch, reason, now=None):
        """Explicit operator recovery; never an automatic retry/reset loop."""
        if not isinstance(reason,str) or len(reason.strip())<12:
            raise ValueError('Recovery requires a concrete reason (at least 12 characters)')
        now=time.time() if now is None else now
        self.db.execute('BEGIN IMMEDIATE')
        try:
            b=self.db.execute('SELECT * FROM batches WHERE id=?',(batch,)).fetchone()
            if b is None or b['status']!='blocked':raise ValueError('Not a blocked batch')
            eligible=[];retained=[];expired=[]
            for gid in json.loads(b['ids_json']):
                f=self.db.execute('SELECT * FROM findings WHERE id=?',(gid,)).fetchone()
                evidence=self.db.execute('SELECT MAX(ts) FROM evidence WHERE gid=?',(gid,)).fetchone()[0]
                from astra.timestamps import as_utc
                from astra.storage import RETENTION_HOURS
                retained_evidence=bool(evidence and as_utc(evidence).timestamp() >= now-RETENTION_HOURS*3600)
                col='rca_b_done' if b['kind']=='rca-b' else 'rca_a_done'
                pending=f and (f['decision'] is None if b['kind']=='triage' else f['decision']=='rca' and not f[col])
                if pending and not retained_evidence:expired.append(gid)
                if pending and retained_evidence:eligible.append(gid)
                else:retained.append({'id':gid,'reason':'evidence missing/expired' if not retained_evidence else 'already decided/completed','decision':f['decision'] if f else None})
            if not eligible:raise ValueError('No recoverable retained work; '+packed(retained))
            for gid in expired:
                self.db.execute("UPDATE findings SET decision='evidence_expired' WHERE id=?",(gid,))
            record={'batch':batch,'kind':b['kind'],'attempts':b['attempts'],'reason':reason.strip(),
                    'at':now,'eligible':eligible,'retained':retained}
            self.db.execute("INSERT INTO runtime_meta VALUES(?,?)",('recovered:'+batch,packed(record)))
            self.db.execute("UPDATE batches SET status='recovered' WHERE id=?",(batch,))
            self.db.commit()
            return {'status':'recovered',**record}
        except Exception:
            self.db.rollback();raise

    def validate_rca_report(self, markdown_text):
        if not isinstance(markdown_text, str) or len(markdown_text.strip()) < 100:
            raise ValueError("RCA report is too short or empty")
        missing = []
        for pattern, name in RCA_REQUIRED_SECTIONS:
            if not re.search(pattern, markdown_text):
                missing.append(name)
        if missing:
            raise ValueError(f"RCA report contract violation; missing required sections: {', '.join(missing)}")

    def complete_rca(self, batch, report_text, archive_dir=None, shared_dir=None, now=None):
        if self.delivery_config:
            from astra.delivery import Delivery
            return Delivery(self,self.delivery_config).complete(batch,report_text,archive_dir,shared_dir,now)
        now = time.time() if now is None else now
        self.validate_rca_report(report_text)
        with self.db:
            b = self.db.execute('SELECT * FROM batches WHERE id=?', (batch,)).fetchone()
            if b is None or b['status'] != 'running' or b['kind'] not in ('rca','rca-a','rca-b'):
                raise ValueError('Not an active RCA batch')
            lane = 'b' if b['kind']=='rca-b' else 'a'
            ids = json.loads(b['ids_json'])
            if not ids:
                raise ValueError('Empty RCA batch')
            finding_id = ids[0]

            archive = Path(archive_dir) if archive_dir else (self.path.parent / "reports" / "rca")
            archive.mkdir(parents=True, exist_ok=True)
            fname = f"rca-{finding_id}-{int(now)}.md"
            archive_path = archive / fname
            archive_path.write_text(report_text, encoding='utf-8')

            summary = ""
            for line in report_text.splitlines():
                if line.strip().startswith("#"):
                    summary = line.strip("# \t")
                    break

            shared_path_str = None
            shared_markdown_path_str = None
            if shared_dir:
                shared = Path(shared_dir)
                shared.mkdir(parents=True, exist_ok=True)
                sh_md_path = shared / fname
                sh_md_path.write_text(report_text, encoding='utf-8')
                sh_html_path = sh_md_path.with_suffix('.html')
                from astra.rca_report import from_markdown, publish
                context = self._item(finding_id)
                document = from_markdown(report_text, finding_id, batch, lane, context)
                # Preserve original Markdown/archive and store contracts. Only the
                # HTML presentation and optional question registration change.
                shared_root = next((parent for parent in (shared, *shared.parents)
                                    if (parent / 'dashboard' / '_private').is_dir()), None)
                publish(sh_html_path, document, shared_root)
                shared_path_str = str(sh_html_path)
                shared_markdown_path_str = str(sh_md_path)

            if lane=='a':
                self.db.execute("UPDATE findings SET rca_done=1, rca_a_done=1, reviewed_at=? WHERE id=?", (now, finding_id))
            else:
                self.db.execute("UPDATE findings SET rca_b_done=1, reviewed_at=? WHERE id=?", (now, finding_id))
            self.db.execute("UPDATE batches SET status='done' WHERE id=?", (batch,))
            self.db.execute(
                "INSERT INTO rca_history(finding_id, batch_id, report_path, shared_path, completed_at, summary, lane) VALUES(?,?,?,?,?,?,?)",
                (finding_id, batch, str(archive_path), shared_path_str, now, summary, lane)
            )

            hid=self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
            self.coverage.register(hid,report_text)
            return {
                'batch': batch,
                'finding_id': finding_id,
                'status': 'persisted',
                'report_path': str(archive_path),
                'shared_path': shared_path_str,
                'shared_markdown_path': shared_markdown_path_str,
                'notification_pending': True
            }

    def _mechanism_key(self, gid):
        item = self._item(gid)
        cause = item.get('cause')
        # Reconnect/permission failures repeat across adapter layers and profiles.
        # One investigation covers the class; a different cause stays distinct.
        if cause in {'missing_access', 'websocket_unhealthy', 'adapter_error'}:
            return ('cause', cause)
        return (item.get('event'), cause, item.get('component'))

    def _recent_mechanisms(self, now=None):
        now = time.time() if now is None else now
        keys = set()
        for row in self.db.execute(
            'SELECT finding_id FROM rca_history WHERE completed_at>=?',
            (now - self.cooldown_seconds,),
        ):
            try:
                keys.add(self._mechanism_key(row['finding_id']))
            except ValueError:
                continue
        return keys

    def cover_equivalent_rca(self, finding_id, now=None):
        # Compatibility API: return references, never fabricate completion flags.
        history=self.db.execute('SELECT id FROM rca_history WHERE finding_id=? ORDER BY id LIMIT 1',(finding_id,)).fetchone()
        if not history:return []
        return sorted(gid for gid,hid in self.coverage.refresh()['covered'].items()
                      if hid==history[0] and gid!=finding_id)

    def _uncovered_rca_ids(self, now=None):
        state=self.coverage.refresh()
        return [r[0] for r in self.db.execute("SELECT id FROM findings WHERE decision='rca'")
                if r[0] not in state['covered'] and r[0] not in state['uncertain']]

    SERIOUS_PATTERNS = [
        (re.compile(r"database disk image is malformed|database\s+corrupt(?:ed|ion)|disk I/O error", re.I), "database corruption"),
        (re.compile(r"\bfatal(?:\s+unhandled\s+exception|\s+error)\b", re.I), "fatal exception"),
    ]
    QUOTED_SPAN = re.compile(r'"[^"]*"|\'[^\']*\'')
    ADAPTER_RECONNECT = re.compile(
        r'fatal\s+\S+\s+adapter error|'
        r'discord_websocket_health_stale|'
        r'telegram_network_error|'
        r'websocket health check failed|'
        r'rebuilding the adapter',
        re.I,
    )
    HEALTH_PROFILE = re.compile(r'^(astra-[a-z0-9-]*health-monitor|astra-system-health)$')

    def _early_findings(self):
        """Deterministic serious-observation scan over unreviewed findings.

        Rules mean "inspect early", not "root cause proved". Explicit fatal /
        corruption wording qualifies; generic isolated 429s and quoted error
        wording inside recovered/success lines do not. Returns (bool, reason).
        """
        coverage=self.coverage.refresh()
        busy = set(coverage['covered']) | set(coverage['uncertain']) | {gid for b in self.db.execute("SELECT ids_json FROM batches WHERE status IN ('running','blocked')") for gid in json.loads(b[0])}
        busy=self.coverage.busy(busy)
        rows = self.db.execute(
            "SELECT e.row_json AS row_json, f.id AS gid FROM evidence e "
            "JOIN findings f ON f.id = e.gid WHERE f.decision IS NULL ORDER BY e.ts, e.id"
        )
        for row in rows:
            if row['gid'] in busy:
                continue
            parsed = json.loads(row['row_json'])
            if parsed.get('record_role') == 'continuation':
                continue
            severity = (parsed.get('astra.severity') or {}).get('label', 'watch')
            if severity == 'ignore':
                continue
            # Quoted wording (e.g. logged "error" and recovered) is not evidence of fatality.
            text = self.QUOTED_SPAN.sub(' ', str(parsed.get('text') or ''))
            if self.ADAPTER_RECONNECT.search(text):
                continue
            profile = str(parsed.get('profile') or '')
            event = (parsed.get('astra.classification') or {}).get('event')
            if self.HEALTH_PROFILE.match(profile) and event in {
                'platform.adapter_failed', 'platform.send_failed', 'platform.disconnected',
            }:
                continue
            for pattern, label in self.SERIOUS_PATTERNS:
                if pattern.search(text):
                    yield row['gid'], f"{label} observed in unreviewed finding {row['gid']}"
                    break

    def check_early_triggers(self, now=None):
        first = next(self._early_findings(), None)
        return (True, first[1]) if first else (False, None)

    def promote_urgent(self, now=None):
        """Existing deterministic rules can escalate without waking triage."""
        now = time.time() if now is None else now
        with self.db:
            findings = dict(self._early_findings())
            for gid, reason in findings.items():
                self.db.execute("UPDATE findings SET decision='rca',reason=?,reviewed_at=? WHERE id=? AND decision IS NULL",
                                ('Deterministic urgent observation; cause unverified: ' + reason, now, gid))
        return list(findings)

    def status(self, now=None):
        now = time.time() if now is None else now
        coverage=self.coverage.refresh()
        pending_triage=sum(r[0] not in coverage['covered'] for r in self.db.execute("SELECT id FROM findings WHERE decision IS NULL")) + sum(self.db.execute('SELECT decision FROM findings WHERE id=?',(gid,)).fetchone()[0]=='rca' for gid in coverage['uncertain'])
        uncovered=set(self._uncovered_rca_ids(now))
        pending_rca = len(uncovered)
        active = self.db.execute("SELECT * FROM batches WHERE status='running'").fetchall()
        active_leases = []
        retry_eligible = 0
        rca_lease_active = False
        triage_lease_active = False
        for b in active:
            rem = b['lease_until'] - now
            if rem > 0:
                active_leases.append({
                    'batch': b['id'],
                    'kind': b['kind'],
                    'attempts': b['attempts'],
                    'lease_until': b['lease_until'],
                    'remaining_seconds': max(0.0, round(rem, 2))
                })
                if b['kind'] in ('rca','rca-a','rca-b'):
                    rca_lease_active = True
                elif b['kind'] == 'triage':
                    triage_lease_active = True
            else:
                retry_eligible += 1
                if b['kind'] in ('rca','rca-a','rca-b'):
                    rca_lease_active = False  # expired retry re-eligible below
        blocked_rca = self.db.execute("SELECT COUNT(*) FROM batches WHERE status='blocked' AND kind IN ('rca','rca-a','rca-b')").fetchone()[0]
        blocked_triage = self.db.execute("SELECT COUNT(*) FROM batches WHERE status='blocked' AND kind='triage'").fetchone()[0]
        blocked = self.db.execute("SELECT COUNT(*) FROM batches WHERE status='blocked'").fetchone()[0]
        total_findings = self.db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        total_evidence = self.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        findings_evidence_expired = self.db.execute(
            "SELECT COUNT(*) FROM findings WHERE decision='evidence_expired'"
        ).fetchone()[0]

        early, early_reason = self.check_early_triggers(now=now)

        # A lease owns only its lane's IDs. Historical failures must not freeze
        # unrelated approved work, and queue age is not an eligibility condition.
        def _lane_gate(col, kinds):
            lane_batches=[b for b in active if b['kind'] in kinds]
            if any(b['lease_until'] > now for b in lane_batches):return False
            if any(not set(json.loads(b['ids_json'])) <= set(coverage['covered']) for b in lane_batches):return True
            marks=','.join('?' for _ in kinds)
            busy={gid for b in self.db.execute(
                "SELECT ids_json FROM batches WHERE status IN ('running','blocked') AND kind IN ('rca','rca-a','rca-b')")
                for gid in json.loads(b[0])}
            busy=self.coverage.busy(busy)
            return any(r[0] not in busy and r[0] in uncovered for r in self.db.execute(
                "SELECT id FROM findings WHERE decision='rca' "
                "AND EXISTS (SELECT 1 FROM evidence WHERE gid=findings.id)"))
        dispatch_rca_a = _lane_gate('rca_a_done', ('rca','rca-a'))
        dispatch_rca_b = _lane_gate('rca_b_done', ('rca-b',))
        dispatch_rca = dispatch_rca_a or dispatch_rca_b
        dispatch_triage_early = False

        return {
            **self.delivery_counts(),
            'pending_triage': pending_triage,
            'pending_rca': pending_rca,
            'pending_rca_b':pending_rca,
            'covered_findings':len(coverage['covered']),
            'coverage_review_pending':len(coverage['uncertain']),
            'reopened_findings':len(coverage['cutoffs']),
            'reused_rca_lanes': self.db.execute("SELECT COUNT(*) FROM runtime_meta WHERE key LIKE 'rca_reused:%'").fetchone()[0],
            'active_leases': active_leases,
            'retry_eligible': retry_eligible,
            'blocked_batches': blocked,
            'total_findings': total_findings,
            'total_evidence': total_evidence,
            'findings_evidence_expired': findings_evidence_expired,
            'early': early,
            'early_reason': early_reason,
            'dispatch_rca': dispatch_rca,
            'dispatch_rca_a': dispatch_rca_a,
            'dispatch_rca_b': dispatch_rca_b,
            'dispatch_triage_early': dispatch_triage_early,
        }

    def delivery_counts(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='astra_incidents'").fetchone():
            return {'pending_repairs':0,'deferred_incidents':0,'delivery_pending':0,'pending_answers':0}
        from astra.delivery import counts
        return counts(self)

    def delivery_fact(self,finding_id):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='astra_incidents'").fetchone():return None
        row=self.db.execute('SELECT status,revision,updated_at,closed_at FROM astra_incidents WHERE finding_id=?',(finding_id,)).fetchone()
        return dict(row) if row else None

    def get_history(self, limit=20):
        rca_rows = self.db.execute(
            "SELECT * FROM rca_history ORDER BY completed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        rca_history = [
            {
                'id': r['id'],
                'finding_id': r['finding_id'],
                'batch_id': r['batch_id'],
                'report_path': r['report_path'],
                'shared_path': r['shared_path'],
                'completed_at': r['completed_at'],
                'summary': r['summary']
            }
            for r in rca_rows
        ]

        reviewed_findings = self.db.execute(
            "SELECT * FROM findings WHERE decision IS NOT NULL ORDER BY reviewed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        findings_history = [
            {
                'id': r['id'],
                'decision': r['decision'],
                'reason': r['reason'],
                'reviewed_at': r['reviewed_at'],
                'rca_done': bool(r['rca_done']), 'repair_state':self.delivery_fact(r['id']),
                'investigation_coverage':self.coverage.for_finding(r['id'])
            }
            for r in reviewed_findings
        ]
        return {
            'rca_history': rca_history,
            'findings_history': findings_history
        }

    def retention_report(self, root_dir, now=None):
        """Report sync backlog and vanished sources; never silently expired."""
        from astra.storage import RETENTION_HOURS
        root = Path(root_dir)
        on_disk = {p.name: p for p in sorted(root.glob('enriched-*.jsonl'))}
        archive_root = root.parent / 'archive' / 'enriched'
        archived_names = {
            p.name[:-3] for p in archive_root.glob('enriched-*.jsonl.gz')
            if p.is_file() and p.name.endswith('.jsonl.gz')
        }
        tracked = {Path(r['path']).name: r['path'] for r in self.db.execute('SELECT path FROM sources').fetchall()}
        unsynced = []
        for name, path in on_disk.items():
            if name not in tracked:
                unsynced.append(name)
                continue
            try:
                st = path.stat()
                stamp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                unsynced.append(name)
                continue
            row = self.db.execute('SELECT stamp FROM sources WHERE path=?', (str(path),)).fetchone()
            if row is None or row['stamp'] != stamp:
                unsynced.append(name)
        missing = sorted(name for name in tracked if name not in on_disk and name not in archived_names)
        from astra.storage import RETENTION_HOURS
        return {
            'gap_detected': bool(unsynced),
            'unsynced_files': unsynced,
            'missing_sources': missing,
            'retention_hours': RETENTION_HOURS,
        }

    def prune_evidence(self, now=None):
        """Expire evidence rows older than the 72h retention window.

        Unreviewed findings whose last evidence expired are explicitly marked
        'evidence_expired' (reported via status), never silently dropped.
        """
        from astra.timestamps import as_utc
        from astra.storage import RETENTION_HOURS, _utcnow
        moment = _utcnow(now)
        horizon = moment.timestamp() - RETENTION_HOURS * 3600
        expired_rows = 0
        with self.db:
            rows = self.db.execute('SELECT id, gid, ts FROM evidence').fetchall()
            expired_gids = set()
            for row in rows:
                try:
                    ts = as_utc(row['ts']).timestamp()
                except (TypeError, ValueError):
                    continue
                if ts < horizon:
                    self.db.execute('DELETE FROM evidence WHERE id=?', (row['id'],))
                    expired_rows += 1
                    expired_gids.add(row['gid'])
            for gid in sorted(expired_gids):
                left = self.db.execute('SELECT COUNT(*) FROM evidence WHERE gid=?', (gid,)).fetchone()[0]
                if left == 0:
                    f = self.db.execute('SELECT decision,rca_done,rca_a_done,rca_b_done FROM findings WHERE id=?', (gid,)).fetchone()
                    if f is not None and (f['decision'] is None or (f['decision']=='rca' and (not f['rca_a_done'] or not f['rca_b_done']))):
                        self.db.execute("UPDATE findings SET decision='evidence_expired' WHERE id=?", (gid,))
                        for batch in self.db.execute("SELECT id,ids_json FROM batches WHERE status='running'").fetchall():
                            if gid in json.loads(batch['ids_json']):
                                self.db.execute("UPDATE batches SET status='blocked' WHERE id=?",(batch['id'],))
        return {'expired_rows': expired_rows}
