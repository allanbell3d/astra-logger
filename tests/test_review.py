"""Behavioral contract for accounted, bounded review batches."""
import importlib.util
from contextlib import closing
from itertools import combinations
import json
import tempfile
import unittest
from pathlib import Path
from astra.classify import classify_event
from astra.enrich import enrich_record
from astra.severity import SeverityPolicy
from astra.review import packed

def event(i, text=None, profile='default'):
    r={'record_id':str(i),'host':'test','profile':profile,'event_ts':'2026-09-16T12:00:00Z','ts':'2026-09-16T12:00:00Z','source_path':'/nonexistent/agent.log','byte_start':i*100,'byte_end':i*100+90,'text':text or '2026-09-16 12:00:00 ERROR agent.vertex_adapter: Your default credentials were not found.'}
    return enrich_record(r,classify_event(r),SeverityPolicy.default(),r['source_path'],r['byte_start'],r['byte_end'])

class TestReviewStore(unittest.TestCase):
    def store(self,p):
        self.assertIsNotNone(importlib.util.find_spec('astra.review'),'accounted review module not implemented')
        from astra.review import ReviewStore
        return ReviewStore(p)

    def prepare_single_member_rca(self, store, pending_ids, now):
        # Deliberately exercise overflow through the existing byte budget.
        # TD3 owns multi-member completion; do not invent its report syntax here.
        budget = max(len(packed({'wakeAgent': True, 'batch': 'b' * 20,
                                'kind': 'rca-a', 'pending_findings': len(pending_ids),
                                'items': [store._item(gid)]}).encode())
                     for gid in pending_ids)
        for first, second in combinations(pending_ids, 2):
            two_member_packet = {'wakeAgent': True, 'batch': 'b' * 20,
                                 'kind': 'rca-a', 'pending_findings': len(pending_ids),
                                 'items': [store._item(first), store._item(second)]}
            self.assertGreater(len(packed(two_member_packet).encode()), budget,
                               'Fixture budget must exclude every two-member packet')
        packet = store.prepare('rca-a', now=now, max_bytes=budget)
        self.assertTrue(packet['wakeAgent'])
        self.assertEqual(len(packet['items']), 1)
        return packet

    def test_batch_is_not_reviewed_until_complete_and_replay_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            s=self.store(Path(td)/'review.sqlite')
            r=event(1)
            self.assertEqual(s.ingest([r,r]),1)
            b=s.prepare('triage',now=1000)
            self.assertTrue(b['wakeAgent'])
            self.assertFalse(s.prepare('triage',now=1001)['wakeAgent'])
            # Failure/lease expiry must retry the SAME evidence, not acknowledge it.
            retry=s.prepare('triage',now=2000)
            self.assertEqual(retry['batch'],b['batch'])
            self.assertEqual(retry['items'][0]['id'],b['items'][0]['id'])
            findings=[{'id':x['id'],'decision':'watch','reason':'Explicit missing credential; requires owner configuration review.'} for x in retry['items']]
            s.complete(b['batch'],findings,now=2001)
            self.assertEqual(s.ingest([r]),0)
            self.assertFalse(s.prepare('triage',now=2002)['wakeAgent'])
            s.close()

    def test_triage_and_rca_hold_independent_leases(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')
            s.ingest([
                event(1, text='2026-09-16 12:00:00 ERROR agent.vertex_adapter: credentials unavailable'),
                event(2, text='2026-09-16 12:00:01 ERROR sqlite: database disk image is malformed'),
            ], now=1000)
            ids = [row['id'] for row in s.db.execute('SELECT id FROM findings ORDER BY rowid')]
            s.db.execute("UPDATE findings SET decision='rca',reason='RCA lane fixture' WHERE id=?", (ids[0],))
            s.db.commit()
            triage = s.prepare('triage', now=1001)
            rca = s.prepare('rca', now=1002)
            self.assertTrue(triage['wakeAgent'])
            self.assertTrue(rca['wakeAgent'])
            self.assertNotEqual(triage['batch'], rca['batch'])
            active = s.db.execute("SELECT kind FROM batches WHERE status='running' ORDER BY kind").fetchall()
            self.assertEqual([row['kind'] for row in active], ['rca-a', 'triage'])
            s.close()

    def test_approved_backlog_progresses_without_retriage(self):
        with tempfile.TemporaryDirectory() as td, closing(self.store(Path(td) / 'review.sqlite')) as s:
            s.ingest([event(1, profile='a'), event(2, profile='b')], now=1000)
            triage = s.prepare('triage', now=1001)
            approved_ids = {x['id'] for x in triage['items']}
            s.complete(triage['batch'], [{'id': x['id'], 'decision': 'rca', 'reason': 'Requires evidence-based investigation.'} for x in triage['items']], now=1002)
            self.assertTrue(s.status(now=1003)['dispatch_rca'])
            rca = self.prepare_single_member_rca(s, approved_ids, now=1004)
            s.complete_rca(rca['batch'], self.RCA_MD, now=1005)
            pending_ids = approved_ids - {x['id'] for x in rca['items']}
            self.assertEqual(s.status(now=1600)['pending_rca'], len(pending_ids))
            self.assertTrue(s.status(now=1600)['dispatch_rca_a'])
            self.assertFalse(s.prepare('triage', now=2800)['wakeAgent'])
            next_rca = self.prepare_single_member_rca(s, pending_ids, now=2801)
            self.assertEqual({x['id'] for x in next_rca['items']}, pending_ids)

    def test_urgent_finding_requires_validated_triage_before_rca(self):
        with tempfile.TemporaryDirectory() as td, closing(self.store(Path(td) / 'review.sqlite')) as s:
            s.ingest([event(1, text='2026-09-16 12:00:00 ERROR sqlite: database disk image is malformed')], now=1000)
            s.promote_urgent(now=1001)
            # Mandatory triage does not prescribe an immediate/early scheduling policy.
            self.assertFalse(s.status(now=1002)['dispatch_rca'])
            self.assertFalse(s.prepare('rca', now=1003)['wakeAgent'])
            triage = s.prepare('triage', now=1004)
            self.assertTrue(triage['wakeAgent'])
            s.complete(triage['batch'], [{'id': x['id'], 'decision': 'rca',
                        'reason': 'Validated corruption evidence warrants investigation.'}
                       for x in triage['items']], now=1005)
            batch = s.prepare('rca', now=1006)
            self.assertTrue(batch['wakeAgent'])
            self.assertEqual({x['id'] for x in batch['items']}, {x['id'] for x in triage['items']})
            s.complete_rca(batch['batch'], self.RCA_MD, now=1007)
            self.assertEqual(s.promote_urgent(now=1600), [])
            self.assertFalse(s.status(now=1601)['dispatch_rca_a'])

    def test_small_batches_account_for_all_distinct_profiles_and_keep_model_identity(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            s=self.store(Path(td)/'review.sqlite')
            rows=[event(i,profile=f'agent-{i}') for i in range(9)]
            for r in rows:r['astra.classification'].update(provider='vertex',model='selected-model')
            s.ingest(rows)
            seen=set()
            for tick in range(9):
                b=s.prepare(now=1000+tick*10,max_bytes=1500)
                if not b['wakeAgent']:break
                self.assertLessEqual(len(json.dumps(b,ensure_ascii=False,separators=(',',':')).encode()),1500)
                self.assertEqual(b['items'][0]['provider'],'vertex')
                self.assertEqual(b['items'][0]['model'],'selected-model')
                self.assertGreaterEqual(b['pending_findings'],len(b['items']))
                ids={x['id'] for x in b['items']}
                self.assertFalse(seen&ids);seen|=ids
                s.complete(b['batch'],[{'id':i,'decision':'watch','reason':'Test review of distinct profile evidence.'} for i in ids])
            self.assertEqual(len(seen),9)
            s.close()

    def test_sync_sources_scans_changed_buckets_and_idempotently_ingests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = self.store(root / 'review.sqlite')
            bucket = root / 'enriched-20260916-00.jsonl'
            r1 = event(1, text='2026-09-16 12:00:00 ERROR agent.core: failed 1')
            r2 = event(2, text='2026-09-16 12:05:00 ERROR agent.core: failed 2')
            import json
            bucket.write_text(json.dumps(r1) + '\n')

            # First sync: ingests r1
            result = s.sync_sources(root, now=1000)
            self.assertEqual(result['scanned_files'], 1)
            self.assertEqual(result['ingested_rows'], 1)

            # Second sync without file modification: does not re-ingest
            result2 = s.sync_sources(root, now=1005)
            self.assertEqual(result2['scanned_files'], 0)
            self.assertEqual(result2['ingested_rows'], 0)

            # File modified with r2: detects change, ingests r2 without duplicating r1
            bucket.write_text(json.dumps(r1) + '\n' + json.dumps(r2) + '\n')
            result3 = s.sync_sources(root, now=1010)
            self.assertEqual(result3['scanned_files'], 1)
            self.assertEqual(result3['ingested_rows'], 1)
            s.close()

    def test_repeated_evidence_respects_cooldown_and_changed_severity_not_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')
            s.cooldown_seconds = 300  # 5 minutes cooldown
            r1 = event(1, text='2026-09-16 12:00:00 ERROR agent.core: disk full')
            s.ingest([r1], now=1000)
            b = s.prepare('triage', now=1000)
            self.assertTrue(b['wakeAgent'])
            gid = b['items'][0]['id']
            s.complete(b['batch'], [{'id': gid, 'decision': 'watch', 'reason': 'Track disk exhaustion over next interval.'}], now=1000)

            # Within cooldown: new occurrence is recorded, but finding is suppressed from triage
            r1_later = event(10, text='2026-09-16 12:02:00 ERROR agent.core: disk full')
            s.ingest([r1_later], now=1100)
            b_suppressed = s.prepare('triage', now=1101)
            self.assertFalse(b_suppressed['wakeAgent'])

            # After cooldown: new occurrence triggers cooldown reconsideration
            r1_much_later = event(20, text='2026-09-16 12:06:00 ERROR agent.core: disk full')
            s.ingest([r1_much_later], now=1400)
            b_reopen = s.prepare('triage', now=1401)
            self.assertTrue(b_reopen['wakeAgent'])
            self.assertEqual(b_reopen['items'][0]['id'], gid)
            self.assertEqual(b_reopen['items'][0]['evidence_rows'], 3)
            s.complete(b_reopen['batch'], [{'id': gid, 'decision': 'watch', 'reason': 'Still observing after cooldown period.'}], now=1402)

            # Changed severity creates a distinct finding immediately, not suppressed
            r_crit = event(30, text='2026-09-16 12:07:00 ERROR agent.core: disk full')
            r_crit['astra.severity']['label'] = 'needs-attention'
            s.ingest([r_crit], now=1405)
            b_crit = s.prepare('triage', now=1406)
            self.assertTrue(b_crit['wakeAgent'])
            self.assertNotEqual(b_crit['items'][0]['id'], gid)
            self.assertEqual(b_crit['items'][0]['severity'], 'needs-attention')
            s.close()

    def test_bounded_evidence_retrieval_resolves_file_and_journald_sources(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log_file = td_path / "agent.log"
            content = "PREAMBLE\n2026-09-16 12:00:00 ERROR [db_pool] connection reset by peer\nPOSTAMBLE\n"
            log_file.write_text(content)

            byte_start = content.index("2026-09-16")
            byte_end = content.index("\nPOSTAMBLE")

            s = self.store(td_path / "review.sqlite")
            r_file = {
                'record_id': 'f1', 'host': 'test', 'profile': 'default',
                'event_ts': '2026-09-16T12:00:00Z', 'source_path': str(log_file),
                'byte_start': byte_start, 'byte_end': byte_end,
                'text': 'connection reset by peer'
            }
            enriched_file = enrich_record(r_file, classify_event(r_file), SeverityPolicy.default(), str(log_file), byte_start, byte_end)
            s.ingest([enriched_file], now=1000)

            b = s.prepare('triage', now=1000)
            gid = b['items'][0]['id']

            # Lookup by finding ID
            ev = s.get_evidence(gid, max_bytes=100)
            self.assertEqual(ev['status'], 'exact_source')
            self.assertIn("connection reset by peer", ev['text'])
            self.assertFalse(ev['truncated'])

            # Bounded retrieval respects max_bytes
            ev_bounded = s.get_evidence(gid, max_bytes=10)
            self.assertLessEqual(len(ev_bounded['text']), 10)
            self.assertTrue(ev_bounded['truncated'])

            # Complete the first batch so second batch can be prepared
            s.complete(b['batch'], [{'id': gid, 'decision': 'watch', 'reason': 'DB pool connection reset observation.'}], now=1000)

            # Journald entry resolution with cursor metadata
            r_journal = {
                'record_id': 'j1', 'host': 'test', 'profile': 'default',
                'event_ts': '2026-09-16T12:01:00Z', 'source_path': 'journald',
                'byte_start': 0, 'byte_end': 0,
                'journal': {'__CURSOR': 's=abc123cursor', '_BOOT_ID': 'boot456', '_SYSTEMD_UNIT': 'hermes.service'},
                'text': '2026-09-16 12:01:00 WARNING systemd: Unit failed'
            }
            enriched_journal = enrich_record(r_journal, classify_event(r_journal), SeverityPolicy.default(), 'journald', 0, 0)
            s.ingest([enriched_journal], now=1001)
            b2 = s.prepare('triage', now=1001)
            gid2 = b2['items'][0]['id']

            ev_j = s.get_evidence(gid2)
            self.assertEqual(ev_j['status'], 'journald_entry')
            self.assertEqual(ev_j['journal']['__CURSOR'], 's=abc123cursor')
            self.assertEqual(ev_j['journal']['_BOOT_ID'], 'boot456')
            s.close()

    def test_rca_complete_validates_contract_and_persists_archive_and_shared_copy(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            s = self.store(td_path / "review.sqlite")
            shared_dir = td_path / "shared_reports"
            shared_dir.mkdir()
            archive_dir = td_path / "archive_reports"
            archive_dir.mkdir()

            r = event(1, text='2026-09-16 12:00:00 ERROR database disk image is malformed')
            s.ingest([r], now=1000)

            # Triage escalates finding to RCA
            b_triage = s.prepare('triage', now=1000)
            gid = b_triage['items'][0]['id']
            s.complete(b_triage['batch'], [{'id': gid, 'decision': 'rca', 'reason': 'Database corruption detected, requires root cause analysis.'}], now=1001)

            # RCA prepare picks up the finding
            b_rca = s.prepare('rca', now=1002)
            self.assertTrue(b_rca['wakeAgent'])
            self.assertEqual(b_rca['items'][0]['id'], gid)

            # Invalid report fails validation
            invalid_md = "# Quick note\nEverything looks bad."
            with self.assertRaises(ValueError):
                s.complete_rca(b_rca['batch'], invalid_md, archive_dir=archive_dir, shared_dir=shared_dir, now=1003)

            # Valid report adhering to contract
            valid_md = """# Root Cause Analysis: SQLite Corruption

## 1. Observed Impact
Agent was unable to load sessions due to malformed SQLite database image.

## 2. Timeline and Evidence
2026-09-16 12:00:00: First corrupt block observed at offset 4096.

## 3. Facts versus Hypotheses
Fact: File header was zeroed out during ungraceful host shutdown.
Hypothesis: Power loss during write cycle caused write tearing.

## 4. Checks and Results
Executed `PRAGMA integrity_check`: returned 1 corrupt page.

## 5. Root Cause and Confidence
Confidence: High. WAL checkpoint was interrupted by abrupt reboot.

## 6. Precise Proposed Fix
Restore from most recent hourly replica and re-execute migration.

## 7. Risks and Prerequisites
Prerequisite: Stop agent service before restoring file.

## 8. Rollback
Preserve corrupted database as `.corrupt.bak` before restoring.

## 9. Post-Repair Verification
Run `PRAGMA integrity_check` on restored file and verify clean OK.
"""
            res = s.complete_rca(b_rca['batch'], valid_md, archive_dir=archive_dir, shared_dir=shared_dir, now=1004)
            self.assertEqual(res['status'], 'persisted')
            self.assertTrue(res['notification_pending'])
            self.assertTrue(Path(res['report_path']).is_file())
            self.assertTrue(Path(res['shared_path']).is_file())
            self.assertTrue(Path(res['shared_markdown_path']).is_file())
            self.assertEqual(Path(res['report_path']).read_text(), valid_md)
            self.assertEqual(Path(res['shared_markdown_path']).read_text(), valid_md)
            self.assertEqual(Path(res['shared_path']).suffix, '.html')
            html = Path(res['shared_path']).read_text()
            self.assertIn('<!doctype html>', html.lower())
            self.assertIn('Root Cause Analysis: SQLite Corruption', html)

            # Status reflects pending RCA is 0, history has 1 record
            st = s.status(now=1005)
            self.assertEqual(st['pending_rca'], 0)
            self.assertEqual(len(st['active_leases']), 0)

            hist = s.get_history(limit=5)
            self.assertEqual(len(hist['rca_history']), 1)
            self.assertEqual(hist['rca_history'][0]['finding_id'], gid)
            s.close()

    def test_source_identity_verified_in_lookup_and_disambiguates_rotation(self):
        import hashlib
        import os
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log_file = td_path / "agent.log"
            first_line = "2026-09-16 12:00:00 ERROR db: corruption in page 3"
            content = first_line + "\ncontinuation folded line\n"
            log_file.write_text(content)
            st = os.stat(log_file)
            content_hash = hashlib.sha256(first_line.encode()).hexdigest()

            s = self.store(td_path / "review.sqlite")
            base = {
                'record_id': 'f1', 'host': 'test', 'profile': 'default',
                'event_ts': '2026-09-16T12:00:00Z', 'source_path': str(log_file),
                'byte_start': 0, 'byte_end': len(content),
                'text': first_line + "\ncontinuation folded line",
            }

            def enriched(ident):
                r = dict(base)
                if ident is not None:
                    r['source_identity'] = ident
                return enrich_record(r, classify_event(r), SeverityPolicy.default(), str(log_file), 0, len(content))

            identity = {'inode': st.st_ino, 'dev': st.st_dev, 'content_hash': content_hash}
            s.ingest([enriched(identity)], now=1000)
            b = s.prepare('triage', now=1000)
            gid = b['items'][0]['id']

            # Verified identity: inode/dev match and first raw line hash matches.
            ev = s.get_evidence(gid, max_bytes=4096)
            self.assertEqual(ev['status'], 'exact_source')
            self.assertTrue(ev['source_identity']['retained'])
            self.assertTrue(ev['source_identity']['verified'])
            self.assertIsNone(ev['source_identity']['reason'])
            s.complete(b['batch'], [{'id': gid, 'decision': 'watch', 'reason': 'Observed once; monitoring for recurrence.'}], now=1001)

            # Historical row without source_identity reports an explicit limitation.
            r_hist = {
                'record_id': 'f2', 'host': 'test', 'profile': 'default',
                'event_ts': '2026-09-16T12:00:30Z', 'source_path': str(log_file),
                'byte_start': 0, 'byte_end': len(content),
                'text': '2026-09-16 12:00:30 ERROR db: page size mismatches expected 4096',
            }
            s.ingest([enrich_record(r_hist, classify_event(r_hist), SeverityPolicy.default(), str(log_file), 0, len(content))], now=1002)
            b2 = s.prepare('triage', now=1002)
            gid2 = b2['items'][0]['id']
            ev2 = s.get_evidence(gid2)
            self.assertFalse(ev2['source_identity']['retained'])
            self.assertIsNone(ev2['source_identity']['verified'])
            self.assertEqual(ev2['source_identity']['reason'], 'not_retained')
            s.complete(b2['batch'], [{'id': gid2, 'decision': 'watch', 'reason': 'Historical record without identity metadata.'}], now=1003)

            # Rotation: same path/offsets but the file was replaced with new content
            # (new inode and new first-line hash) -> NEW evidence row (eid disambiguation).
            log_file.unlink()
            first_line2 = "2026-09-16 12:00:00 ERROR db: corruption in page 7 after restore"
            content2 = first_line2 + "\ncontinuation folded line\n"
            log_file.write_text(content2)
            st2 = os.stat(log_file)
            hash2 = hashlib.sha256(first_line2.encode()).hexdigest()
            self.assertNotEqual(content_hash, hash2)
            identity2 = {'inode': st2.st_ino, 'dev': st2.st_dev, 'content_hash': hash2}
            r_rot = dict(base)
            r_rot['source_identity'] = identity2
            rot_enriched = enrich_record(r_rot, classify_event(r_rot), SeverityPolicy.default(), str(log_file), 0, len(content))
            self.assertEqual(s.ingest([rot_enriched], now=1004), 1)
            # Replay of the same rotated row stays idempotent.
            self.assertEqual(s.ingest([rot_enriched], now=1005), 0)
            s.close()

    def test_early_triggers_flag_fatal_and_corruption_not_quoted_success_or_429(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')

            def ev(seq, text):
                r = {'record_id': str(seq), 'host': 'test', 'profile': 'default',
                     'event_ts': '2026-09-16T12:00:%02dZ' % seq,
                     'source_path': '/nonexistent/agent.log', 'byte_start': seq * 100,
                     'byte_end': seq * 100 + 90, 'text': text}
                return enrich_record(r, classify_event(r), SeverityPolicy.default(), r['source_path'], r['byte_start'], r['byte_end'])

            # Unreviewed 429: NOT an automatic emergency (explicit counterexample).
            s.ingest([ev(1, '2026-09-16 12:00:01 ERROR api: 429 rate limited by provider')], now=1000)
            early, reason = s.check_early_triggers()
            self.assertFalse(early)

            # Quoted/suppressed error wording: not an emergency (counterexample).
            s.ingest([ev(2, '2026-09-16 12:00:02 INFO watchdog: logged "error" and recovered successfully')], now=1001)
            early, reason = s.check_early_triggers()
            self.assertFalse(early)

            # Explicit database corruption: early trigger with a reason.
            s.ingest([ev(3, '2026-09-16 12:00:03 ERROR sqlite: database disk image is malformed')], now=1002)
            early, reason = s.check_early_triggers()
            self.assertTrue(early)
            self.assertIn('corruption', reason.lower())
            s.close()

    def test_early_trigger_fatal_exception_and_cleared_after_review(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')

            def ev(seq, text):
                r = {'record_id': str(seq), 'host': 'test', 'profile': 'default',
                     'event_ts': '2026-09-16T12:00:%02dZ' % seq,
                     'source_path': '/nonexistent/agent.log', 'byte_start': seq * 100,
                     'byte_end': seq * 100 + 90, 'text': text}
                return enrich_record(r, classify_event(r), SeverityPolicy.default(), r['source_path'], r['byte_start'], r['byte_end'])

            s.ingest([ev(1, '2026-09-16 12:00:01 CRITICAL core: FATAL unhandled exception in main loop')], now=1000)
            early, reason = s.check_early_triggers()
            self.assertTrue(early)
            self.assertIn('fatal', reason.lower())

            # Once the finding is reviewed (decision set), the trigger clears.
            b = s.prepare('triage', now=1001)
            s.complete(b['batch'], [{'id': x['id'], 'decision': 'watch', 'reason': 'Fatal observed; owner notified via triage.'} for x in b['items']], now=1002)
            early, reason = s.check_early_triggers()
            self.assertFalse(early)
            s.close()

    def test_retention_report_surfaces_unsynced_and_missing_sources_and_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            s = self.store(td_path / 'review.sqlite')

            def bucket(name, seq, hour):
                r = {'record_id': str(seq), 'host': 'test', 'profile': 'default',
                     'event_ts': '2026-09-16T%02d:00:00Z' % hour,
                     'source_path': '/nonexistent/agent.log', 'byte_start': seq * 100,
                     'byte_end': seq * 100 + 90, 'text': '2026-09-16 %02d:00:00 ERROR agent.core: overflow %d' % (hour, seq)}
                return r

            b_old = td_path / 'enriched-20260916-00.jsonl'
            b_old.write_text(json.dumps(bucket('a', 1, 8)) + '\n')

            # Bucket exists but was never synced: processing fell behind; must be
            # reported, not silently ignored (it ages out of 72h retention).
            rep = s.retention_report(td_path, now='2026-09-16T14:00:00Z')
            self.assertTrue(rep['gap_detected'])
            self.assertIn('enriched-20260916-00.jsonl', rep['unsynced_files'])

            # After sync the gap clears.
            s.sync_sources(td_path)
            rep2 = s.retention_report(td_path, now='2026-09-16T14:00:00Z')
            self.assertFalse(rep2['gap_detected'])
            self.assertEqual(rep2['unsynced_files'], [])

            # A source recorded earlier but since deleted (retention/rotation) is
            # reported explicitly; DB retains the ingested copy.
            b_new = td_path / 'enriched-20260916-12.jsonl'
            b_new.write_text(json.dumps(bucket('b', 2, 13)) + '\n')
            s.sync_sources(td_path)
            b_new.unlink()
            rep3 = s.retention_report(td_path, now='2026-09-16T14:00:00Z')
            self.assertIn('enriched-20260916-12.jsonl', rep3['missing_sources'])
            self.assertFalse(rep3['gap_detected'])
            s.close()

    def test_prune_expires_old_evidence_without_silently_dropping_unreviewed(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')

            def ev(seq, ts):
                r = {'record_id': str(seq), 'host': 'test', 'profile': 'default',
                     'event_ts': ts, 'source_path': '/nonexistent/agent.log',
                     'byte_start': seq * 100, 'byte_end': seq * 100 + 90,
                     'text': '%s ERROR agent.core: unique failure %d' % (ts, seq)}
                return enrich_record(r, classify_event(r), SeverityPolicy.default(), r['source_path'], r['byte_start'], r['byte_end'])

            s.ingest([ev(1, '2026-09-13T10:00:00Z')], now=1000)  # unreviewed, old
            s.ingest([ev(2, '2026-09-16T10:00:00Z')], now=1001)  # unreviewed, fresh

            res = s.prune_evidence(now='2026-09-16T14:00:00Z')
            self.assertEqual(res['expired_rows'], 1)

            # Unreviewed finding whose only evidence expired is EXPLICITLY marked,
            # never silently dropped.
            row = s.db.execute("SELECT decision FROM findings ORDER BY rowid").fetchall()
            self.assertEqual(row[0]['decision'], 'evidence_expired')
            self.assertEqual(row[1]['decision'], None)
            st = s.status(now=1002)
            self.assertEqual(st['findings_evidence_expired'], 1)
            self.assertEqual(st['pending_triage'], 1)
            s.close()

    DISCORD_FATAL = (
        '2026-09-17 05:04:49,032 ERROR gateway.run: Fatal discord adapter error '
        '(discord_websocket_health_stale): Discord Gateway WebSocket health check failed: socket_closed'
    )
    RCA_MD = """# Root Cause Analysis: fixture

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

    def test_fatal_discord_adapter_reconnect_is_not_an_early_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')
            r = event(1, text=self.DISCORD_FATAL, profile='astra-dell-health-monitor')
            s.ingest([r], now=1000)
            early, reason = s.check_early_triggers()
            self.assertFalse(early)
            self.assertIsNone(reason)
            st = s.status(now=1000)
            self.assertFalse(st['dispatch_triage_early'])
            s.close()

    def test_fatal_telegram_adapter_reconnect_is_not_an_early_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')
            text = (
                '2026-09-17 05:12:08,447 ERROR gateway.run: Fatal telegram adapter error '
                '(telegram_network_error): Telegram updater.stop() did not finish before '
                'the network-recovery deadline; rebuilding the adapter instead of reusing '
                'an Updater whose lifecycle lock may still be held.'
            )
            r = event(1, text=text, profile='default')
            s.ingest([r], now=1000)
            early, reason = s.check_early_triggers()
            self.assertFalse(early, reason)
            self.assertIsNone(reason)
            s.close()

    def test_completed_rca_does_not_cover_other_findings_with_broad_labels(self):
        with tempfile.TemporaryDirectory() as td, closing(self.store(Path(td) / 'review.sqlite')) as s:
            rumi = event(1, text=self.DISCORD_FATAL, profile='rumi')
            eva = event(2, text=self.DISCORD_FATAL, profile='eva')
            corrupt = event(3, text='2026-09-16 12:00:03 ERROR sqlite: database disk image is malformed', profile='eva')
            s.ingest([rumi, eva, corrupt], now=1000)
            b = s.prepare('triage', now=1001, max_bytes=24000)
            s.complete(
                b['batch'],
                [{'id': x['id'], 'decision': 'rca', 'reason': 'Actionable failure requires a bounded investigation.'} for x in b['items']],
                now=1002,
            )
            all_ids = {x['id'] for x in b['items']}
            first = self.prepare_single_member_rca(s, all_ids, now=1003)
            first_id = first['items'][0]['id']
            s.complete_rca(first['batch'], self.RCA_MD, archive_dir=Path(td) / 'a', shared_dir=Path(td) / 's', now=1004)

            done = {r['id'] for r in s.db.execute(
                "SELECT id FROM findings WHERE decision='rca' AND rca_done=1"
            )}
            pending = {r['id'] for r in s.db.execute(
                "SELECT id FROM findings WHERE decision='rca' AND rca_done=0"
            )}
            self.assertEqual(done, {first_id})
            self.assertEqual(pending, all_ids - {first_id})
            self.assertTrue(s.status(now=1005)['dispatch_rca_a'])

            reviewed = {first_id}
            now = 1006
            while pending:
                batch = self.prepare_single_member_rca(s, pending, now=now)
                self.assertTrue(batch['wakeAgent'])
                current = batch['items'][0]['id']
                self.assertNotIn(current, reviewed)
                s.complete_rca(batch['batch'], self.RCA_MD, archive_dir=Path(td) / 'a', shared_dir=Path(td) / 's', now=now + 1)
                reviewed.add(current)
                pending.remove(current)
                now += 2
            self.assertEqual(reviewed, all_ids)
            self.assertEqual(s.status(now=now)['pending_rca'], 0)
            self.assertFalse(s.status(now=now)['dispatch_rca_a'])

    def test_pending_rca_is_not_reset_to_triage_by_later_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.store(Path(td) / 'review.sqlite')
            first = event(1, text=self.DISCORD_FATAL, profile='rumi')
            s.ingest([first], now=1000)
            b = s.prepare('triage', now=1001)
            gid = b['items'][0]['id']
            s.complete(b['batch'], [{'id': gid, 'decision': 'rca', 'reason': 'Websocket failure needs a single investigation.'}], now=1002)
            later = event(2, text=self.DISCORD_FATAL, profile='rumi')
            later['byte_start'] = 9000
            later['byte_end'] = 9090
            s.ingest([later], now=1002 + 4000)
            row = s.db.execute('SELECT decision, rca_done FROM findings WHERE id=?', (gid,)).fetchone()
            self.assertEqual(row['decision'], 'rca')
            self.assertEqual(row['rca_done'], 0)
            s.close()
