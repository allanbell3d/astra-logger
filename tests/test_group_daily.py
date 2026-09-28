#!/usr/bin/env python3
"""Daily/windowed grouped storage: latest-72h rollup, no blank timestamps."""
import json
import tempfile
import unittest
from pathlib import Path

from astra.group import GroupIndex


def _enriched(key, text, event_ts, basis="text", severity="watch"):
    return {
        "host": "dell", "profile": "ana", "text": text, "event_ts": event_ts,
        "timestamp_basis": basis,
        "astra.classification": {"event": "api.request_failed", "cause": "timeout",
                                 "kind": "incident", "event_ts": event_ts,
                                 "event_ts_basis": basis},
        "astra.fingerprint": {"key": key, "host": "dell", "profile": "ana",
                              "normalized_text": text, "event": "api.request_failed",
                              "cause": "timeout", "domain": "llm", "kind": "incident"},
        "astra.severity": {"label": severity, "rule_id": "default", "policy_version": "v"},
    }


class TestDailyWindows(unittest.TestCase):
    def test_groups_carry_day_window_and_blank_ts_are_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "grouped.jsonl")
            index = GroupIndex.load(path, window_hours=72, now="2026-09-12T12:00:00Z")
            index.add(_enriched("k1", "fail one", "2026-09-12T11:00:00Z"))
            index.add(_enriched("k1", "fail two", "2026-09-12T11:30:00Z"))
            index.add(_enriched("k1", "fail old", "2026-09-09T11:00:00Z"))
            # Untimestamped record: grouped storage must never emit blank ts.
            index.add(_enriched("k2", "no time", None, basis="new_append"))
            index.save(path)
            rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
            by_key = {row["fingerprint"]: row for row in rows}
            self.assertNotIn("k2", by_key)
            group = by_key["k1"]
            self.assertEqual(group["n"], 2)
            self.assertEqual(group["window_start"], "2026-09-09T12:00:00Z")
            self.assertEqual(group["window_end"], "2026-09-12T12:00:00Z")
            self.assertEqual(group["first_ts"], "2026-09-12T11:00:00Z")
            self.assertEqual(group["last_ts"], "2026-09-12T11:30:00Z")
            for field in ("first_ts", "last_ts"):
                self.assertTrue(group[field])

    def test_stale_groups_evicted_on_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "grouped.jsonl")
            index = GroupIndex.load(path, window_hours=72, now="2026-09-12T12:00:00Z")
            index.add(_enriched("k1", "fresh", "2026-09-12T11:00:00Z"))
            index.save(path)
            # Three days later: the stored group's last event is beyond 72h.
            reloaded = GroupIndex.load(path, window_hours=72, now="2026-09-15T12:00:00Z")
            self.assertEqual(reloaded.records(), [])

    def test_short_text_samples_not_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "grouped.jsonl")
            index = GroupIndex.load(path, window_hours=72, now="2026-09-12T12:00:00Z")
            index.add(_enriched("k1", "short but real", "2026-09-12T11:00:00Z"))
            index.save(path)
            rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
            self.assertEqual(rows[0]["text"], "short but real")
            self.assertFalse(rows[0]["text_truncated"])

    def test_default_window_is_72h_without_injected_now(self):
        index = GroupIndex()
        self.assertEqual(index.window_hours, 72)
        self.assertIsNone(index.now)


if __name__ == "__main__":
    unittest.main()
