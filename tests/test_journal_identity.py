"""Distinct journal cursors must survive even when MESSAGE repeats."""
import json
import tempfile
import unittest
from pathlib import Path
from astra.journald_source import JournaldEntry
from astra.raw_pipeline import run_raw_pipeline

class TestJournalIdentity(unittest.TestCase):
    def test_repeated_message_distinct_cursors_are_not_lost(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            entries=[JournaldEntry({'MESSAGE':'Database failure','PRIORITY':'3','__CURSOR':f'c{i}','__REALTIME_TIMESTAMP':str(1789560000000000+i*1000000),'_SYSTEMD_UNIT':'hermes.service'},f'c{i}') for i in range(2)]
            def reader(after_cursor,limit,since=None):
                return entries if after_cursor is None else []
            r=run_raw_pipeline(str(p/'.hermes'),'test',str(p/'e'),str(p/'g'),str(p/'s'),journal_reader=reader,now='2026-09-16T12:10:00Z')
            self.assertEqual(r['processed'],2)
            out=[json.loads(x) for x in (p/'e').read_text().splitlines()]
            self.assertEqual(len({x['record_id'] for x in out}),2)
