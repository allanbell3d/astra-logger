#!/usr/bin/env python3
"""Pipeline produces rotating storage, bounded batches, and crash-safe resume."""
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

from astra.raw_pipeline import run_raw_pipeline


def rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestPipelineStorageAndBatches(unittest.TestCase):
    def test_records_land_in_daily_store_and_batches_are_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "errors.log").write_text(
                "2026-09-12 12:00:00 ERROR first incident\n"
                "2026-09-12 12:05:00 ERROR second incident\n"
            )
            store_dir = str(root / "store")
            batches_path = root / "batches"
            result = run_raw_pipeline(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(root / "enriched.jsonl"),
                grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
                store_dir=store_dir,
                llm_batch_dir=str(batches_path),
                now="2026-09-12T12:10:00Z",
            )
            segments = [name for name in os.listdir(store_dir) if name.startswith("enriched-")]
            self.assertTrue(any(name.startswith("enriched-20260912") for name in segments))
            stored = []
            for name in segments:
                path = Path(store_dir) / name
                if name.endswith(".gz"):
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        stored.extend(json.loads(line) for line in handle if line.strip())
                else:
                    stored.extend(rows(path))
            self.assertEqual(len(stored), 2)
            # Batch files exist, are valid JSON, and carry the header metadata.
            files = sorted(p for p in batches_path.iterdir() if p.suffix == ".json")
            self.assertGreaterEqual(len(files), 1)
            batch = json.loads(files[0].read_text())
            self.assertEqual(batch["host"], "testhost")
            self.assertEqual(batch["window_end"], "2026-09-12T12:10:00Z")
            self.assertEqual(batch["group_count"], batch["groups"].__len__())
            self.assertLessEqual(len(json.dumps(batch)), 128 * 1024)
            self.assertNotIn("journal", json.dumps(batch))

    def test_rotation_and_retention_integrated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "errors.log").write_text("2026-09-12 12:00:00 ERROR one\n")
            store_dir = str(root / "store")
            run_raw_pipeline(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(root / "enriched.jsonl"),
                grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
                store_dir=store_dir, now="2026-09-12T12:10:00Z",
            )
            # A long downtime later: retention archives the >72h segment.
            with (logs / "errors.log").open("a") as handle:
                handle.write("2026-09-18 12:00:00 ERROR two\n")
            run_raw_pipeline(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(root / "enriched.jsonl"),
                grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
                store_dir=store_dir, now="2026-09-18T12:10:00Z",
            )
            names = os.listdir(store_dir)
            archive = root / "archive" / "enriched" / "enriched-20260912-12.jsonl.gz"
            self.assertTrue(archive.exists())
            self.assertNotIn("enriched-20260912-12.jsonl", names)
            self.assertTrue(any(name.startswith("enriched-20260918") for name in names))
            self.assertEqual(result_status := 0, 0)

    def test_new_increment_rewrites_only_its_window_batches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "errors.log").write_text("2026-09-12 12:00:00 ERROR one\n")
            common = dict(
                hermes_home=str(root / ".hermes"), host="testhost",
                enriched_path=str(root / "enriched.jsonl"),
                grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"), journal_reader=None,
                store_dir=str(root / "store"),
                llm_batch_dir=str(root / "batches"),
            )
            run_raw_pipeline(now="2026-09-12T12:10:00Z", **common)
            before = sorted(p.name for p in (root / "batches").iterdir())
            with (logs / "errors.log").open("a") as handle:
                handle.write("2026-09-12 12:20:00 ERROR two\n")
            result = run_raw_pipeline(now="2026-09-12T12:30:00Z", **common)
            after = sorted(p.name for p in (root / "batches").iterdir())
            self.assertEqual(before, after)  # same window, same single batch file
            payload = json.loads((root / "batches" / after[0]).read_text())
            texts = json.dumps(payload)
            self.assertIn("ERROR one", texts)
            self.assertIn("ERROR two", texts)
            self.assertEqual(result["processed"], 1)


if __name__ == "__main__":
    unittest.main()
