"""Bounded failure contracts for ASTRA delivery projections and lifecycle state."""
import hashlib
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/agent"))

from astra.delivery import Config, Delivery, DeliveryError, install_schema
from astra.review import ReviewStore
from tests.test_review import event
from tests.test_delivery import attach_fake_web


RCA_MARKDOWN = """# Root Cause Analysis: fixture

## Observed impact
Service could not open its database.

## Timeline and evidence
Finding recorded at 12:00 with bounded evidence.

## Facts versus hypotheses
Fact: the log names a malformed database image.
Hypothesis: unclean shutdown.

## Checks and results
Read-only integrity check reported corruption.

## Root cause and confidence
High confidence the named database file is structurally invalid.

## Proposed fix
Stop the process, restore the file from backup, verify integrity.

## Risks and prerequisites
Do not repair until the owner approves the restore.

## Rollback
Keep the corrupt file as a backup copy.

## Post-repair verification
Re-run integrity check and confirm the original error is gone.
"""


class DeliveryFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def engine(self):
        skill = self.root / "profile/skills/monitoring/astra-delivery"
        assets = skill / "assets"
        assets.mkdir(parents=True)
        for source, name in (
            (ROOT / "src/agent/astra/rca_report.py", "rca_report.py"),
            (ROOT / "src/agent/astra/rca_report_shell.html", "rca_report_shell.html"),
            (ROOT / "src/report-template/rca-template.html", "rca-template.html"),
        ):
            shutil.copy2(source, assets / name)
        (assets / "manifest.json").write_text(json.dumps({
            "version": "1.0.0",
            "assets": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets.iterdir()},
        }))
        shared = self.root / "shared"
        (shared / "dashboard").mkdir(parents=True)
        (shared / "dashboard/handover.md").write_text("fixture conventions")
        store = ReviewStore(self.root / "records/review.sqlite")
        self.addCleanup(store.close)
        install_schema(store)
        store.ingest([event(1)], now=1000)
        triage = store.prepare("triage", now=1001)
        store.complete(triage["batch"], [
            {"id": item["id"], "decision": "rca", "reason": "Synthetic delivery fixture."}
            for item in triage["items"]
        ], now=1002)
        batch = store.prepare("rca-a", now=1003)["batch"]
        config = Config(self.root / "profile", skill, self.root / "records", shared, "https://fixture.example:8443")
        delivery = Delivery(store, config)
        attach_fake_web(delivery)
        return delivery, store, batch

    @property
    def markdown(self):
        return RCA_MARKDOWN

    def _sibling_batch(self, store, finding, batch_id="synthetic-rca-b", created=None):
        store.db.execute(
            "INSERT INTO batches VALUES(?,?,?,?,?,?,?)",
            (batch_id, "rca-b", json.dumps([finding]), "running", 1, 9999999999, created or time.time()),
        )
        store.db.commit()
        return batch_id

    def _fixed_repair(self, delivery, report_id):
        repair = {
            "assigned_agent": "repairer",
            "assigned_host": "fixture-host",
            "assigned_at": delivery.renderer.timestamp(),
            "attempts": [], "followups": [],
        }
        delivery.update(report_id, {"status": "repair assigned", "repair": repair}, 1, "owner")
        delivery.update(report_id, {"status": "repairing"}, 2, "repairer")
        repair["attempts"] = [{
            "agent": "repairer", "host": "fixture-host",
            "started_at": delivery.renderer.timestamp(), "finished_at": delivery.renderer.timestamp(),
            "actions": ["Synthetic repair"], "changes": [],
            "verification": [{"check": "fixture", "passed": True, "result": "pass", "evidence_ref": "test"}],
        }]
        delivery.update(report_id, {"status": "fixed", "repair": repair}, 3, "repairer")

    def test_siblings_share_repair_facts_but_keep_lane_rca_and_flags_independent(self):
        delivery, store, primary = self.engine()
        first = delivery.complete(primary, self.markdown, now=1004)
        before_flags=store.db.execute('SELECT rca_a_done,rca_b_done FROM findings WHERE id=?',(first['finding_id'],)).fetchone()
        self.assertEqual(tuple(before_flags),(1,0))
        sibling = self._sibling_batch(store, first["finding_id"])
        second = delivery.complete(sibling, self.markdown + "\n", now=1005)
        before_first = delivery.show(first["report_id"])
        before_second = delivery.show(second["report_id"])
        repair = {"assigned_agent": "peer", "assigned_host": "peer-host", "assigned_at": delivery.renderer.timestamp(), "attempts": [], "followups": []}
        delivery.update(first["report_id"], {"status": "repair assigned", "repair": repair}, 1, "owner")
        after_first, after_second = delivery.show(first["report_id"]), delivery.show(second["report_id"])
        self.assertEqual(after_second["status"], "repair assigned")
        self.assertEqual(after_second["document"]["repair"], repair)
        self.assertEqual(after_first["original_rca_hash"], before_first["original_rca_hash"])
        self.assertEqual(after_second["original_rca_hash"], before_second["original_rca_hash"])
        self.assertNotEqual(after_first["original_rca_hash"], after_second["original_rca_hash"])
        flags = store.db.execute("SELECT rca_a_done, rca_b_done FROM findings WHERE id=?", (first["finding_id"],)).fetchone()
        self.assertEqual(tuple(flags), (1, 1))

    def test_stale_sibling_update_is_rejected_after_sibling_sync(self):
        delivery, store, primary = self.engine()
        first = delivery.complete(primary, self.markdown, now=1004)
        second = delivery.complete(self._sibling_batch(store, first["finding_id"]), self.markdown + "\n", now=1005)
        repair = {"assigned_agent": "peer", "assigned_host": "peer-host", "assigned_at": delivery.renderer.timestamp(), "attempts": [], "followups": []}
        delivery.update(first["report_id"], {"status": "repair assigned", "repair": repair}, 1, "owner")
        with self.assertRaisesRegex(DeliveryError, "Stale report revision"):
            delivery.update(second["report_id"], {"status": "deferred", "reason": "stale writer"}, 1, "owner")

    def test_missing_or_corrupt_pinned_asset_has_actionable_error(self):
        delivery, _, _ = self.engine()
        assets = delivery.config.skill_dir / "assets"
        (assets / "rca_report_shell.html").unlink()
        with self.assertRaisesRegex(DeliveryError, "Pinned template asset missing/changed: rca_report_shell.html"):
            Delivery(delivery.store, delivery.config)
        shutil.copy2(ROOT / "src/agent/astra/rca_report_shell.html", assets / "rca_report_shell.html")
        (assets / "rca_report.py").write_text("corrupt")
        with self.assertRaisesRegex(DeliveryError, "Pinned template asset missing/changed: rca_report.py"):
            Delivery(delivery.store, delivery.config)

    def test_public_write_failure_preserves_local_archive_and_accounted_batch(self):
        delivery, store, batch = self.engine()
        delivery.renderer.publish = lambda *args: (_ for _ in ()).throw(OSError("fixture public write failure"))
        result = delivery.complete(batch, self.markdown, now=1004)
        self.assertEqual(result["mode"], "DEGRADED")
        self.assertTrue(Path(result["report_path"]).is_file())
        self.assertTrue(Path(result["record_path"]).is_file())
        self.assertEqual(store.db.execute("SELECT status FROM batches WHERE id=?", (batch,)).fetchone()[0], "done")
        self.assertEqual(delivery.show(result["report_id"])["publication_state"], "pending")

    def test_deleted_projection_is_republished_from_local_authority(self):
        delivery, _, batch = self.engine()
        result = delivery.complete(batch, self.markdown, now=1004)
        public = Path(result["shared_path"])
        public.unlink()
        delivery.reconcile(result["report_id"])
        self.assertTrue(public.is_file())
        self.assertEqual(delivery.show(result["report_id"])["publication_state"], "published")

    def test_unchanged_receipt_ingests_once_and_changed_receipt_conflicts(self):
        delivery, _, batch = self.engine()
        result = delivery.complete(batch, self.markdown, now=1004)
        receipt_id = "b" * 32
        receipt = delivery.config.shared_root / "dashboard/_private/inbox/astra" / receipt_id / "answer.json"
        receipt.parent.mkdir(parents=True)
        payload = {"receipt_id": receipt_id, "item_id": delivery.show(result["report_id"])["catalog_item_id"], "item_path": delivery.show(result["report_id"])["shared_relpath"], "recipient": "astra", "answers": {}, "attachments": []}
        receipt.write_text(json.dumps(payload))
        self.assertEqual(len(delivery.read_answers(result["report_id"])["answers"]), 1)
        self.assertEqual(len(delivery.read_answers(result["report_id"])["answers"]), 1)
        payload["comment"] = "tampered after ingestion"
        receipt.write_text(json.dumps(payload))
        read = delivery.read_answers(result["report_id"])
        self.assertFalse(read["ok"])
        self.assertIn("Idempotency conflict", read["warnings"][0])
        self.assertEqual(len(read["answers"]), 1)

    def test_same_batch_replay_with_changed_markdown_is_rejected(self):
        delivery, _, batch = self.engine()
        delivery.complete(batch, self.markdown, now=1004)
        with self.assertRaisesRegex(DeliveryError, "Batch already completed with different Markdown"):
            delivery.complete(batch, self.markdown + "\nchanged", now=1005)

    def test_owner_verification_requires_explicit_confirmation(self):
        delivery, _, batch = self.engine()
        result = delivery.complete(batch, self.markdown, now=1004)
        self._fixed_repair(delivery, result["report_id"])
        with self.assertRaisesRegex(DeliveryError, "Explicit current owner confirmation"):
            delivery.update(result["report_id"], {"status": "verified by owner"}, 4, "owner")
        delivery.update(result["report_id"], {"status": "verified by owner", "owner_confirmation": "owner-ticket-42"}, 4, "owner")
        self.assertEqual(delivery.show(result["report_id"])["status"], "verified by owner")

    def test_new_post_closure_evidence_starts_open_episode_not_fixed_inheritance(self):
        delivery, store, batch = self.engine()
        first = delivery.complete(batch, self.markdown, now=1004)
        self._fixed_repair(delivery, first["report_id"])
        fresh = event(2)
        fresh["record_id"] = "fresh-post-closure"
        fresh["byte_start"], fresh["byte_end"] = 500, 590
        from datetime import datetime,timezone
        fresh["event_ts"] = fresh["ts"] = datetime.fromtimestamp(time.time()+1,timezone.utc).isoformat()
        store.ingest([fresh], now=2000)
        replay = self._sibling_batch(store, first["finding_id"], "post-closure-rca", created=time.time() + 1)
        new = delivery.complete(replay, self.markdown + "\n", now=2001)
        self.assertEqual(delivery.show(new["report_id"])["status"], "open")
        self.assertEqual(delivery.show(new["report_id"])["episode"], 2)
        self.assertEqual(delivery.show(first["report_id"])["status"], "fixed")


    def test_concurrent_sibling_updates_accept_only_one_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        delivery,store,batch=self.engine();a=delivery.complete(batch,self.markdown,now=1004)
        b=delivery.complete(self._sibling_batch(store,a['finding_id']),self.markdown+'\n',now=1005)
        barrier=Barrier(2)
        def attempt(report_id):
            other=ReviewStore(store.path)
            try:
                engine=Delivery(other,delivery.config);engine.web_available=lambda:False
                barrier.wait(timeout=10)
                try:engine.update(report_id,{'status':'deferred','reason':'Concurrent '+report_id},1,'owner');return 'accepted'
                except DeliveryError as e:return str(e)
            finally:other.close()
        with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(attempt,[a['report_id'],b['report_id']]))
        self.assertEqual(out.count('accepted'),1,out)
        self.assertEqual(sum('Stale report revision' in x for x in out),1,out)
        self.assertEqual(delivery.show(a['report_id'])['revision'],2);self.assertEqual(delivery.show(b['report_id'])['revision'],2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
