"""Isolated coverage contract: completion is provenance, never another lane's work."""
import json
import tempfile
import unittest
from pathlib import Path
from astra.review import ReviewStore, packed
from tests.test_review import event, TestReviewStore as Reports
REPORT=Reports.RCA_MD
del Reports

class InvestigationCoverage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'state.sqlite'
        self.s=ReviewStore(self.path);self.addCleanup(lambda:self.s.close())

    def add(self,gid,text='ERROR [cron_job1_20260916_120000] provider: request rejected on /data/jobs.json',**scope):
        row=event(1,text)
        row['astra.classification'].update(scope)
        with self.s.db:
            self.s.db.execute('INSERT OR IGNORE INTO findings(id,decision) VALUES(?,\'rca\')',(gid,))
            self.s.db.execute('INSERT INTO evidence VALUES(?,?,?,?)',('e-'+gid,gid,row['ts'],packed(row)))
        return row

    def finish(self,gid='original',report=REPORT):
        self.add(gid)
        packet=self.s.prepare('rca-a',now=1000)
        self.assertEqual(packet['items'][0]['id'],gid)
        self.s.complete_rca(packet['batch'],report,self.root/'archive',self.root/'shared',now=1001)
        return self.s.db.execute('SELECT max(id) FROM rca_history').fetchone()[0]

    def flags(self):
        return [tuple(r) for r in self.s.db.execute('SELECT id,rca_done,rca_a_done,rca_b_done FROM findings ORDER BY id')]

    def test_recurrence_other_id_both_lanes_and_pruning(self):
        hid=self.finish();self.add('alias', 'ERROR [cron_job1_20260917_140000] provider: request rejected on /data/jobs.json')
        before=self.flags()
        for role in ('rca-a','rca-b'):
            self.assertFalse(self.s.prepare(role,now=1010)['wakeAgent'])
        self.assertEqual(self.s.coverage.for_finding('alias')['history_id'],hid)
        self.assertIn('alias',self.s.cover_equivalent_rca('original'))
        with self.s.db:self.s.db.execute("DELETE FROM evidence WHERE gid='original'")
        self.s.close();self.s=ReviewStore(self.path)
        self.add('later','ERROR [cron_job1_20260918_140000] provider: request rejected on /data/jobs.json')
        self.assertFalse(self.s.prepare('rca-b',now=1011)['wakeAgent'])
        self.assertEqual(self.s.coverage.for_finding('later')['history_id'],hid)
        self.assertEqual(self.flags()[:1],before[:1])
        self.assertEqual(tuple(self.s.db.execute("SELECT rca_a_done,rca_b_done FROM findings WHERE id='later'").fetchone()),(0,0))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM rca_history').fetchone()[0],1)

    def test_scope_pair_and_resource_are_not_suppressed(self):
        self.finish()
        self.add('resource','ERROR [cron_job1_20260916_120000] provider: request rejected on /data/other.json')
        self.add('pair',provider='another-provider',model='another-model')
        state=self.s.coverage.refresh()
        for gid in ('resource','pair'):
            self.assertNotIn(gid,state['covered']);self.assertNotIn(gid,state['uncertain'])
        self.assertTrue(self.s.status(now=1002)['dispatch_rca_b'])

    def test_material_impact_reopens_with_reason_and_keeps_history(self):
        self.finish();before=self.flags()
        row=json.loads(self.s.db.execute("SELECT row_json FROM evidence WHERE gid='original'").fetchone()[0]);row['astra.impact']={'lost_jobs':3}
        with self.s.db:self.s.db.execute("UPDATE evidence SET row_json=? WHERE gid='original'",(packed(row),))
        self.assertNotIn('original',self.s.coverage.refresh()['covered'])
        reasons=[json.loads(r[0]) for r in self.s.db.execute('SELECT reopen_json FROM rca_coverage WHERE reopened_at IS NOT NULL')]
        self.assertIn('impact',reasons[0]['reason'])
        self.assertEqual(self.flags(),before)
        self.assertTrue(self.s.prepare('rca-b',now=1002)['wakeAgent'])

    def test_new_discriminator_required_for_related_uncertainty(self):
        hid=self.finish();self.add('changed','ERROR [cron_job1_20260916_120000] provider: request rejected on /data/jobs.json and durable queue lost 3 work items')
        self.assertFalse(self.s.prepare('rca-b',now=1002)['wakeAgent'])
        t=self.s.prepare('triage',now=1003)
        self.assertEqual(t['items'][0]['coverage_candidates'][0]['history_id'],hid)
        result={'id':'changed','decision':'rca','reason':'New persisted queue loss warrants a bounded investigation.'}
        with self.assertRaisesRegex(ValueError,'discriminating'):
            self.s.complete(t['batch'],[result],now=1004)
        result['reopen']={'history_id':hid,'reason':'New queue loss changes the previously bounded failure impact.', 'evidence_id':'e-changed','quote':'durable queue lost 3 work items'}
        self.s.complete(t['batch'],[result],now=1005)
        self.assertTrue(self.s.prepare('rca-b',now=1006)['wakeAgent'])
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM rca_coverage WHERE finding_id='changed' AND reopened_at IS NOT NULL").fetchone()[0],1)

    def test_report_backed_relation_is_scoped_and_idempotent(self):
        quote='This is the same mechanism as finding original, rather than a new failure.'
        hid=self.finish(report=REPORT+'\n'+quote)
        self.add('related','ERROR [cron_job1_20260916_120000] provider: wrapper layer failed on /data/jobs.json')
        # Citation must name the other finding. Here the related report points back.
        path=self.root/'relationship.md';path.write_text(REPORT+'\n'+quote)
        with self.s.db:
            self.s.db.execute('INSERT INTO rca_history(finding_id,batch_id,report_path,completed_at,lane) VALUES(?,?,?,?,?)',('related','old-report',str(path),1002,'rca-a'))
        rid=self.s.db.execute('SELECT max(id) FROM rca_history').fetchone()[0]
        before=self.flags()
        self.s.coverage.link('related',hid,rid,quote,now=1003)
        n=self.s.db.execute('SELECT count(*) FROM rca_coverage').fetchone()[0]
        self.s.coverage.link('related',hid,rid,quote,now=1004)
        self.assertEqual(n,self.s.db.execute('SELECT count(*) FROM rca_coverage').fetchone()[0])
        self.assertEqual(self.flags(),before)
        self.assertEqual(self.s.coverage.for_finding('related')['history_id'],hid)
        self.add('wrong','ERROR [cron_other_20260916_120000] provider: wrapper failed on /different/file')
        with self.assertRaisesRegex(ValueError,'scope'):
            self.s.coverage.link('wrong',hid,rid,quote)

    def test_unrelated_prose_and_changed_own_evidence_cannot_set_coverage(self):
        hid=self.finish();self.add('other','ERROR [cron_job1_20260916_120000] provider: other fault on /data/jobs.json')
        with self.assertRaises(ValueError):self.s.coverage.link('other',hid,hid,'invented same mechanism for original other')
        with self.s.db:
            raw=json.loads(self.s.db.execute("SELECT row_json FROM evidence WHERE gid='original'").fetchone()[0]);raw['text']+=' newly lost work'
            self.s.db.execute("UPDATE evidence SET row_json=? WHERE gid='original'",(packed(raw),))
        with self.assertRaisesRegex(ValueError,'Changed evidence'):
            self.s.coverage.link('original',hid)

    def test_inconclusive_unrepaired_missing_delivery_not_retry(self):
        report=REPORT.replace('## Root cause and confidence','## Root cause and confidence\nRoot cause undetermined; investigation inconclusive.\n')
        hid=self.finish(report=report)
        for p in (self.root/'shared').rglob('*'):
            if p.is_file():p.unlink()
        self.add('repeat')
        coverage=self.s.coverage.for_finding('repeat')
        self.assertEqual(coverage['outcome'],'inconclusive');self.assertTrue(coverage['delivery_recovery_needed'])
        self.assertEqual(coverage['repair_state'],'not asserted by coverage')
        self.assertFalse(self.s.status(now=1600)['dispatch_rca_b'])
        with self.assertRaisesRegex(ValueError,'already covered'):
            self.s.coverage.reopen('repeat','Root cause still unknown is not new evidence.',evidence_id='e-repeat',quote='request rejected on /data/jobs.json')
        self.s.coverage.reopen('repeat','Owner explicitly requests another investigation.',owner_authorization='isolated explicit owner fixture')
        self.assertTrue(self.s.prepare('rca-b',now=1601)['wakeAgent'])

    def test_invalid_multi_result_rolls_back_coverage_and_cache(self):
        hid=self.finish()
        self.add('changed','ERROR [cron_job1_20260916_120000] provider: request rejected on /data/jobs.json and durable queue lost 3 work items')
        self.add('unrelated','ERROR on /different/resource')
        with self.s.db:self.s.db.execute("UPDATE findings SET decision=NULL WHERE id IN ('changed','unrelated')")
        t=self.s.prepare('triage',now=1002)
        before=[tuple(r) for r in self.s.db.execute('SELECT * FROM rca_coverage')]
        good={'id':'changed','decision':'rca','reason':'New queue loss merits investigation.',
              'reopen':{'history_id':hid,'reason':'New queue loss changes the previously bounded failure impact.',
                        'evidence_id':'e-changed','quote':'durable queue lost 3 work items'}}
        with self.assertRaises(ValueError):
            self.s.complete(t['batch'],[good,{'id':'unrelated','decision':'watch','reason':'Invalid second reference must roll everything back.','coverage':{'history_id':999999}}],now=1003)
        self.assertEqual(before,[tuple(r) for r in self.s.db.execute('SELECT * FROM rca_coverage')])
        self.assertIn('changed',self.s.coverage.refresh()['uncertain'])
        self.assertNotIn('changed',self.s.coverage.refresh()['cutoffs'])

    def test_diagnosed_unrepaired_and_malformed_reopen(self):
        text=REPORT.replace('High confidence the named database file is structurally invalid.',
                            '**Root cause (established):** the named file is invalid; repair not authorized.')
        self.finish(report=text)
        self.assertEqual(self.s.coverage.for_finding('original')['outcome'],'diagnosed')
        self.assertEqual(self.s.get_history()['findings_history'][0]['investigation_coverage']['outcome'],'diagnosed')
        self.add('unrelated','ERROR on /another/resource')
        with self.s.db:self.s.db.execute("UPDATE findings SET decision=NULL WHERE id='unrelated'")
        t=self.s.prepare('triage',now=1002)
        for ref in ({},{'reason':[]},{'reason':'Owner says retry please','owner_authorization':'invented by model'}):
            with self.assertRaises(ValueError):
                self.s.complete(t['batch'],[{'id':'unrelated','decision':'rca','reason':'Concrete new failure to inspect.','reopen':ref}])

    def test_lane_b_compares_frozen_members_without_duplicate_alias_or_changing_a_lease(self):
        self.add('one');a=self.s.prepare('rca-a',now=1000)
        before=tuple(self.s.db.execute('SELECT * FROM batches WHERE id=?',(a['batch'],)).fetchone())
        self.add('two')
        comparison = self.s.prepare('rca-b', now=1001)
        self.assertTrue(comparison['wakeAgent'])
        self.assertNotEqual(comparison['batch'], a['batch'])
        self.assertEqual(comparison['items'], a['items'])
        self.assertEqual([item['id'] for item in comparison['items']], ['one'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM evidence').fetchone()[0], 2)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM rca_history').fetchone()[0], 0)
        self.assertEqual(tuple(self.s.db.execute('SELECT * FROM batches WHERE id=?',(a['batch'],)).fetchone()),before)

    def test_emergency_covered_alias_keeps_evidence_without_wake(self):
        self.finish(report=REPORT)
        row=self.add('alias')
        with self.s.db:self.s.db.execute("UPDATE findings SET decision=NULL WHERE id='alias'")
        self.assertEqual(self.s.promote_urgent(now=1002),[])
        self.assertFalse(self.s.prepare('triage',now=1003)['wakeAgent'])
        self.assertEqual(self.s.status(now=1004)['pending_triage'],0)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM evidence').fetchone()[0],2)

    def test_legacy_missing_original_basis_does_not_claim_latest_as_diagnosed(self):
        self.add('old')
        with self.s.db:
            self.s.db.execute("INSERT INTO rca_history(finding_id,batch_id,report_path,completed_at,lane) VALUES('old','legacy','/absent/archive',10,'rca-a')")
        self.add('same-text')
        state=self.s.coverage.refresh()
        self.assertNotIn('old', state['covered'])
        self.assertNotIn('same-text', state['covered'])
        coverage = self.s.coverage.for_finding('old')
        if coverage is not None:
            self.assertNotEqual(coverage['outcome'], 'diagnosed')
        history = self.s.db.execute("SELECT report_path,completed_at FROM rca_history WHERE finding_id='old'").fetchone()
        self.assertEqual(tuple(history), ('/absent/archive', 10))
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM findings WHERE id IN ('old','same-text')").fetchone()[0], 2)
        basis = self.s.db.execute('SELECT evidence_json FROM rca_coverage').fetchone()
        if basis is not None:
            self.assertEqual(json.loads(basis[0]), {})

if __name__=='__main__':unittest.main()
