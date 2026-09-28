import unittest,tempfile,shutil,json,hashlib,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src/agent'))
def attach_fake_web(engine):
 from urllib.parse import unquote
 engine.web_available=lambda:True
 def get(path):
  if path=='/dashboard/api/v1/catalog':
   items=[]
   for row in engine.db.execute('SELECT * FROM astra_deliveries'):
    d=json.loads(row['document_json']);items.append({'id':'catalog-'+row['report_id'],'path':row['shared_relpath'],'stable_id':row['report_id'],'status':d['status']})
   return 200,json.dumps({'items':items}).encode()
  return 200,(engine.config.shared_root/unquote(path.lstrip('/'))).read_bytes()
 engine._get=get

class DeliveryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def engine(self):
  try:from astra.delivery import Config,Delivery,install_schema
  except ImportError:self.fail('Delivery engine is not implemented')
  from astra.review import ReviewStore
  from tests.test_review import event,TestReviewStore
  self.md=TestReviewStore.RCA_MD
  skill=self.root/'profile/skills/monitoring/astra-delivery';assets=skill/'assets';assets.mkdir(parents=True)
  for src,name in [(ROOT/'src/agent/astra/rca_report.py','rca_report.py'),(ROOT/'src/agent/astra/rca_report_shell.html','rca_report_shell.html'),(ROOT/'src/report-template/rca-template.html','rca-template.html')]:shutil.copy2(src,assets/name)
  (assets/'manifest.json').write_text(json.dumps({'version':'1.0.0','assets':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in assets.iterdir()}}))
  shared=self.root/'shared';(shared/'dashboard').mkdir(parents=True);(shared/'dashboard/handover.md').write_text('conventions')
  s=ReviewStore(self.root/'records/review.sqlite');self.addCleanup(s.close);install_schema(s)
  s.ingest([event(1)],now=1000);t=s.prepare('triage',now=1001);s.complete(t['batch'],[{'id':i['id'],'decision':'rca','reason':'Synthetic integration fixture'} for i in t['items']],now=1002);batch=s.prepare('rca-a',now=1003)['batch']
  cfg=Config(self.root/'profile',skill,self.root/'records',shared,'https://fixture.example:8443');e=Delivery(s,cfg);attach_fake_web(e)
  return e,s,batch
 def test_full_publish_idempotence_and_resolution(self):
  e,s,b=self.engine();a=e.complete(b,self.md,now=1004);again=e.complete(b,self.md,now=1005)
  self.assertEqual(a['report_id'],again['report_id']);self.assertEqual(a['shared_path'],again['shared_path']);self.assertEqual(a['mode'],'FULL');self.assertEqual(e.counts()['pending_repairs'],1)
  d=e.show(a['report_id']);rca_hash=e.renderer.fingerprint(d['document']['rca']);repair={'assigned_agent':'other-agent','assigned_host':'other-peer','assigned_at':e.renderer.timestamp(),'attempts':[],'followups':[]}
  e.update(a['report_id'],{'status':'repair assigned','repair':repair},1,'owner')
  e.update(a['report_id'],{'status':'repairing'},2,'other-agent')
  repair['attempts']=[{'agent':'other-agent','host':'other-peer','started_at':e.renderer.timestamp(),'finished_at':e.renderer.timestamp(),'actions':['Synthetic test repair'],'changes':[],'verification':[{'check':'synthetic fixture','passed':True,'result':'pass','evidence_ref':'test'}]}]
  e.update(a['report_id'],{'status':'fixed','repair':repair},3,'other-agent');d=e.show(a['report_id'])
  self.assertEqual(d['status'],'fixed');self.assertEqual(e.counts()['pending_repairs'],0);self.assertEqual(e.renderer.fingerprint(d['document']['rca']),rca_hash)
  self.assertEqual(len(list((e.config.shared_root/'astra/rca/rca-a').glob('*.html'))),1)
 def test_dashboard_down_does_not_block_completion_or_repair(self):
  e,s,b=self.engine();e.web_available=lambda:False
  a=e.complete(b,self.md,now=1004);self.assertEqual(a['mode'],'DEGRADED');self.assertTrue(Path(a['record_path']).exists());self.assertEqual(s.db.execute('SELECT status FROM batches WHERE id=?',(b,)).fetchone()[0],'done')
  gid=s.db.execute('SELECT finding_id FROM rca_history').fetchone()[0]
  self.assertIsNotNone(s.coverage.for_finding(gid))
  self.assertFalse(s.prepare('rca-b',now=1005)['wakeAgent'])
  self.assertEqual(s.db.execute('SELECT rca_b_done FROM findings WHERE id=?',(gid,)).fetchone()[0],0)
  e.update(a['report_id'],{'status':'deferred','reason':'Owner deferred fixture'},1,'owner');self.assertEqual(e.show(a['report_id'])['status'],'deferred')
  e.web_available=lambda:True;e.reconcile(a['report_id']);self.assertEqual(e.show(a['report_id'])['publication_state'],'published');self.assertEqual(e.show(a['report_id'])['status'],'deferred')
 def test_answers_ingest_once_ack_and_external_repair(self):
  e,s,b=self.engine();a=e.complete(b,self.md,now=1004);rid=a['report_id'];self.assertTrue(callable(getattr(e,'read_answers',None)),'Answer ingestion missing')
  receipt=e.config.shared_root/'dashboard/_private/inbox/astra'/('a'*32)/'answer.json';receipt.parent.mkdir(parents=True)
  data={'receipt_id':'a'*32,'item_id':e.show(rid)['catalog_item_id'],'item_path':e.show(rid)['shared_relpath'],'recipient':'astra','submission_id':'123e4567-e89b-12d3-a456-426614174000','received_at':'2026-09-23T00:00:00Z','answers':{},'comment':'Owner test','attachments':[]}
  receipt.write_text(json.dumps(data));self.assertEqual(len(e.read_answers(rid)['answers']),1);self.assertEqual(len(e.read_answers(rid)['answers']),1)
  e.acknowledge(rid,'a'*32,'agent','Reviewed');self.assertEqual(e.counts()['pending_answers'],0)
  public=Path(a['shared_path']);e.renderer.update_report(public,{'status':'deferred','reason':'Owner requested pause'},1,'owner',e.config.shared_root)
  e.reconcile(rid);self.assertEqual(e.show(rid)['status'],'deferred')
  doc=e.renderer.read_report(public);doc['rca']['cause']='tampered';public.write_text(e.renderer.render(doc));e.reconcile(rid)
  self.assertEqual(e.show(rid)['publication_state'],'conflict');self.assertNotEqual(e.show(rid)['document']['rca']['cause'],'tampered')

 def test_crash_gap_after_updated_file_write_reconciles(self):
  e,s,b=self.engine();a=e.complete(b,self.md,now=1004);rid=a['report_id']
  e.verify_public=lambda path,data:False
  e.update(rid,{'status':'deferred','reason':'Owner deferred'},1,'owner')
  self.assertEqual(e.show(rid)['publication_state'],'pending')
  e.verify_public=lambda path,data:True;e.reconcile(rid)
  self.assertEqual(e.show(rid)['publication_state'],'published')

 def test_native_complete_hook_and_sync_failure_isolation(self):
  from unittest.mock import patch
  from astra.review import ReviewStore
  from astra.runtime import reconcile_results
  e,s,b=self.engine();profile=e.config.profile_home
  (profile/'astra-jobs.json').write_text(json.dumps({'root':str(e.config.records_root),'delivery':{'enabled':True,'origin':e.config.origin}}))
  with patch.dict(__import__('os').environ,{'HOME':str(self.root),'USERPROFILE':str(self.root),'HERMES_HOME':str(profile)}),patch('astra.delivery.Delivery.web_available',return_value=False):
   native=ReviewStore(s.path)
   try:
    result=native.complete_rca(b,self.md,now=1004)
    self.assertEqual(result['mode'],'DEGRADED');self.assertTrue(Path(result['record_path']).is_file());self.assertEqual(native.status()['pending_repairs'],1)
    self.assertEqual(native.get_history()['findings_history'][0]['repair_state']['status'],'open')
    (e.config.skill_dir/'assets/rca_report_shell.html').unlink()
    notices=reconcile_results(native,{},now=1005,manifest={})
    self.assertTrue(any('delivery sync deferred' in n for n in notices))
   finally:native.close()

if __name__=='__main__':unittest.main(verbosity=2)
