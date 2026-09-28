#!/usr/bin/env python3
"""Retention: closed segments older than 72h removed; 250 MiB cap; active safe."""
import gzip
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from astra.storage import apply_retention, scan_enriched_segments


def _make_segment(root: Path, day: str, payload: str, mtime: datetime) -> str:
    path = root / f"enriched-{day}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(payload)
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return str(path)


def _make_active(root: Path, day: str, payload: str) -> str:
    path = root / f"enriched-{day}.jsonl"
    path.write_text(payload)
    return str(path)


class TestRetention(unittest.TestCase):
    def test_closed_older_than_72h_removed_recent_kept_active_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_segment(root, "20260911", "0" * 500,
                          datetime(2026, 9, 11, 6, tzinfo=timezone.utc))
            _make_segment(root, "20260912", "0" * 500,
                          datetime(2026, 9, 12, 6, tzinfo=timezone.utc))
            _make_active(root, "20260915", "0" * 100)
            result = apply_retention(str(root), now="2026-09-15T12:00:00Z")
            archive = root.parent / "archive" / "enriched" / "enriched-20260911.jsonl.gz"
            self.assertFalse((root / "enriched-20260911.jsonl.gz").exists())
            self.assertTrue(archive.exists())
            with gzip.open(archive, "rt", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "0" * 500)
            # 20260912 closes at 09-13T00:00Z > horizon 09-12T12:00Z: still within 72h retention.
            self.assertTrue((root / "enriched-20260912.jsonl.gz").exists())
            self.assertTrue((root / "enriched-20260915.jsonl").exists())
            self.assertEqual(result["removed_old"], 0)
            self.assertEqual(result["archived_old"], 1)

    def test_secondary_cap_removes_oldest_first_and_never_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_segment(root, "20260912", "0" * 2000,
                          datetime(2026, 9, 12, 6, tzinfo=timezone.utc))
            _make_segment(root, "20260913", "0" * 1000,
                          datetime(2026, 9, 13, 6, tzinfo=timezone.utc))
            _make_active(root, "20260915", "0" * 100000)
            result = apply_retention(
                str(root), now="2026-09-15T12:00:00Z", max_total_bytes=1500,
            )
            remaining = [s.day for s in scan_enriched_segments(str(root))]
            self.assertIn("20260915", remaining)  # active survives regardless of cap
            self.assertNotIn("20260912", remaining)  # oldest closed is archived first
            self.assertIn("20260913", remaining)  # newest closed still under cap
            self.assertEqual(result["removed_cap"], 0)
            self.assertEqual(result["archived_cap"], 1)
            self.assertTrue((root.parent / "archive" / "enriched" / "enriched-20260912.jsonl.gz").exists())

    def test_nothing_removed_when_all_within_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_segment(root, "20260913", "0" * 500,
                          datetime(2026, 9, 13, 6, tzinfo=timezone.utc))
            _make_active(root, "20260915", "0" * 100)
            result = apply_retention(str(root), now="2026-09-15T12:00:00Z")
            self.assertEqual(result, {
                "removed_old": 0,
                "removed_cap": 0,
                "archived_old": 0,
                "archived_boundary": 0,
                "archived_cap": 0,
            })
            self.assertEqual(len(scan_enriched_segments(str(root))), 2)


if __name__ == "__main__":
    unittest.main()
