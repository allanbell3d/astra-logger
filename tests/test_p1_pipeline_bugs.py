"""Isolated regressions for audit B1/B2/B3/B8. Tempdirs only; no prod IO."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("cron", MagicMock())
sys.modules.setdefault("cron.jobs", MagicMock())

from astra.review import ReviewStore
from astra.review_cli import main as cli_main
from astra import runtime
from tests.test_review import event
import tests.test_review as _review_mod

RCA_MD = _review_mod.TestReviewStore.RCA_MD

REPO = Path(__file__).resolve().parents[1]
DISPATCH_SRC = REPO / "src" / "agent" / "astra_dispatch.py"
PREPARE_B = REPO / "src" / "deploy" / "cron-wrappers" / "astra_rca_prepare_b.py"


def _rca_batch(store, kind, now=1000):
    store.ingest([event(1)], now=now)
    triage = store.prepare("triage", now=now + 1)
    gid = triage["items"][0]["id"]
    store.complete(
        triage["batch"],
        [{"id": gid, "decision": "rca", "reason": "Bounded investigation required."}],
        now=now + 2,
    )
    return store.prepare(kind, now=now + 3)


def _cli_complete_report(root, state, batch_id, report_path, archive, shared):
    return cli_main(
        [
            "--root",
            str(root),
            "--state",
            str(state),
            "complete",
            "--batch",
            batch_id,
            "--report",
            str(report_path),
            "--archive-dir",
            str(archive),
            "--shared-dir",
            str(shared),
        ]
    )


class TestB1CliRcaLaneRouting(unittest.TestCase):
    def _complete_kind(self, prepare_kind):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            state = td / "review.sqlite"
            root = td / "enriched"
            root.mkdir()
            s = ReviewStore(state)
            packet = _rca_batch(s, prepare_kind)
            row = s.db.execute(
                "SELECT kind FROM batches WHERE id=?", (packet["batch"],)
            ).fetchone()
            report_path = td / "report.md"
            report_path.write_text(RCA_MD)
            rc = _cli_complete_report(
                root, state, packet["batch"], report_path, td / "arch", td / "shared"
            )
            flags = s.db.execute(
                "SELECT rca_done,rca_a_done,rca_b_done,decision FROM findings"
            ).fetchone()
            batch = s.db.execute(
                "SELECT status FROM batches WHERE id=?", (packet["batch"],)
            ).fetchone()
            s.close()
            return {
                "prepared_kind": packet["kind"],
                "db_kind": row["kind"],
                "cli_rc": rc,
                "batch_status": batch["status"],
                "flags": tuple(flags),
            }

    def test_complete_accepts_rca_a_batch(self):
        got = self._complete_kind("rca")
        self.assertEqual(got["db_kind"], "rca-a")
        self.assertEqual(got["cli_rc"], 0)
        self.assertEqual(got["batch_status"], "done")
        self.assertEqual(got["flags"][0], 1)
        self.assertEqual(got["flags"][1], 1)

    def test_complete_accepts_rca_b_batch(self):
        got = self._complete_kind("rca-b")
        self.assertEqual(got["prepared_kind"], "rca-b")
        self.assertEqual(got["cli_rc"], 0)
        self.assertEqual(got["batch_status"], "done")
        self.assertEqual(got["flags"][2], 1)


class TestB2ReconcileSuccessFallthrough(unittest.TestCase):
    def _reconcile(self, role, prepare_kind):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "profile"
            out = home / "cron/output/r"
            out.mkdir(parents=True)
            s = ReviewStore(root / "review.sqlite")
            batch = _rca_batch(s, prepare_kind)
            output = (
                "# Cron Job: RCA\n\n## Prompt\n\n"
                + "```json\n"
                + json.dumps({"batch": batch["batch"]})
                + "\n```\n\n## Response\n\n"
                + RCA_MD
            )
            (out / "1970-01-01_00-17-00.md").write_text(output)
            jobs = {
                role: {
                    "id": "r",
                    "last_run_at": "1970-01-01T00:17:00+00:00",
                    "last_status": "ok",
                    "state": "completed",
                }
            }
            manifest = {"shared": str(root / "shared" / "rca")}
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                notices = runtime.reconcile_results(s, jobs, now=1021, manifest=manifest)
            rows = [
                dict(x)
                for x in s.db.execute(
                    "SELECT id,kind,status,attempts FROM batches WHERE kind LIKE 'rca%'"
                )
            ]
            flags = [
                tuple(x)
                for x in s.db.execute(
                    "SELECT rca_done,rca_a_done,rca_b_done,decision FROM findings"
                )
            ]
            st = s.status(now=1022)
            s.close()
            return {
                "batch_kind": batch["kind"],
                "notices": notices,
                "pending_rca": st["pending_rca"],
                "batches": rows,
                "flags": flags,
            }

    def test_successful_complete_rca_does_not_reset_rca_a(self):
        got = self._reconcile("rca-a", "rca")
        self.assertEqual(got["batch_kind"], "rca-a")
        self.assertEqual(got["notices"], [])
        self.assertEqual(got["batches"][0]["status"], "done")
        self.assertEqual(got["flags"][0][0], 1)
        self.assertEqual(got["flags"][0][1], 1)

    def test_successful_complete_rca_does_not_reset_rca_b(self):
        got = self._reconcile("rca-b", "rca-b")
        self.assertEqual(got["batch_kind"], "rca-b")
        self.assertEqual(got["notices"], [])
        self.assertEqual(got["batches"][0]["status"], "done")
        self.assertEqual(got["flags"][0][2], 1)


class TestB3DispatchLedger(unittest.TestCase):
    def test_main_declares_global_ledger(self):
        mod = ast.parse(DISPATCH_SRC.read_text())
        has_global = False
        assigns = False
        for n in ast.walk(mod):
            if isinstance(n, ast.FunctionDef) and n.name == "main":
                for x in ast.walk(n):
                    if isinstance(x, ast.Global) and "LEDGER" in x.names:
                        has_global = True
                    if isinstance(x, ast.Assign):
                        for t in x.targets:
                            if isinstance(t, ast.Name) and t.id == "LEDGER":
                                assigns = True
        self.assertTrue(has_global, "main() must declare global LEDGER")
        self.assertTrue(assigns)

    def test_already_sent_is_check_only_and_mark_after_success(self):
        import astra_dispatch as d

        self.assertTrue(hasattr(d, "_mark_sent"))
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / ".notices.sent"
            d.LEDGER = str(ledger)
            msg = "ASTRA host=test function=rca-a analysis incomplete"
            self.assertFalse(d._already_sent(msg))
            self.assertFalse(ledger.exists(), "check must not create the ledger")
            with patch.object(d, "_discord_send", return_value=False) as send:
                delivered = d._discord_send("chan", msg)
                if delivered:
                    d._mark_sent(msg)
            self.assertFalse(delivered)
            send.assert_called_once()
            self.assertFalse(ledger.exists(), "failed delivery must not mark sent")
            with patch.object(d, "_discord_send", return_value=True):
                delivered = d._discord_send("chan", msg)
                if delivered:
                    d._mark_sent(msg)
            self.assertTrue(ledger.exists())
            self.assertTrue(d._already_sent(msg))
            self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_main_marks_sent_only_after_successful_send(self):
        tree = ast.parse(DISPATCH_SRC.read_text())
        main = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        src = ast.dump(main)
        self.assertIn("_mark_sent", src)
        self.assertIn("_discord_send", src)
        send_lines, mark_lines, already_lines = [], [], []
        for n in ast.walk(main):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if n.func.id == "_discord_send":
                    send_lines.append(n.lineno)
                elif n.func.id == "_mark_sent":
                    mark_lines.append(n.lineno)
                elif n.func.id == "_already_sent":
                    already_lines.append(n.lineno)
        self.assertTrue(already_lines)
        self.assertTrue(send_lines)
        self.assertTrue(mark_lines)
        self.assertLess(min(already_lines), min(send_lines))
        self.assertLess(min(send_lines), min(mark_lines))


class TestB8PrepareBStdout(unittest.TestCase):
    def test_wrapper_does_not_print_pre_script_return(self):
        src = PREPARE_B.read_text()
        self.assertNotIn("print(pre_script", src)
        self.assertIn("pre_script('rca-b')", src)

    def test_prepare_b_stdout_is_json_without_none(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            scripts = home / ".hermes" / "scripts"
            pkg = scripts / "astra"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "runtime.py").write_text(
                "def pre_script(role):\n"
                "    print('{\"wakeAgent\":true}')\n"
                "    return None\n"
            )
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "USERPROFILE": str(home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(scripts),
            })
            proc = subprocess.run(
                [sys.executable, str(PREPARE_B)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, '{"wakeAgent":true}\n')
            self.assertNotIn("None", proc.stdout)


if __name__ == "__main__":
    unittest.main()
