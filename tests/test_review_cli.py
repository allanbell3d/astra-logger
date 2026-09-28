"""Behavioral contract for the deterministic review CLI (stdlib only)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from astra.classify import classify_event
from astra.enrich import enrich_record
from astra.severity import SeverityPolicy


def ev(seq, text, ts='2026-09-16T12:00:00Z'):
    r = {'record_id': str(seq), 'host': 'test', 'profile': 'default', 'event_ts': ts, 'ts': ts,
         'source_path': '/nonexistent/agent.log', 'byte_start': seq * 100, 'byte_end': seq * 100 + 90,
         'text': text}
    return enrich_record(r, classify_event(r), SeverityPolicy.default(), r['source_path'], r['byte_start'], r['byte_end'])


def run_cli(root, state, *args):
    proc = subprocess.run(
        [sys.executable, '-m', 'astra.review_cli', '--root', str(root), '--state', str(state), *args],
        capture_output=True, text=True, env={'TZ': 'UTC', 'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src'/'agent')},
    )
    return proc


class TestReviewCLI(unittest.TestCase):
    def test_prepare_triage_complete_rca_status_history_and_evidence_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            logs = td_path / 'logs-watch'
            logs.mkdir()
            state = td_path / 'review.sqlite'
            (logs / 'enriched-20260916-00.jsonl').write_text(
                json.dumps(ev(1, '2026-09-16 12:00:00 ERROR api: 429 too many requests')) + '\n')
            (logs / 'enriched-20260916-12.jsonl').write_text(
                json.dumps(ev(2, '2026-09-16 13:00:00 ERROR sqlite: database disk image is malformed')) + '\n')

            # sync: idempotent ingest from buckets
            p = run_cli(logs, state, 'sync')
            self.assertEqual(p.returncode, 0, p.stderr)
            first = json.loads(p.stdout)
            self.assertEqual(first['ingested_rows'], 2)
            p2 = run_cli(logs, state, 'sync')
            self.assertEqual(json.loads(p2.stdout)['ingested_rows'], 0)

            # prepare triage: bounded packet
            p = run_cli(logs, state, 'prepare', 'triage', '--max-bytes', '12000')
            self.assertEqual(p.returncode, 0, p.stderr)
            packet = json.loads(p.stdout)
            self.assertTrue(packet['wakeAgent'])
            self.assertEqual(len(packet['items']), 2)
            batch_id = packet['batch']

            # complete triage: escalate malformed-db finding to RCA, watch the rest
            results = [{'id': i['id'],
                        'decision': 'rca' if 'malformed' in i['sample'] else 'watch',
                        'reason': 'Deterministic triage decision with explicit reason text.'}
                       for i in packet['items']]
            (td_path / 'result.json').write_text(json.dumps(results))
            p = run_cli(logs, state, 'complete', '--batch', batch_id, '--result', str(td_path / 'result.json'))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)['status'], 'reviewed')

            # prepare rca: one finding, then complete with a contract-valid report
            p = run_cli(logs, state, 'prepare', 'rca')
            packet_rca = json.loads(p.stdout)
            self.assertTrue(packet_rca['wakeAgent'])
            archive = td_path / 'archive'
            shared = td_path / 'shared'
            report = ("# RCA: malformed database\n\n"
                      "## Observed impact\nAgent database unreadable.\n\n"
                      "## Timeline and evidence\nAt 13:00 corruption first seen.\n\n"
                      "## Facts versus hypotheses\nFact: header zeroed. Hypothesis: power loss.\n\n"
                      "## Checks and results\nPRAGMA integrity_check fails on page 1.\n\n"
                      "## Root cause and confidence\nInterrupted checkpoint; high confidence.\n\n"
                      "## Proposed fix\nRestore from replica.\n\n"
                      "## Risks and prerequisites\nStop service first.\n\n"
                      "## Rollback\nKeep corrupted file as .bak.\n\n"
                      "## Post-repair verification\nintegrity_check returns ok.\n")
            (td_path / 'report.md').write_text(report)
            p = run_cli(logs, state, 'complete', '--batch', packet_rca['batch'], '--report', str(td_path / 'report.md'),
                        '--archive-dir', str(archive), '--shared-dir', str(shared))
            self.assertEqual(p.returncode, 0, p.stderr)
            done = json.loads(p.stdout)
            self.assertEqual(done['status'], 'persisted')
            self.assertTrue(done['notification_pending'])
            self.assertTrue(Path(done['report_path']).is_file())
            self.assertTrue(Path(done['shared_path']).is_file())

            # evidence: bounded lookup by finding id
            p = run_cli(logs, state, 'evidence', '--id', packet['items'][0]['id'], '--max-bytes', '200')
            self.assertEqual(p.returncode, 0, p.stderr)
            evd = json.loads(p.stdout)
            self.assertEqual(evd['finding_id'], packet['items'][0]['id'])
            self.assertLessEqual(len(evd['text'].encode()), 200)

            # status: counts plus bridge booleans
            p = run_cli(logs, state, 'status')
            st = json.loads(p.stdout)
            self.assertEqual(st['pending_triage'], 0)
            self.assertEqual(st['pending_rca'], 0)
            self.assertFalse(st['dispatch_rca_a'])
            self.assertFalse(st['dispatch_rca_b'])
            self.assertIn('early', st)

            # history: one rca record + reviewed findings, bounded
            p = run_cli(logs, state, 'history', '--limit', '10')
            hist = json.loads(p.stdout)
            self.assertEqual(len(hist['rca_history']), 1)
            self.assertGreaterEqual(len(hist['findings_history']), 2)

            # retention: explicit gap reporting
            p = run_cli(logs, state, 'retention')
            rep = json.loads(p.stdout)
            self.assertIn('gap_detected', rep)

            # invalid result file: exit 1, stderr message, no stdout JSON
            (td_path / 'bad.json').write_text(json.dumps([{'id': 'nope', 'decision': 'watch', 'reason': 'x' * 20}]))
            p = run_cli(logs, state, 'complete', '--batch', batch_id, '--result', str(td_path / 'bad.json'))
            self.assertEqual(p.returncode, 1)
            self.assertTrue(p.stderr.strip())
            self.assertEqual(p.stdout, '')

    def test_early_status_flags_serious_unreviewed_observation(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            logs = td_path / 'logs-watch'
            logs.mkdir()
            state = td_path / 'review.sqlite'
            (logs / 'enriched-20260916-00.jsonl').write_text(
                json.dumps(ev(1, '2026-09-16 12:00:00 CRITICAL core: FATAL unhandled exception')) + '\n')
            self.assertEqual(run_cli(logs, state, 'sync').returncode, 0)
            p = run_cli(logs, state, 'status')
            st = json.loads(p.stdout)
            self.assertTrue(st['early'])
            self.assertFalse(st['dispatch_triage_early'])
            self.assertIn('fatal', st['early_reason'].lower())
