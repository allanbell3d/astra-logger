#!/usr/bin/env python3
"""Durable per-file byte/inode cursor for JSONL tails."""
import json
import os
import tempfile
import unittest

from astra.cursor import CursorStore
from astra.jsonl_tail import read_new_complete_records


def _write(path, text, mode="w"):
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


class TestJsonlTailCursor(unittest.TestCase):
    def test_reads_only_complete_newly_appended_rows(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            state_path = os.path.join(td, "cursor.json")
            first = {"host": "dell", "text": "one", "extra": 1}
            second = {"host": "RpiClaude", "text": "two"}
            _write(source, json.dumps(first) + "\n" + json.dumps(second) + "\n")
            store = CursorStore(state_path)

            rows, errors = read_new_complete_records(source, store)
            store.save()

            self.assertEqual(errors, [])
            self.assertEqual([row.record for row in rows], [first, second])
            self.assertEqual(len(rows), 2)

            later = {"host": "dell", "text": "three"}
            _write(source, json.dumps(later) + "\n", mode="a")
            store = CursorStore(state_path)
            rows, errors = read_new_complete_records(source, store)
            store.save()

            self.assertEqual(errors, [])
            self.assertEqual([row.record for row in rows], [later])

    def test_does_not_consume_incomplete_final_line(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            state_path = os.path.join(td, "cursor.json")
            complete = {"host": "dell", "text": "complete"}
            _write(source, json.dumps(complete) + "\n{\"host\": \"dell\", \"text\": \"partial")
            store = CursorStore(state_path)

            rows, errors = read_new_complete_records(source, store)
            store.save()

            self.assertEqual(errors, [])
            self.assertEqual([row.record for row in rows], [complete])

            _write(source, "\"}\n", mode="a")
            store = CursorStore(state_path)
            rows, errors = read_new_complete_records(source, store)

            self.assertEqual(errors, [])
            self.assertEqual([row.record for row in rows], [{"host": "dell", "text": "partial"}])

    def test_malformed_json_does_not_advance_past_the_bad_line(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            state_path = os.path.join(td, "cursor.json")
            good = {"host": "RpiClaude", "text": "ok"}
            _write(source, json.dumps(good) + "\n{not-json}\n{\"host\": \"dell\", \"text\": \"after\"}\n")
            store = CursorStore(state_path)

            rows, errors = read_new_complete_records(source, store)
            store.save()

            self.assertEqual([row.record for row in rows], [good])
            self.assertEqual(len(errors), 1)
            self.assertIn("malformed JSON", errors[0].message)

            store = CursorStore(state_path)
            rows, errors = read_new_complete_records(source, store)
            self.assertEqual(rows, [])
            self.assertEqual(len(errors), 1)

            with open(source, encoding="utf-8") as handle:
                first = handle.readline()
                rest = handle.read()
            _write(source, first + rest.replace("{not-json}\n", json.dumps({"host": "dell", "text": "fixed"}) + "\n"))
            store = CursorStore(state_path)
            rows, errors = read_new_complete_records(source, store)
            self.assertEqual(errors, [])
            self.assertEqual(
                [row.record for row in rows],
                [{"host": "dell", "text": "fixed"}, {"host": "dell", "text": "after"}],
            )

    def test_rotation_and_truncation_reset_the_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events.jsonl")
            state_path = os.path.join(td, "cursor.json")
            _write(source, json.dumps({"host": "dell", "text": "old"}) + "\n")
            store = CursorStore(state_path)
            rows, _ = read_new_complete_records(source, store)
            store.save()
            self.assertEqual(len(rows), 1)

            os.replace(source, source + ".1")
            _write(source, json.dumps({"host": "dell", "text": "rotated"}) + "\n")
            store = CursorStore(state_path)
            rows, errors = read_new_complete_records(source, store)
            store.save()
            self.assertEqual(errors, [])
            self.assertEqual([row.record for row in rows], [{"host": "dell", "text": "rotated"}])

            _write(source, json.dumps({"host": "dell", "text": "t"}) + "\n")
            store = CursorStore(state_path)
            rows, errors = read_new_complete_records(source, store)
            self.assertEqual(errors, [])
            self.assertEqual([row.record for row in rows], [{"host": "dell", "text": "t"}])


if __name__ == "__main__":
    unittest.main()
