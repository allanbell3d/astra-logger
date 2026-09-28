#!/usr/bin/env python3
"""Bundled configuration loads through the actual severity-policy reader."""
import os
import unittest

from astra.severity import SeverityPolicy, apply_severity

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestSampleConfig(unittest.TestCase):
    def test_bundled_policy_loads_and_matches_v4_quota_rule(self):
        path = os.path.join(ROOT, "src", "config", "severity_policy.json")
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


if __name__ == "__main__":
    unittest.main()
