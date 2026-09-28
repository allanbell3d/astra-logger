#!/usr/bin/env python3
"""Host-local 12-hour enriched evidence buckets and durable retention."""
import gzip
import json
import os
import tempfile
import unittest
from datetime import timezone
from unittest.mock import patch
from pathlib import Path
from zoneinfo import ZoneInfo

from astra.storage import EnrichedStore, apply_retention, scan_enriched_segments


def _row(seed: str) -> dict:
    return {"text": f"2026-09-12 12:00:00 ERROR seeded evidence {seed}", "seq": seed}


class TestEnrichedStore(unittest.TestCase):
    def setUp(self):
        zone = patch("astra.storage.system_timezone", return_value=timezone.utc)
        zone.start()
        self.addCleanup(zone.stop)

    def test_bucket_can_exceed_retired_ten_mib_limit_without_splitting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            store = EnrichedStore(str(root), now="2026-09-12T12:00:00Z")
            store.append({"event_ts": "2026-09-12T12:00:00Z", "text": "x" * (11 * 1024 * 1024)})
            bucket = root / "enriched-20260912-12.jsonl"
            self.assertTrue(bucket.exists())
            self.assertGreater(bucket.stat().st_size, 10 * 1024 * 1024)

    def test_uses_separate_twelve_hour_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            store = EnrichedStore(str(root), now="2026-09-12T23:59:59Z")
            store.append({"event_ts": "2026-09-12T23:59:59Z", "seq": "late"})
            store.append({"event_ts": "2026-09-13T00:00:01Z", "seq": "next-day"})
            segments = scan_enriched_segments(str(root), now="2026-09-13T00:00:01Z")
            self.assertEqual([Path(segment.path).name for segment in segments], [
                "enriched-20260912-12.jsonl", "enriched-20260913-00.jsonl",
            ])
            self.assertFalse(segments[0].active)
            self.assertTrue(segments[1].active)

    def test_uses_host_local_twelve_hour_bucket_at_utc_day_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            store = EnrichedStore(
                str(root), now="2026-09-12T20:00:00Z", timezone_=ZoneInfo("Asia/Dubai")
            )
            store.append({"event_ts": "2026-09-12T20:00:00Z", "seq": "dubai-midnight"})
            self.assertTrue((root / "enriched-20260913-00.jsonl").exists())

    def test_complete_record_append_semantics(self):
        """A record is fully durable in the active segment or not written."""
        with tempfile.TemporaryDirectory() as td:
            store = EnrichedStore(str(Path(td) / "store"), now="2026-09-12T12:00:00Z")
            row = _row("atomic")
            store.append(row)
            # Every persisted line parses as one complete JSON record.
            with open(store.active_path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            parsed = [json.loads(line) for line in lines if line.strip()]
            self.assertEqual(parsed, [row])

    def test_prior_bucket_remains_plain_and_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            store = EnrichedStore(str(root), now="2026-09-12T12:00:00Z")
            store.append({"event_ts": "2026-09-12T12:00:00Z", "seq": "day1"})
            store.append({"event_ts": "2026-09-13T00:00:01Z", "seq": "next-day"})
            closed = root / "enriched-20260912-12.jsonl"
            self.assertTrue(closed.exists())
            self.assertFalse((Path(str(closed) + ".gz")).exists())
            self.assertEqual(json.loads(closed.read_text()), {"event_ts": "2026-09-12T12:00:00Z", "seq": "day1"})

    def test_append_order_preserved_across_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            store = EnrichedStore(str(root), now="2026-09-12T12:00:00Z")
            store.append_many([
                {"event_ts": "2026-09-12T01:00:00Z", "seq": "a0"},
                {"event_ts": "2026-09-12T01:01:00Z", "seq": "a1"},
                {"event_ts": "2026-09-13T13:00:00Z", "seq": "b0"},
            ])
            seen = []
            for segment in scan_enriched_segments(str(root), now="2026-09-13T13:00:00Z"):
                with open(segment.path, encoding="utf-8") as handle:
                    seen.extend(json.loads(line)["seq"] for line in handle if line.strip())
            self.assertEqual(seen, ["a0", "a1", "b0"])

    def test_event_time_buckets_sort_records_and_use_twelve_hour_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            store = EnrichedStore(str(root), now="2026-09-13T18:00:00Z")
            store.append_many([
                {"seq": "late", "event_ts": "2026-09-13T11:59:59Z"},
                {"seq": "early", "event_ts": "2026-09-13T00:00:01Z"},
                {"seq": "afternoon", "event_ts": "2026-09-13T12:00:00Z"},
            ])

            morning = root / "enriched-20260913-00.jsonl"
            afternoon = root / "enriched-20260913-12.jsonl"
            self.assertTrue(morning.exists())
            self.assertTrue(afternoon.exists())
            self.assertEqual(
                [json.loads(line)["seq"] for line in morning.read_text().splitlines()],
                ["early", "late"],
            )
            self.assertEqual(
                [json.loads(line)["seq"] for line in afternoon.read_text().splitlines()],
                ["afternoon"],
            )

    def test_new_instance_reads_prior_bucket_without_recompressing(self):
        with tempfile.TemporaryDirectory() as td:
            root = str(Path(td) / "store")
            first = EnrichedStore(root, now="2026-09-12T12:00:00Z")
            first.append({"event_ts": "2026-09-12T12:00:00Z", "seq": "leftover"})
            later = EnrichedStore(root, now="2026-09-18T12:00:00Z")
            later.append({"event_ts": "2026-09-18T12:00:00Z", "seq": "today"})
            segments = scan_enriched_segments(root, now="2026-09-18T12:00:00Z")
            names = {Path(segment.path).name for segment in segments}
            self.assertIn("enriched-20260912-12.jsonl", names)
            self.assertIn("enriched-20260918-12.jsonl", names)
            self.assertFalse(any(segment.path.endswith(".gz") for segment in segments))

    def test_retention_prunes_only_expired_rows_in_boundary_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "enriched-20260910-12.jsonl"
            path.write_text(
                '{"event_ts":"2026-09-10T12:01:00Z","seq":"expired"}\n'
                '{"event_ts":"2026-09-10T16:51:00Z","seq":"fresh"}\n'
            )
            apply_retention(str(root), now="2026-09-13T16:50:00Z")
            kept = [json.loads(line)["seq"] for line in path.read_text().splitlines()]
            self.assertEqual(kept, ["fresh"])

    def test_active_path_reports_day(self):
        with tempfile.TemporaryDirectory() as td:
            store = EnrichedStore(str(Path(td) / "store"), now="2026-09-12T12:00:00Z")
            self.assertIn("20260912", store.active_path)


if __name__ == "__main__":
    unittest.main()
