#!/usr/bin/env python3
"""Deterministic alarm, observation bookmark, and source-pointer slicing."""
import json
import tempfile
import unittest
from pathlib import Path

from astra.pager import (
    INSPECT_PROMPT_RULES,
    RCA_PROMPT_RULES,
    annotate_report_fields,
    append_observation,
    attention_rows,
    event_key,
    format_alarm,
    format_job_error,
    load_seen,
    model_function,
    parse_escalate,
    slice_source,
    source_pointers,
    unseen_rows,
    write_escalate_request,
)


def _row(**kwargs):
    base = {
        "ts": "2026-09-14T08:00:00+04:00",
        "host": "dell",
        "profile": "eva",
        "event": "database.corruption_detected",
        "cause": "malformed_sqlite",
        "severity": "needs-attention",
        "source_path": "/tmp/errors.log",
        "byte_start": 10,
        "byte_end": 40,
        "text": "database disk image is malformed",
    }
    base.update(kwargs)
    return base


class TestPager(unittest.TestCase):
    def test_quiet_when_only_watch(self):
        rows = [_row(severity="watch", event="security.audit_warning")]
        self.assertEqual(attention_rows(rows), [])
        self.assertEqual(format_alarm("dell", []), "")

    def test_alarm_includes_source_pointer(self):
        rows = attention_rows([_row()])
        text = format_alarm("dell", rows)
        self.assertIn("**ALARM dell**", text)
        self.assertIn("database.corruption_detected", text)
        self.assertIn("agent=`eva`", text)
        self.assertIn("function=`unknown`", text)
        self.assertIn("/tmp/errors.log:10-40", text)
        self.assertNotIn("\t", text)
        self.assertNotIn("profile=`", text)

    def test_bookmark_skips_already_seen_keys(self):
        first = _row()
        seen = {event_key(first)}
        self.assertEqual(unseen_rows([first], seen), [])
        second = _row(byte_start=99, byte_end=120, ts="2026-09-14T08:10:00+04:00")
        unseen = unseen_rows([first, second], seen)
        self.assertEqual(len(unseen), 1)
        self.assertEqual(unseen[0]["byte_start"], 99)

    def test_slice_source_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "errors.log"
            payload = b"0123456789ABCDEFGHIJ"
            path.write_bytes(payload)
            self.assertEqual(slice_source(str(path), 10, 16), "ABCDEF")

    def test_observation_and_escalate_bookmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = Path(tmp) / "observations.jsonl"
            rec = append_observation(
                obs,
                host="dell",
                model="deepseek-ai/deepseek-v4-flash-0731",
                last_ts="2026-09-14T08:00:00+04:00",
                summary="sqlite malformed in eva",
                escalate=True,
                pointers=[{"source_path": "/tmp/errors.log", "byte_start": 10, "byte_end": 40}],
            )
            self.assertTrue(rec["id"])
            lines = obs.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(json.loads(lines[0])["escalate"])
            dest = Path(tmp) / "astra-escalate.json"
            write_escalate_request(dest, rec)
            saved = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(saved["observation_id"], rec["id"])
            self.assertEqual(saved["pointers"][0]["byte_start"], 10)

    def test_parse_escalate_yes_no(self):
        self.assertTrue(parse_escalate("Escalate: yes — sqlite is toast", attention=0))
        self.assertFalse(parse_escalate("escalate: no (watch only)", attention=2))
        self.assertTrue(parse_escalate("no explicit flag", attention=1))
        self.assertFalse(parse_escalate("no explicit flag", attention=0))

    def test_load_seen_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alarm-state.json"
            self.assertEqual(load_seen(path), set())
            path.write_text(json.dumps({"seen": ["a", "b"]}), encoding="utf-8")
            self.assertEqual(load_seen(path), {"a", "b"})

    def test_model_function_maps_slots(self):
        self.assertEqual(model_function({"component": "agent.title_generator"}), "title")
        self.assertEqual(model_function({"component": "agent.conversation_loop"}), "main")
        self.assertEqual(model_function({"component": "agent.auxiliary"}), "aux")
        self.assertEqual(model_function({"tool": "delegate_task"}), "delegation")
        self.assertEqual(model_function({"operation": "fallback"}), "fallback")
        self.assertEqual(model_function({"component": "tools.approval"}), "approvals")
        self.assertEqual(model_function({"component": "hermes_cli.plugins"}), "plugin")
        self.assertEqual(model_function({"component": "gateway.platforms"}), "gateway")

    def test_alarm_names_title_slot(self):
        rows = attention_rows(
            [
                _row(
                    event="api.title_generation_failed",
                    cause="auth_invalid",
                    component="agent.title_generator",
                    operation="title_generation",
                    provider="vertex",
                    profile="default",
                )
            ]
        )
        text = format_alarm("dell", rows)
        self.assertIn("agent=`default`", text)
        self.assertIn("function=`title`", text)
        self.assertIn("provider=`vertex`", text)

    def test_job_error_names_rca_slot(self):
        line = format_job_error(
            agent="astra-rca",
            function="rca_model",
            message="HTTP 404",
            provider="vertex",
            model="gemini-3.8-flash",
            host="dell",
            config="/home/allanbell3d/.hermes/scripts/astra-rca.json",
            now="2026-09-14T12:07:24+04:00",
        )
        self.assertEqual(
            line,
            "2026-09-14T12:07:24+04:00 agent=astra-rca function=rca_model host=dell "
            "provider=vertex model=gemini-3.8-flash "
            "config=/home/allanbell3d/.hermes/scripts/astra-rca.json HTTP 404",
        )

    def test_prompt_rules_require_agent_function(self):
        self.assertIn("agent", INSPECT_PROMPT_RULES)
        self.assertIn("function", INSPECT_PROMPT_RULES)
        self.assertIn("Never omit agent or function", INSPECT_PROMPT_RULES)
        self.assertIn("agent (profile)", RCA_PROMPT_RULES)
        self.assertIn("function", RCA_PROMPT_RULES)

    def test_annotate_and_pointers_carry_slot(self):
        row = annotate_report_fields(
            _row(component="agent.title_generator", operation="title_generation")
        )
        self.assertEqual(row["agent"], "eva")
        self.assertEqual(row["function"], "title")
        pointers = source_pointers([row])
        self.assertEqual(pointers[0]["agent"], "eva")
        self.assertEqual(pointers[0]["function"], "title")


if __name__ == "__main__":
    unittest.main()
