import json,tempfile,unittest
from pathlib import Path
from astra.review import ReviewStore,packed
from tests.test_review import event,TestReviewStore as Fixture
REPORT=Fixture.RCA_MD
del Fixture

class RepairIdentity(unittest.TestCase):
    def test_legacy_alias_coverage_preserves_actual_lane_history(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite'); self.addCleanup(s.close)
            row=event(1,'2026-09-16 12:00:00 WARNING [cron_abc123_20260916_120000] agent.tool_executor: Tool terminal returned error: validation failed')
            s.ingest([row],now=1000);t=s.prepare(now=1001);gid=t['items'][0]['id']
            s.complete(t['batch'],[{'id':gid,'decision':'rca','reason':'Actual bounded validation failure.'}],now=1002)
            a=s.prepare('rca-a',now=1003);done=s.complete_rca(a['batch'],REPORT,Path(td)/'archive-a',Path(td)/'shared-a',now=1004)
            # Model a retained pre-normalization fragment without rewriting evidence.
            old=event(2,row['text'].replace('120000','130000'))
            with s.db:
                s.db.execute('INSERT INTO evidence VALUES(?,?,?,?)',('old-eid','old-gid',old['ts'],packed(old)))
                s.db.execute("INSERT INTO findings(id,decision,reason,reviewed_at) VALUES('old-gid','rca','Prior approved duplicate fragment',1002)")
            self.assertFalse(s.prepare('rca-a',now=1005)['wakeAgent'])
            flags=s.db.execute("SELECT rca_a_done,rca_b_done FROM findings WHERE id='old-gid'").fetchone()
            self.assertEqual(tuple(flags),(0,0));self.assertIsNotNone(s.coverage.for_finding('old-gid'))
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM rca_history').fetchone()[0],1)
            self.assertEqual(s.db.execute("SELECT row_json FROM evidence WHERE id='old-eid'").fetchone()[0],packed(old))
            self.assertFalse(s.prepare('rca-b',now=1006)['wakeAgent'])
            # New recurrence joins a prior gid, preserving history and occurrence count.
            third=event(3,row['text'].replace('120000','140000'))
            s.ingest([third],now=1007)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM findings').fetchone()[0],2)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],3)
            changed=event(4,row['text'].replace('validation failed','database disk image is malformed'))
            s.ingest([changed],now=1008)
            self.assertTrue(s.prepare('triage',now=1009)['wakeAgent'])

    def test_unpublished_history_is_covered_but_other_profile_is_not(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite');self.addCleanup(s.close)
            row=event(1,'WARNING [cron_abc_20260916_120000] agent.tool_executor: Tool terminal returned error: failed')
            s.ingest([row],now=1000);t=s.prepare(now=1001);gid=t['items'][0]['id']
            s.complete(t['batch'],[{'id':gid,'decision':'rca','reason':'Actual bounded validation failure.'}],now=1002)
            a=s.prepare('rca-a',now=1003);s.complete_rca(a['batch'],REPORT,now=1004)
            with s.db:
                s.db.execute('INSERT INTO evidence VALUES(?,?,?,?)',('alias-eid','alias',row['ts'],packed(row)))
                s.db.execute("INSERT INTO findings(id,decision) VALUES('alias','rca')")
            self.assertFalse(s.prepare('rca-a',now=1005)['wakeAgent'])
            self.assertTrue(s.coverage.for_finding('alias')['delivery_recovery_needed'])
            other=event(3,row['text'],profile='another-agent');s.ingest([other],now=1006)
            self.assertTrue(s.prepare('triage',now=1007)['wakeAgent'])

    def test_latest_evidence_preserves_timestamp_id_order_and_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite');self.addCleanup(s.close)
            def add(eid, gid, ts, text):
                row=event(1,text);row['ts']=row['event_ts']=ts
                with s.db:
                    s.db.execute('INSERT OR IGNORE INTO findings(id) VALUES(?)',(gid,))
                    s.db.execute('INSERT INTO evidence VALUES(?,?,?,?)',(eid,gid,ts,packed(row)))
            cron='WARNING [cron_abc_20260916_120000] validation failed'
            add('z','first','2026-09-16T00:00:00Z',cron)
            add('a','first','2026-09-17T00:00:00Z',cron+' newer')
            add('b','first','2026-09-17T00:00:00Z',cron+' tie winner')
            add('c','second','2026-09-17T00:00:00Z',cron)
            def reference():
                groups={}
                for f in s.db.execute("SELECT f.*,e.row_json FROM findings f JOIN evidence e ON e.id=(SELECT id FROM evidence WHERE gid=f.id ORDER BY ts DESC,id DESC LIMIT 1) ORDER BY f.rowid"):
                    groups.setdefault(s._canonical_key(json.loads(f['row_json'])),[]).append(dict(f))
                return groups
            self.assertEqual(s._canonical_groups(),reference())
            add('d','second','2026-09-18T00:00:00Z',cron+' changed')
            self.assertEqual(s._canonical_groups(),reference())
            with s.db:s.db.execute("UPDATE findings SET decision='watch',rca_b_done=1 WHERE id='first'")
            self.assertEqual(s._canonical_groups(),reference())

    def test_duplicate_ingest_keeps_evidence_and_skips_identity_rebuild(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            s=ReviewStore(Path(td)/'review.sqlite');self.addCleanup(s.close)
            row=event(1,'WARNING [cron_abc_20260916_120000] validation failed')
            s.ingest([row],now=1000)
            before=[tuple(x) for x in s.db.execute('SELECT * FROM evidence')]
            with patch.object(s,'_canonical_groups',side_effect=AssertionError('duplicate rebuilt history')):
                self.assertEqual(s.ingest([row],now=1001),0)
            self.assertEqual([tuple(x) for x in s.db.execute('SELECT * FROM evidence')],before)
