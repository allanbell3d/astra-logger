#!/usr/bin/env python3
"""Raw Hermes log file tailing: baseline, cursor, rotation, backlog."""
import os
import tempfile
import unittest

from astra.cursor import CursorStore
from astra.raw_tail import read_new_raw_lines


def line_i(i: int) -> str:
    return f"L{i:04d}".ljust(99, "x") + "\n"


def write_lines(path: str, count: int, start: int = 0) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        for i in range(start, start + count):
            handle.write(line_i(i))


class TestRawTailFirstSight(unittest.TestCase):
    def test_first_sight_reads_only_last_200k_aligned_after_first_newline(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 2500)  # 2500 * 100 = 250000 bytes
            store = CursorStore(os.path.join(td, "cursor.json"))
            result = read_new_raw_lines(path, store)
            # window starts at byte 50000 (a line boundary); alignment skips
            # the partial-or-complete first line, so first returned line is
            # the one starting at byte 50100 -> L0501
            self.assertEqual(result.lines[0].text, line_i(501).rstrip("\n"))
            self.assertEqual(result.lines[0].byte_start, 50100)
            self.assertEqual(len(result.lines), 2500 - 501)

    def test_small_file_first_sight_reads_whole_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 3)
            store = CursorStore(os.path.join(td, "cursor.json"))
            result = read_new_raw_lines(path, store)
            self.assertEqual([l.text for l in result.lines], [line_i(i).rstrip("\n") for i in range(3)])
            self.assertEqual(result.backlog_bytes, 0)

    def test_blank_lines_are_skipped_without_emitting_records(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "errors.log")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write("ERROR one\n\n\nERROR two\n")
            store = CursorStore(os.path.join(td, "cursor.json"))
            result = read_new_raw_lines(path, store)
            self.assertEqual([l.text for l in result.lines], ["ERROR one", "ERROR two"])


class TestRawTailIncremental(unittest.TestCase):
    def test_second_run_with_no_change_reads_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 5)
            store = CursorStore(os.path.join(td, "cursor.json"))
            first = read_new_raw_lines(path, store)
            store.save()
            store2 = CursorStore(os.path.join(td, "cursor.json"))
            second = read_new_raw_lines(path, store2)
            self.assertEqual(len(first.lines), 5)
            self.assertEqual(second.lines, [])
            self.assertEqual(second.backlog_bytes, 0)

    def test_appended_lines_are_read_once(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 5)
            store = CursorStore(os.path.join(td, "cursor.json"))
            read_new_raw_lines(path, store)
            with open(path, "a", encoding="utf-8", newline="") as handle:
                handle.write(line_i(5) + line_i(6))
            result = read_new_raw_lines(path, store)
            self.assertEqual([l.text for l in result.lines], [line_i(5).rstrip("\n"), line_i(6).rstrip("\n")])
            # third run: nothing new
            self.assertEqual(read_new_raw_lines(path, store).lines, [])

    def test_partial_trailing_line_is_not_consumed_and_backlog_reports_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 2)
            partial = "ERROR partial without newline"
            with open(path, "a", encoding="utf-8", newline="") as handle:
                handle.write(partial)
            store = CursorStore(os.path.join(td, "cursor.json"))
            result = read_new_raw_lines(path, store)
            self.assertEqual([l.text for l in result.lines], [line_i(0).rstrip("\n"), line_i(1).rstrip("\n")])
            self.assertEqual(result.backlog_bytes, len(partial.encode()))
            entry = store.get(os.path.realpath(path))
            self.assertEqual(entry["offset"], 200)  # cursor never past processed bytes
            # completing the line makes it consumable next run
            with open(path, "a", encoding="utf-8", newline="") as handle:
                handle.write("\n")
            result2 = read_new_raw_lines(path, store)
            self.assertEqual([l.text for l in result2.lines], [partial])
            self.assertEqual(result2.backlog_bytes, 0)


class TestRawTailRotationTruncation(unittest.TestCase):
    def test_truncation_resets_cursor_to_start(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 5)
            store = CursorStore(os.path.join(td, "cursor.json"))
            read_new_raw_lines(path, store)
            replacement = "T0".ljust(99, "y") + "\n"
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(line_i(0) + line_i(1) + replacement)
            result = read_new_raw_lines(path, store)
            self.assertTrue(result.truncated)
            self.assertEqual(
                [l.text for l in result.lines],
                [line_i(0).rstrip("\n"), line_i(1).rstrip("\n"), replacement.rstrip("\n")],
            )

    def test_rotation_to_new_inode_applies_first_sight_baseline_not_whole_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 10)
            store = CursorStore(os.path.join(td, "cursor.json"))
            read_new_raw_lines(path, store)
            # real rotation renames the old inode out of the way first
            os.rename(path, os.path.join(td, "agent.log.1"))
            write_lines(path, 2500)  # new inode, fresh 250000-byte file
            result = read_new_raw_lines(path, store)
            self.assertTrue(result.rotated)
            self.assertEqual(result.lines[0].text, line_i(501).rstrip("\n"))
            self.assertEqual(len(result.lines), 2500 - 501)
            # and the new inode's cursor persists
            self.assertEqual(read_new_raw_lines(path, store).lines, [])


class TestRawTailWorkCeiling(unittest.TestCase):
    def test_work_ceiling_limits_run_and_reports_backlog_drained_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "agent.log")
            write_lines(path, 20)  # 2000 bytes
            store = CursorStore(os.path.join(td, "cursor.json"))
            result = read_new_raw_lines(path, store, work_ceiling=500)
            self.assertEqual(len(result.lines), 5)
            self.assertEqual(result.backlog_bytes, 1500)
            self.assertEqual(store.get(os.path.realpath(path))["offset"], 500)
            drained = read_new_raw_lines(path, store)
            self.assertEqual(len(drained.lines), 15)
            self.assertEqual(drained.backlog_bytes, 0)


if __name__ == "__main__":
    unittest.main()
