#!/usr/bin/env python3
"""Deterministic LLM-ready batches: severity/recency/count order, hard caps."""
import json
import unittest

from astra.llm_batch import MAX_BATCH_BYTES, MAX_BATCH_GROUPS, build_llm_batches


def _group(key, severity="watch", last_ts="2026-09-12T11:00:00Z", n=1, text="fail"):
    return {
        "fingerprint": key, "n": n, "host": "dell", "profile": "ana",
        "event": "api.request_failed", "cause": "timeout", "domain": "llm",
        "kind": "incident", "severity": severity, "text": text,
        "normalized_text": text, "first_ts": last_ts, "last_ts": last_ts,
        "source_files": ["/logs/agent.log"],
    }


class TestBuildLlmBatches(unittest.TestCase):
    def test_orders_by_severity_then_recency_then_count(self):
        groups = [
            _group("watch-old", "watch", "2026-09-12T10:00:00Z", 1),
            _group("crit-new", "needs-attention", "2026-09-12T11:30:00Z", 1),
            _group("watch-new", "watch", "2026-09-12T11:00:00Z", 1),
            _group("ignore", "ignore", "2026-09-12T11:00:00Z", 9),
            _group("watch-newer-count", "watch", "2026-09-12T11:00:00Z", 5),
        ]
        batches = build_llm_batches(
            groups, host="dell",
            window=("2026-09-09T12:00:00Z", "2026-09-12T12:00:00Z"),
            now="2026-09-12T12:00:00Z",
        )
        self.assertEqual(len(batches), 1)
        order = [group["fingerprint"] for group in batches[0]["groups"]]
        self.assertEqual(
            order,
            ["crit-new", "watch-newer-count", "watch-new", "watch-old", "ignore"],
        )

    def test_caps_groups_and_bytes_with_numbering_and_header(self):
        groups = [
            _group(f"k{i}", "watch", "2026-09-12T11:00:00Z", n=1,
                   text="x" * 9000)
            for i in range(250)
        ]
        batches = build_llm_batches(
            groups, host="dell",
            window=("2026-09-09T12:00:00Z", "2026-09-12T12:00:00Z"),
            now="2026-09-12T12:00:00Z",
        )
        self.assertGreater(len(batches), 2)
        self.assertEqual(sum(batch["group_count"] for batch in batches), 250)
        for index, batch in enumerate(batches):
            self.assertEqual(batch["batch_index"], index + 1)
            self.assertEqual(batch["batch_count"], len(batches))
            self.assertLessEqual(len(json.dumps(batch, ensure_ascii=False)), MAX_BATCH_BYTES)
            self.assertLessEqual(len(batch["groups"]), MAX_BATCH_GROUPS)
            blob = json.dumps(batch)
            self.assertIn("dell", blob)
            self.assertIn("2026-09-09T12:00:00Z", blob)
            self.assertIn("occurrence_count", blob)

    def test_severity_totals_and_determinism(self):
        groups = [
            _group("a", "needs-attention", "2026-09-12T11:00:00Z", 2),
            _group("b", "watch", "2026-09-12T10:00:00Z", 3),
            _group("c", "ignore", "2026-09-12T09:00:00Z", 4),
        ]
        kwargs = dict(
            host="dell", window=("2026-09-09T12:00:00Z", "2026-09-12T12:00:00Z"),
            now="2026-09-12T12:00:00Z",
        )
        first = build_llm_batches(groups, **kwargs)
        second = build_llm_batches(list(reversed(groups)), **kwargs)
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )
        batch = first[0]
        self.assertEqual(batch["group_count"], 3)
        self.assertEqual(batch["occurrence_count"], 9)
        self.assertEqual(
            batch["severity_totals"],
            {"needs-attention": 1, "watch": 1, "ignore": 1},
        )

    def test_batches_carry_no_raw_or_enriched_corpus(self):
        fat = _group("fat", "watch", "2026-09-12T11:00:00Z", 1)
        fat["journal"] = {"MESSAGE": "y" * 5000}
        fat["astra.provenance"] = {"byte_start": 0, "byte_end": 9999}
        fat["text"] = "z" * 20000
        batches = build_llm_batches(
            [fat], host="dell",
            window=("2026-09-09T12:00:00Z", "2026-09-12T12:00:00Z"),
            now="2026-09-12T12:00:00Z",
        )
        blob = json.dumps(batches)
        self.assertNotIn("journal", blob)
        self.assertNotIn("astra.provenance", blob)
        self.assertNotIn("z" * 5000, blob)
        self.assertLessEqual(len(json.dumps(batches)), MAX_BATCH_BYTES)

    def test_empty_input_yields_no_batches(self):
        self.assertEqual(build_llm_batches([], host="dell", window=None), [])


if __name__ == "__main__":
    unittest.main()
