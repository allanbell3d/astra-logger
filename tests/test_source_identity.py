import json
from pathlib import Path
import tempfile
import unittest
from astra.raw_pipeline import run_raw_pipeline

class SourceIdentity(unittest.TestCase):
    def test_captured_file_keeps_inode_device_and_content_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); home=root/'.hermes'; logs=home/'logs';logs.mkdir(parents=True)
            source=logs/'agent.log'
            source.write_text('2026-09-16 12:00:00 ERROR agent.storage: database disk image is malformed\n')
            result=run_raw_pipeline(str(home),'test',str(root/'enriched'),str(root/'groups'),str(root/'state'),journal_reader=None,now='2026-09-16T12:01:00Z')
            self.assertEqual(result['processed'],1)
            row=json.loads((root/'enriched').read_text().splitlines()[0])
            identity=row.get('source_identity',{})
            self.assertEqual(identity.get('inode'),source.stat().st_ino)
            self.assertEqual(identity.get('dev'),source.stat().st_dev)
            self.assertEqual(len(identity.get('content_hash','')),64)
