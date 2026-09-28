#!/usr/bin/env python3
"""CLI entry for python -m astra."""
import json
import os
import subprocess
import sys
import tempfile
import unittest


class TestCli(unittest.TestCase):
    def test_cli_accepts_glob_patterns_and_writes_outputs(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with tempfile.TemporaryDirectory() as td:
            source = os.path.join(td, "events-dell.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "host": "dell",
                            "profile": "ana",
                            "text": "INFO agent.loop: hello from CLI",
                            "sig": "unclassified",
                        }
                    )
                    + "\n"
                )
            enriched = os.path.join(td, "enriched.jsonl")
            grouped = os.path.join(td, "grouped.jsonl")
            state = os.path.join(td, "cursor.json")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "astra",
                    "--input",
                    os.path.join(td, "events-*.jsonl"),
                    "--enriched",
                    enriched,
                    "--grouped",
                    grouped,
                    "--state",
                    state,
                ],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["processed"], 1)
            with open(enriched, encoding="utf-8") as handle:
                row = json.loads(handle.readline())
            self.assertEqual(row["host"], "dell")
            self.assertIn("astra.classification", row)


if __name__ == "__main__":
    unittest.main()
