import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from astra.review import ReviewStore
from tests.test_review import event

class Runtime(unittest.TestCase):
    def test_deployment_has_all_native_entrypoints(self):
        root=Path(__file__).resolve().parents[1]/'src'
        for name in ('astra_review.py','astra_dispatch.py'):
            self.assertTrue((root/'agent'/name).is_file(),name)
        for name in ('astra_triage_prepare.py','astra_rca_prepare.py','astra_daily_prepare.py'):
            self.assertTrue((root/'deploy'/'cron-wrappers'/name).is_file(),name)

    def test_native_success_without_ack_stays_pending_and_notifies_once(self):
        from astra import runtime
        self.assertTrue(hasattr(runtime,'reconcile_results'),'completion verification missing')
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite');s.ingest([event(1)])
            b=s.prepare(now=1000)
            jobs={'triage':{'id':'t','last_run_at':'1970-01-01T00:20:00+00:00','last_status':'ok','state':'scheduled'}}
            notices=runtime.reconcile_results(s,jobs,now=1201)
            self.assertEqual(len(notices),1)
            self.assertEqual(s.status(now=1201)['pending_triage'],1)
            self.assertEqual(runtime.reconcile_results(s,jobs,now=1202),[])
            self.assertEqual(s.prepare(now=1203)['batch'],b['batch'])
            s.close()

    def test_rca_cron_response_is_reconciled_when_agent_skips_completion_helper(self):
        from astra import runtime
        from tests.test_review import TestReviewStore
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'profile';out=home/'cron/output/r';out.mkdir(parents=True)
            s=ReviewStore(root/'review.sqlite');s.ingest([event(1)],now=1000)
            triage=s.prepare('triage',now=1001)
            gid=triage['items'][0]['id']
            s.complete(triage['batch'],[{'id':gid,'decision':'rca','reason':'Bounded investigation required.'}],now=1002)
            batch=s.prepare('rca',now=1003)
            output=(
                '# Cron Job: RCA\n\n## Prompt\n\n'
                f'```json\n{{"batch":"{batch["batch"]}"}}\n```\n\n'
                '## Response\n\n'+TestReviewStore.RCA_MD
            )
            (out/'1970-01-01_00-17-00.md').write_text(output)
            jobs={'rca':{'id':'r','last_run_at':'1970-01-01T00:17:00+00:00','last_status':'ok','state':'completed'}}
            s.db.execute('CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT)')
            s.db.execute("INSERT INTO runtime_meta VALUES('checked:r','1970-01-01T00:17:00+00:00')")
            s.db.commit()
            manifest={'shared':str(root/'shared'/'rca')}
            with patch.dict(os.environ,{'HERMES_HOME':str(home)}):
                notices=runtime.reconcile_results(s,jobs,now=1021,manifest=manifest)
            self.assertEqual(notices,[])
            self.assertEqual(s.status(now=1022)['pending_rca'],0)
            self.assertTrue(list((root/'rca-a').glob('*.md')))
            self.assertTrue(list((root/'shared'/'rca'/'rca-a').glob('*.md')))
            s.close()

    def test_gate_accounts_prepared_work_and_quiet_has_no_model(self):
        self.assertIsNotNone(importlib.util.find_spec('astra.runtime'),'native integration missing')
        from astra.runtime import prepare_job
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'enriched').mkdir()
            m={'root':str(root),'shared':str(root/'shared'),'host':'test','enabled':True}
            s=ReviewStore(root/'review.sqlite')
            self.assertFalse(prepare_job('triage',m,s)['wakeAgent'])
            s.ingest([event(1)])
            packet=prepare_job('triage',m,s,prune=False)
            self.assertTrue(packet['wakeAgent']);self.assertIn('helper',packet)
            self.assertFalse(prepare_job('rca',m,s,prune=False)['wakeAgent'])
            s.complete(packet['batch'],[{'id':packet['items'][0]['id'],'decision':'watch','reason':'Known commissioning signal retained for review.'}])
            self.assertFalse(prepare_job('triage',m,s,prune=False)['wakeAgent'])
            s.close()
