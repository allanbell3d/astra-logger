import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock

class LauncherDispatch(unittest.TestCase):
    def test_capture_hands_off_to_managed_native_cron_without_waiting_for_llm(self):
        path=Path(__file__).resolve().parents[1]/'src/agent/astra_pipeline.py'
        spec=importlib.util.spec_from_file_location('capture_launcher',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);manifest=home/'.hermes/profiles/astra-test-health/astra-jobs.json';manifest.parent.mkdir(parents=True);manifest.write_text(json.dumps({'enabled':True}))
            route=home/'.hermes/scripts/astra-host.json';route.parent.mkdir(parents=True);route.write_text(json.dumps({'profile':'astra-test-health'}))
            result={'processed':0,'errors':[],'backlog_bytes':0}
            with patch.object(m.Path,'home',return_value=home),patch.object(m,'run_raw_pipeline',return_value=result),patch('subprocess.run',return_value=Mock(returncode=0)) as run:
                self.assertEqual(m.main(),0)
                self.assertEqual(run.call_count,1)
                self.assertEqual(run.call_args.args[0],['systemctl','--user','start','--no-block','astra-health-dispatch.service'])
                self.assertLessEqual(run.call_args.kwargs['timeout'],10)
