#!/usr/bin/env python3
"""Tests for CompressedStream chronological event stream."""
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest

from astra.compressed import CompressedStream, _extract_compressed_record
from astra.raw_pipeline import run_raw_pipeline


def _sample_enriched(
    ts="2026-09-14T05:00:00Z",
    host="dell",
    profile="dell-researcher",
    event="platform.disconnected",
    cause="websocket_stale",
    severity="needs-attention",
    rule_id="severe-platform-runtime",
    source_path="/home/allanbell3d/.hermes/profiles/dell-researcher/logs/errors.log",
    byte_start=1000,
    byte_end=1500,
    text="Fatal discord adapter error: ack_stale",
    role="parent",
):
    return {
        "host": host,
        "profile": profile,
        "ts": ts,
        "event_ts": ts,
        "record_role": role,
        "text": text,
        "astra.classification": {
            "domain": "platform",
            "kind": "incident",
            "event": event,
            "cause": cause,
            "event_ts": ts,
            "platform": "discord",
        },
        "astra.severity": {
            "label": severity,
            "rule_id": rule_id,
            "policy_version": "v4.1",
        },
        "astra.provenance": {
            "source_path": source_path,
            "byte_start": byte_start,
            "byte_end": byte_end,
        },
    }


class TestCompressedStream(unittest.TestCase):
    def test_noise_filter_skips_ignore_and_continuation(self):
        ignore_row = _sample_enriched(event="tool.check_fn_false", severity="ignore")
        self.assertIsNone(_extract_compressed_record(ignore_row))

        continuation_row = _sample_enriched(role="continuation")
        self.assertIsNone(_extract_compressed_record(continuation_row))

        unknown_ts_row = _sample_enriched(ts="UNKNOWN")
        unknown_ts_row["event_ts"] = "UNKNOWN"
        self.assertIsNone(_extract_compressed_record(unknown_ts_row))

    def test_retains_labeled_fields_and_omits_nulls(self):
        row = _sample_enriched()
        rec = _extract_compressed_record(row)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["ts"], "2026-09-14T05:00:00Z")
        self.assertEqual(rec["host"], "dell")
        self.assertEqual(rec["profile"], "dell-researcher")
        self.assertEqual(rec["event"], "platform.disconnected")
        self.assertEqual(rec["cause"], "websocket_stale")
        self.assertEqual(rec["severity"], "needs-attention")
        self.assertEqual(rec["severity_rule_id"], "severe-platform-runtime")
        self.assertEqual(rec["source_path"], "/home/allanbell3d/.hermes/profiles/dell-researcher/logs/errors.log")
        self.assertEqual(rec["byte_start"], 1000)
        self.assertEqual(rec["byte_end"], 1500)
        self.assertIn("ack_stale", rec["text"])
        for k, v in rec.items():
            self.assertIsNotNone(v)
            self.assertNotEqual(v, "")

    def test_append_many_and_chronological_sorting(self):
        with tempfile.TemporaryDirectory() as td:
            stream_path = os.path.join(td, "compressed-events.jsonl")
            stream = CompressedStream(stream_path)

            row1 = _sample_enriched(ts="2026-09-14T05:10:00Z")
            row2 = _sample_enriched(ts="2026-09-14T05:05:00Z")
            ignore = _sample_enriched(severity="ignore")

            count = stream.append_many([row1, ignore, row2])
            self.assertEqual(count, 2)

            with open(stream_path, encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["ts"], "2026-09-14T05:05:00Z")
            self.assertEqual(lines[1]["ts"], "2026-09-14T05:10:00Z")

    def test_read_tail_window(self):
        with tempfile.TemporaryDirectory() as td:
            stream_path = os.path.join(td, "compressed-events.jsonl")
            stream = CompressedStream(stream_path)

            now = "2026-09-14T06:00:00Z"
            r_recent = _sample_enriched(ts="2026-09-14T05:40:00Z")
            r_older = _sample_enriched(ts="2026-09-14T04:40:00Z")

            stream.append_many([r_older, r_recent])

            tail_60 = CompressedStream.read_tail(stream_path, window_minutes=60, now=now)
            self.assertEqual(len(tail_60), 1)
            self.assertEqual(tail_60[0]["ts"], "2026-09-14T05:40:00Z")

            tail_120 = CompressedStream.read_tail(stream_path, window_minutes=120, now=now)
            self.assertEqual(len(tail_120), 2)

    def test_read_tail_since_ts(self):
        with tempfile.TemporaryDirectory() as td:
            stream_path = os.path.join(td, "compressed-events.jsonl")
            stream = CompressedStream(stream_path)

            r1 = _sample_enriched(ts="2026-09-14T01:00:00Z")
            r2 = _sample_enriched(ts="2026-09-14T02:00:00Z")
            r3 = _sample_enriched(ts="2026-09-14T03:00:00Z")
            stream.append_many([r1, r2, r3])

            tail = CompressedStream.read_tail(stream_path, window_minutes=None, since_ts="2026-09-14T01:30:00Z")
            self.assertEqual(len(tail), 2)
            self.assertEqual(tail[0]["ts"], "2026-09-14T02:00:00Z")
            self.assertEqual(tail[1]["ts"], "2026-09-14T03:00:00Z")

    def test_prune_retention_archives_rows_and_preserves_source_offsets(self):
        with tempfile.TemporaryDirectory() as td:
            stream_path = os.path.join(td, "compressed-events.jsonl")
            stream = CompressedStream(stream_path, retention_hours=72)

            now = "2026-09-14T12:00:00Z"
            fresh = _sample_enriched(ts="2026-09-14T10:00:00Z", byte_start=2000, byte_end=2500)
            stale = _sample_enriched(ts="2026-09-10T10:00:00Z", byte_start=1000, byte_end=1500)
            stream.append_many([stale, fresh])

            rotated = stream.prune_retention(now=now)
            self.assertEqual(rotated, 1)

            with open(stream_path, encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["ts"], "2026-09-14T10:00:00Z")
            self.assertEqual(lines[0]["byte_start"], 2000)

            archives = sorted((Path(td) / "archive" / "compressed").glob("*.jsonl.gz"))
            self.assertEqual(len(archives), 1)
            with gzip.open(archives[0], "rt", encoding="utf-8") as handle:
                archived = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(archived[0]["ts"], "2026-09-10T10:00:00Z")
            self.assertEqual(archived[0]["byte_start"], 1000)

    def test_raw_pipeline_writes_compressed_stream(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = root / ".hermes" / "logs"
            logs.mkdir(parents=True)
            (logs / "errors.log").write_text(
                "2026-09-12 12:00:00 ERROR discord gateway connection lost ack_stale\n"
            )
            store_dir = str(root / "store")
            compressed_file = str(root / "compressed" / "compressed-events.jsonl")

            result = run_raw_pipeline(
                hermes_home=str(root / ".hermes"),
                host="testhost",
                enriched_path=str(root / "enriched.jsonl"),
                grouped_path=str(root / "groups.jsonl"),
                state_path=str(root / "state.json"),
                journal_reader=None,
                store_dir=store_dir,
                compressed_path=compressed_file,
                now="2026-09-12T12:10:00Z",
            )
            self.assertEqual(result["processed"], 1)
            self.assertTrue(os.path.isfile(compressed_file))
            with open(compressed_file, encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["host"], "testhost")
            self.assertIn("ack_stale", events[0]["text"])


if __name__ == "__main__":
    unittest.main()
