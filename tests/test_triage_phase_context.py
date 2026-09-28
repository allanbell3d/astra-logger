import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from astra.review import ReviewStore
from astra import runtime
from tests.test_review import event

class PacketContext(unittest.TestCase):
 def test_prior_status_and_clipping_are_supplied(self):
  with tempfile.TemporaryDirectory() as td:
   s=ReviewStore(Path(td)/'review.sqlite')
   text='ERROR missing key '+('context '*50)+' recovered'
   s.ingest([event(1,text=text)],now=1000)
   b=s.prepare('triage',now=1001);gid=b['items'][0]['id']
   s.complete(b['batch'],[{'id':gid,'decision':'rca','reason':'Owner requests bounded investigation of this failure.'}],now=1002)
   item=s._item(gid)
   self.assertEqual(item['prior_status']['decision'],'rca')
   self.assertIn('Owner requests',item['prior_status']['reason'])
   self.assertTrue(item['sample_truncated'])
   self.assertEqual(item['sample_original_chars'],len(text))
   self.assertEqual(len(item['sample']),240)
   s.close()

class TriagePhase(unittest.TestCase):
 def run_script(self,packet):
  td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);home=Path(td.name)
  (home/'scripts').mkdir();(home/'astra-jobs.json').write_text(json.dumps({'enabled':True,'root':str(home),'jobs':{'triage':'abc123'}}))
  (home/'scripts/astra_phase_models.py').write_text('def arm_restore(phase,prefixes,ttl):\n from pathlib import Path\n import os,json\n Path(os.environ["HERMES_HOME"],"called.json").write_text(json.dumps([phase,prefixes,ttl]))\n print("APPLIED diagnostic only")\n')
  stdout=io.StringIO();stderr=io.StringIO()
  with patch.dict(os.environ,{'HERMES_HOME':str(home)}),patch.object(runtime,'ReviewStore'),patch.object(runtime,'prepare_job',return_value=packet),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):runtime.pre_script('triage')
  return home,stdout.getvalue(),stderr.getvalue()
 def test_waking_triage_arms_phase_without_polluting_packet(self):
  packet={'wakeAgent':True,'batch':'test','items':[]}
  home,out,err=self.run_script(packet)
  self.assertEqual(json.loads(out),packet)
  self.assertEqual(json.loads((home/'called.json').read_text()),['triage',['cron_abc123_'],60])
  self.assertIn('APPLIED',err)
 def test_quiet_triage_does_not_touch_phase(self):
  home,out,err=self.run_script({'wakeAgent':False,'reason':'empty'})
  self.assertFalse((home/'called.json').exists())
  self.assertFalse(json.loads(out)['wakeAgent'])

if __name__=='__main__':unittest.main()
