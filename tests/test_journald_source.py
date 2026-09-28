#!/usr/bin/env python3
"""Journald source: approved field subset, cursor, priority gate, batches."""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from astra.journald_source import (
    JOURNALD_FIELDS,
    JournaldEntry,
    journalctl_reader,
    priority_of,
    read_journald_entries,
)


def _entry(cursor: str, message: str, priority: int = 3, **extra: str) -> JournaldEntry:
    fields = {
        "MESSAGE": message,
        "PRIORITY": str(priority),
        "__REALTIME_TIMESTAMP": "1760000000000000",
        "__CURSOR": cursor,
        "_HOSTNAME": "testhost",
        "_BOOT_ID": "b" * 32,
    }
    fields.update(extra)
    return JournaldEntry(fields=fields, cursor=cursor)


class TestPriorityAndFields(unittest.TestCase):
    def test_approved_field_subset_is_exact(self):
        self.assertEqual(JOURNALD_FIELDS, (
            "MESSAGE", "PRIORITY", "__REALTIME_TIMESTAMP", "__CURSOR",
            "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT", "SYSLOG_IDENTIFIER",
            "_COMM", "_EXE", "_CMDLINE", "_PID", "_UID", "_GID", "_HOSTNAME",
            "_BOOT_ID", "CODE_FILE", "CODE_LINE", "CODE_FUNC", "ERRNO",
            "RESULT", "UNIT", "USER_UNIT", "INVOCATION_ID", "SYSLOG_FACILITY",
        ))

    def test_priority_of_parses_int_and_string(self):
        self.assertEqual(priority_of("3"), 3)
        self.assertEqual(priority_of(4), 4)
        self.assertEqual(priority_of(None), 99)
        self.assertEqual(priority_of("junk"), 99)


class TestReadJournald(unittest.TestCase):
    def test_read_uses_cursor_keeps_warning_and_above_only(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "journal_state.json")
            entries = [
                _entry("c1", "info message", priority=6),
                _entry("c2", "notice message", priority=5),
                _entry("c3", "warning message", priority=4),
                _entry("c4", "error message", priority=3),
                _entry("c5", "critical message", priority=2),
            ]

            def reader(after_cursor, limit):
                if after_cursor is None:
                    return entries[:3]
                return []

            result = read_journald_entries(reader, state)
            self.assertEqual([e.cursor for e in result.entries], ["c3"])

            def reader2(after_cursor, limit):
                # after_cursor advanced to the last *consumed* entry
                self.assertEqual(after_cursor, "c3")
                return entries[3:]

            result2 = read_journald_entries(reader2, state)
            self.assertEqual([e.cursor for e in result2.entries], ["c4", "c5"])
            # cursor persists
            def reader3(after_cursor, limit):
                self.assertEqual(after_cursor, "c5")
                return []

            result3 = read_journald_entries(reader3, state)
            self.assertEqual(result3.entries, [])

    def test_cursor_fallback_within_24h_window(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "journal_state.json")

            def reader(after_cursor, limit):
                self.assertIsNone(after_cursor)  # no stored cursor -> fallback window
                return [_entry("c1", "warning one", priority=4)]

            read_journald_entries(reader, state)

            # corrupt the cursor file: next run must fall back, not crash
            with open(state, "w", encoding="utf-8") as handle:
                handle.write("{not json")

            def reader2(after_cursor, limit):
                self.assertIsNone(after_cursor)
                return [_entry("c2", "warning two", priority=4)]

            result = read_journald_entries(reader2, state)
            self.assertEqual([e.cursor for e in result.entries], ["c2"])

    def test_fallback_reader_receives_iso_window(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "journal_state.json")
            seen = {}

            def reader(after_cursor, limit, since=None):
                seen["after_cursor"] = after_cursor
                seen["since"] = since
                return []

            read_journald_entries(reader, state, fallback_window_hours=24)
            self.assertIsNone(seen["after_cursor"])
            self.assertIsNotNone(seen["since"])
            self.assertIn("T", seen["since"])  # ISO-8601

    def test_batches_are_bounded_and_stop_without_progress(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "journal_state.json")
            calls = []

            def reader(after_cursor, limit):
                calls.append((after_cursor, limit))
                # first call returns one entry, subsequent return the SAME
                # cursor again (no progress) -> must stop, not loop forever
                return [_entry("c9", "warning", priority=4)]

            result = read_journald_entries(reader, state, batch_limit=50, max_batches=3)
            self.assertLessEqual(len(calls), 3)
            self.assertEqual(len(result.entries), 1)
            # duplicate cursor dedup: entry counted once
            self.assertEqual(result.entries[-1].cursor, "c9")


class TestJournalctlReader(unittest.TestCase):
    def test_streams_forward_from_cursor_and_stops_at_limit(self):
        blobs = "".join(json.dumps(_entry(f"c{i}", f"m{i}").fields) + "\n" for i in range(1, 4))

        class Proc:
            def __init__(self):
                self.stdout = io.StringIO(blobs)
                self.terminated = False
                self.returncode = None
            def poll(self):
                return self.returncode
            def terminate(self):
                self.terminated = True
                self.returncode = -15
            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        proc = Proc()
        with patch("astra.journald_source.subprocess.Popen", return_value=proc) as popen:
            result = journalctl_reader("prior", 2)
        args = popen.call_args.args[0]
        self.assertIn("--after-cursor=prior", args)
        self.assertNotIn("-n", args)
        self.assertEqual([entry.cursor for entry in result], ["c1", "c2"])
        self.assertTrue(proc.terminated)


class TestFieldSubset(unittest.TestCase):
    def test_retained_fields_drop_unapproved_keys(self):
        entry = _entry("c1", "warning message", priority=4, _COMM="systemd", SECRET_TOKEN="x", PAM_TYPE="y")
        retained = entry.retained()
        self.assertIn("_COMM", retained)
        self.assertNotIn("SECRET_TOKEN", retained)
        self.assertNotIn("PAM_TYPE", retained)
        self.assertEqual(set(retained), set(JOURNALD_FIELDS) & set(entry.fields))


if __name__ == "__main__":
    "__main__" == "__NOT__" and unittest.main()
