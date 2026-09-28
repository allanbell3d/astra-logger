"""Durable investigation provenance; never writes finding/lane completion flags.

Exact identity reuse is deterministic. Cross-symptom relationships require a
persisted report citation and matching affected scope. All state is in ReviewStore.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import time
from pathlib import Path
from astra.fingerprint import (canonical_host, CRON_RUN, COOLDOWN_EXPIRY,
                               LEADING_TS, ISO_TS, TB_FRAME, POSIX_PATH_FULL,
                               WINDOWS_PATH_FULL)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def identity(row):
    """Do not discard resource names, error details, duration or impact facts."""
    c=row.get('astra.classification') or {}
    raw=str(row.get('text') or '')
    resources=TB_FRAME.sub('',raw)
    scope={'host':canonical_host(row.get('host')), 'profile':str(row.get('profile') or 'UNKNOWN'),
           'provider':c.get('provider'), 'model':c.get('model'),
           'target':c.get('target'),
           'resources':sorted(set(POSIX_PATH_FULL.findall(resources)+WINDOWS_PATH_FULL.findall(resources))),
           'jobs':sorted(set(CRON_RUN.findall(raw)))}
    text=LEADING_TS.sub('',raw)
    text=CRON_RUN.sub(r'[\1_<RUN>]',text)
    text=COOLDOWN_EXPIRY.sub(r'\1<TIME>',text)
    text=ISO_TS.sub('<TIMESTAMP>',text)
    text=re.sub(r'(?i)\battempt\s+\d+(?:\s*/\s*\d+|\s+of\s+\d+)?','attempt <N>',text)
    # Traceback source line numbers and log prefixes are not affected resources.
    text=TB_FRAME.sub('File "<FRAME>", line',text)
    text=re.sub(r'^\s*(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?:\[[^\]]+\]\s+)?[\w.]+:\s*','',text)
    text=re.sub(r'\s+',' ',text).strip()
    material={'scope':scope,'text':text,'severity':(row.get('astra.severity') or {}).get('label','watch'),
              'impact':row.get('astra.impact')}
    return {'signature':digest(material), **material}


class Coverage:
    def __init__(self,store):
        self.store=store;self.db=store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS rca_investigations(
            history_id INTEGER PRIMARY KEY, problem_key TEXT NOT NULL,
            outcome TEXT NOT NULL, scope_json TEXT NOT NULL, basis_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS rca_coverage(
            finding_id TEXT NOT NULL, history_id INTEGER NOT NULL, signature TEXT NOT NULL,
            evidence_json TEXT NOT NULL, reason TEXT NOT NULL, covered_at REAL NOT NULL,
            reopen_json TEXT, reopened_at REAL,
            PRIMARY KEY(finding_id,history_id,signature));
          CREATE INDEX IF NOT EXISTS rca_coverage_signature ON rca_coverage(signature);
        ''')
        self.cache=None

    @contextmanager
    def transaction(self):
        # Savepoints preserve the caller's prepare/complete transaction.
        self.db.execute('SAVEPOINT investigation_coverage')
        try:
            yield
            self.db.execute('RELEASE investigation_coverage')
        except Exception:
            self.cache=None
            self.db.execute('ROLLBACK TO investigation_coverage')
            self.db.execute('RELEASE investigation_coverage')
            raise

    def basis(self,gid):
        e=self.db.execute('SELECT id,row_json FROM evidence WHERE gid=? ORDER BY ts DESC,id DESC LIMIT 1',(gid,)).fetchone()
        if e is None:return None
        raw=e['row_json'];return {'evidence_id':e['id'],'payload_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                                 **identity(json.loads(raw))}

    def report(self,hid):
        h=self.db.execute('SELECT * FROM rca_history WHERE id=?',(hid,)).fetchone()
        if h is None:raise ValueError('Unknown investigation history reference')
        p=Path(h['report_path'] or '')
        if p.is_file():return h,p.read_text()
        # Delivery can persist its immutable original before filesystem projection.
        if self.store.delivery_config:
            d=self.db.execute('SELECT document_json FROM astra_deliveries WHERE batch_id=?',(h['batch_id'],)).fetchone()
            if d:return h,json.loads(d[0])['rca']['source_markdown']
        return h,None

    def register(self,hid,report_text=None):
        """Internal transaction participant, also used by both completion paths."""
        if self.db.execute('SELECT 1 FROM rca_investigations WHERE history_id=?',(hid,)).fetchone():return
        h,text=self.report(hid)
        if report_text is not None:
            if text is not None and text!=report_text:raise ValueError('Report differs from persisted investigation')
            text=report_text
        b=self.basis(h['finding_id']) if report_text is not None else None
        if b is None and text:
            # Legacy completion must not inherit today's possibly changed evidence.
            # Only the original report's explicit raw evidence reference is trusted.
            for eid in sorted(set(re.findall(r'\b[0-9a-f]{64}\b',text))):
                e=self.db.execute('SELECT row_json FROM evidence WHERE id=? AND gid=?',(eid,h['finding_id'])).fetchone()
                if e:
                    raw=e[0]
                    b={'evidence_id':eid,'payload_sha256':hashlib.sha256(raw.encode()).hexdigest(),**identity(json.loads(raw))}
                    break
        # A missing archive is a recovery issue, not permission to investigate again.
        # Missing legacy raw evidence limits automatic cross-ID matching, not own-ID coverage.
        scope=b['scope'] if b else {}
        section=re.search(r'(?ims)^#{2,6}[ \t]*(?:\d+[.)][ \t]*)?root cause[^\n]*\n(.*?)(?=\n#+ |\Z)',text or '')
        cause=section.group(1) if section else ''
        outcome='investigated'
        if re.search(r'(?i)undetermined|inconclusive|not established',cause):outcome='inconclusive'
        elif re.search(r'(?i)root cause[^\n]*(?:established|identified)|mechanism[^\n]*established',cause):outcome='diagnosed'
        report_basis={'report_sha256':hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
                      'cause_excerpt':cause[:2000], 'repair_state':'not asserted by coverage'}
        key=b['signature'] if b else 'legacy-history:'+str(hid)
        self.db.execute('INSERT INTO rca_investigations VALUES(?,?,?,?,?)',
                        (hid,key,outcome,packed(scope),packed(report_basis)))
        self._link(h['finding_id'],hid,b,'Investigation completed for this finding; no other lane completion implied',h['completed_at'])

    def _link(self,gid,hid,b,reason,now):
        sig=b['signature'] if b else 'legacy-finding:'+gid
        self.db.execute('INSERT OR IGNORE INTO rca_coverage VALUES(?,?,?,?,?,?,NULL,NULL)',
                        (gid,hid,sig,packed(b or {}),reason,now))

    def link(self,gid,hid,relationship_history_id=None,quote=None,now=None):
        """Public store API: exact reuse or report-backed, scope-checked alias."""
        now=time.time() if now is None else now
        if type(hid) is not int or (relationship_history_id is not None and type(relationship_history_id) is not int):raise ValueError('Integer history references required')
        with self.transaction():
            self.register(hid)
            b=self.basis(gid)
            if b is None:raise ValueError('Related finding requires retained evidence for scope validation')
            h,_=self.report(hid)
            inv=self.db.execute('SELECT * FROM rca_investigations WHERE history_id=?',(hid,)).fetchone()
            anchors=self.db.execute('SELECT signature,evidence_json FROM rca_coverage WHERE history_id=?',(hid,)).fetchall()
            exact=any(a['signature']==b['signature'] for a in anchors)
            if not exact:
                if gid==h['finding_id']:
                    raise ValueError('Changed evidence cannot be silently added to its own investigation')
                scope=json.loads(inv['scope_json'])
                if b['scope']!=scope or not (scope.get('jobs') or scope.get('resources') or (scope.get('provider') and scope.get('model'))):
                    raise ValueError('Coverage scope mismatch or no concrete shared resource')
                rel,text=self.report(relationship_history_id)
                other=gid if rel['finding_id']==h['finding_id'] else h['finding_id']
                if rel['finding_id'] not in {gid,h['finding_id']} or not isinstance(quote,str) or not 30<=len(quote)<=3000 or text is None or quote not in text or other not in quote:
                    raise ValueError('Relationship needs a verbatim persisted report citation naming the other finding')
                if re.search(r'(?i)not (?:the |a )?(?:same|duplicate|shared)|different mechanism|unrelated|hypothes[ie]s|possibly|might be',quote) or not re.search(r'(?i)(?:same mechanism|same (?:diagnosed )?problem|duplicate|shared with sibling).{0,250}'+re.escape(other),quote):
                    raise ValueError('Citation does not establish an explicit investigation relationship')
                reason=packed({'relationship_history_id':relationship_history_id,'report_sha256':hashlib.sha256(text.encode()).hexdigest(),'quote':quote})
            else:reason='Exact scoped evidence signature matches a persisted investigation'
            # A previous reopening is never erased by repeating a mapping.
            self._link(gid,hid,b,reason,now)
        return self.for_finding(gid)

    def refresh(self):
        """Build once per DB version; metadata-only reloads keep hashing bounded."""
        version=(self.db.total_changes,self.db.execute('PRAGMA data_version').fetchone()[0],self.db.in_transaction)
        if self.cache and self.cache[0]==version:return self.cache[1]
        previous=self.cache[2] if self.cache else {};basis={};payloads={}
        with self.transaction():
            for h in self.db.execute('SELECT id FROM rca_history WHERE id NOT IN (SELECT history_id FROM rca_investigations)').fetchall():
                self.register(h['id'])
            # Only potentially actionable or already covered findings need current evidence.
            rows=self.db.execute('''WITH latest AS (
                SELECT gid,id,ROW_NUMBER() OVER(PARTITION BY gid ORDER BY ts DESC,id DESC) n FROM evidence)
                SELECT l.gid,l.id,e.row_json FROM latest l JOIN evidence e ON e.id=l.id
                JOIN findings f ON f.id=l.gid WHERE l.n=1 AND (f.decision IS NULL OR f.decision='rca'
                OR EXISTS(SELECT 1 FROM rca_coverage c WHERE c.finding_id=f.id))''')
            for e in rows:
                raw=e['row_json'];old=previous.get(e['id'])
                b=old[1] if old and old[0]==raw else {'evidence_id':e['id'],'payload_sha256':hashlib.sha256(raw.encode()).hexdigest(),**identity(json.loads(raw))}
                basis[e['gid']]=b;payloads[e['id']]=(raw,b)
            links=self.db.execute('''SELECT c.*,h.completed_at FROM rca_coverage c
                JOIN rca_history h ON h.id=c.history_id ORDER BY h.completed_at,c.history_id''').fetchall()
            by_signature={};by_gid={};cutoffs={}
            for c in links:
                by_gid.setdefault(c['finding_id'],[]).append(c)
                if c['reopened_at'] is not None:cutoffs[c['finding_id']]=max(cutoffs.get(c['finding_id'],0),c['history_id'])
                if c['reopened_at'] is None:by_signature.setdefault(c['signature'],[]).append(c)
            covered={}
            for gid in set(basis)|set(by_gid):
                b=basis.get(gid);sig=b['signature'] if b else None
                choices=(by_signature.get(sig,[])+[c for c in by_gid.get(gid,[]) if not json.loads(c['evidence_json'])]) if sig else by_gid.get(gid,[])
                for c in choices:
                    if c['history_id']<=cutoffs.get(gid,-1):continue
                    if gid==c['finding_id'] and c['reopened_at'] is not None:continue
                    if not b and c['reopen_json']:continue
                    covered[gid]=c['history_id']
                    self._link(gid,c['history_id'],b,'Exact scoped recurrence; original investigation retained',time.time()) if json.loads(c['evidence_json']) else None
                    break
            scopes={}
            for c in links:
                anchor=json.loads(c['evidence_json'])
                scope=anchor.get('scope',{})
                if scope.get('jobs') or scope.get('resources') or (scope.get('provider') and scope.get('model')):
                    scopes.setdefault(digest(scope),[]).append(c)
            uncertain={}
            for gid,b in basis.items():
                if self.db.execute('SELECT decision FROM findings WHERE id=?',(gid,)).fetchone()[0] not in (None,'rca'):continue
                if gid in covered or gid in cutoffs:continue
                prior=by_gid.get(gid) or scopes.get(digest(b['scope']),[])
                if not prior:continue
                c=prior[0];old=json.loads(c['evidence_json'])
                if b['scope']!=old.get('scope') or b['impact']!=old.get('impact'):
                    self._link(gid,c['history_id'],b,'Material impact changed; previous investigation retained',time.time())
                    self.db.execute('UPDATE rca_coverage SET reopen_json=?,reopened_at=? WHERE finding_id=? AND history_id=? AND signature=? AND reopened_at IS NULL',
                        (packed({'reason':'Affected scope or explicit impact facts changed','evidence_id':b['evidence_id'],
                                 'previous_signature':c['signature'],'new_signature':b['signature']}),time.time(),gid,c['history_id'],b['signature']))
                    cutoffs[gid]=c['history_id']
                else:
                    # Shared resource alone cannot prove a common cause. The existing
                    # triage result must supply a validated relation or new discriminator.
                    uncertain[gid]=sorted({c['history_id'] for c in prior})[:3]
            state={'covered':covered,'basis':basis,'cutoffs':cutoffs,'uncertain':uncertain}
        self.cache=((self.db.total_changes,self.db.execute('PRAGMA data_version').fetchone()[0],self.db.in_transaction),state,payloads)
        return state

    def busy(self, gids):
        """Do not lease exact aliases concurrently; unrelated lanes still progress."""
        state=self.refresh()
        signatures={state['basis'][g]['signature'] for g in gids if g in state['basis']}
        return set(gids)|{g for g,b in state['basis'].items() if b['signature'] in signatures}

    def candidates(self,gid):
        refs=[]
        for hid in self.refresh()['uncertain'].get(gid,[]):
            h=self.db.execute('SELECT i.*,h.finding_id,h.report_path FROM rca_investigations i JOIN rca_history h ON h.id=i.history_id WHERE i.history_id=?',(hid,)).fetchone()
            refs.append({'history_id':hid,'finding_id':h['finding_id'],'outcome':h['outcome'],
                         'report_path':h['report_path'],'scope':json.loads(h['scope_json']),
                         'cause_excerpt':json.loads(h['basis_json'])['cause_excerpt'][:600]})
        return refs

    def for_finding(self,gid):
        state=self.refresh();hid=state['covered'].get(gid)
        if hid is None:return None
        h=self.db.execute('''SELECT i.*,h.report_path,h.shared_path,h.completed_at FROM rca_investigations i
                            JOIN rca_history h ON h.id=i.history_id WHERE i.history_id=?''',(hid,)).fetchone()
        return {'history_id':hid,'problem_key':h['problem_key'],'outcome':h['outcome'],'completed_at':h['completed_at'],
                'report_path':h['report_path'],'shared_path':h['shared_path'],
                'delivery_recovery_needed':not (h['shared_path'] and Path(h['shared_path']).is_file()),
                'repair_state':'not asserted by coverage',
                'provenance':[{'signature':c['signature'],'reason':c['reason'],'evidence_id':json.loads(c['evidence_json']).get('evidence_id')} for c in self.db.execute('SELECT signature,reason,evidence_json FROM rca_coverage WHERE finding_id=? AND history_id=?',(gid,hid))]}

    def reopen(self,gid,reason,evidence_id=None,quote=None,owner_authorization=None,now=None,history_id=None):
        now=time.time() if now is None else now
        if not isinstance(reason,str) or not 20<=len(reason)<=1000:raise ValueError('Concrete reopening reason required')
        state=self.refresh();b=self.basis(gid)
        links=self.db.execute('SELECT * FROM rca_coverage WHERE finding_id=?',(gid,)).fetchall()
        if not links:
            if history_id not in state['uncertain'].get(gid,[]):raise ValueError('No applicable prior investigation to reopen')
            links=self.db.execute('SELECT * FROM rca_coverage WHERE history_id=?',(history_id,)).fetchall()
        if not owner_authorization:
            if b is None or b['evidence_id']!=evidence_id or not isinstance(quote,str) or not 20<=len(quote)<=3000 or quote not in b['text']:
                raise ValueError('Reopening requires new discriminating evidence or explicit owner authorization')
            if any(quote in json.loads(c['evidence_json']).get('text','') for c in links):raise ValueError('Evidence was already covered')
        elif not isinstance(owner_authorization,str) or len(owner_authorization)<12:raise ValueError('Owner authorization reference required')
        record={'reason':reason,'evidence_id':evidence_id,'quote':quote,'owner_authorization':owner_authorization}
        with self.transaction():
            if history_id is not None and not self.db.execute('SELECT 1 FROM rca_coverage WHERE finding_id=?',(gid,)).fetchone():
                self._link(gid,history_id,b,'Explicit new evidence against a related investigation',now)
                links=self.db.execute('SELECT * FROM rca_coverage WHERE finding_id=?',(gid,)).fetchall()
            for c in links:
                if c['reopened_at'] is None:
                    self.db.execute('UPDATE rca_coverage SET reopen_json=?,reopened_at=? WHERE finding_id=? AND history_id=? AND signature=?',
                                    (packed(record),now,gid,c['history_id'],c['signature']))
        return record
