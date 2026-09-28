import json,tempfile,unittest
from pathlib import Path
from astra.review import ReviewStore,packed
from astra.runtime import prepare_job
from tests.test_review import event
class OwnerKnownIssues(unittest.TestCase):
 def test_exact_owner_reference_is_supplied_without_reclassification(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'enriched').mkdir();manual='Known OpenRouter auxiliary warnings: monitor. New material failures still require evidence.'
   (p/'triage-known-issues.md').write_text(manual)
   s=ReviewStore(p/'review.sqlite');s.ingest([event(1)],now=1000)
   packet=prepare_job('triage',{'root':str(p),'host':'sandbox','shared':str(p/'shared')},s,prune=False)
   self.assertTrue(packet['wakeAgent']);self.assertEqual(packet['owner_known_issues']['text'],manual)
   self.assertLessEqual(len(packed(packet).encode()),32768)
   self.assertIsNone(s.db.execute('SELECT decision FROM findings').fetchone()[0]);s.close()
 def test_no_reference_needed_for_quiet_packet(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);(p/'enriched').mkdir();s=ReviewStore(p/'review.sqlite')
   packet=prepare_job('triage',{'root':str(p),'host':'sandbox','shared':str(p/'shared')},s,prune=False)
   self.assertFalse(packet['wakeAgent']);self.assertNotIn('owner_known_issues',packet);s.close()
if __name__=='__main__':unittest.main()
