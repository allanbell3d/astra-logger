"""ASTRA-only durable delivery and repair projection. No model calls/scheduler.

SQLite is authoritative; local/public files are recoverable projections. Raw
monitoring evidence and immutable dashboard receipts are never rewritten here.
"""
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
import copy,hashlib,importlib.util,json,os,re,sqlite3,time,urllib.request
from urllib.parse import quote,urlsplit

class DeliveryError(ValueError):pass

def packed(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def digest(value):return hashlib.sha256(packed(value).encode()).hexdigest()
def stamp(epoch=None):return datetime.fromtimestamp(time.time() if epoch is None else epoch,timezone.utc).isoformat().replace('+00:00','Z')

class Config:
    def __init__(self,profile_home,skill_dir,records_root,shared_root,origin):
        self.profile_home=Path(profile_home).expanduser().resolve();self.skill_dir=Path(skill_dir).expanduser().resolve()
        self.records_root=Path(records_root).expanduser().resolve();self.shared_root=Path(shared_root).expanduser().resolve()
        parsed=urlsplit(origin)
        if parsed.scheme!='https' or not parsed.netloc or parsed.path not in ('','/') or parsed.username or parsed.password or parsed.query or parsed.fragment:raise DeliveryError('delivery.origin must be a private HTTPS origin without a path')
        self.origin=origin.rstrip('/')
    @classmethod
    def from_profile(cls,profile_home,skill_dir=None):
        profile=Path(profile_home).expanduser().resolve();p=profile/'astra-jobs.json'
        if not p.is_file():raise DeliveryError('ASTRA manifest missing: set --profile-home to the ASTRA profile')
        m=json.loads(p.read_text());options=m.get('delivery',{})
        if not options.get('enabled'):raise DeliveryError('ASTRA delivery integration is not enabled in this profile manifest')
        skill=Path(skill_dir) if skill_dir else profile/options.get('skill_dir','skills/monitoring/astra-delivery')
        canonical=(Path.home()/'shared').resolve()
        if options.get('shared_root') and Path(options['shared_root']).expanduser().resolve()!=canonical:raise DeliveryError('ASTRA publication must use canonical ~/shared; do not migrate files from the skill')
        return cls(profile,skill,m['root'],canonical,options.get('origin',''))

def install_schema(store):
    if store.db.in_transaction:raise DeliveryError('Schema migration requires an independent transaction')
    try:
        store.db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS astra_incidents(
          finding_id TEXT PRIMARY KEY,status TEXT NOT NULL,revision INTEGER NOT NULL,
          repair_json TEXT NOT NULL,owner_confirmation TEXT,updated_at TEXT NOT NULL,
          closed_at TEXT,evidence_cutoff TEXT,episode INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS astra_deliveries(
          report_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,finding_id TEXT NOT NULL,
          lane TEXT NOT NULL,revision INTEGER NOT NULL,document_json TEXT NOT NULL,
          original_rca_hash TEXT NOT NULL,markdown_hash TEXT NOT NULL,markdown_path TEXT NOT NULL,
          record_path TEXT NOT NULL,shared_relpath TEXT NOT NULL,public_url TEXT,
          publication_state TEXT NOT NULL DEFAULT 'pending',published_revision INTEGER NOT NULL DEFAULT 0,
          published_hash TEXT,delivered_at TEXT,mode TEXT NOT NULL DEFAULT 'DEGRADED',
          last_error TEXT,notice_ack_revision INTEGER NOT NULL DEFAULT 0,episode INTEGER NOT NULL DEFAULT 1,catalog_item_id TEXT);
        CREATE TABLE IF NOT EXISTS astra_delivery_events(
          event_id TEXT PRIMARY KEY,finding_id TEXT NOT NULL,report_id TEXT NOT NULL,
          kind TEXT NOT NULL,request_hash TEXT NOT NULL,payload_json TEXT NOT NULL,at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS astra_deliveries_finding ON astra_deliveries(finding_id);
        CREATE INDEX IF NOT EXISTS astra_delivery_events_report ON astra_delivery_events(report_id,kind);
        CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT);
        INSERT OR IGNORE INTO runtime_meta VALUES('astra_delivery_schema','1');
        COMMIT;
        ''')
    except BaseException:
        store.db.rollback();raise


def counts(store):
    db=store.db
    return {'delivery_unregistered_history':db.execute('SELECT count(*) FROM rca_history h WHERE NOT EXISTS (SELECT 1 FROM astra_deliveries d WHERE d.batch_id=h.batch_id)').fetchone()[0],
            'pending_repairs':db.execute("SELECT count(*) FROM astra_incidents WHERE status IN ('open','repair assigned','repairing')").fetchone()[0],
            'deferred_incidents':db.execute("SELECT count(*) FROM astra_incidents WHERE status='deferred'").fetchone()[0],
            'delivery_pending':db.execute("SELECT count(*) FROM astra_deliveries WHERE publication_state!='published'").fetchone()[0],
            'pending_answers':db.execute("SELECT count(*) FROM astra_delivery_events a WHERE kind='answer' AND NOT EXISTS (SELECT 1 FROM astra_delivery_events b WHERE b.event_id='ack:'||a.event_id)").fetchone()[0]}

def load_renderer(config):
    assets=config.skill_dir/'assets';pin=assets/'manifest.json'
    if not pin.is_file():raise DeliveryError('Pinned template manifest missing; restore astra-delivery assets, do not improvise a template')
    manifest=json.loads(pin.read_text())
    for name in ('rca-template.html','rca_report.py','rca_report_shell.html'):
        p=assets/name
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=manifest.get('assets',{}).get(name):raise DeliveryError('Pinned template asset missing/changed: '+name+'; deliberate version update required')
    spec=importlib.util.spec_from_file_location('astra_delivery_pinned_report',assets/'rca_report.py')
    if spec is None or spec.loader is None:raise DeliveryError('Cannot load pinned renderer')
    renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
    if renderer.VERSION!=manifest['version']:raise DeliveryError('Pinned renderer/template version mismatch')
    return renderer

class Delivery:
    def __init__(self,store,config):
        self.store=store;self.db=store.db;self.config=config
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='astra_deliveries'").fetchone():install_schema(store)
        self.renderer=load_renderer(config)
    @contextmanager
    def transaction(self):
        # Serialize reads plus all finding/sibling updates, not just individual writes.
        if self.db.in_transaction:
            raise DeliveryError('Delivery mutation requires an independent transaction boundary')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback();raise
    def counts(self):return counts(self.store)
    def _row(self,report_id):
        row=self.db.execute('SELECT * FROM astra_deliveries WHERE report_id=?',(report_id,)).fetchone()
        if not row:raise DeliveryError('Unknown report ID')
        return row
    def _event(self,key,row,kind,payload):
        hashed=digest(payload);prior=self.db.execute('SELECT request_hash FROM astra_delivery_events WHERE event_id=?',(key,)).fetchone()
        if prior:
            if prior[0]!=hashed:raise DeliveryError('Idempotency conflict: event ID reused with different content')
            return False
        self.db.execute('INSERT INTO astra_delivery_events VALUES(?,?,?,?,?,?,?)',(key,row['finding_id'],row['report_id'],kind,hashed,packed(payload),stamp()));return True
    def show(self,report_id):
        row=dict(self._row(report_id));doc=json.loads(row.pop('document_json'));row['document']=doc;row['status']=doc['status'];return row
    def _get(self,path):
        with urllib.request.urlopen(self.config.origin+path,timeout=3) as response:
            return response.status,response.read(4*1024*1024)
    def web_available(self):
        try:
            if not (self.config.shared_root/'dashboard/handover.md').is_file():return False
            status,data=self._get('/dashboard/api/v1/session')
            return status==200 and isinstance(json.loads(data).get('csrf'),str)
        except (OSError,ValueError):return False
    def verify_public(self,path,data):
        try:
            status,body=self._get('/'+quote(path,safe='/'))
            if status!=200 or body.decode()!=self.renderer.render(data):return False
            _,body=self._get('/dashboard/api/v1/catalog');catalog=json.loads(body)
            item=next((i for i in catalog.get('items',[]) if i.get('path')==path and i.get('stable_id')==data['id'] and i.get('status')==data['status']),None)
            if not item or not isinstance(item.get('id'),str):return False
            with self.db:self.db.execute('UPDATE astra_deliveries SET catalog_item_id=? WHERE report_id=? AND revision=?',(item['id'],data['id'],data['report_revision']))
            return True
        except (OSError,ValueError):return False
    def complete(self,batch,markdown,archive_dir=None,shared_dir=None,now=None):
        self.store.validate_rca_report(markdown);prior=self.db.execute('SELECT report_id,markdown_hash FROM astra_deliveries WHERE batch_id=?',(batch,)).fetchone()
        if prior:
            if prior['markdown_hash']!=hashlib.sha256(markdown.encode()).hexdigest():raise DeliveryError('Batch already completed with different Markdown')
            self.reconcile(prior['report_id']);return self.result(prior['report_id'])
        b=self.db.execute('SELECT * FROM batches WHERE id=?',(batch,)).fetchone()
        if not b or b['status']!='running' or b['kind'] not in ('rca','rca-a','rca-b'):raise DeliveryError('Not an active RCA batch')
        finding=json.loads(b['ids_json'])[0];lane='rca-b' if b['kind']=='rca-b' else 'rca-a';context=self.store._item(finding)
        d=self.renderer.from_markdown(markdown,finding,batch,lane,context);d['published_at']=d['updated_at']=stamp(now);d['history'][0]['at']=d['published_at'];ident=d['id']
        archive=Path(archive_dir).expanduser().resolve() if archive_dir else self.config.records_root/lane
        if not archive.is_relative_to(self.config.records_root):raise DeliveryError('Archive must remain inside ASTRA records root')
        target=Path(shared_dir).expanduser().resolve() if shared_dir else self.config.shared_root/'astra/rca'/lane
        if not target.is_relative_to(self.config.shared_root/'astra'):raise DeliveryError('Publication must use the canonical shared ASTRA subtree')
        rel=(target/(ident+'.html')).relative_to(self.config.shared_root).as_posix()
        with self.transaction():
            replay=self.db.execute('SELECT * FROM astra_deliveries WHERE batch_id=?',(batch,)).fetchone()
            if replay:
                if replay['markdown_hash']!=hashlib.sha256(markdown.encode()).hexdigest():raise DeliveryError('Concurrent batch content conflict')
                return self.result(replay['report_id'])
            if self.db.execute('SELECT status FROM batches WHERE id=?',(batch,)).fetchone()[0]!='running':raise DeliveryError('Batch changed concurrently')
            existing=self.db.execute('SELECT * FROM astra_incidents WHERE finding_id=?',(finding,)).fetchone()
            if existing and existing['closed_at']:
                def epoch(value):
                    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp() if value else 0
                if b['created']>epoch(existing['closed_at']) and epoch(context.get('last'))>max(epoch(existing['evidence_cutoff']),epoch(existing['closed_at'])):
                    # A newly investigated post-closure recurrence is a new episode;
                    # old lane reports remain historical rather than being reopened.
                    self.db.execute('UPDATE astra_incidents SET status=?,revision=revision+1,repair_json=?,owner_confirmation=NULL,updated_at=?,closed_at=NULL,evidence_cutoff=?,episode=episode+1 WHERE finding_id=?',('open',packed(d['repair']),stamp(now),context.get('last'),finding))
                    existing=self.db.execute('SELECT * FROM astra_incidents WHERE finding_id=?',(finding,)).fetchone()
            if existing:
                # Comparison lanes share repair facts, never their original investigations.
                d['status']=existing['status'];d['repair']=json.loads(existing['repair_json'])
                if existing['owner_confirmation']:d['owner_confirmation']=existing['owner_confirmation']
                d['history'][0]['status']=d['status']
            else:self.db.execute('INSERT INTO astra_incidents(finding_id,status,revision,repair_json,owner_confirmation,updated_at,closed_at,evidence_cutoff) VALUES(?,?,?,?,?,?,?,?)',(finding,'open',1,packed(d['repair']),None,stamp(now),None,context.get('last')))
            self.db.execute('''INSERT INTO astra_deliveries(report_id,batch_id,finding_id,lane,revision,document_json,original_rca_hash,markdown_hash,markdown_path,record_path,shared_relpath) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(ident,batch,finding,lane,1,packed(d),self.renderer.fingerprint(d['rca']),hashlib.sha256(markdown.encode()).hexdigest(),str(archive/(ident+'.md')),str(self.config.records_root/'delivery'/ (ident+'.html')),rel))
            self.db.execute('UPDATE astra_deliveries SET episode=? WHERE report_id=?',(existing['episode'] if existing else 1,ident))
            row=self._row(ident);self._event('create:'+batch,row,'created',{'batch':batch,'markdown_hash':row['markdown_hash']})
            col='rca_b_done' if lane=='rca-b' else 'rca_a_done'
            self.db.execute('UPDATE findings SET '+col+'=1'+(',rca_done=1' if lane=='rca-a' else '')+',reviewed_at=? WHERE id=?',(time.time() if now is None else now,finding))
            self.db.execute("UPDATE batches SET status='done' WHERE id=?",(batch,))
            self.db.execute('INSERT INTO rca_history(finding_id,batch_id,report_path,shared_path,completed_at,summary,lane) VALUES(?,?,?,?,?,?,?)',(finding,batch,row['markdown_path'],None,time.time() if now is None else now,d['title'],lane[-1]))
            self.store.coverage.register(self.db.execute('SELECT last_insert_rowid()').fetchone()[0],markdown)
        self.reconcile(ident);return self.result(ident)
    def _write_local(self,row,d):
        markdown=Path(row['markdown_path']);text=d['rca'].get('source_markdown','')
        if markdown.exists() and markdown.read_text()!=text:raise DeliveryError('Original Markdown archive changed; refusing overwrite')
        if not markdown.exists():self.renderer.atomic_text(markdown,text)
        record=Path(row['record_path']);html=self.renderer.render(d)
        if not record.exists() or record.read_text()!=html:self.renderer.atomic_text(record,html)
    def ensure_local(self,report_id):
        import fcntl
        fd=os.open(self.config.records_root,os.O_RDONLY)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            row=self._row(report_id);self._write_local(row,json.loads(row['document_json']));return True
        finally:os.close(fd)
    def _projection(self,report_id,web):
        import fcntl
        fd=os.open(self.config.records_root,os.O_RDONLY)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return  # durable pending row will be retried
            self._projection_locked(report_id,web)
        finally:os.close(fd)
    def _projection_locked(self,report_id,web):
        import fcntl
        row=self._row(report_id);d=json.loads(row['document_json']);error=None;mode='DEGRADED';state='pending';url=None;public_fd=None
        try:
            if web:
                path=self.config.shared_root/row['shared_relpath'];path.parent.mkdir(parents=True,exist_ok=True)
                public_fd=os.open(path.parent,os.O_RDONLY);fcntl.flock(public_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self._import_web(report_id)
                row=self._row(report_id)
                if row['publication_state']=='conflict':return
                d=json.loads(row['document_json'])
            self._write_local(row,d)
            if not web:raise DeliveryError('Dashboard unavailable or conventions missing; local report and DB retained')
            self.renderer.publish(path,d,self.config.shared_root)
            if not self.verify_public(row['shared_relpath'],d):raise DeliveryError('Publication written but HTTP/catalog verification failed')
            mode='FULL';state='published';url=self.config.origin+'/'+quote(row['shared_relpath'],safe='/')
        except (OSError,ValueError) as exc:
            error=str(exc)[:500]
            # A public-directory failure must not prevent a local fallback.
            try:self._write_local(row,d)
            except (OSError,ValueError) as local:error+='; local record unavailable: '+str(local)[:200]
        finally:
            if public_fd is not None:os.close(public_fd)
        with self.transaction():
            if self._row(report_id)['revision']!=row['revision']:return
            self.db.execute('UPDATE astra_deliveries SET publication_state=?,mode=?,last_error=?,public_url=?,published_revision=?,published_hash=?,delivered_at=? WHERE report_id=?',(state,mode,error,url,row['revision'] if state=='published' else row['published_revision'],hashlib.sha256(self.renderer.render(d).encode()).hexdigest() if state=='published' else row['published_hash'],stamp() if state=='published' else row['delivered_at'],report_id))
            if state=='published':self.db.execute('UPDATE rca_history SET shared_path=? WHERE batch_id=?',(str(self.config.shared_root/row['shared_relpath']),row['batch_id']))
    def result(self,report_id):
        r=self._row(report_id);d=json.loads(r['document_json'])
        try:
            local_current=Path(r['record_path']).read_text()==self.renderer.render(d) and hashlib.sha256(Path(r['markdown_path']).read_bytes()).hexdigest()==r['markdown_hash']
        except (OSError,ValueError):local_current=False
        return {'ok':local_current and r['publication_state']!='conflict','local_record_available':Path(r['record_path']).is_file(),'local_record_current':local_current,'status':'persisted','report_id':report_id,'resolution_status':d['status'],'revision':r['revision'],'batch':r['batch_id'],'finding_id':r['finding_id'],'mode':r['mode'],'publication_state':r['publication_state'],'report_path':r['markdown_path'],'record_path':r['record_path'],'shared_path':str(self.config.shared_root/r['shared_relpath']) if r['publication_state']=='published' else None,'public_url':r['public_url'],'error':r['last_error'],'notification_pending':r['notice_ack_revision']<r['revision'],'notification_text':self.ping(report_id)}
    def ping(self,report_id):
        r=self._row(report_id);d=json.loads(r['document_json']);line='ASTRA '+','.join(d['affected_hosts'])+' '+r['lane']+' | '+d['severity']+' | '+d['status']+' — '+re.sub(r'\s+',' ',d['title']).replace('@','＠')[:100]
        if d['questions']:line+=' | reply requested'
        return line+('' if r['publication_state']=='published' else ' | DEGRADED')+'\n'+(r['public_url'] if r['publication_state']=='published' else 'MEDIA:'+r['record_path'])
    def _changed_document(self,before,change,actor):
        if not isinstance(change,dict):raise DeliveryError('Status change must be a JSON object')
        allowed={'status','repair','questions','next_action','reason','owner_confirmation'}
        if set(change)-allowed:raise DeliveryError('Updates cannot replace RCA content or report identity')
        d=copy.deepcopy(before);new=change.get('status',d['status'])
        transitions={'open':{'repair assigned','deferred',"won't fix"},'repair assigned':{'repairing','deferred',"won't fix"},'repairing':{'fixed','open','deferred',"won't fix"},'fixed':{'verified by owner','open'},'verified by owner':{'open'},'deferred':{'open','repair assigned'},"won't fix":{'open'}}
        if new!=d['status'] and new not in transitions[d['status']]:raise DeliveryError('Invalid incident transition')
        for k in allowed-{'reason'}:
            if k in change:d[k]=copy.deepcopy(change[k])
        old=before['repair'].get('attempts',[])
        if d['repair'].get('attempts',[])[:len(old)]!=old:raise DeliveryError('Repair attempts must remain append-only')
        if new in ('repair assigned','repairing') and not d['repair'].get('assigned_agent'):raise DeliveryError('Assigned repairer required')
        if new in ('deferred',"won't fix") and not change.get('reason'):raise DeliveryError('Disposition reason required')
        if new=='verified by owner' and new!=before['status'] and not change.get('owner_confirmation'):raise DeliveryError('Explicit current owner confirmation reference required')
        if new=='open' and before['status'] in ('fixed','verified by owner'):d.pop('owner_confirmation',None)
        if d['questions']!=before['questions']:d['form_version']+=1
        d['report_revision']+=1;d['updated_at']=stamp();d['history'].append({'revision':d['report_revision'],'at':d['updated_at'],'actor':actor,'status':new,'note':change.get('reason') or 'Recorded repair/status/question update'})
        self.renderer.validate(d);return d
    def update(self,report_id,change,expected_revision,actor):
        if not isinstance(actor,str) or not actor.strip():raise DeliveryError('Actor required')
        request={'report_id':report_id,'change':change,'expected_revision':expected_revision,'actor':actor};key='update:'+digest(request)
        if self.db.execute('SELECT 1 FROM astra_delivery_events WHERE event_id=?',(key,)).fetchone():return self.result(report_id)
        with self.transaction():
            if self.db.execute('SELECT 1 FROM astra_delivery_events WHERE event_id=?',(key,)).fetchone():return self.result(report_id)
            row=self._row(report_id)
            if row['revision']!=expected_revision:raise DeliveryError('Stale report revision; reread before update')
            d=self._changed_document(json.loads(row['document_json']),change,actor)
            self._commit_documents(row,d,key,request)
        self.reconcile(report_id,import_web=False);return self.result(report_id)
    def _commit_documents(self,row,document,key,event):
        self._event(key,row,'status',event)
        terminal=document['status'] in ('fixed','verified by owner',"won't fix")
        cutoff=self.db.execute('SELECT max(ts) FROM evidence WHERE gid=?',(row['finding_id'],)).fetchone()[0]
        self.db.execute('UPDATE astra_incidents SET status=?,revision=revision+1,repair_json=?,owner_confirmation=?,updated_at=?,closed_at=?,evidence_cutoff=? WHERE finding_id=? AND episode=?',(document['status'],packed(document['repair']),document.get('owner_confirmation'),stamp(),stamp() if terminal else None,cutoff,row['finding_id'],row['episode']))
        for sibling in self.db.execute('SELECT * FROM astra_deliveries WHERE finding_id=? AND episode=?',(row['finding_id'],row['episode'])).fetchall():
            d=document if sibling['report_id']==row['report_id'] else json.loads(sibling['document_json'])
            if sibling['report_id']!=row['report_id']:
                d['status']=document['status'];d['repair']=copy.deepcopy(document['repair'])
                if document.get('owner_confirmation'):d['owner_confirmation']=document['owner_confirmation']
                else:d.pop('owner_confirmation',None)
                d['report_revision']+=1;d['updated_at']=stamp();d['history'].append({'revision':d['report_revision'],'at':d['updated_at'],'actor':'incident-sync','status':d['status'],'note':'Repair facts synchronized from '+row['report_id']+'; original lane RCA preserved'})
            self.renderer.validate(d)
            if self.renderer.fingerprint(d['rca'])!=sibling['original_rca_hash']:raise DeliveryError('Original RCA changed')
            self.db.execute("UPDATE astra_deliveries SET document_json=?,revision=?,publication_state='pending',mode='DEGRADED' WHERE report_id=?",(packed(d),d['report_revision'],sibling['report_id']))
    def reconcile(self,report_id=None,import_web=True,catch_up=False):
        if report_id:
            target=self._row(report_id)
            rows=self.db.execute('SELECT rowid AS position,report_id FROM astra_deliveries WHERE finding_id=? AND episode=?',(target['finding_id'],target['episode'])).fetchall()
        else:
            self.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT)')
            previous=self.db.execute("SELECT value FROM runtime_meta WHERE key='delivery_sync_cursor'").fetchone();cursor=int(previous[0]) if previous else 0
            rows=self.db.execute('SELECT rowid AS position,report_id FROM astra_deliveries WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,1000 if catch_up else 10)).fetchall()
            if not rows and cursor:rows=self.db.execute('SELECT rowid AS position,report_id FROM astra_deliveries ORDER BY rowid LIMIT 10').fetchall()
        if not rows:return {'ok':True,'mode':'IDLE','reports':[],'has_more':False}
        started=time.monotonic();web=self.web_available();processed=[];warnings=[]
        for row in rows:
            if processed and not report_id and not catch_up and time.monotonic()-started>=5:break
            ident=row['report_id']
            try:
                self.ensure_local(ident)
                if import_web:self._import_web(ident)
                self.ensure_local(ident)
                intake=self.read_answers(ident,limit=1)
                if intake['warnings']:warnings.extend(intake['warnings'])
                current=self._row(ident);unchanged=False
                if current['publication_state']=='conflict':warnings.append('Report conflict: '+ident+' — '+str(current['last_error']))
                if current['publication_state']=='published' and current['published_revision']==current['revision'] and web:
                    public=self.config.shared_root/current['shared_relpath']
                    unchanged=public.is_file() and hashlib.sha256(public.read_bytes()).hexdigest()==current['published_hash'] and Path(current['record_path']).is_file()
                if current['publication_state']!='conflict' and not unchanged:self._projection(ident,web)
            except (OSError,ValueError) as exc:
                message='Report sync deferred: '+str(exc)[:240];warnings.append(message)
                with self.db:self.db.execute("UPDATE astra_deliveries SET last_error=?,mode='DEGRADED' WHERE report_id=?",(message,ident))
            processed.append(row)
        if processed and not report_id:
            with self.db:self.db.execute("INSERT OR REPLACE INTO runtime_meta VALUES('delivery_sync_cursor',?)",(str(processed[-1]['position']),))
        return {'ok':not warnings,'mode':'FULL' if web else 'DEGRADED','reports':[self.result(r['report_id']) for r in processed],'warnings':warnings,'error':warnings[0] if warnings else None,'has_more':len(processed)<len(rows) or (catch_up and len(rows)==1000)}
    def _import_web(self,report_id):
        row=self._row(report_id);path=self.config.shared_root/row['shared_relpath']
        if not path.is_file():return
        if path.stat().st_size>4*1024*1024:raise DeliveryError('Published report exceeds supported size')
        raw=path.read_bytes();h=hashlib.sha256(raw).hexdigest()
        expected=self.renderer.render(json.loads(row['document_json'])).encode()
        if raw==expected or h==row['published_hash']:
            if row['publication_state']=='conflict':
                with self.db:self.db.execute("UPDATE astra_deliveries SET publication_state='pending',last_error=NULL WHERE report_id=?",(report_id,))
            return
        if not row['published_hash']:
            with self.db:self.db.execute("UPDATE astra_deliveries SET publication_state='conflict',mode='DEGRADED',last_error=? WHERE report_id=?",('Unregistered different file occupies the report destination; preserved without overwrite',report_id))
            return
        try:
            new=self.renderer.read_report(path);old=json.loads(row['document_json'])
            if new['id']!=report_id or self.renderer.fingerprint(new['rca'])!=row['original_rca_hash']:raise DeliveryError('Published identity/original RCA changed')
            if new['report_revision']<row['revision']:
                # An older completed projection is recoverable, not an owner edit.
                if new['history']==old['history'][:len(new['history'])]:return
                raise DeliveryError('Older conflicting published history')
            if new['report_revision']==row['revision']:
                if row['publication_state']!='published' and packed(new)==row['document_json'] and raw.decode()==self.renderer.render(new):return
                raise DeliveryError('Published bytes changed without a new revision')
            if row['published_revision']!=row['revision']:raise DeliveryError('Concurrent DB/web updates; rebase against current records')
            if new['history'][:len(old['history'])]!=old['history']:raise DeliveryError('Published history is not append-only')
            mutable={'status','repair','questions','next_action','owner_confirmation','updated_at','report_revision','form_version','history'}
            if {k:v for k,v in old.items() if k not in mutable}!={k:v for k,v in new.items() if k not in mutable}:raise DeliveryError('Published immutable metadata changed')
            # Replay history transition names through the same validator; validate
            # final repair payload and reject missing history/revision continuity.
            transitions={'open':{'repair assigned','deferred',"won't fix"},'repair assigned':{'repairing','deferred',"won't fix"},'repairing':{'fixed','open','deferred',"won't fix"},'fixed':{'verified by owner','open'},'verified by owner':{'open'},'deferred':{'open','repair assigned'},"won't fix":{'open'}}
            state=old['status'];revision=old['report_revision']
            for event in new['history'][len(old['history']):]:
                revision+=1
                if event['revision']!=revision or (event['status']!=state and event['status'] not in transitions[state]):raise DeliveryError('Invalid external lifecycle history')
                state=event['status']
            if revision!=new['report_revision'] or state!=new['status']:raise DeliveryError('External revision/status disagrees with history')
            attempts=old['repair'].get('attempts',[])
            if new['repair'].get('attempts',[])[:len(attempts)]!=attempts:raise DeliveryError('External repair attempts were removed/edited')
            if new['questions']!=old['questions'] and new['form_version']<=old['form_version']:raise DeliveryError('Changed questions need a new form version')
            if new['status'] in ('repair assigned','repairing') and not new['repair'].get('assigned_agent'):raise DeliveryError('Assigned repairer missing')
            with self.transaction():
                if self._row(report_id)['revision']!=row['revision']:raise DeliveryError('Concurrent record update; retry sync')
                self._commit_documents(row,new,'web:'+report_id+':'+str(new['report_revision']),{'source':'published-report','sha256':h,'document':new})
        except (ValueError,KeyError,TypeError,OSError) as exc:
            with self.db:self.db.execute("UPDATE astra_deliveries SET publication_state='conflict',mode='DEGRADED',last_error=? WHERE report_id=?",(str(exc)[:500],report_id))
    def read_answers(self,report_id,limit=20):
        row=self._row(report_id);warnings=[]
        inbox=self.config.shared_root/'dashboard/_private/inbox/astra'
        for p in sorted(inbox.glob('*/answer.json')):
            try:
                if p.is_symlink() or p.parent.is_symlink() or p.stat().st_size>131072:raise DeliveryError('Unsafe or oversized receipt')
                d=json.loads(p.read_text())
                if d.get('item_path')!=row['shared_relpath']:continue
                if not row['catalog_item_id'] or d.get('item_id')!=row['catalog_item_id']:raise DeliveryError('Receipt catalog identity does not match the verified report')
                if d.get('form_id') not in (None,'rca-'+report_id):raise DeliveryError('Receipt form does not belong to this report')
                rid=d.get('receipt_id')
                if not isinstance(rid,str) or not re.fullmatch('[a-f0-9]{32}',rid) or p.parent.name!=rid or d.get('recipient')!='astra':raise DeliveryError('Receipt binding invalid')
                key='answer:'+rid
                for a in d.get('attachments',[]):
                    target=p.parent/a['stored_relative_name']
                    if not target.resolve().is_relative_to(p.parent.resolve()) or target.is_symlink() or target.stat().st_size!=a['byte_count']:raise DeliveryError('Attachment containment/size mismatch')
                    h=hashlib.sha256()
                    with target.open('rb') as f:
                        for chunk in iter(lambda:f.read(65536),b''):h.update(chunk)
                    if h.hexdigest()!=a['sha256']:raise DeliveryError('Attachment hash mismatch')
                payload={'receipt_id':rid,'receipt_path':p.relative_to(self.config.shared_root).as_posix(),'receipt':d}
                with self.transaction():self._event(key,row,'answer',payload)
            except (OSError,ValueError,KeyError,TypeError) as exc:warnings.append('Receipt not ingested: '+p.parent.name+' — '+str(exc)[:160])
        answers=[json.loads(r[0]) for r in self.db.execute("SELECT a.payload_json FROM astra_delivery_events a WHERE a.report_id=? AND a.kind='answer' AND NOT EXISTS (SELECT 1 FROM astra_delivery_events b WHERE b.event_id='ack:'||a.event_id) ORDER BY a.at,a.event_id LIMIT ?",(report_id,max(1,min(int(limit),100))))]
        return {'ok':not warnings,'report_id':report_id,'answers':answers,'warnings':warnings}
    def acknowledge(self,report_id,receipt_id,actor,note=''):
        row=self._row(report_id);key='answer:'+receipt_id
        if not actor:raise DeliveryError('Acknowledging actor required')
        if not self.db.execute("SELECT 1 FROM astra_delivery_events WHERE event_id=? AND report_id=? AND kind='answer'",(key,report_id)).fetchone():raise DeliveryError('Receipt must be ingested for this report first')
        with self.transaction():created=self._event('ack:'+key,row,'answer_ack',{'receipt_id':receipt_id,'actor':actor,'note':note})
        return {'ok':True,'acknowledged':receipt_id,'duplicate':not created}
    def acknowledge_notice(self,report_id,revision,reference):
        row=self._row(report_id)
        if not reference or not 1<=int(revision)<=row['revision']:raise DeliveryError('Valid revision and actual delivery reference required')
        with self.transaction():
            self._event('notice:'+report_id+':'+str(revision),row,'notice_ack',{'revision':revision,'reference':reference})
            self.db.execute('UPDATE astra_deliveries SET notice_ack_revision=max(notice_ack_revision,?) WHERE report_id=?',(revision,report_id))
        return {'ok':True,'acknowledged_revision':revision}


def configured(profile_home=None):
    home=Path(profile_home or os.environ.get('HERMES_HOME','')).expanduser()
    p=home/'astra-jobs.json'
    if not p.is_file():return None
    m=json.loads(p.read_text())
    return Config.from_profile(home) if m.get('delivery',{}).get('enabled') else None


def reconcile_best_effort(store):
    try:return Delivery(store,store.delivery_config).reconcile()
    except Exception as exc:
        return {'ok':False,'mode':'DEGRADED','error':type(exc).__name__+': '+str(exc)[:240]}


def run_command(command,args,*,profile_home,skill_dir):
    cfg=Config.from_profile(profile_home,skill_dir)
    renderer=load_renderer(cfg)
    if command=='assemble':
        d=json.loads(Path(args['input']).expanduser().read_text());out=Path(args['output']).expanduser().resolve()
        if out.is_relative_to(cfg.shared_root):raise DeliveryError('Assemble into private drafts; use publish for shared delivery')
        text=renderer.render(d)
        if out.exists() and out.read_text()!=text:raise DeliveryError('Assemble output exists with different content')
        renderer.atomic_text(out,text);return {'ok':True,'path':str(out),'report_id':d['id']}
    if not (cfg.records_root/'review.sqlite').is_file():raise DeliveryError('ASTRA records database missing; verify the profile root before delivery')
    if command=='preflight':
        docs=(cfg.shared_root/'dashboard/handover.md').is_file()
        canonical=(cfg.shared_root/'astra/report-template/rca-template.html').is_file()
        # The verified bundled template is the runtime prerequisite. A missing
        # canonical reference must not defeat its deliberately pinned fallback.
        try:
            with urllib.request.urlopen(cfg.origin+'/dashboard/api/v1/session',timeout=3) as response:web=response.status==200 and bool(json.load(response).get('csrf'))
        except (OSError,ValueError):web=False
        warnings=[]
        if not docs or not web:warnings.append('Dashboard/conventions unavailable; local records remain authoritative')
        if not canonical:warnings.append('Canonical template reference unavailable; using the verified pinned copy')
        return {'ok':True,'mode':'FULL' if docs and web else 'DEGRADED','template_version':renderer.VERSION,'shared_root':str(cfg.shared_root),'records_root':str(cfg.records_root),'warnings':warnings}
    from astra.review import ReviewStore
    store=ReviewStore(cfg.records_root/'review.sqlite')
    try:
        e=Delivery(store,cfg)
        if command=='publish':return e.complete(args['batch'],Path(args['report']).expanduser().read_text(),args.get('archive_dir'),args.get('shared_dir'))
        if command=='read-answers':return e.read_answers(args['report_id'],args.get('limit',20))
        if command=='acknowledge':return e.acknowledge(args['report_id'],args['receipt_id'],args['actor'],args.get('note') or '')
        if command=='update-status':return e.update(args['report_id'],json.loads(Path(args['change']).expanduser().read_text()),args['expected_revision'],args['actor'])
        if command=='reconcile':return e.reconcile(args.get('report_id'),catch_up=bool(args.get('catch_up')))
        if command=='show':
            view=e.show(args['report_id']);document=view.pop('document')
            view.update(form_version=document['form_version'],repairer=document['repair'].get('assigned_agent'),owner_confirmation=document.get('owner_confirmation'))
            return {'ok':True,**view}
        if command=='pending':return {'ok':True,**e.counts(),'incidents':[dict(x) for x in store.db.execute("SELECT finding_id,status,revision,updated_at FROM astra_incidents WHERE status IN ('open','repair assigned','repairing') ORDER BY updated_at LIMIT 100")]}
        if command=='ping':return {'ok':True,'text':e.ping(args['report_id']),'notification_needed':e._row(args['report_id'])['notice_ack_revision']<e._row(args['report_id'])['revision']}
        if command=='acknowledge-notice':return e.acknowledge_notice(args['report_id'],args['revision'],args['reference'])
        raise DeliveryError('Unsupported delivery command')
    finally:store.close()
