#!/usr/bin/env python3
"""Discover host-agnostic JSONL inputs from glob patterns."""
import os
import tempfile
import unittest

from astra.inputs import discover_input_files


class TestDiscoverInputFiles(unittest.TestCase):
    def test_expands_globs_and_deduplicates_overlapping_matches(self):
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "events-pi.jsonl")
            b = os.path.join(td, "events-dell.jsonl")
            open(a, "w", encoding="utf-8").close()
            open(b, "w", encoding="utf-8").close()
            patterns = [
                os.path.join(td, "events-*.jsonl"),
                os.path.join(td, "events-pi.jsonl"),
                os.path.join(td, "events-dell.jsonl"),
            ]

            found = discover_input_files(patterns)

            self.assertEqual(
                found,
                sorted({os.path.realpath(a), os.path.realpath(b)}),
            )


if __name__ == "__main__":
    unittest.main()
