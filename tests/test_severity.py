#!/usr/bin/env python3
"""Editable external severity policy."""
import json
import os
import tempfile
import unittest

from astra.severity import SeverityPolicy, apply_severity


class TestSeverityPolicy(unittest.TestCase):
    def test_default_policy_matches_v4_triage_table(self):
        policy = SeverityPolicy.default()
        self.assertEqual(
            apply_severity({"kind": "continuation", "event": None, "causes": [], "log_level": None}, policy)["label"],
            "ignore",
        )
        self.assertEqual(
            apply_severity(
                {"kind": "observation", "event": "api.watchdog_tuned", "causes": ["watchdog_tuning"], "log_level": "INFO"},
                policy,
            )["label"],
            "ignore",
        )
        self.assertEqual(
            apply_severity(
                {
                    "kind": "incident",
                    "event": "platform.operation_rate_limited",
                    "causes": ["typing_rate_limited"],
                    "log_level": "WARNING",
                },
                policy,
            )["label"],
            "ignore",
        )
        self.assertEqual(
            apply_severity(
                {
                    "kind": "incident",
                    "event": "api.quota_exhausted",
                    "causes": ["usage_quota_exhausted"],
                    "log_level": "ERROR",
                    "cause": "usage_quota_exhausted",
                },
                policy,
            )["label"],
            "needs-attention",
        )
        self.assertEqual(
            apply_severity(
                {
                    "kind": "incident",
                    "event": "system.oom_invoked",
                    "causes": ["memory_exhausted"],
                    "log_level": None,
                    "cause": "memory_exhausted",
                },
                policy,
            )["label"],
            "needs-attention",
        )
        self.assertEqual(
            apply_severity(
                {
                    "kind": "incident",
                    "event": "api.stream_stalled",
                    "causes": ["stream_stalled"],
                    "log_level": "ERROR",
                    "cause": "stream_stalled",
                },
                policy,
            )["label"],
            "needs-attention",
        )
        self.assertEqual(
            apply_severity(
                {
                    "kind": "incident",
                    "event": "api.request_failed",
                    "causes": ["timeout"],
                    "log_level": "ERROR",
                    "cause": "timeout",
                },
                policy,
            )["label"],
            "watch",
        )

    def test_loads_editable_json_and_first_match_wins(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "severity.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": "test",
                        "default": "watch",
                        "rules": [
                            {"id": "quota", "when": {"causes_any": ["usage_quota_exhausted"]}, "label": "needs-attention"},
                            {"id": "all-errors", "when": {"log_level": ["ERROR"]}, "label": "ignore"},
                        ],
                    },
                    handle,
                )
            policy = SeverityPolicy.load(path)
            applied = apply_severity(
                {
                    "kind": "incident",
                    "event": "api.quota_exhausted",
                    "causes": ["usage_quota_exhausted"],
                    "log_level": "ERROR",
                    "cause": "usage_quota_exhausted",
                },
                policy,
            )
            self.assertEqual(applied["label"], "needs-attention")
            self.assertEqual(applied["rule_id"], "quota")
            self.assertEqual(applied["policy_version"], "test")


if __name__ == "__main__":
    unittest.main()
