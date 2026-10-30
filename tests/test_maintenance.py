"""Maintenance contracts use isolated catalogs, evidence, index and journals."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'maintenance.py'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def entry_sha(entry):
    return sha(json.dumps(entry, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), default=str).encode())


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'catalogs').mkdir()
        self.catalog = self.home / 'catalogs' / 'FAILURE_MODES.yaml'
        self.original = ('# retained header\n- id: a\n  fix_pattern: old\n'
                         '  recurrences: 2\n  recurrence_2026_01_01: corrected remedy\n'
                         '- id: b\n  fix_pattern: other\n  context: untouched\n')
        self.catalog.write_text(self.original)
        (self.home / 'catalogs' / 'PROCESS_FAILURES.yaml').write_text('- id: pf-control\n  fix_pattern: retained\n')
        self.evidence = self.home / 'evidence.txt'
        self.evidence.write_text('independent verifier observed corrected behavior\n')
        self.config = self.home / 'checks.json'
        self.config.write_text(json.dumps({'schema_version': 1, 'checks': {
            'fixed-remedy': {'kind': 'field_contains', 'field': 'fix_pattern', 'value': 'fixed'},
            'inactive': {'kind': 'field_equals', 'field': 'status', 'value': 'retired'},
            'merged': {'kind': 'field_equals', 'field': 'status', 'value': 'superseded'}}}))
        self.plan = {'schema_version': 1, 'author': 'writer', 'catalog': 'FM', 'outcome_ids': [],
                     'checks_sha256': sha(self.config.read_bytes()),
                     'catalog_sha256': sha(self.catalog.read_bytes()), 'actions': [{
                         'type': 'rewrite', 'id': 'a', 'entry_sha256': entry_sha({
                             'id': 'a', 'fix_pattern': 'old', 'recurrences': 2,
                             'recurrence_2026_01_01': 'corrected remedy'}),
                         'changes': {'fix_pattern': 'fixed remedy'},
                         'rationale': 'verified correction', 'trigger': 'real problem',
                         'nearby_non_trigger': 'unrelated syntax',
                         'evidence': [{'path': str(self.evidence),
                                       'sha256': sha(self.evidence.read_bytes())}],
                         'checks': ['fixed-remedy']}]}
        self.planpath = self.home / 'plan.json'
        self.reviewpath = self.home / 'review.json'
        self.env = dict(os.environ, RECALL_HOME=str(self.home),
                        RECALL_CATALOG_DIR=str(self.home / 'catalogs'),
                        RECALL_PROBE_INDEX=str(self.home / 'index.json'),
                        RECALL_MAINTENANCE_CHECKS=str(self.config), RECALL_AUTOCOMMIT='0')
        self.save_plan()

    def save_plan(self):
        self.planpath.write_text(json.dumps(self.plan))
        self.reviewpath.write_text(json.dumps({'schema_version': 1, 'approved': True,
                                              'reviewer': 'independent',
                                              'plan_sha256': sha(self.planpath.read_bytes())}))

    def run_cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                           capture_output=True, text=True)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def apply(self, *args, **kwargs):
        return self.run_cli('apply', '--plan', str(self.planpath), '--review',
                            str(self.reviewpath), *args, **kwargs)

    def test_cli_help(self):
        p = subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b'scan', p.stdout)

    def test_dry_run_never_writes_store(self):
        result = self.apply()
        self.assertEqual(result['state'], 'dry_run')
        self.assertEqual(self.catalog.read_text(), self.original)
        self.assertFalse((self.home / 'maintenance').exists())

    def test_rewrite_preserves_unrelated_bytes_and_provenance_and_rebuilds(self):
        result = self.apply('--apply')
        self.assertEqual(result['state'], 'applied')
        self.assertIn('  recurrence_2026_01_01: corrected remedy\n', self.catalog.read_text())
        self.assertTrue(self.catalog.read_text().endswith('- id: b\n  fix_pattern: other\n  context: untouched\n'))
        self.assertEqual(Path(result['backup']).read_text(), self.original)
        self.assertTrue((self.home / 'index.json').exists())
        self.assertEqual(json.loads(Path(result['journal']).read_text())['after_sha256'], sha(self.catalog.read_bytes()))

    def test_idempotence(self):
        first = self.apply('--apply')
        second = self.apply('--apply')
        self.assertEqual(second['state'], 'already_applied')
        self.assertEqual(first['operation_id'], second['operation_id'])

    def test_stale_plan_rejected(self):
        self.catalog.write_text(self.original + '# concurrent edit\n')
        self.apply('--apply', ok=False)
        self.assertIn('# concurrent edit', self.catalog.read_text())

    def test_stale_entry_rejected(self):
        self.plan['actions'][0]['entry_sha256'] = '0' * 64
        self.save_plan()
        self.apply('--apply', ok=False)
        self.assertEqual(self.catalog.read_text(), self.original)

    def test_review_bound_to_exact_plan(self):
        self.planpath.write_text(self.planpath.read_text() + ' ')
        self.apply('--apply', ok=False)

    def test_author_cannot_review_own_plan(self):
        r = json.loads(self.reviewpath.read_text()); r['reviewer'] = 'writer'
        self.reviewpath.write_text(json.dumps(r))
        self.apply('--apply', ok=False)

    def test_failed_evidence_rejected(self):
        self.evidence.write_text('changed')
        self.apply('--apply', ok=False)

    def test_failed_named_check_rejected(self):
        self.plan['actions'][0]['changes']['fix_pattern'] = 'wrong'
        self.save_plan()
        self.apply('--apply', ok=False)
        self.assertEqual(self.catalog.read_text(), self.original)

    def test_arbitrary_model_command_is_never_executed(self):
        self.plan['actions'][0]['checks'] = ['touch /tmp/forbidden-maintenance']
        self.save_plan()
        self.apply('--apply', ok=False)

    def test_broken_duplicate_yaml_rejected(self):
        self.catalog.write_text(self.original.replace('  recurrences: 2', '  id: hidden\n  recurrences: 2'))
        self.plan['catalog_sha256'] = sha(self.catalog.read_bytes())
        self.save_plan()
        self.apply('--apply', ok=False)

    def test_duplicate_ids_rejected(self):
        self.catalog.write_text(self.original.replace('- id: b', '- id: a'))
        self.plan['catalog_sha256'] = sha(self.catalog.read_bytes())
        self.save_plan()
        self.apply('--apply', ok=False)

    def test_outside_catalog_rejected(self):
        self.plan['catalog'] = str(self.evidence)
        self.save_plan()
        self.apply('--apply', ok=False)

    def test_wrong_id_and_provenance_edits_rejected(self):
        for field in ('id', 'recurrences', 'recurrence_2026_01_01'):
            self.plan['actions'][0]['changes'] = {field: 'bad'}
            self.save_plan()
            self.apply('--apply', ok=False)
        self.assertEqual(self.catalog.read_text(), self.original)

    def test_retire_preserves_id_and_history(self):
        a = self.plan['actions'][0]
        a['type'] = 'retire'; a['changes'] = {}; a['checks'] = ['inactive']
        self.save_plan()
        self.apply('--apply')
        self.assertIn('- id: a\n', self.catalog.read_text())
        self.assertIn('status: retired', self.catalog.read_text())
        self.assertIn('recurrence_2026_01_01: corrected remedy', self.catalog.read_text())

    def test_merge_tombstones_donor_same_file(self):
        a = self.plan['actions'][0]
        a['type'] = 'merge'; a['merge_into'] = 'b'; a['changes'] = {}; a['checks'] = ['merged']
        a['merge_into_sha256'] = entry_sha({'id': 'b', 'fix_pattern': 'other', 'context': 'untouched'})
        self.save_plan()
        self.apply('--apply')
        text = self.catalog.read_text()
        self.assertIn('status: superseded', text)
        self.assertIn('superseded_by: b', text)
        self.assertIn('fix_pattern: old', text)
        self.assertIn('context: untouched', text)

    def test_cross_file_merge_needs_review(self):
        self.plan['actions'][0].update(type='merge', merge_into='PF:b')
        self.save_plan()
        self.apply('--apply', ok=False)

    def test_namespace_collision_in_other_catalog_does_not_edit_it(self):
        pf = self.home / 'catalogs' / 'PROCESS_FAILURES.yaml'
        pf.write_text('- id: a\n  fix_pattern: different\n')
        self.apply('--apply')
        self.assertEqual(pf.read_text(), '- id: a\n  fix_pattern: different\n')

    def test_scan_prioritizes_corrections_and_is_bounded(self):
        self.catalog.write_text(self.original + '- id: c\n  fix_pattern: other\n- id: d\n  recurrences: 3\n')
        result = self.run_cli('scan')
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual(result['candidates'][0]['id'], 'a')
        self.assertEqual(result, self.run_cli('scan'))

    def test_outcomes_require_independent_evidence_and_feed_scan(self):
        outcome = {'schema_version': 1, 'author': 'agent', 'verifier': 'test-runner',
                   'result': 'failed', 'catalog': 'FM', 'entry_id': 'b',
                   'lesson_revision': entry_sha({'id': 'b', 'fix_pattern': 'other', 'context': 'untouched'}),
                   'run': 'run1', 'task': 'task1',
                   'arm': 'revised', 'session': None, 'time': None,
                   'actual_tokens': None,
                   'evidence': [{'path': str(self.evidence), 'sha256': sha(self.evidence.read_bytes())}]}
        path = self.home / 'outcome.json'; path.write_text(json.dumps(outcome))
        result = self.run_cli('outcome', '--file', str(path))
        self.assertEqual(result['state'], 'recorded')
        self.assertEqual(self.run_cli('scan')['candidates'][0]['id'], 'b')
        self.assertIn('not causal', self.run_cli('report')['interpretation'])
        outcome['verifier'] = 'agent'; path.write_text(json.dumps(outcome))
        self.run_cli('outcome', '--file', str(path), ok=False)

    def test_schema_required(self):
        self.plan.pop('schema_version'); self.save_plan()
        self.apply('--apply', ok=False)

    def test_index_failure_is_pending_and_retry_repairs_without_rewriting(self):
        pf = self.home / 'catalogs' / 'PROCESS_FAILURES.yaml'
        pf.unlink()
        failed = self.apply('--apply', ok=False)
        result = json.loads(failed.stdout)
        self.assertEqual(result['state'], 'pending_index_repair')
        post = self.catalog.read_bytes()
        pf.write_text('- id: restored\n  fix_pattern: retained\n')
        repaired = self.apply('--apply')
        self.assertEqual(repaired['state'], 'applied')
        self.assertEqual(self.catalog.read_bytes(), post)

    def test_applied_operation_cannot_overwrite_later_work(self):
        self.apply('--apply')
        self.catalog.write_text(self.catalog.read_text() + '# later work\n')
        self.apply('--apply', ok=False)
        self.assertIn('# later work', self.catalog.read_text())

    def test_changed_trusted_config_invalidates_review(self):
        self.config.write_text(self.config.read_text() + ' ')
        self.apply('--apply', ok=False)

    def test_guidance_rewrite_preserves_comments_before_other_fields(self):
        self.original = self.original.replace('  recurrences: 2', '  # provenance comment\n  recurrences: 2')
        self.catalog.write_text(self.original)
        self.plan['catalog_sha256'] = sha(self.catalog.read_bytes()); self.save_plan()
        self.apply('--apply')
        self.assertIn('  # provenance comment\n  recurrences: 2', self.catalog.read_text())

    def test_reviewed_unchanged_revision_is_not_queued_again(self):
        self.apply('--apply')
        self.assertNotIn('a', [r['id'] for r in self.run_cli('scan')['candidates']])

    def test_independent_failure_after_review_requeues_exact_revision(self):
        self.apply('--apply')
        row = {'id': 'a', 'fix_pattern': 'fixed remedy', 'recurrences': 2,
               'recurrence_2026_01_01': 'corrected remedy'}
        outcome = {'schema_version': 1, 'author': 'agent', 'verifier': 'runner',
                   'result': 'failed', 'catalog': 'FM', 'entry_id': 'a',
                   'lesson_revision': entry_sha(row), 'run': 'r', 'task': 't',
                   'arm': 'revised', 'session': None, 'time': None, 'actual_tokens': 30,
                   'evidence': [{'path': str(self.evidence), 'sha256': sha(self.evidence.read_bytes())}]}
        path = self.home / 'outcome.json'; path.write_text(json.dumps(outcome))
        self.run_cli('outcome', '--file', str(path))
        self.assertEqual(self.run_cli('scan')['candidates'][0]['id'], 'a')

    def test_lock_wait_rereads_and_refuses_changed_source(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import maintenance
        with maintenance.catalog_lock(self.catalog):
            process = subprocess.Popen([sys.executable, str(SCRIPT), 'apply', '--apply',
                                        '--plan', str(self.planpath), '--review', str(self.reviewpath)],
                                       env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            self.catalog.write_text(self.original + '# cooperating writer\n')
        stdout, stderr = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertIn('# cooperating writer', self.catalog.read_text())

    def test_keep_records_review_without_modifying_source(self):
        self.plan['actions'][0].update(type='keep', changes={}, checks=['old-remedy'])
        config = json.loads(self.config.read_text())
        config['checks']['old-remedy'] = {'kind': 'field_equals', 'field': 'fix_pattern', 'value': 'old'}
        self.config.write_text(json.dumps(config))
        self.plan['checks_sha256'] = sha(self.config.read_bytes()); self.save_plan()
        result = self.apply('--apply')
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(self.catalog.read_text(), self.original)
        self.assertNotIn('a', [r['id'] for r in self.run_cli('scan')['candidates']])

    def test_dirty_target_commit_is_deferred(self):
        subprocess.run(['git', 'init', '-q', str(self.home)], check=True)
        subprocess.run(['git', '-C', str(self.home), 'add', 'catalogs'], check=True)
        subprocess.run(['git', '-C', str(self.home), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.catalog.write_text(self.original + '# unrelated dirty bytes\n')
        self.plan['catalog_sha256'] = sha(self.catalog.read_bytes()); self.save_plan()
        result = self.apply('--apply')
        self.assertIn('deferred', result['commit'])
        self.assertIn('# unrelated dirty bytes', self.catalog.read_text())

    def test_merge_cycle_rejected(self):
        donor = self.plan['actions'][0]
        donor.update(type='merge', changes={}, checks=['merged'], merge_into='b',
                     merge_into_sha256=entry_sha({'id': 'b', 'fix_pattern': 'other', 'context': 'untouched'}))
        other = dict(donor, id='b', entry_sha256=donor['merge_into_sha256'],
                     merge_into='a', merge_into_sha256=donor['entry_sha256'])
        self.plan['actions'].append(other); self.save_plan()
        self.apply('--apply', ok=False)
        self.assertEqual(self.catalog.read_text(), self.original)

    def test_evidence_changed_while_waiting_for_lock_is_rejected(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import maintenance
        with maintenance.catalog_lock(self.catalog):
            process = subprocess.Popen([sys.executable, str(SCRIPT), 'apply', '--apply',
                                        '--plan', str(self.planpath), '--review', str(self.reviewpath)],
                                       env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            self.evidence.write_text('different evidence after initial validation')
        stdout, stderr = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.catalog.read_text(), self.original)

    def test_inactive_duplicate_does_not_nominate_active_sibling(self):
        self.catalog.write_text('- id: active\n  fix_pattern: same remedy\n'
                                '- id: history\n  status: superseded\n  fix_pattern: same remedy\n')
        self.assertEqual(self.run_cli('scan')['candidates'], [])

    def test_empty_primary_paths_fall_back_to_legacy_overrides(self):
        self.env.update(RECALL_HOME='', OKWOW_HOME=str(self.home),
                        RECALL_CATALOG_DIR='', OKWOW_CATALOG_DIR=str(self.home / 'catalogs'))
        result = self.apply('--apply')
        self.assertEqual(result['state'], 'applied')
        self.assertTrue(Path(result['journal']).is_relative_to(self.home))

    def test_canonical_fix_summary_and_trigger_fields_can_be_rewritten(self):
        for field in ('fix', 'summary', 'trigger'):
            with self.subTest(field=field):
                self.catalog.write_text(self.original.replace('fix_pattern: old', field + ': old'))
                self.plan['catalog_sha256'] = sha(self.catalog.read_bytes())
                a = self.plan['actions'][0]
                a['entry_sha256'] = entry_sha({'id': 'a', field: 'old', 'recurrences': 2,
                                              'recurrence_2026_01_01': 'corrected remedy'})
                a['changes'] = {field: 'fixed remedy'}
                self.config.write_text(json.dumps({'schema_version': 1, 'checks': {
                    'fixed-remedy': {'kind': 'field_equals', 'field': field, 'value': 'fixed remedy'}}}))
                self.plan['checks_sha256'] = sha(self.config.read_bytes()); self.save_plan()
                self.apply('--apply')
                self.assertIn(field + ': fixed remedy', self.catalog.read_text())
                self.assertIn('recurrence_2026_01_01: corrected remedy', self.catalog.read_text())

    def test_review_revocation_and_plan_change_while_waiting_refuse_before_write(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import maintenance
        for mode in ('review_revoked', 'plan_whitespace_changed'):
            with self.subTest(mode=mode):
                self.save_plan()
                with maintenance.catalog_lock(self.catalog):
                    process = subprocess.Popen([sys.executable, str(SCRIPT), 'apply', '--apply',
                                                '--plan', str(self.planpath), '--review', str(self.reviewpath)],
                                               env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    time.sleep(0.2)
                    self.assertIsNone(process.poll())
                    if mode == 'review_revoked':
                        review = json.loads(self.reviewpath.read_text()); review['approved'] = False
                        self.reviewpath.write_text(json.dumps(review))
                    else:
                        self.planpath.write_text(self.planpath.read_text() + ' ')
                stdout, stderr = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual(self.catalog.read_text(), self.original)
                self.assertFalse((self.home / 'maintenance').exists())

    def make_outcome(self, entry_id='a', revision=None, run='new-run'):
        if revision is None:
            revision = entry_sha({'id': 'a', 'fix_pattern': 'old', 'recurrences': 2,
                                  'recurrence_2026_01_01': 'corrected remedy'})
        outcome = {'schema_version': 1, 'author': 'agent', 'verifier': 'runner',
                   'result': 'failed', 'catalog': 'FM', 'entry_id': entry_id,
                   'lesson_revision': revision, 'run': run, 'task': 'task',
                   'arm': 'current', 'session': None, 'time': None, 'actual_tokens': None,
                   'evidence': [{'path': str(self.evidence), 'sha256': sha(self.evidence.read_bytes())}]}
        path = self.home / ('outcome-' + run + '.json'); path.write_text(json.dumps(outcome))
        return self.run_cli('outcome', '--file', str(path))['outcome_id']

    def set_keep_plan(self):
        config = json.loads(self.config.read_text())
        config['checks']['old-remedy'] = {'kind': 'field_equals', 'field': 'fix_pattern', 'value': 'old'}
        self.config.write_text(json.dumps(config))
        self.plan['checks_sha256'] = sha(self.config.read_bytes())
        self.plan['actions'][0].update(type='keep', changes={}, checks=['old-remedy'])
        self.save_plan()

    def test_new_failure_after_review_remains_unconsumed(self):
        self.set_keep_plan()
        oid = self.make_outcome()
        result = self.apply('--apply')
        self.assertNotIn(oid, result['outcome_ids_reviewed'])
        candidate = self.run_cli('scan')['candidates'][0]
        self.assertEqual(candidate['id'], 'a')
        self.assertIn(oid, candidate['outcome_ids'])

    def test_only_explicit_reviewed_outcome_ids_are_consumed(self):
        reviewed_id = self.make_outcome(run='before-review')
        self.plan['outcome_ids'] = [reviewed_id]
        self.set_keep_plan()
        later_id = self.make_outcome(run='after-review')
        result = self.apply('--apply')
        self.assertEqual(result['outcome_ids_reviewed'], [reviewed_id])
        candidate = self.run_cli('scan')['candidates'][0]
        self.assertEqual(candidate['outcome_ids'], [later_id])

    def test_plan_outcome_ids_require_matching_entry_and_lesson_revision(self):
        for oid in (self.make_outcome(entry_id='b', run='other-entry'),
                    self.make_outcome(revision='old-revision', run='other-revision')):
            self.plan['outcome_ids'] = [oid]; self.save_plan()
            self.apply('--apply', ok=False)
            self.assertEqual(self.catalog.read_text(), self.original)

    def test_old_revision_failure_is_diagnostic_not_current_revision_blame(self):
        self.apply('--apply')
        oid = self.make_outcome(revision='old-revision')
        result = self.run_cli('scan')
        self.assertNotIn('a', [r['id'] for r in result['candidates']])
        self.assertIn(oid, [r['outcome_id'] for r in result['historical_outcomes']])

    def test_explicit_outcome_ids_required_even_when_empty(self):
        self.plan.pop('outcome_ids'); self.save_plan()
        self.apply('--apply', ok=False)

    def test_journal_preserves_exact_approved_plan_and_review_bytes(self):
        self.planpath.write_text(json.dumps(self.plan, indent=3) + '\n')
        review = {'schema_version': 1, 'approved': True, 'reviewer': 'independent',
                  'plan_sha256': sha(self.planpath.read_bytes())}
        self.reviewpath.write_text(json.dumps(review, indent=1) + '\n')
        plan_bytes, review_bytes = self.planpath.read_bytes(), self.reviewpath.read_bytes()
        result = self.apply('--apply')
        root = Path(result['journal']).parent
        self.assertEqual((root / 'plan.json').read_bytes(), plan_bytes)
        self.assertEqual((root / 'review.json').read_bytes(), review_bytes)

    def test_prepare_parses_the_same_plan_byte_snapshot_it_hashes(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import maintenance
        original_read = Path.read_bytes
        replacement = dict(self.plan)
        replacement['actions'] = [dict(self.plan['actions'][0], rationale='replacement rationale')]
        replacement_bytes = json.dumps(replacement).encode()
        swapped = [False]

        def replace_before_first_byte_read(path):
            if path == self.planpath and not swapped[0]:
                swapped[0] = True
                path.write_bytes(replacement_bytes)
                self.reviewpath.write_text(json.dumps({'schema_version': 1, 'approved': True,
                    'reviewer': 'independent', 'plan_sha256': sha(replacement_bytes)}))
            return original_read(path)

        with mock.patch.object(Path, 'read_bytes', replace_before_first_byte_read):
            with mock.patch.dict(os.environ, self.env):
                prepared = maintenance.prepare(self.planpath, self.reviewpath)
        self.assertEqual(prepared[0]['actions'][0]['rationale'], 'replacement rationale')
        self.assertEqual(prepared[1], sha(replacement_bytes))


if __name__ == '__main__':
    unittest.main()
