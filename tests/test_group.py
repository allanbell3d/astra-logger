#!/usr/bin/env python3
"""Compact grouped JSONL for later reporting."""
import json
import os
import tempfile
import unittest

from astra.group import GroupIndex, grouped_record


def _enriched(host, profile, text, key, label="watch", event="api.request_failed"):
    return {
        "host": host,
        "profile": profile,
        "text": text,
        "ts": "2026-09-11T10:00:00+00:00",
        "astra.classification": {"event": event, "cause": "timeout", "kind": "incident"},
        "astra.fingerprint": {
            "key": key,
            "host": host,
            "profile": profile,
            "normalized_text": text,
            "event": event,
            "cause": "timeout",
            "domain": "llm",
            "kind": "incident",
        },
        "astra.severity": {"label": label, "rule_id": "default", "policy_version": "v4.1-table"},
    }


class TestGroupIndex(unittest.TestCase):
    def test_merges_same_fingerprint_and_keeps_host_profile_apart(self):
        index = GroupIndex()
        index.add(_enriched("dell", "ana", "fail after 1s", "k-dell"))
        index.add(_enriched("dell", "ana", "fail after 9s", "k-dell"))
        index.add(_enriched("RpiClaude", "ana", "fail after 1s", "k-pi"))
        groups = {row["fingerprint"]: row for row in index.records()}
        self.assertEqual(groups["k-dell"]["n"], 2)
        self.assertEqual(groups["k-pi"]["n"], 1)
        self.assertEqual(groups["k-dell"]["host"], "dell")
        self.assertEqual(groups["k-pi"]["host"], "RpiClaude")
        self.assertEqual(groups["k-dell"]["profile"], "ana")
        self.assertEqual(sum(row["n"] for row in groups.values()), 3)

    def test_persists_and_reloads_compact_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "grouped.jsonl")
            index = GroupIndex()
            index.add(_enriched("dell", "ana", "fail", "k1", label="needs-attention"))
            index.save(path)
            reloaded = GroupIndex.load(path)
            reloaded.add(_enriched("dell", "ana", "fail again", "k1", label="needs-attention"))
            reloaded.save(path)
            rows = []
            with open(path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["n"], 2)
            self.assertIn("fingerprint", rows[0])
            # Short text samples are retained, not omitted.
            self.assertEqual(rows[0]["text"], "fail")

    def test_grouped_record_is_compact(self):
        row = grouped_record(_enriched("dell", "rumi", "x", "abc"))
        self.assertIn("fingerprint", row)
        self.assertIn("n", row)
        self.assertNotIn("field_evidence", row)
        self.assertNotIn("astra.classification", row)

    def test_compact_group_retains_reporting_dimensions_and_stays_small(self):
        original_text = (
            "2026-09-08 19:45:12 ERROR agent.conversation_loop: "
            "The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna"
        )
        classification = {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "fallback_provider": "openrouter",
            "fallback_model": "gpt-4o",
            "platform": "discord",
            "component": "agent.conversation_loop",
            "tool": "web_search",
            "target": "tool:web_search",
            "operation": "chat_completion",
            "stream_stage": "before_delivery",
            "http_status": 429,
            "provider_code": "usage_limit_reached",
            "rpc_code": -32000,
            "errno": 11,
            "exit_code": 137,
            "signal": "SIGKILL",
            "event": "api.quota_exhausted",
            "events": ["api.quota_exhausted", "api.request_failed"],
            "cause": "usage_quota_exhausted",
            "causes": ["usage_quota_exhausted", "rate_limited"],
            "exception": "RateLimitError",
            "exceptions": ["RateLimitError"],
            "matched_rules": [
                {"id": "cause.usage_quota_exhausted", "matched": "usage limit has been reached"},
                {"id": "event.api.quota_exhausted", "matched": "usage limit has been reached"},
            ],
            "conflicts": [{"field": "model", "candidates": ["gpt-5.6-luna", "gpt-4o"]}],
            "event_ts": "2026-09-08T19:45:12",
            "event_ts_basis": "text",
            "field_confidence": {"event_ts": "explicit"},
            "possible_truncation": True,
            "kind": "incident",
            "domain": "llm",
            "log_level": "ERROR",
            "field_evidence": {"provider": "x" * 4000, "model": "y" * 4000},
        }
        enriched = {
            "host": "dell",
            "profile": "ana",
            "text": original_text,
            "sig": "ratelimit",
            "ts": "2026-09-08T19:45:12+00:00",
            "file": "errors.log",
            "payload": "z" * 8000,
            "astra.classification": classification,
            "astra.fingerprint": {
                "key": "abc123",
                "host": "dell",
                "profile": "ana",
                "normalized_text": "ERROR agent.conversation_loop: The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna",
                "event": "api.quota_exhausted",
                "cause": "usage_quota_exhausted",
                "domain": "llm",
                "kind": "incident",
            },
            "astra.severity": {
                "label": "needs-attention",
                "rule_id": "severe-causes",
                "policy_version": "v4.1-table",
            },
            "astra.provenance": {
                "source_path": "/var/log/events-dell.jsonl",
                "byte_start": 0,
                "byte_end": 120,
                "rule_version": "v4.1-corpus",
                "tier0_sig_original": "ratelimit",
            },
        }
        compact = grouped_record(enriched)
        self.assertEqual(compact["provider"], "openai-codex")
        self.assertEqual(compact["model"], "gpt-5.6-luna")
        self.assertEqual(compact["fallback_provider"], "openrouter")
        self.assertEqual(compact["fallback_model"], "gpt-4o")
        self.assertEqual(compact["platform"], "discord")
        self.assertEqual(compact["component"], "agent.conversation_loop")
        self.assertEqual(compact["tool"], "web_search")
        self.assertEqual(compact["target"], "tool:web_search")
        self.assertEqual(compact["operation"], "chat_completion")
        self.assertEqual(compact["stream_stage"], "before_delivery")
        self.assertEqual(compact["http_status"], 429)
        self.assertEqual(compact["provider_code"], "usage_limit_reached")
        self.assertEqual(compact["rpc_code"], -32000)
        self.assertEqual(compact["errno"], 11)
        self.assertEqual(compact["exit_code"], 137)
        self.assertEqual(compact["signal"], "SIGKILL")
        self.assertEqual(compact["events"], ["api.quota_exhausted", "api.request_failed"])
        self.assertEqual(compact["causes"], ["usage_quota_exhausted", "rate_limited"])
        self.assertEqual(compact["exceptions"], ["RateLimitError"])
        self.assertEqual(
            compact["matched_rule_ids"],
            ["cause.usage_quota_exhausted", "event.api.quota_exhausted"],
        )
        self.assertEqual(compact["conflicts"], [{"field": "model", "candidates": ["gpt-5.6-luna", "gpt-4o"]}])
        self.assertEqual(compact["ts_quality"]["basis"], "text")
        self.assertIn(original_text[:40], compact["text"])
        self.assertTrue(compact["text_truncated"])
        self.assertEqual(compact["source_files"], ["/var/log/events-dell.jsonl"])
        self.assertEqual(compact["capture_sig"], "ratelimit")
        self.assertEqual(compact["severity"], "needs-attention")
        self.assertEqual(compact["severity_rule_id"], "severe-causes")
        self.assertEqual(compact["severity_policy_version"], "v4.1-table")
        self.assertNotIn("field_evidence", compact)
        self.assertNotIn("payload", compact)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(enriched)) // 4)

    def test_retains_original_keys_when_they_collide_with_astra_namespace(self):
        original_classification = {"event": "do-not-clobber"}
        enriched = _enriched("dell", "ana", "fail", "k-ns")
        enriched["astra.classification"] = original_classification
        enriched["astra.pipeline"] = {
            "astra.classification": {
                "event": "api.request_failed",
                "cause": "timeout",
                "kind": "incident",
                "domain": "llm",
                "provider": "openai-codex",
                "events": ["api.request_failed"],
                "causes": ["timeout"],
                "exceptions": [],
                "matched_rules": [{"id": "cause.timeout", "matched": "timed out"}],
                "conflicts": [],
                "event_ts_basis": "stored_unverified",
                "possible_truncation": False,
                "log_level": "ERROR",
            }
        }
        compact = grouped_record(enriched)
        self.assertEqual(compact["event"], "api.request_failed")
        self.assertEqual(compact["provider"], "openai-codex")


if __name__ == "__main__":
    unittest.main()
