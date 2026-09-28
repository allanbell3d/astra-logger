#!/usr/bin/env python3
"""Continuation attachment, timestamp inheritance, and record shape."""
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from pathlib import Path

from astra.raw_pipeline import run_raw_pipeline


def rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


import json  # noqa: E402


class TestContinuationInheritance(unittest.TestCase):
    def setUp(self):
        zone = patch("astra.timestamps.system_timezone", return_value=ZoneInfo("Asia/Dubai"))
        zone.start()
        self.addCleanup(zone.stop)

    def test_traceback_lines_attach_to_parent_and_inherit_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "agent.log").write_text(
                "2026-09-12 12:00:00 ERROR agent.tools: Tool patch failed\n"
                '  File "m.py", line 3, in handler\n'
                "    raise ValueError('boom')\n"
            )
            enriched = root / "enriched.jsonl"
            grouped = root / "groups.jsonl"
            result = run_raw_pipeline(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(enriched), grouped_path=str(grouped),
                state_path=str(root / "state.json"), journal_reader=None,
                now="2026-09-12T12:10:00Z",
            )
            out = rows(enriched)
            self.assertEqual(len(out), 1)
            parent = out[0]
            self.assertEqual(parent["record_role"], "parent")
            self.assertEqual(parent["event_ts"], "2026-09-12T12:00:00+04:00")
            self.assertEqual(parent["timestamp_basis"], "text")
            self.assertIsNone(parent["parent_id"])
            self.assertIn("File \"m.py\"", parent["text"])
            self.assertIn("ValueError", parent["text"])
            self.assertEqual(result["processed"], 1)

    def test_leading_continuation_without_parent_is_dropped_on_initial_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "errors.log").write_text('  File "orphan.py", line 9\n')
            enriched = root / "enriched.jsonl"
            result = run_raw_pipeline(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(enriched), grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
            )
            # Initial discovery: an orphan continuation has no parent to
            # inherit from and no timestamp of its own, so it is discarded.
            self.assertEqual(rows(enriched), [])
            self.assertEqual(result["dropped_missing_timestamp"], 1)


if __name__ == "__main__":
    unittest.main()
