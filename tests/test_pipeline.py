#!/usr/bin/env python3
"""End-to-end ASTRA pipeline."""
import json
import os
import tempfile
import unittest
from unittest import mock

from astra.cursor import CursorStore
from astra.pipeline import PipelineCollisionError, run_pipeline


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


PI_ROW = {
    "ts": "2026-07-12T02:46:32+00:00",
    "host": "RpiClaude",
    "profile": "system:NetworkManager",
    "file": "journald",
    "sig": "unclassified",
    "sev": "watch",
    "keep_me": "pi-original",
    "text": "NetworkManager invoked oom-killer: gfp_mask=0x1100cca(GFP_HIGHUSER_MOVABLE), order=0, oom_score_adj=0",
}
DELL_ROW = {
    "ts": "2026-09-08T19:45:12+00:00",
    "host": "dell",
    "profile": "ana",
    "file": "errors.log",
    "sig": "ratelimit",
    "sev": None,
    "event": "original-event",
    "keep_me": "dell-original",
    "text": "2026-09-08 19:45:12 ERROR [20260908_194500_abcd] agent.conversation_loop: The usage limit has been reached (429 RateLimitError) for openai-codex gpt-5.6-luna",
}


class TestPipeline(unittest.TestCase):
    def test_processes_pi_and_dell_rows_without_dropping_originals(self):
        with tempfile.TemporaryDirectory() as td:
            pi = os.path.join(td, "events-pi.jsonl")
            dell = os.path.join(td, "events-dell.jsonl")
            _write_jsonl(pi, [PI_ROW])
            _write_jsonl(dell, [DELL_ROW])
            enriched_path = os.path.join(td, "enriched.jsonl")
            grouped_path = os.path.join(td, "grouped.jsonl")
            state_path = os.path.join(td, "cursor.json")

            result = run_pipeline(
                patterns=[os.path.join(td, "events-*.jsonl"), pi],
                enriched_path=enriched_path,
                grouped_path=grouped_path,
                state_path=state_path,
            )
            rows = _read_jsonl(enriched_path)
            hosts = {row["host"] for row in rows}
            self.assertEqual(result["processed"], 2)
            self.assertEqual(hosts, {"RpiClaude", "dell"})
            by_host = {row["host"]: row for row in rows}
            self.assertEqual(by_host["RpiClaude"]["keep_me"], "pi-original")
            self.assertEqual(by_host["dell"]["keep_me"], "dell-original")
            self.assertEqual(by_host["dell"]["event"], "original-event")
            self.assertEqual(by_host["dell"]["astra.classification"]["event"], "api.quota_exhausted")
            self.assertEqual(by_host["RpiClaude"]["astra.classification"]["event"], "system.oom_invoked")
            self.assertEqual(by_host["dell"]["astra.severity"]["label"], "needs-attention")
            grouped = _read_jsonl(grouped_path)
            self.assertEqual(len(grouped), 2)
            self.assertEqual({row["host"] for row in grouped}, {"RpiClaude", "dell"})

            result2 = run_pipeline(
                patterns=[os.path.join(td, "events-*.jsonl")],
                enriched_path=enriched_path,
                grouped_path=grouped_path,
                state_path=state_path,
            )
            self.assertEqual(result2["processed"], 0)
            self.assertEqual(len(_read_jsonl(enriched_path)), 2)

    def test_refuses_output_input_collision(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            _write_jsonl(source, [DELL_ROW])
            with self.assertRaises(PipelineCollisionError):
                run_pipeline(
                    patterns=[source],
                    enriched_path=source,
                    grouped_path=os.path.join(td, "grouped.jsonl"),
                    state_path=os.path.join(td, "cursor.json"),
                )

    def test_refuses_journal_sidecar_collision_with_input(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "cursor.json")
            source = state_path + ".journal"
            _write_jsonl(source, [DELL_ROW])
            with self.assertRaises(PipelineCollisionError):
                run_pipeline(
                    patterns=[source],
                    enriched_path=os.path.join(td, "enriched.jsonl"),
                    grouped_path=os.path.join(td, "grouped.jsonl"),
                    state_path=state_path,
                )

    def test_crash_after_enriched_write_before_cursor_does_not_duplicate_on_retry(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            _write_jsonl(source, [DELL_ROW])
            enriched_path = os.path.join(td, "enriched.jsonl")
            grouped_path = os.path.join(td, "grouped.jsonl")
            state_path = os.path.join(td, "cursor.json")
            real_save = CursorStore.save

            def crashing_save(self):
                crashing_save.calls += 1
                if crashing_save.calls == 1:
                    raise RuntimeError(
                        "simulated failure after enriched output write but before cursor commit"
                    )
                return real_save(self)

            crashing_save.calls = 0
            with mock.patch.object(CursorStore, "save", crashing_save):
                with self.assertRaises(RuntimeError):
                    run_pipeline(
                        patterns=[source],
                        enriched_path=enriched_path,
                        grouped_path=grouped_path,
                        state_path=state_path,
                    )

            result = run_pipeline(
                patterns=[source],
                enriched_path=enriched_path,
                grouped_path=grouped_path,
                state_path=state_path,
            )
            rows = _read_jsonl(enriched_path)
            grouped = _read_jsonl(grouped_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(grouped), 1)
            self.assertEqual(grouped[0]["n"], 1)
            self.assertEqual(result["processed"], 1)

    def test_crash_before_enriched_append_replays_without_loss(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            _write_jsonl(source, [DELL_ROW])
            enriched_path = os.path.join(td, "enriched.jsonl")
            grouped_path = os.path.join(td, "grouped.jsonl")
            state_path = os.path.join(td, "cursor.json")

            with mock.patch("astra.pipeline._append_jsonl", side_effect=RuntimeError("pre-append crash")):
                with self.assertRaises(RuntimeError):
                    run_pipeline(
                        patterns=[source],
                        enriched_path=enriched_path,
                        grouped_path=grouped_path,
                        state_path=state_path,
                    )

            result = run_pipeline(
                patterns=[source],
                enriched_path=enriched_path,
                grouped_path=grouped_path,
                state_path=state_path,
            )
            self.assertEqual(result["processed"], 1)
            self.assertEqual(len(_read_jsonl(enriched_path)), 1)
            self.assertEqual(_read_jsonl(grouped_path)[0]["n"], 1)
            self.assertFalse(os.path.exists(state_path + ".journal"))

    def test_policy_change_reapplies_to_existing_groups_without_new_input(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            _write_jsonl(
                source,
                [
                    {
                        "ts": "2026-09-08T19:45:12+00:00",
                        "host": "dell",
                        "profile": "ana",
                        "file": "errors.log",
                        "sig": "timeout",
                        "text": "2026-09-08 19:45:12 ERROR agent.api: openai-codex model=gpt-5.6-luna request timed out after 1.0s",
                    }
                ],
            )
            enriched_path = os.path.join(td, "enriched.jsonl")
            grouped_path = os.path.join(td, "grouped.jsonl")
            state_path = os.path.join(td, "cursor.json")
            policy_watch = os.path.join(td, "policy-watch.json")
            policy_hot = os.path.join(td, "policy-hot.json")
            with open(policy_watch, "w", encoding="utf-8") as handle:
                json.dump({"version": "watch-default", "default": "watch", "rules": []}, handle)
            with open(policy_hot, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": "hot-default", "default": "needs-attention", "rules": []},
                    handle,
                )

            run_pipeline(
                patterns=[source],
                enriched_path=enriched_path,
                grouped_path=grouped_path,
                state_path=state_path,
                policy_path=policy_watch,
            )
            first = _read_jsonl(grouped_path)
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["severity"], "watch")
            fingerprint = first[0]["fingerprint"]
            count = first[0]["n"]

            result = run_pipeline(
                patterns=[source],
                enriched_path=enriched_path,
                grouped_path=grouped_path,
                state_path=state_path,
                policy_path=policy_hot,
            )
            second = _read_jsonl(grouped_path)
            self.assertEqual(result["processed"], 0)
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["fingerprint"], fingerprint)
            self.assertEqual(second[0]["n"], count)
            self.assertEqual(second[0]["severity"], "needs-attention")
            self.assertEqual(len(_read_jsonl(enriched_path)), 1)


if __name__ == "__main__":
    unittest.main()
