#!/usr/bin/env python3
"""Discovery of raw Hermes log sources and per-source line filtering."""
import os
import tempfile
import unittest

from astra.raw_sources import (
    default_hermes_patterns,
    discover_raw_sources,
    filter_lines_for_source,
)


def _make(base: str, rel: str) -> str:
    path = os.path.normpath(os.path.join(base, rel))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x\n")
    return path


class TestDiscoverRawSources(unittest.TestCase):
    def test_default_patterns_cover_root_and_profiles_with_approved_globs(self):
        home = os.path.join("XHERMES", "home")  # placeholder replaced below
        patterns = default_hermes_patterns(home)
        self.assertEqual(patterns, [
            os.path.join(home, "logs", "agent.log*"),
            os.path.join(home, "logs", "errors.log*"),
            os.path.join(home, "logs", "gateway.log*"),
            os.path.join(home, "profiles", "*", "logs", "agent.log*"),
            os.path.join(home, "profiles", "*", "logs", "errors.log*"),
            os.path.join(home, "profiles", "*", "logs", "gateway.log*"),
        ])

    def test_discover_finds_rotated_and_profile_logs_and_skips_others(self):
        with tempfile.TemporaryDirectory() as td:
            files = [
                _make(td, "logs/agent.log"),
                _make(td, "logs/agent.log.1"),
                _make(td, "logs/errors.log"),
                _make(td, "logs/gateway.log.2"),
                _make(td, "logs/other.log"),          # not an approved source name
                _make(td, "profiles/ana/logs/agent.log"),
                _make(td, "profiles/rumi/logs/errors.log.1"),
                _make(td, "profiles/ana/agent.log"),  # wrong depth
            ]
            found = discover_raw_sources([
                os.path.join(td, "logs", "agent.log*"),
                os.path.join(td, "logs", "errors.log*"),
                os.path.join(td, "logs", "gateway.log*"),
                os.path.join(td, "profiles", "*", "logs", "agent.log*"),
                os.path.join(td, "profiles", "*", "logs", "errors.log*"),
                os.path.join(td, "profiles", "*", "logs", "gateway.log*"),
            ])
            found_paths = {s.path for s in found}
            for kept in (files[0], files[1], files[2], files[3], files[5], files[6]):
                self.assertIn(kept, found_paths)
            self.assertNotIn(files[4], found_paths)
            self.assertNotIn(files[7], found_paths)
            by_path = {s.path: s for s in found}
            self.assertEqual(by_path[files[5]].profile, "ana")
            self.assertEqual(by_path[files[6]].profile, "rumi")
            self.assertEqual(by_path[files[0]].profile, "default")


class TestFilterLines(unittest.TestCase):
    def test_errors_log_keeps_every_complete_nonblank_line(self):
        lines = ["INFO routine heartbeat", "WARNING diskusage 91%", "INFO another"]
        kept = filter_lines_for_source("errors.log", lines)
        self.assertEqual(kept, [True, True, True])

    def test_agent_log_keeps_warning_error_critical_and_drops_info_debug(self):
        lines = [
            "2026-09-12 10:00:00 INFO [s1] agent.loop: routine",
            "2026-09-12 10:00:01 DEBUG agent.loop: chatter",
            "2026-09-12 10:00:02 WARNING agent.loop: retrying",
            "2026-09-12 10:00:03 ERROR agent.loop: failed",
            "2026-09-12 10:00:04 CRITICAL agent.loop: dead",
        ]
        kept = filter_lines_for_source("agent.log", lines)
        self.assertEqual(kept, [False, False, True, True, True])

    def test_gateway_log_variant_names_filter_identically(self):
        lines = ["INFO g: ok", "ERROR g: bad"]
        self.assertEqual(filter_lines_for_source("gateway.log.1", lines), [False, True])
        self.assertEqual(filter_lines_for_source("errors.log.2026-09-11", lines), [True, True])

    def test_traceback_continuation_lines_are_kept_for_agent_log(self):
        lines = [
            "2026-09-12 10:00:03 ERROR agent.loop: boom",
            'Traceback (most recent call last):',
            '  File "/app/main.py", line 10, in handler',
            "    result = do_work()",
            "ValueError: invalid value",
            "2026-09-12 10:00:05 INFO agent.loop: recovered",
        ]
        kept = filter_lines_for_source("agent.log", lines)
        self.assertEqual(kept, [True, True, True, True, True, False])

    def test_traceback_continuation_at_increment_boundary_is_kept(self):
        lines = ['  File "/app/main.py", line 10, in handler', "ValueError: invalid value"]
        self.assertEqual(filter_lines_for_source("agent.log", lines), [True, True])

    def test_unlevelled_line_after_error_is_kept_as_context(self):
        lines = [
            "2026-09-12 10:00:03 ERROR agent.loop: boom",
            "connection reset by peer while streaming",
            "INFO noise",
        ]
        kept = filter_lines_for_source("agent.log", lines)
        self.assertEqual(kept, [True, True, False])


if __name__ == "__main__":
    unittest.main()
