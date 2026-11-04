"""Recurring maintenance uses isolated trusted config, command stubs and real engine."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import maintenance
import maintenance_tick as tick
import maintenance_worker as worker


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.catalogs = self.home / 'catalogs'; self.catalogs.mkdir()
        self.catalog = self.catalogs / 'FAILURE_MODES.yaml'
        self.catalog.write_text('- id: a\n  fix_pattern: old\n  probe_when: ["`check_hook_parity.py`"]\n  recurrences: 1\n')
        (self.catalogs / 'PROCESS_FAILURES.yaml').write_text('[]\n')
        self.checks = self.home / 'checks.json'
        self.checks.write_text(json.dumps({'schema_version': 1, 'checks': {
            'fixed': {'kind': 'field_equals', 'field': 'fix_pattern', 'value': 'fixed remedy'},
            'kept': {'kind': 'field_equals', 'field': 'fix_pattern', 'value': 'old'}}}))
        self.evidence = self.home / 'evidence.txt'; self.evidence.write_text('Verified remedy: fixed remedy.\n')
        self.stub = self.home / 'stub.py'
        self.stub.write_text('''import json,os,sys,time\nfrom pathlib import Path\nrole,request,output=sys.argv[1:]\nr=json.loads(Path(request).read_text())\nmode=os.environ.get('TICK_TEST_MODE','')\nif mode=='timeout': time.sleep(3)\nif mode=='failure': sys.exit(2)\nif mode=='invalid': Path(output).write_text('{broken'); sys.exit(0)\nif role=='propose':\n a={'id':r['candidates'][0]['id'],'type':'rewrite','entry_sha256':r['candidates'][0]['entry_sha256'],'changes':{'fix_pattern':'fixed remedy'},'rationale':'verified local source','trigger':'host parity','nearby_non_trigger':'unrelated program','evidence':[r['evidence'][0]['reference'] if r['evidence'] else r['candidates'][0]['raw_snapshot_reference']],'checks':['fixed']}\n if mode=='keep': a.update(type='keep',changes={},checks=['kept'])\n if mode=='retire': a.update(type='retire',changes={})\n if mode=='long': a['changes']['fix_pattern']='x'*301\n if mode=='forged-evidence': a['evidence'][0]['sha256']='0'*64\n if mode=='changed-catalog': Path(r['catalog_path']).write_text(Path(r['catalog_path']).read_text()+'# concurrent\\n')\n o={'schema_version':1,'actions':[a]}\n if mode=='forged-author': o['author']='reviewer'\nelse:\n o={'schema_version':1,'approved':True,'plan_sha256':r['plan_sha256'],'checks_sha256':r['checks_sha256'],'review_context_sha256':r['review_context_sha256'],'rationale':'independently checked'}\n if mode=='refuse': o['approved']=False\n if mode=='bad-binding': o['plan_sha256']='0'*64\n if mode=='bad-checks': o['checks_sha256']='0'*64\n if mode=='bad-context': o['review_context_sha256']='0'*64\n if mode=='forged-reviewer': o['reviewer']='writer'\nPath(output).write_text(json.dumps(o))\n''')
        self.config = {'schema_version': 1, 'enabled': True, 'period_seconds': 86400, 'limit': 3,
                       'proposer_command': [sys.executable, str(self.stub), 'propose', '{request}', '{output}'],
                       'reviewer_command': [sys.executable, str(self.stub), 'review', '{request}', '{output}'],
                       'timeout_seconds': 2, 'checks_path': str(self.checks),
                       'evidence_paths': [str(self.evidence)]}
        self.cfg = self.home / 'maintenance' / 'config.json'; self.cfg.parent.mkdir()
        self.save()
        self.env = {'RECALL_HOME': str(self.home), 'RECALL_CATALOG_DIR': str(self.catalogs),
                    'RECALL_PROBE_INDEX': str(self.home / 'index.json'), 'RECALL_AUTOCOMMIT': '0',
                    'RECALL_SKILLS_DIR': str(self.home / 'no-skills'),
                    'RECALL_SURFACED_LOG': str(self.home / 'surfaced.jsonl')}
        self.patch = mock.patch.dict(os.environ, self.env); self.patch.start(); self.addCleanup(self.patch.stop)

    def save(self): self.cfg.write_text(json.dumps(self.config))
    def run_tick(self, mode='', **kwargs):
        with mock.patch.dict(os.environ, {'TICK_TEST_MODE': mode}): return tick.run_tick(**kwargs)
    def state(self): return json.loads((self.cfg.parent / 'state.json').read_text())

    def test_complete_tick_engine_index_and_next_scan(self):
        result = self.run_tick(); self.assertEqual(result['state'], 'applied')
        self.assertIn('fixed remedy', self.catalog.read_text())
        self.assertTrue((self.home / 'index.json').exists())
        index = json.loads((self.home / 'index.json').read_text())
        self.assertEqual(next(iter(index['entries'].values()))['fix'], 'fixed remedy')
        self.assertEqual(maintenance.scan()['candidates'], [])
        self.assertEqual(self.run_tick()['state'], 'not_due')
        self.assertEqual(self.state()['completed'], 1)

    def test_disabled(self):
        self.config['enabled'] = False; self.save()
        self.assertEqual(self.run_tick()['state'], 'disabled')
        self.assertFalse((self.cfg.parent / 'state.json').exists())

    def test_interrupted_attempt_waits_before_automatic_retry(self):
        with mock.patch.object(tick, 'perform', side_effect=SystemExit('interrupted')):
            with self.assertRaises(SystemExit):
                tick.run_tick()
        self.assertEqual(self.run_tick()['state'], 'backoff')
        self.assertEqual(self.state()['attempts'], 1)
        self.assertEqual(self.state()['completed'], 0)

    def test_no_capture_queue_still_runs(self):
        self.assertFalse((self.home / 'queue').exists())
        self.assertEqual(self.run_tick()['state'], 'applied')

    def test_empty_scan_completes_without_workers(self):
        self.catalog.write_text('[]\n')
        self.assertEqual(self.run_tick('failure')['state'], 'empty')
        self.assertEqual(self.state()['completed'], 1)

    def test_busy_capture(self):
        pid = self.home / 'capture.pid'; pid.write_text(str(os.getpid()))
        self.config['capture_drain_pid_path'] = str(pid); self.save()
        self.assertEqual(self.run_tick()['state'], 'capture_busy')
        self.assertNotIn('fixed remedy', self.catalog.read_text())

    def test_stale_capture_pid_does_not_block(self):
        pid = self.home / 'capture.pid'; pid.write_text('999999999')
        self.config['capture_drain_pid_path'] = str(pid); self.save()
        self.assertEqual(self.run_tick()['state'], 'applied')

    def test_default_guard_reads_the_drain_lock_pid(self):
        # hooks/recall-drain.sh holds $RECALL_HOME/drain.lock and writes its pid there.
        lock = self.home / 'drain.lock'; lock.mkdir()
        (lock / 'pid').write_text(f'{os.getpid()}\n')
        self.assertTrue(tick.capture_busy({}))
        self.assertEqual(self.run_tick()['state'], 'capture_busy')
        self.assertNotIn('fixed remedy', self.catalog.read_text())
        (lock / 'pid').write_text('999999999\n')
        self.assertFalse(tick.capture_busy({}))
        self.assertEqual(self.run_tick()['state'], 'applied')

    def test_recursion_guard(self):
        with mock.patch.dict(os.environ, {'RECALL_MAINTENANCE_WORKER': '1'}):
            self.assertEqual(self.run_tick()['state'], 'worker_recursion')

    def test_lock_busy(self):
        import fcntl
        with (self.cfg.parent / 'tick.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_tick()['state'], 'tick_busy')

    def test_timeout_does_not_complete_or_mark_success(self):
        self.config['timeout_seconds'] = .05; self.save()
        self.assertEqual(self.run_tick('timeout')['state'], 'refused')
        s = self.state(); self.assertEqual(s['attempts'], 1); self.assertEqual(s['completed'], 0)
        self.assertNotIn('last_completed_at', s)
        self.assertEqual(self.run_tick()['state'], 'backoff')

    def test_command_failure_invalid_refusal_and_bindings(self):
        for mode in ['failure', 'invalid', 'refuse', 'bad-binding', 'bad-checks', 'bad-context',
                     'changed-catalog', 'forged-evidence', 'retire', 'long', 'forged-author', 'forged-reviewer']:
            with self.subTest(mode=mode):
                result = self.run_tick(mode, force=True)
                self.assertEqual(result['state'], 'refused', result)
                self.assertEqual(self.state()['completed'], 0)
                self.assertNotIn('fixed remedy', self.catalog.read_text())

    def test_keep_suppressed_until_new_failed_outcome(self):
        self.assertEqual(self.run_tick('keep')['state'], 'applied')
        self.assertEqual(maintenance.scan()['candidates'], [])
        outcome = {'schema_version': 1, 'author': 'tester', 'verifier': 'independent',
                   'result': 'failed', 'catalog': 'FM', 'entry_id': 'a', 'lesson_revision': maintenance.entry_hash(maintenance.load_catalog(self.catalog.read_bytes())['a']),
                   'run': 'later', 'task': 'test', 'arm': None, 'session': None, 'time': None,
                   'actual_tokens': None, 'evidence': [{'path': str(self.evidence),
                                                       'sha256': maintenance.digest(self.evidence.read_bytes())}]}
        p = self.home / 'outcome.json'; p.write_text(json.dumps(outcome)); maintenance.ingest_outcome(p)
        self.assertEqual(maintenance.scan()['candidates'][0]['id'], 'a')

    def add_failure(self, run):
        outcome = {'schema_version': 1, 'author': 'tester', 'verifier': 'independent',
                   'result': 'failed', 'catalog': 'FM', 'entry_id': 'a',
                   'lesson_revision': maintenance.entry_hash(maintenance.load_catalog(self.catalog.read_bytes())['a']),
                   'run': run, 'task': 'test', 'arm': None, 'session': None, 'time': None,
                   'actual_tokens': None, 'evidence': [{'path': str(self.evidence),
                                                       'sha256': maintenance.digest(self.evidence.read_bytes())}]}
        path = self.home / ('outcome-' + run + '.json')
        path.write_text(json.dumps(outcome))
        return maintenance.ingest_outcome(path)['outcome_id']

    def test_late_failure_not_swallowed_by_reviewed_keep(self):
        early = self.add_failure('early')
        original = tick.invoke
        late = []
        def invoke(command, *args):
            if 'review' in command:
                late.append(self.add_failure('arrived-during-review'))
            return original(command, *args)
        with mock.patch.object(tick, 'invoke', side_effect=invoke):
            result = self.run_tick('keep')
        self.assertEqual(result['state'], 'applied', result)
        self.assertEqual(result['plan']['outcome_ids'], [early])
        self.assertEqual(result['outcome_ids_reviewed'], [early])
        pending = maintenance.scan()['candidates'][0]
        self.assertEqual(pending['outcome_ids'], late)
        self.assertIn('failed_independent_outcome', pending['reasons'])

    def test_evidence_and_checks_changed_while_review_waits_are_refused(self):
        original = tick.invoke
        for target in (self.evidence, self.checks):
            with self.subTest(target=target.name):
                before = target.read_bytes()
                def invoke(command, *args):
                    result = original(command, *args)
                    if 'review' in command:
                        target.write_bytes(before + b' ')
                    return result
                try:
                    with mock.patch.object(tick, 'invoke', side_effect=invoke):
                        result = self.run_tick(force=True)
                    self.assertEqual(result['state'], 'refused', result)
                    self.assertIn('fix_pattern: old', self.catalog.read_text())
                    self.assertEqual(self.state()['completed'], 0)
                finally:
                    target.write_bytes(before)

    def test_no_verified_source_can_only_keep(self):
        self.config['evidence_paths'] = []; self.save()
        self.assertEqual(self.run_tick()['state'], 'refused')
        self.assertEqual(self.run_tick('keep', force=True)['state'], 'applied')
        self.assertIn('fix_pattern: old', self.catalog.read_text())

    def test_index_failure_retries_exact_reviewed_operation(self):
        with mock.patch.object(maintenance, 'rebuild_index', return_value=(False, 'fixture failure')):
            first = self.run_tick()
        self.assertEqual(first['state'], 'pending_index_repair')
        self.assertEqual(self.state()['completed'], 0)
        with mock.patch.object(tick, 'invoke', side_effect=AssertionError('must not call models again')):
            result = self.run_tick(force=True)
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(result['operation_id'], first['operation_id'])
        self.assertEqual(self.state()['completed'], 1)
        self.assertEqual(maintenance.scan()['candidates'], [])

    def test_untrusted_command_placeholder_and_limit_rejected(self):
        for change in [{'limit': 4}, {'timeout_seconds': 121},
                       {'proposer_command': ['echo', '{candidate_text}', '{output}']}]:
            self.config.update(change); self.save()
            self.assertEqual(self.run_tick()['state'], 'refused')

    def test_worker_result_json_validation(self):
        good = {'schema_version': 1, 'actions': []}
        self.assertEqual(worker.parse_result(json.dumps({'result': json.dumps(good)})), good)
        self.assertEqual(worker.parse_result(json.dumps({'structured_output': good})), good)
        self.assertEqual(worker.parse_result(json.dumps({'result': '', 'structured_output': good})), good)
        self.assertEqual(worker.parse_result(json.dumps({'result': json.dumps(good), 'structured_output': good})), good)
        for bad in ['{}', '{broken', '{"result":"{}","result":"{}"}',
                    json.dumps({'result': '{}', 'structured_output': good}),
                    json.dumps({'result': '{"schema_version":1,"approved":1}',
                                'structured_output': {'schema_version': 1, 'approved': True}}),
                    json.dumps({'is_error': True, 'result': json.dumps(good)}),
                    '{"result":"{\"schema_version\":1,\"value\":NaN}"}']:
            with self.assertRaises(ValueError): worker.parse_result(bad)

    def test_worker_safe_native_flags_without_running_paid_call(self):
        argv = worker.command('claude', 'haiku', 'prompt')
        for flag in ['--safe-mode', '--strict-mcp-config', '--tools', '--no-session-persistence']:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(argv[argv.index('--max-budget-usd') + 1], '.50')

    def test_worker_requests_native_structured_roles(self):
        for role, required in [('proposer', {'schema_version','actions'}),
                               ('reviewer', {'schema_version','approved','plan_sha256',
                                             'checks_sha256','review_context_sha256','rationale'})]:
            argv=worker.command('claude','haiku','prompt',role)
            schema=json.loads(argv[argv.index('--json-schema')+1])
            self.assertEqual(set(schema['required']),required)
            self.assertFalse(schema['additionalProperties'])


if __name__ == '__main__': unittest.main()
