"""Regression: a bounded convenience tail must not read the whole file.
The accounted review feed is separate; this API intentionally returns a partial tail.
"""
import json
import tempfile
import unittest
from pathlib import Path
from astra.compressed import CompressedStream

class TestBoundedTail(unittest.TestCase):
    def test_byte_budget_never_returns_partial_or_oversized_record(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'events.jsonl'
            old=json.dumps({'ts':'2026-09-16T10:00:00+04:00','text':'old'*100})+'\n'
            new=json.dumps({'ts':'2026-09-16T10:01:00+04:00','text':'new'})+'\n'
            p.write_bytes((old+new).encode("utf-8"))
            self.assertEqual(CompressedStream.read_tail(str(p),window_minutes=None,max_bytes=1),[])
            self.assertEqual(CompressedStream.read_tail(str(p),window_minutes=None,max_bytes=len(new.encode())),[json.loads(new)])
