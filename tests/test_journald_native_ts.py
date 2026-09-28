#!/usr/bin/env python3
"""Journald records: native __REALTIME_TIMESTAMP is authoritative."""
import json
import tempfile
import unittest
from pathlib import Path

from astra.journald_source import JournaldEntry
from astra.raw_pipeline import run_raw_pipeline
from astra.timestamps import as_utc


def rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# 2026-09-12T12:00:00Z in journald microsecond realtime units.
RECENT_USEC = "1789214400000000"
STALE_USEC = "1782993600000000"  # 2026-07-02T12:00:00Z


class TestJournaldNativeTimestamp(unittest.TestCase):
    def _run(self, root, realtime_usec, now):
        def journal_reader(after_cursor, limit, since=None):
            if after_cursor:
                return []
            fields = {
                "MESSAGE": "2020-01-01 00:00:00 ERROR message timestamp must not win",
                "PRIORITY": "3",
                "__REALTIME_TIMESTAMP": realtime_usec,
                "__CURSOR": "jc-native",
                "_SYSTEMD_UNIT": "hermes.service",
            }
            return [JournaldEntry(fields=fields, cursor="jc-native")]

        return run_raw_pipeline(
            hermes_home=str(root / ".hermes"), host="testhost",
            enriched_path=str(root / "enriched.jsonl"),
            grouped_path=str(root / "groups.jsonl"),
            state_path=str(root / "state.json"), journal_reader=journal_reader,
            now=now,
        )

    def test_native_timestamp_beats_message_text_and_fresh_entry_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".hermes" / "logs").mkdir(parents=True)
            result = self._run(root, RECENT_USEC, "2026-09-12T12:05:00Z")
            out = rows(root / "enriched.jsonl")
            self.assertEqual(len(out), 1)
            journal = out[0]
            self.assertEqual(journal["source_type"], "journald")
            self.assertEqual(as_utc(journal["event_ts"]), as_utc("2026-09-12T12:00:00Z"))
            self.assertEqual(journal["timestamp_basis"], "journald")
            self.assertEqual(result["dropped_old"], 0)

    def test_stale_journald_entry_is_dropped_old(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".hermes" / "logs").mkdir(parents=True)
            result = self._run(root, STALE_USEC, "2026-09-12T12:05:00Z")
            self.assertEqual(rows(root / "enriched.jsonl"), [])
            self.assertEqual(result["dropped_old"], 1)


if __name__ == "__main__":
    unittest.main()
