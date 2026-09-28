#!/usr/bin/env python3
import json
import os
from pathlib import Path
import tempfile
import unittest

from astra.journald_source import JournaldEntry
from astra.raw_pipeline import run_raw_pipeline


def rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestRawPipeline(unittest.TestCase):
    def test_direct_sources_enrich_compact_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hermes = root / ".hermes"
            logs = hermes / "logs"
            logs.mkdir(parents=True)
            duplicate = "2026-09-12 12:00:00 WARNING repeated warning"
            long_line = "2026-09-12 12:01:00 ERROR " + ("x" * 1000)
            (logs / "agent.log").write_text(
                "2026-09-12 11:59:00 INFO routine chatter\n"
                + duplicate + "\n" + duplicate + "\n" + long_line + "\n"
            )
            (logs / "errors.log").write_text(
                "2026-09-12 12:10:00 plain diagnostic with stamp\n\n2026-09-12 12:11:00 plain diagnostic two\n"
            )
            enriched = root / "enriched.jsonl"
            grouped = root / "groups.jsonl"
            state = root / "state.json"
            journal_calls = []

            def journal_reader(after_cursor, limit, since=None):
                journal_calls.append((after_cursor, since))
                if after_cursor:
                    return []
                fields = {
                    "MESSAGE": "k" * 1200, "PRIORITY": "4",
                    "__REALTIME_TIMESTAMP": "1789230000000000", "__CURSOR": "jc1",
                    "_SYSTEMD_UNIT": "kernel.service", "SECRET_TOKEN": "must-drop",
                }
                return [JournaldEntry(fields=fields, cursor="jc1")]

            result = run_raw_pipeline(
                hermes_home=str(hermes), host="testhost",
                enriched_path=str(enriched), grouped_path=str(grouped),
                state_path=str(state), journal_reader=journal_reader,
                now="2026-09-12T16:20:00Z",
            )
            out = rows(enriched)
            self.assertEqual(result["processed"], 6)
            self.assertEqual(len(out), 6)
            self.assertFalse(any("routine chatter" in row.get("text", "") for row in out))
            repeated_rows = [row for row in out if row.get("text") == duplicate]
            self.assertEqual(len(repeated_rows), 2)
            repeated = repeated_rows[0]
            self.assertNotIn("occurrence_count", repeated)
            self.assertNotIn("duplicate_compaction", repeated)
            provenance = repeated["astra.provenance"]
            self.assertTrue(provenance["source_path"].endswith("agent.log"))
            self.assertGreater(provenance["byte_end"], provenance["byte_start"])
            long = next(row for row in out if row.get("original_text_bytes", 0) > 800)
            self.assertTrue(long["text_truncated"])
            self.assertLessEqual(len(long["text"]), 800)
            journal = next(row for row in out if row.get("source_type") == "journald")
            self.assertNotIn("SECRET_TOKEN", journal["journal"])
            self.assertEqual(journal["journal"]["_SYSTEMD_UNIT"], "kernel.service")
            self.assertEqual(len(journal["journal"]["MESSAGE"]), 800)
            self.assertTrue(journal["text_truncated"])
            groups = rows(grouped)
            self.assertTrue(groups)
            self.assertEqual(sum(row["n"] for row in groups), 6)
            self.assertTrue(all(len(json.dumps(row)) < 10000 for row in out))

            before = (enriched.stat().st_size, grouped.stat().st_size)
            result2 = run_raw_pipeline(
                hermes_home=str(hermes), host="testhost",
                enriched_path=str(enriched), grouped_path=str(grouped),
                state_path=str(state), journal_reader=journal_reader,
                now="2026-09-12T16:20:00Z",
            )
            self.assertEqual(result2["processed"], 0)
            self.assertEqual(before, (enriched.stat().st_size, grouped.stat().st_size))
            self.assertEqual(journal_calls[-1][0], "jc1")

            with (logs / "agent.log").open("a") as handle:
                handle.write("2026-09-12 12:02:00 ERROR new failure\n")
            result3 = run_raw_pipeline(
                hermes_home=str(hermes), host="testhost",
                enriched_path=str(enriched), grouped_path=str(grouped),
                state_path=str(state), journal_reader=journal_reader,
                now="2026-09-12T16:25:00Z",
            )
            self.assertEqual(result3["processed"], 1)
            self.assertEqual(len(rows(enriched)), 7)


if __name__ == "__main__":
    unittest.main()
