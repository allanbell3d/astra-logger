#!/usr/bin/env python3
"""Sandbox tests for astra_phase_models.py. Never touches live profile files."""
import datetime
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'src' / 'deploy' / 'cron-wrappers' / 'astra_phase_models.py'
ALLAN = ROOT / 'tmp' / 'allan'
ORIG = ALLAN / 'config-orignal.yaml'
SEEDS = {
    1: ALLAN / 'models-test-01.yaml',
    2: ALLAN / 'models-test-02..yaml',
    3: ALLAN / 'models-test-03.yaml',
}
OLD_OUT = ALLAN
PY = sys.executable

# Self-contained fixtures so SwapRevertBugs does not need tmp/allan.
SYNTH_CONFIG = textwrap.dedent('''\
model:
  provider: zai
  default: glm-flash
  reasoning_effort: medium
fallback_providers:
  - provider: openai
    model: gpt-x
memory:
  memory_enabled: true
  user_profile_enabled: false
other:
  keep: me
''')

SYNTH_MODELS = textwrap.dedent('''\
default:
model:
  provider: zai
  default: glm-flash
  reasoning_effort: medium
triage:
model:
  provider: zai
  default: cheap
  reasoning_effort: low
memory:
  memory_enabled: false
  user_profile_enabled: false
rca:
model:
  provider: zai
  default: smart
  reasoning_effort: high
''')


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_apm():
    import importlib.util
    spec = importlib.util.spec_from_file_location('astra_phase_models_test', SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def deep_equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b


class PhaseSandbox(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix='phase-test-'))
        (self.td / 'logs').mkdir()
        self.addCleanup(shutil.rmtree, self.td, True)

    def home(self, models_src=None, models_text=None, mode=0o600, config_bytes=None):
        if config_bytes is None:
            config_bytes = SYNTH_CONFIG.encode('utf-8')
        (self.td / 'config.yaml').write_bytes(config_bytes)
        os.chmod(self.td / 'config.yaml', mode)
        if models_src:
            shutil.copy2(models_src, self.td / 'models.yaml')
        else:
            text = models_text if models_text is not None else SYNTH_MODELS
            (self.td / 'models.yaml').write_text(text)
        os.chmod(self.td / 'models.yaml', 0o600)
        return self.td

    def run_cli(self, *args):
        env = os.environ.copy()
        env['HERMES_HOME'] = str(self.td)
        return subprocess.run(
            [PY, str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def cfg(self):
        return (self.td / 'config.yaml').read_bytes()

    def cfg_mode(self):
        return stat.S_IMODE((self.td / 'config.yaml').stat().st_mode)

    def backups(self):
        return sorted(self.td.glob('config.yaml.bak-phase-*'))


@unittest.skipUnless(ORIG.is_file(), 'owner fixtures missing')
class OwnerNineCase(PhaseSandbox):
    def _check_apply(self, n, phase, applied):
        apm = load_apm()
        seed = SEEDS[n]
        orig_txt = ORIG.read_text()
        orig_lines = orig_txt.splitlines(keepends=True)
        _, orig_blocks, _ = apm.split_block_lines(orig_lines)
        orig_parsed = yaml.safe_load(orig_txt)
        chunks = apm._chunk_models(seed.read_text())
        parsed = yaml.safe_load(chunks[phase]) if chunks[phase].strip() else {}
        _, sec_blocks, _ = apm.split_block_lines(chunks[phase].splitlines(keepends=True))
        out_txt = applied.decode()
        _, out_blocks, _ = apm.split_block_lines(out_txt.splitlines(keepends=True))
        out_parsed = yaml.safe_load(out_txt)
        for k, v in parsed.items():
            self.assertTrue(deep_equal(out_parsed.get(k), v), f'{n}-{phase} semantic {k}')
            self.assertEqual(''.join(out_blocks.get(k, [])), ''.join(sec_blocks[k]),
                             f'{n}-{phase} raw {k}')
        for k, blines in orig_blocks.items():
            if k in parsed:
                continue
            self.assertEqual(''.join(out_blocks.get(k, [])), ''.join(blines),
                             f'{n}-{phase} absent {k}')
        self.assertEqual(sorted(set(orig_parsed) - set(out_parsed)), [])
        old = OLD_OUT / f'test0{n}-{phase}.yaml'
        if old.is_file():
            self.assertEqual(applied, old.read_bytes(),
                             f'fresh {n}-{phase} must match old artifact bytes')

    def test_all_nine_apply_match_seed_and_restore_original(self):
        orig_b = ORIG.read_bytes()
        for n, seed in SEEDS.items():
            for phase in ('default', 'triage', 'rca'):
                with self.subTest(n=n, phase=phase, seed=seed.name):
                    self.home(models_src=seed, mode=0o600, config_bytes=orig_b)
                    r = self.run_cli('apply', phase)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    applied = self.cfg()
                    self.assertNotEqual(applied, orig_b)
                    self.assertEqual(self.cfg_mode(), 0o600)
                    self._check_apply(n, phase, applied)
                    rr = self.run_cli('restore')
                    self.assertEqual(rr.returncode, 0, rr.stderr)
                    self.assertEqual(self.cfg(), orig_b)
                    self.assertEqual(self.cfg_mode(), 0o600)


class SwapRevertBugs(PhaseSandbox):
    def test_unindented_list_applies_and_restores(self):
        self.home(models_text=textwrap.dedent('''\
            triage:
            fallback_providers:
            - provider: zai
              model: glm-4.6
            model:
              provider: zai
              default: glm-4.6
              reasoning_effort: medium
            '''))
        orig = self.cfg()
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        live = yaml.safe_load(self.cfg())
        self.assertEqual(live['model']['default'], 'glm-4.6')
        self.assertEqual(live['fallback_providers'][0]['provider'], 'zai')
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)

    def test_planted_newer_backup_is_ignored_on_restore(self):
        self.home()
        orig = self.cfg()
        self.assertEqual(self.run_cli('apply', 'triage').returncode, 0)
        plant = self.td / 'config.yaml.bak-phase-planted-20990101-000000'
        plant.write_bytes(orig.replace(b'glm-flash', b'PLANTED-WRONG-MODEL', 1))
        os.utime(plant, (os.path.getmtime(plant) + 50, os.path.getmtime(plant) + 50))
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)
        self.assertNotIn(b'PLANTED-WRONG-MODEL', self.cfg())

    def test_skip_then_stale_backup_still_restores_original(self):
        self.home()
        orig = self.cfg()
        self.assertEqual(self.run_cli('apply', 'triage').returncode, 0)
        skip = self.run_cli('apply', 'triage')
        self.assertEqual(skip.returncode, 0)
        self.assertIn('SKIP', skip.stdout)
        plant = self.td / 'config.yaml.bak-phase-stale-20990101'
        plant.write_bytes(orig.replace(b'glm-flash', b'STALE-SKIP', 1))
        os.utime(plant, (os.path.getmtime(plant) + 80, os.path.getmtime(plant) + 80))
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)
        self.assertNotIn(b'STALE-SKIP', self.cfg())

    def test_added_block_restore_returns_original(self):
        self.home(models_text=textwrap.dedent('''\
            triage:
            brand_new_block:
              foo: bar
            memory:
              memory_enabled: false
              user_profile_enabled: false
            '''))
        orig = self.cfg()
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('brand_new_block', yaml.safe_load(self.cfg()))
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)
        self.assertNotIn('brand_new_block', yaml.safe_load(self.cfg()))

    def test_leftover_backup_without_apply_refuses(self):
        self.home()
        orig = self.cfg()
        plant = self.td / 'config.yaml.bak-phase-leftover-20200101'
        plant.write_bytes(orig.replace(b'glm-flash', b'LEFTOVER', 1))
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 1)
        self.assertIn('no last-apply backup recorded', rr.stderr)
        self.assertEqual(self.cfg(), orig)
        self.assertNotIn(b'LEFTOVER', self.cfg())

    def test_duplicate_section_header_refuses(self):
        self.home(models_text=textwrap.dedent('''\
            default:
            model:
              provider: first
              default: aaa
              reasoning_effort: low
            default:
            model:
              provider: second
              default: bbb
              reasoning_effort: high
            '''))
        orig = self.cfg()
        r = self.run_cli('apply', 'default')
        self.assertEqual(r.returncode, 1)
        self.assertIn('duplicate', r.stderr)
        self.assertEqual(self.cfg(), orig)

    def test_duplicate_section_keys_refuse(self):
        self.home(models_text=textwrap.dedent('''\
            triage:
            model:
              provider: first
              default: aaa
              reasoning_effort: low
            model:
              provider: second
              default: bbb
              reasoning_effort: high
            '''))
        orig = self.cfg()
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 1)
        self.assertIn('duplicate column-0', r.stderr)
        self.assertEqual(self.cfg(), orig)

    def test_mode_preserved_on_apply_and_restore(self):
        self.home(mode=0o600)
        self.assertEqual(self.cfg_mode(), 0o600)
        self.assertEqual(self.run_cli('apply', 'triage').returncode, 0)
        self.assertEqual(self.cfg_mode(), 0o600)
        self.assertEqual(self.run_cli('restore').returncode, 0)
        self.assertEqual(self.cfg_mode(), 0o600)

    def test_crlf_original_config_restores_exact_bytes(self):
        orig = SYNTH_CONFIG.replace('\n', '\r\n').encode('utf-8')
        self.assertIn(b'\r\n', orig)
        self.home(config_bytes=orig)
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)
        self.assertIn(b'\r\n', self.cfg())

    def test_crlf_seed_splices_raw_crlf_block(self):
        self.home()
        seed = SYNTH_MODELS.replace('\n', '\r\n').encode('utf-8')
        (self.td / 'models.yaml').write_bytes(seed)
        orig = self.cfg()
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        applied = self.cfg()
        self.assertIn(b'model:\r\n', applied)
        self.assertIn(b'default: cheap\r\n', applied)
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)

    def test_semantic_equal_differing_raw_does_not_skip(self):
        cfg = textwrap.dedent('''\
            model:
              provider: zai
              default: cheap
              reasoning_effort: low
            other:
              keep: me
            ''')
        models = textwrap.dedent('''\
            triage:
            model:
              provider: zai
              default: "cheap"
              reasoning_effort: low
            ''')
        self.home(models_text=models, config_bytes=cfg.encode('utf-8'))
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('SKIP', r.stdout)
        self.assertIn('APPLIED', r.stdout)
        self.assertIn(b'default: "cheap"', self.cfg())
        self.assertTrue(deep_equal(yaml.safe_load(self.cfg())['model'],
                                   {'provider': 'zai', 'default': 'cheap',
                                    'reasoning_effort': 'low'}))

    def test_raw_identical_still_skips(self):
        cfg = textwrap.dedent('''\
            model:
              provider: zai
              default: cheap
              reasoning_effort: low
            other:
              keep: me
            ''')
        models = textwrap.dedent('''\
            triage:
            model:
              provider: zai
              default: cheap
              reasoning_effort: low
            ''')
        self.home(models_text=models, config_bytes=cfg.encode('utf-8'))
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('SKIP', r.stdout)

    def test_same_second_backup_collision_keeps_both(self):
        self.home()
        orig = self.cfg()
        self.assertEqual(self.run_cli('apply', 'triage').returncode, 0)
        self.assertEqual(self.run_cli('restore').returncode, 0)
        self.assertEqual(self.cfg(), orig)
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        collide = self.td / f'config.yaml.bak-phase-triage-{ts}'
        collide.write_bytes(b'COLLIDE-PLACEHOLDER\n')
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(collide.read_bytes(), b'COLLIDE-PLACEHOLDER\n')
        baks = [p for p in self.backups() if p.name.startswith('config.yaml.bak-phase-triage-')]
        self.assertTrue(any(p.read_bytes() == orig for p in baks),
                        'apply must keep a unique backup of the resting snapshot')

    def test_overlapping_apply_refuses_and_restore_returns_original(self):
        self.home()
        orig = self.cfg()
        r1 = self.run_cli('apply', 'triage')
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(yaml.safe_load(self.cfg())['model']['default'], 'cheap')
        mid = self.cfg()
        r2 = self.run_cli('apply', 'rca')
        self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
        self.assertIn('overlapping', r2.stderr.lower() + r2.stdout.lower())
        self.assertEqual(self.cfg(), mid)
        rr = self.run_cli('restore')
        self.assertEqual(rr.returncode, 0, rr.stderr)
        self.assertEqual(self.cfg(), orig)

    def test_apply_does_not_prune_old_backups(self):
        self.home()
        planted = []
        for i in range(7):
            p = self.td / f'config.yaml.bak-phase-old-2020010{i}-000000'
            p.write_text(f'old{i}\n')
            planted.append(p)
        r = self.run_cli('apply', 'triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        for p in planted:
            self.assertTrue(p.is_file(), f'pruned {p.name}')
            self.assertTrue(p.read_text().startswith('old'))


if __name__ == '__main__':
    unittest.main()
