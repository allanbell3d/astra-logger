import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from astra import runtime
class RcaPreflight(unittest.TestCase):
 def test_both_rca_roles_use_rca_phase_and_own_job_prefix(self):
  for role,jid in [('rca','lanea'),('rca-a','lanea'),('rca-b','laneb')]:
   with self.subTest(role=role),tempfile.TemporaryDirectory() as td:
    p=Path(td);(p/'scripts').mkdir();(p/'astra-jobs.json').write_text(json.dumps({'enabled':True,'root':td,'jobs':{'rca':'lanea','rca-a':'lanea','rca-b':'laneb'}}))
    (p/'scripts/astra_phase_models.py').write_text('def arm_restore(phase,prefixes,ttl):\n import os,json\n from pathlib import Path\n Path(os.environ["HERMES_HOME"],"call.json").write_text(json.dumps([phase,prefixes,ttl]))\n')
    out=io.StringIO()
    with patch.dict(os.environ,{'HERMES_HOME':td}),patch.object(runtime,'ReviewStore'),patch.object(runtime,'prepare_job',return_value={'wakeAgent':True}),contextlib.redirect_stdout(out):runtime.pre_script(role)
    self.assertEqual(json.loads(out.getvalue()),{'wakeAgent':True})
    self.assertEqual(json.loads((p/'call.json').read_text()),['rca',[f'cron_{jid}_'],60])
if __name__=='__main__':unittest.main()
