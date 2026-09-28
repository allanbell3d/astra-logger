#!/usr/bin/env python3
"""72h freshness gating, drop counters, and incremental new_append basis."""
import json
import tempfile
import unittest
from pathlib import Path

from astra.raw_pipeline import run_raw_pipeline
from astra.timestamps import as_utc


def rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestFreshnessGating(unittest.TestCase):
    def test_initial_run_discards_stale_and_untimestamped_with_counters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "agent.log").write_text(
                "2026-09-05 12:00:00 ERROR stale error beyond 72h\n"
                "totally untimestamped standalone line\n"
                "2026-13-45 99:99:99 ERROR malformed timestamp line\n"
                "2026-09-12 12:00:00 ERROR fresh error\n"
            )
            enriched = root / "enriched.jsonl"
            result = run_raw_pipeline(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(enriched), grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
                now="2026-09-12T12:00:00Z",
            )
            out = rows(enriched)
            self.assertEqual(len(out), 1)
            self.assertIn("fresh error", out[0]["text"])
            self.assertEqual(out[0]["timestamp_basis"], "text")
            self.assertEqual(result["dropped_old"], 1)
            self.assertEqual(result["dropped_missing_timestamp"], 1)
            self.assertEqual(result["dropped_malformed_timestamp"], 1)
            # Drop counters carry no dropped text; only counts cross the boundary.
            blob = json.dumps(result)
            self.assertNotIn("stale error", blob)
            self.assertNotIn("untimestamped", blob)
            self.assertNotIn("malformed timestamp line", blob)

    def test_incremental_new_append_uses_observed_time(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "errors.log").write_text("2026-09-12 12:00:00 first known error\n")
            base = dict(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(root / "enriched.jsonl"),
                grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
            )
            run_raw_pipeline(now="2026-09-12T12:30:00Z", **base)
            with (logs / "errors.log").open("a") as handle:
                handle.write("untimestamped just-appended line\n")
            result = run_raw_pipeline(now="2026-09-12T12:40:00Z", **base)
            self.assertEqual(result["processed"], 1)
            out = rows(root / "enriched.jsonl")
            appended = out[-1]
            self.assertIn("just-appended", appended["text"])
            self.assertEqual(as_utc(appended["event_ts"]), as_utc("2026-09-12T12:40:00Z"))
            self.assertEqual(appended["timestamp_basis"], "new_append")
            # Explicit timestamps older than 72h are still dropped when appended.
            with (logs / "errors.log").open("a") as handle:
                handle.write("2026-08-01 00:00:00 ancient explicit error\n")
            result2 = run_raw_pipeline(now="2026-09-12T12:45:00Z", **base)
            self.assertEqual(result2["processed"], 0)
            self.assertEqual(result2["dropped_old"], 1)


if __name__ == "__main__":
    unittest.main()
