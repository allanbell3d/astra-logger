import sys,json,re,tempfile,unittest,importlib.util,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('ASTRA_TEST_PACKAGE',str(ROOT/'src/agent')))
class Reporting(unittest.TestCase):
 def module(self):
  self.assertIsNotNone(importlib.util.find_spec('astra.rca_report'),'Standard report renderer missing')
  from astra import rca_report
  return rca_report
 def test_self_contained_report_has_status_rca_repair_questions_and_metadata(self):
  r=self.module();d=r.new_report('Example','incident-1','author','test-host')
  d['rca']['impact']='Owner cannot receive updates'
  d['questions']=[{'id':'decision','label':'Proceed?','type':'choice','choices':['Yes','No'],'required':True}]
  page=r.render(d)
  self.assertIn('Owner cannot receive updates',page);self.assertIn('Repair pending',page)
  meta=json.loads(re.search(r'id="delivery-metadata">(.*?)</script>',page,re.S)[1])
  self.assertEqual(meta['id'],'incident-1');self.assertEqual(meta['status'],'open')
  self.assertNotRegex(page,r'<(?:script|link)[^>]+(?:src|href)=')
  self.assertIn('Proceed?',page)
 def test_update_in_place_preserves_rca_and_rejects_stale_or_unverified_fix(self):
  r=self.module();self.assertTrue(callable(getattr(r,'update_report',None)),'Repair update implementation missing')
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'astra').mkdir();p=root/'astra/report.html'
   d=r.new_report('Incident','one','author','host');d['rca']['cause']='Original diagnosis';r.publish(p,d,root)
   original=r.fingerprint(d['rca'])
   assigned={'assigned_agent':'different-repairer','assigned_host':'other-host','assigned_at':'2026-09-22T12:00:00Z','attempts':[],'followups':[]}
   updated=r.update_report(p,{'status':'repair assigned','repair':assigned},1,'owner',root)
   self.assertEqual(updated['id'],'one');self.assertEqual(r.fingerprint(updated['rca']),original)
   with self.assertRaises(ValueError):r.update_report(p,{'status':'fixed'},2,'repairer',root)
   with self.assertRaises(ValueError):r.update_report(p,{'status':'repairing'},1,'repairer',root)
   updated=r.update_report(p,{'status':'repairing'},2,'repairer',root)
   repaired=dict(assigned);repaired['attempts']=[{'agent':'different-repairer','host':'other-host','started_at':'2026-09-22T12:00:00Z','finished_at':'2026-09-22T12:01:00Z','actions':['Changed exactly one setting'],'changes':['File /example; backup /example.bak'],'verification':[{'check':'Service response','passed':True,'result':'Expected response','evidence_ref':'test-only'}]}]
   updated=r.update_report(p,{'status':'fixed','repair':repaired},3,'repairer',root)
   self.assertEqual(updated['status'],'fixed');self.assertEqual(r.fingerprint(updated['rca']),original);self.assertEqual(len(list((root/'astra').glob('*.html'))),1)
   with self.assertRaises(ValueError):r.update_report(p,{'rca':{}},4,'repairer',root)
   with self.assertRaises(ValueError):r.update_report(p,{'status':'verified by owner'},4,'repairer',root)
   self.assertEqual(r.update_report(p,{'status':'verified by owner','owner_confirmation':'Owner reply receipt XYZ'},4,'owner',root)['status'],'verified by owner')

 def test_markdown_adapter_keeps_evidence_and_authored_questions(self):
  r=self.module();self.assertTrue(callable(getattr(r,'from_markdown',None)),'ASTRA adapter missing')
  text='# Test incident\n```rca-meta\n'+json.dumps({'impact':'Queue stalled','questions':[{'id':'retry','label':'Retry?','type':'choice','choices':['Yes','No']}]})+'\n```\n## Observed impact\nQueue stalled\n## Root cause and confidence\nPermission denied; high confidence\n## Rollback\nRestore backup'
  d=r.from_markdown(text,'finding','batch','a',{'host':'peer','severity':'watch','component':'queue'})
  self.assertEqual(d['rca']['impact'],'Queue stalled');self.assertEqual(d['questions'][0]['id'],'retry');self.assertEqual(d['severity'],'watch')
  self.assertEqual(d['rca']['source_markdown'],text);self.assertEqual(d['status'],'open')
  page=r.render(d);self.assertIn('Restore backup',page)

 def test_complete_rca_publishes_standard_html_without_changing_markdown(self):
  self.module()
  from astra.review import ReviewStore
  from test_review import event,TestReviewStore
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);s=ReviewStore(root/'review.sqlite')
   try:
    s.ingest([event(1)],now=1000);triage=s.prepare('triage',now=1001)
    s.complete(triage['batch'],[{'id':x['id'],'decision':'rca','reason':'Report rendering fixture'} for x in triage['items']],now=1002)
    batch=s.prepare('rca',now=1003)
    result=s.complete_rca(batch['batch'],TestReviewStore.RCA_MD,root/'archive',root/'shared/astra/rca-a',now=1004)
    self.assertIn('id="delivery-metadata"',Path(result['shared_path']).read_text())
    self.assertEqual(Path(result['report_path']).read_text(),TestReviewStore.RCA_MD)
   finally:s.close()

 def test_escape_lifecycle_and_no_unverified_initial_fixed_report(self):
  r=self.module();d=r.new_report('Test','escapes','author','peer')
  bad=dict(d);bad['status']='fixed'
  with self.assertRaises(ValueError):r.render(bad)
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'incident.html';r.publish(p,d)
   for status in ('deferred','open',"won't fix",'open'):
    d=r.update_report(p,{'status':status,'reason':'Test disposition'},d['report_revision'],'test-owner')
    self.assertEqual(d['status'],status)

 def test_home_relative_publish_and_read(self):
  from unittest.mock import patch
  from contextlib import chdir
  r=self.module()
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'HOME':d,'USERPROFILE':d}),chdir(d):
   data=r.new_report('Portable','portable-test','author','host')
   r.publish('~/shared/agent/report.html',data,'~/shared')
   self.assertTrue((Path(d)/'shared/agent/report.html').exists())
   self.assertEqual(r.read_report('~/shared/agent/report.html')['id'],'portable-test')

if __name__=='__main__':unittest.main(verbosity=2)
