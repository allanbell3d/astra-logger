#!/usr/bin/env python3
"""Fingerprint/group key preserves host, profile, and semantic dimensions."""
import unittest

from astra.classify import classify_event
from astra.fingerprint import build_fingerprint, canonical_host


class TestFingerprint(unittest.TestCase):
    def test_canonical_host_aliases(self):
        self.assertEqual(canonical_host("pi"), "RpiClaude")
        self.assertEqual(canonical_host("RpiClaude"), "RpiClaude")
        self.assertEqual(canonical_host("dell"), "dell")
        self.assertEqual(canonical_host(""), "UNKNOWN")

    def test_same_text_on_pi_and_dell_do_not_share_a_fingerprint(self):
        text = "ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 1.0s"
        pi = classify_event({"text": text, "host": "RpiClaude", "profile": "ana"})
        dell = classify_event({"text": text, "host": "dell", "profile": "ana"})
        self.assertNotEqual(
            build_fingerprint(pi, host="RpiClaude", profile="ana", text=text)["key"],
            build_fingerprint(dell, host="dell", profile="ana", text=text)["key"],
        )

    def test_different_profiles_on_same_host_do_not_share_a_fingerprint(self):
        text = "ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 1.0s"
        classification = classify_event({"text": text, "host": "dell", "profile": "ana"})
        self.assertNotEqual(
            build_fingerprint(classification, host="dell", profile="ana", text=text)["key"],
            build_fingerprint(classification, host="dell", profile="rumi", text=text)["key"],
        )

    def test_volatile_duration_collapses_but_model_does_not(self):
        first = classify_event({"text": "ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 1.0s"})
        second = classify_event({"text": "ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 9.9s"})
        other = classify_event({"text": "ERROR agent.api: openai-codex model=gpt-5.6-terra stream failed after 1.0s"})
        self.assertEqual(
            build_fingerprint(first, host="dell", profile="ana", text="ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 1.0s")["key"],
            build_fingerprint(second, host="dell", profile="ana", text="ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 9.9s")["key"],
        )
        self.assertNotEqual(
            build_fingerprint(first, host="dell", profile="ana", text="ERROR agent.api: openai-codex model=gpt-5.6-luna stream failed after 1.0s")["key"],
            build_fingerprint(other, host="dell", profile="ana", text="ERROR agent.api: openai-codex model=gpt-5.6-terra stream failed after 1.0s")["key"],
        )

    def test_fingerprint_exposes_semantic_dimensions(self):
        classification = classify_event({
            "text": "2026-09-08 19:45:12 ERROR agent.conversation_loop: The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna",
            "host": "dell",
            "profile": "ana",
        })
        fp = build_fingerprint(
            classification,
            host="dell",
            profile="ana",
            text="2026-09-08 19:45:12 ERROR agent.conversation_loop: The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna",
        )
        self.assertEqual(fp["host"], "dell")
        self.assertEqual(fp["profile"], "ana")
        self.assertEqual(fp["event"], "api.quota_exhausted")
        self.assertEqual(fp["cause"], "usage_quota_exhausted")
        self.assertEqual(fp["provider"], "openai-codex")
        self.assertEqual(fp["model"], "gpt-5.6-luna")
        self.assertIn("normalized_text", fp)
        self.assertTrue(fp["key"])


if __name__ == "__main__":
    unittest.main()
