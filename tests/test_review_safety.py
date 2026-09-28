"""Regression checks for trustworthy evidence retrieval."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from astra.review import ReviewStore
from tests.test_review import event

class ReviewSafety(unittest.TestCase):
    def test_triage_prioritizes_serious_then_newest_not_old_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite')
            old=event(1,profile='old');new=event(2,profile='new');fatal=event(3,text='FATAL unhandled exception in fixture',profile='fatal')
            old['event_ts']='2026-09-15T12:00:00Z';new['event_ts']='2026-09-17T12:00:00Z';fatal['event_ts']='2026-09-16T12:00:00Z'
            s.ingest([old,new,fatal]);b=s.prepare()
            self.assertEqual([i['agent'] for i in b['items']],['fatal','new','old'])
            s.close()

    def test_unchanged_recurrence_retains_completed_rca_after_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite');s.ingest([event(1)],now=1000);b=s.prepare(now=1000)
            gid=b['items'][0]['id'];s.complete(b['batch'],[{'id':gid,'decision':'rca','reason':'Previous investigation was completed; retain evidence.'}],now=1001)
            from tests.test_review import TestReviewStore
            rca=s.prepare('rca-a',now=1002)
            s.complete_rca(rca['batch'],TestReviewStore.RCA_MD,now=1003)
            s.ingest([event(2)],now=5000)
            self.assertFalse(s.prepare(now=5000)['wakeAgent'])
            self.assertEqual(s._item(gid)['evidence_rows'],2)
            self.assertEqual(s.status(now=5001)['pending_rca'],0)
            changed=event(3);changed['astra.severity']['label']='needs-attention'
            s.ingest([changed],now=5002)
            self.assertTrue(s.prepare(now=5002)['wakeAgent'])
            s.close()

    def test_expired_rca_evidence_is_visible_not_endless_retry(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite');s.ingest([event(1)]);b=s.prepare(now=1000)
            s.complete(b['batch'],[{'id':b['items'][0]['id'],'decision':'rca','reason':'Needs actual investigation of this configuration failure.'}],now=1001)
            s.prepare('rca',now=1002)
            s.prune_evidence(now='2026-09-20T12:00:00Z')
            status=s.status(now=2000)
            self.assertEqual(status['pending_rca'],0)
            self.assertEqual(status['findings_evidence_expired'],1)
            self.assertEqual(status['blocked_batches'],1)
            s.close()

    def test_source_hash_is_checked_at_record_offset_not_file_start(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);raw=root/'agent.log';prefix=b'older unrelated line\n';body=b'actual error\n';raw.write_bytes(prefix+body)
            st=raw.stat();r=event(1);r.update(source_path=str(raw),byte_start=len(prefix),byte_end=len(prefix+body),text='actual error')
            r['astra.provenance']={'source_path':str(raw),'byte_start':len(prefix),'byte_end':len(prefix+body)}
            r['source_identity']={'inode':st.st_ino,'dev':st.st_dev,'content_hash':hashlib.sha256(body.rstrip(b'\n')).hexdigest()}
            s=ReviewStore(root/'state.sqlite');s.ingest([r]);gid=s.prepare()['items'][0]['id']
            result=s.get_evidence(gid)
            self.assertEqual(result['status'],'exact_source');self.assertTrue(result['source_identity']['verified'])
            s.close()

    def test_malformed_bucket_is_not_silently_acknowledged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);s=ReviewStore(root/'state.sqlite')
            (root/'enriched-20260916-00.jsonl').write_text('{malformed}\n')
            with self.assertRaises(ValueError):s.sync_sources(root)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0],0)
            s.close()

    def test_evidence_budget_rejects_unbounded_reads(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'state.sqlite');s.ingest([event(1)])
            gid=s.prepare()['items'][0]['id']
            for budget in (-1,0,131073):
                with self.subTest(budget=budget), self.assertRaises(ValueError):
                    s.get_evidence(gid,max_bytes=budget)
            s.close()

    def test_identity_mismatch_never_returns_replacement_as_exact_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); raw=root/'agent.log'; raw.write_text('original error\n')
            st=raw.stat(); r=event(1); r.update(source_path=str(raw),byte_start=0,byte_end=15,text='original error')
            r['astra.provenance']={'source_path':str(raw),'byte_start':0,'byte_end':15}
            r['source_identity']={'inode':st.st_ino,'dev':st.st_dev,'content_hash':hashlib.sha256(b'original error').hexdigest()}
            s=ReviewStore(root/'state.sqlite');s.ingest([r]);gid=s.prepare()['items'][0]['id']
            raw.write_text('replacement private data\n')
            result=s.get_evidence(gid)
            self.assertEqual(result['status'],'rotated_or_truncated')
            self.assertEqual(result['text'],'original error')
            self.assertFalse(result['source_identity']['verified'])
            s.close()
