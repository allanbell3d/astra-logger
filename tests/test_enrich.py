#!/usr/bin/env python3
"""Lossless namespaced enrichment."""
import unittest

from astra.classify import classify_event
from astra.enrich import enrich_record
from astra.severity import SeverityPolicy


class TestEnrichRecord(unittest.TestCase):
    def test_retains_every_original_key_and_namespaces_additions(self):
        raw = {
            "ts": "2026-09-08T19:45:12+00:00",
            "host": "dell",
            "profile": "ana",
            "file": "errors.log",
            "sig": "ratelimit",
            "sev": "watch",
            "event": "do-not-clobber",
            "custom": {"nested": [1, 2]},
            "text": "2026-09-08 19:45:12 ERROR [20260908_194500_abcd] agent.conversation_loop: The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna",
        }
        classification = classify_event(raw)
        enriched = enrich_record(
            raw,
            classification=classification,
            policy=SeverityPolicy.default(),
            source_path="/tmp/events-dell.jsonl",
            byte_start=0,
            byte_end=10,
        )
        for key, value in raw.items():
            self.assertEqual(enriched[key], value)
        self.assertEqual(enriched["event"], "do-not-clobber")
        self.assertEqual(enriched["astra.classification"]["event"], "api.quota_exhausted")
        self.assertEqual(enriched["astra.classification"]["cause"], "usage_quota_exhausted")
        self.assertEqual(enriched["astra.severity"]["label"], "needs-attention")
        self.assertEqual(enriched["astra.fingerprint"]["host"], "dell")
        self.assertEqual(enriched["astra.fingerprint"]["profile"], "ana")
        self.assertTrue(enriched["astra.fingerprint"]["key"])
        self.assertEqual(enriched["astra.provenance"]["source_path"], "/tmp/events-dell.jsonl")
        self.assertEqual(enriched["astra.provenance"]["byte_start"], 0)
        self.assertEqual(enriched["astra.provenance"]["byte_end"], 10)
        self.assertEqual(enriched["astra.provenance"]["rule_version"], classification["rule_version"])
        self.assertNotIn("label", enriched["astra.classification"])
    def test_classification_uses_the_pipeline_canonical_event_timestamp(self):
        raw = {
            "host": "dell",
            "profile": "ana",
            "text": "2026-09-08 19:45:12 ERROR gateway: failed",
            "ts": "2026-09-08T19:45:12",
            "event_ts": "2026-09-08T19:45:12+04:00",
        }
        enriched = enrich_record(
            raw,
            classification=classify_event(raw),
            policy=SeverityPolicy.default(),
            source_path="/tmp/errors.log",
            byte_start=0,
            byte_end=10,
        )
        self.assertEqual(enriched["astra.classification"]["event_ts"], raw["event_ts"])


if __name__ == "__main__":
    unittest.main()
