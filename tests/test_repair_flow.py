"""Focused repair regressions; all writes use isolated temporary stores."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from astra.review import ReviewStore
from astra.runtime import prepare_job, reconcile_results
from tests.test_review import event

class RepairFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ReviewStore(self.root/'review.sqlite')
        self.addCleanup(self.store.close)

    def triage(self, n=1):
        self.store.ingest([event(i, profile=f'fixture-{i}') for i in range(n)], now=1000)
        return self.store.prepare('triage', now=1001)

    def approved(self, n=1):
        triage = self.triage(n)
        self.store.complete(triage['batch'], [self.result(x['id'], 'rca') for x in triage['items']], now=1002)
        return triage

    @staticmethod
    def result(gid, decision='watch'):
        return {'id':gid, 'decision':decision, 'reason':'Retained evidence needs bounded review.'}

    def batch(self, bid):
        return dict(self.store.db.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone())

    def block(self, bid):
        with self.store.db:
            self.store.db.execute("UPDATE batches SET status='blocked', attempts=2 WHERE id=?",(bid,))

    def job(self, role='rca', stamp='1970-01-01T00:17:00+00:00', status='ok'):
        return {role: {'id':'fixture-job', 'last_run_at':stamp, 'last_status':status, 'state':'completed'}}

    def test_blocked_lane_does_not_freeze_unrelated_approved_backlog(self):
        self.approved(3)
        blocked = self.store.prepare('rca-a', now=1003)
        self.block(blocked['batch'])
        self.assertTrue(self.store.status(now=1004)['dispatch_rca_a'])
        work = self.store.prepare('rca-a', now=1005)
        self.assertNotEqual(work['items'][0]['id'], blocked['items'][0]['id'])
        self.assertEqual(self.batch(blocked['batch'])['status'], 'blocked')
        self.assertTrue(self.store.status(now=1006)['dispatch_rca_b'])
        # Comparison is already enabled for these findings in this isolated fixture.
        # Enable/disable gating is separate; either frozen A packet may be selected.
        before_blocked = self.batch(blocked['batch'])
        before_work = self.batch(work['batch'])
        other = self.store.prepare('rca-b', now=1007)
        self.assertTrue(other['wakeAgent'])
        self.assertNotIn(other['batch'], {blocked['batch'], work['batch']})
        self.assertIn(other['items'], [blocked['items'], work['items']])
        self.assertEqual(self.batch(blocked['batch']), before_blocked)
        self.assertEqual(self.batch(work['batch']), before_work)

    def test_expired_retry_keeps_batch_then_blocks_after_two_attempts(self):
        self.approved()
        first=self.store.prepare('rca-a', now=1003)
        self.assertTrue(self.store.status(now=1604)['dispatch_rca_a'])
        second=self.store.prepare('rca-a', now=1604)
        self.assertEqual(second['batch'], first['batch'])
        self.assertEqual(self.batch(first['batch'])['attempts'],2)
        with self.assertRaisesRegex(RuntimeError, 'two attempts'):
            self.store.prepare('rca-a', now=2205)
        self.assertEqual(self.batch(first['batch'])['status'],'blocked')
        self.assertFalse(self.store.status(now=2206)['dispatch_rca_a'])
        self.assertFalse(self.store.prepare('rca-a',now=2207)['wakeAgent'])

    def test_explicit_recovery_only_releases_retained_undecided_ids_once(self):
        t=self.triage(3)
        gids=[x['id'] for x in t['items']]
        with self.store.db:
            self.store.db.execute("UPDATE findings SET decision='watch' WHERE id=?",(gids[1],))
            self.store.db.execute('DELETE FROM evidence WHERE gid=?',(gids[2],))
        self.block(t['batch'])
        recovered=self.store.recover_blocked_batch(t['batch'],'Corrected root cause with owner approval.',now=1004)
        self.assertEqual(recovered['eligible'],[gids[0]])
        self.assertEqual(len(recovered['retained']),2)
        self.assertEqual(json.loads(self.batch(t['batch'])['ids_json']),gids)
        with self.assertRaises(ValueError):
            self.store.recover_blocked_batch(t['batch'],'No repeated reset of same batch.',now=1005)
        work=self.store.prepare('triage',now=1006)
        self.assertEqual([x['id'] for x in work['items']],[gids[0]])
        self.assertEqual(self.batch(work['batch'])['attempts'],1)

    def test_lane_b_recovery_preserves_lane_a_completion(self):
        self.approved()
        b=self.store.prepare('rca-b',now=1003)
        gid=b['items'][0]['id']
        with self.store.db:self.store.db.execute('UPDATE findings SET rca_done=1,rca_a_done=1 WHERE id=?',(gid,))
        self.block(b['batch'])
        recovered=self.store.recover_blocked_batch(b['batch'],'Owner approved remaining independent lane.',now=1004)
        self.assertEqual(recovered['eligible'],[gid])
        self.assertEqual(self.store.db.execute('SELECT rca_a_done FROM findings WHERE id=?',(gid,)).fetchone()[0],1)
        self.assertEqual(self.store.prepare('rca-b',now=1005)['items'][0]['id'],gid)

    def test_missing_extra_duplicate_ids_report_correction_without_writes(self):
        t=self.triage(2);gids=[x['id'] for x in t['items']]
        cases=[([self.result(gids[0])], 'missing'),
               ([self.result(gids[0]),self.result('foreign')],'extra'),
               ([self.result(gids[0]),self.result(gids[0])],'duplicates')]
        for results,label in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError,label):
                self.store.complete(t['batch'],results,now=1002)
        self.assertEqual(self.batch(t['batch'])['status'],'running')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM findings WHERE decision IS NULL').fetchone()[0],2)
        self.assertEqual(self.store.complete(t['batch'],[self.result(x) for x in gids])['status'],'reviewed')

    def test_malformed_result_types_are_validation_errors(self):
        t=self.triage();gid=t['items'][0]['id']
        malformed=[None,{},'not array',[None],[{'id':[]}],
                   [dict(self.result(gid),decision=[])],
                   [dict(self.result(gid),reason={})]]
        for result in malformed:
            with self.subTest(result=result),self.assertRaises(ValueError):
                self.store.complete(t['batch'],result,now=1002)
        self.assertEqual(self.batch(t['batch'])['status'],'running')

    def test_alias_reconciliation_releases_failed_first_lease(self):
        self.approved();b=self.store.prepare('rca',now=1003)
        notices=reconcile_results(self.store,self.job(status='error'),now=1021)
        self.assertEqual(len(notices),1)
        self.assertEqual(self.batch(b['batch'])['lease_until'],1021)
        self.assertEqual(self.store.prepare('rca-a',now=1022)['batch'],b['batch'])

    def test_invalid_response_retries_once_and_reconciliation_is_idempotent(self):
        self.approved();b=self.store.prepare('rca',now=1003)
        with patch('astra.runtime._recover_rca_response',return_value='invalid'):
            self.assertEqual(len(reconcile_results(self.store,self.job(),now=1021)),1)
            self.assertEqual(self.batch(b['batch'])['attempts'],1)
            self.assertEqual(self.batch(b['batch'])['status'],'running')
            self.assertEqual(reconcile_results(self.store,self.job(),now=1022),[])
            self.store.prepare('rca-a',now=1023)
            before=self.batch(b['batch'])
            self.assertEqual(reconcile_results(self.store,self.job(),now=1024),[])
            self.assertEqual(self.batch(b['batch']),before)
            later=self.job(stamp='1970-01-01T00:18:00+00:00')
            self.assertEqual(len(reconcile_results(self.store,later,now=1081)),1)
            self.assertEqual(self.batch(b['batch'])['status'],'blocked')
            self.assertEqual(self.batch(b['batch'])['attempts'],2)
            self.assertEqual(reconcile_results(self.store,later,now=1082),[])

    def test_template_copies_ids_but_remains_invalid_until_filled(self):
        self.store.ingest([event(1)],now=1000)
        manifest={'root':str(self.root),'shared':str(self.root/'shared'),'host':'fixture'}
        packet=prepare_job('triage',manifest,self.store,prune=False)
        template=json.loads(Path(packet['result_template']).read_text())
        self.assertEqual([x['id'] for x in template],[x['id'] for x in packet['items']])
        with self.assertRaises(ValueError):self.store.complete(packet['batch'],template)
        for x in template:x.update(decision='watch',reason='Known recurrence retained for comparison.')
        self.assertEqual(self.store.complete(packet['batch'],template)['status'],'reviewed')

    def test_recovery_does_not_reuse_output_from_previous_lease(self):
        import os
        from astra.runtime import _recover_rca_response
        home=self.root/'hermes';out=home/'cron'/'output'/'job';out.mkdir(parents=True)
        result=out/'old.md';result.write_text('## Prompt\nbatch-1\n## Response\nold invalid output')
        os.utime(result,(1000,1000))
        with patch.dict(os.environ,{'HERMES_HOME':str(home)}):
            self.assertIsNone(_recover_rca_response('batch-1','job',not_before=1001))
            self.assertEqual(_recover_rca_response('batch-1','job',not_before=999),'old invalid output')

    def test_recovery_does_not_release_expired_members_of_mixed_batch(self):
        old=event(1,profile='old');old['ts']=old['event_ts']='1970-01-01T00:00:00Z'
        fresh=event(2,profile='fresh')
        self.store.ingest([old,fresh],now=1000)
        batch=self.store.prepare('triage',now=1001)
        with self.store.db:
            self.store.db.execute("UPDATE batches SET status='blocked' WHERE id=?",(batch['batch'],))
        result=self.store.recover_blocked_batch(batch['batch'],'Isolated recovery after repairing validation',now=400000)
        self.assertEqual(len(result['eligible']),1)
        self.assertEqual(len(result['retained']),1)
        next_batch=self.store.prepare('triage',now=400001)
        self.assertEqual([x['agent'] for x in next_batch['items']],['fresh'])

    def test_lane_b_skips_six_pruned_lane_a_findings_without_waiving_lane_b(self):
        self.approved(7)
        gids=[r[0] for r in self.store.db.execute('SELECT id FROM findings ORDER BY rowid')]
        expired=gids[:6];retained=gids[6]
        # Retention has already removed evidence; completed A state must survive.
        with self.store.db:
            for gid in expired:
                self.store.db.execute('UPDATE findings SET rca_done=1,rca_a_done=1 WHERE id=?',(gid,))
                self.store.db.execute('DELETE FROM evidence WHERE gid=?',(gid,))
        before={gid:tuple(self.store.db.execute('SELECT * FROM findings WHERE id=?',(gid,)).fetchone()) for gid in expired}
        packet=self.store.prepare('rca-b',now=1003)
        self.assertEqual([x['id'] for x in packet['items']],[retained])
        after={gid:tuple(self.store.db.execute('SELECT * FROM findings WHERE id=?',(gid,)).fetchone()) for gid in expired}
        self.assertEqual(before,after)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],1)

if __name__=='__main__':unittest.main()
