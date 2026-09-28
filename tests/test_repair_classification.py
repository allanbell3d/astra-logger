"""Regression cases from repeated live triage and auxiliary RCA reports."""
import unittest
from astra.classify import classify_event
from astra.fingerprint import build_fingerprint


class RepairClassificationTests(unittest.TestCase):
    def key(self, text, profile='astra-dell-health-monitor'):
        return build_fingerprint(classify_event({'text': text}), 'dell', profile, text)['key']

    def test_cron_execution_changes_preserve_job_identity(self):
        first = 'WARNING [cron_07fd6c5f86b7_20260922_215040] agent.tool_executor: Tool terminal returned error (0.24s): Result must account for every finding exactly once'
        later = first.replace('20260922_215040', '20260923_011748').replace('0.24s', '0.29s')
        self.assertEqual(self.key(first), self.key(later))
        self.assertNotEqual(self.key(first), self.key(later.replace('07fd6c5f86b7', '1952d32466a6')))
        self.assertNotEqual(self.key(first), self.key(later, 'another-profile'))
        self.assertNotEqual(self.key(first), self.key(later.replace('Result must account for every finding exactly once', 'database disk image is malformed')))

    def test_cooldown_expiry_is_volatile_but_provider_is_not(self):
        first = 'WARNING agent.auxiliary_client: Auxiliary: marking vertex unhealthy for 600s (payment / credit error). Subsequent auxiliary calls will skip it until 00:56:33.'
        later = first.replace('00:56:33', '01:47:16')
        self.assertEqual(self.key(first), self.key(later))
        self.assertNotEqual(self.key(first), self.key(later.replace('vertex', 'openrouter')))
        self.assertNotEqual(self.key('ERROR gateway: schedule failed at 00:56:33'), self.key('ERROR gateway: schedule failed at 01:47:16'))

    def test_cron_normalization_keeps_resource_identity(self):
        first = 'WARNING [cron_07fd6c5f86b7_20260922_215040] agent.tool_executor: Tool terminal returned error: database disk image is malformed: /var/lib/one.sqlite'
        self.assertNotEqual(self.key(first), self.key(first.replace('one.sqlite', 'two.sqlite')))

    def test_generic_auxiliary_payment_wording_does_not_prove_billing(self):
        text = 'WARNING agent.auxiliary_client: Auxiliary: marking vertex unhealthy for 600s (payment / credit error). Subsequent auxiliary calls will skip it until 01:47:16.'
        result = classify_event({'text': text})
        self.assertEqual(result['event'], 'api.provider_cooldown')
        self.assertNotIn('credit_balance_exhausted', result['causes'])
        result = classify_event({'text': text + ' Error code: 429 RESOURCE_EXHAUSTED'})
        self.assertIn('resource_exhausted', result['causes'])
        self.assertNotIn('credit_balance_exhausted', result['causes'])
        self.assertIn('credit_balance_exhausted', classify_event({'text': 'ERROR agent.auxiliary_client: prepayment credits are exhausted'})['causes'])

    def test_refusing_write_alone_does_not_mean_curator_policy(self):
        result = classify_event({'text': 'WARNING agent.tool_executor: Tool terminal returned error: refusing to write unreadable config.yaml: YAML parse failed'})
        self.assertNotIn('curator_policy_refusal', result['causes'])
        self.assertEqual(result['event'], 'tool.execution_failed')
        result = classify_event({'text': 'WARNING agent.tool_executor: Tool terminal returned error: curator refusing to write protected file'})
        self.assertIn('curator_policy_refusal', result['causes'])
        self.assertEqual(result['event'], 'tool.execution_blocked')
