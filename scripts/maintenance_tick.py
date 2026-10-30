#!/usr/bin/env python3
"""Run a due, bounded local maintenance review independently of capture queues.

Trusted configuration lives at RECALL_HOME/maintenance/config.json. Commands are
owner-supplied argv arrays; only supervisor-created request/output paths expand.
The engine alone writes catalog transactions, journals and the trigger index.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import maintenance as engine

MAX_PAYLOAD = 64000
MAX_ENTRY = 16000
MAX_EVIDENCE = 12000
MAX_OUTPUT = 128000
REMEDIES = {'fix_pattern', 'fix', 'remedy', 'guidance'}


def config_load():
    root = engine.home() / 'maintenance'
    path = root / 'config.json'
    if not path.exists():
        return None
    cfg = engine.read_json(path); engine.require_version(cfg)
    if type(cfg.get('enabled')) is not bool:
        raise engine.Refused('enabled must be boolean')
    if not cfg['enabled']:
        return cfg
    for key, default, minimum, maximum in [('period_seconds', 86400, 1, 86400 * 365),
                                           ('limit', 3, 1, 3), ('timeout_seconds', 120, .001, 120),
                                           ('backoff_seconds', 300, .001, 86400)]:
        value = cfg.get(key, default)
        if type(value) not in (int, float) or not minimum <= value <= maximum:
            raise engine.Refused('invalid bounded config: ' + key)
        if key == 'limit' and type(value) is not int:
            raise engine.Refused('limit must be integer')
        cfg[key] = value
    for key in ('proposer_command', 'reviewer_command'):
        cmd = cfg.get(key)
        if not isinstance(cmd, list) or not cmd or any(not isinstance(x, str) or not x for x in cmd):
            raise engine.Refused('trusted command must be a nonempty argv array')
        for word in cmd:
            remaining = word.replace('{request}', '').replace('{output}', '')
            if '{' in remaining or '}' in remaining:
                raise engine.Refused('unsupported command placeholder')
        if '{request}' not in cmd or '{output}' not in cmd:
            raise engine.Refused('commands require standalone request/output placeholders')
    for key, default in [('state_path', root / 'state.json'), ('lock_path', root / 'tick.lock'),
                         ('runs_path', root / 'runs')]:
        cfg[key] = Path(cfg.get(key, default)).expanduser()
        if not cfg[key].is_absolute():
            raise engine.Refused('state paths must be absolute')
    cfg['checks_path'] = Path(engine.nonempty(cfg, 'checks_path')).expanduser()
    if not cfg['checks_path'].is_absolute():
        raise engine.Refused('checks_path must be absolute')
    cfg['allowed_actions'] = cfg.get('allowed_actions', ['rewrite', 'keep'])
    if (not isinstance(cfg['allowed_actions'], list) or not cfg['allowed_actions']
            or set(cfg['allowed_actions']) - {'rewrite', 'keep', 'merge', 'retire'}):
        raise engine.Refused('invalid allowed_actions')
    # Supervisor-owned identities cannot be supplied by catalog text or workers.
    cfg['author'] = 'maintenance-supervisor:proposer'
    cfg['reviewer'] = 'maintenance-supervisor:independent-reviewer'
    return cfg


def capture_busy(cfg):
    paths = [cfg['capture_drain_pid_path']] if cfg.get('capture_drain_pid_path') else [
        engine.home() / 'lot' / 'drain.pid', engine.home() / 'maintenance' / 'capture-drain.pid']
    for path in paths:
        try:
            pid = int(Path(path).expanduser().read_text().strip())
            if pid <= 0:
                continue
            os.kill(pid, 0)
            return True
        except (ValueError, FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            return True
    return False


@contextmanager
def configured_checks(path):
    previous = os.environ.get('RECALL_MAINTENANCE_CHECKS')
    os.environ['RECALL_MAINTENANCE_CHECKS'] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('RECALL_MAINTENANCE_CHECKS', None)
        else:
            os.environ['RECALL_MAINTENANCE_CHECKS'] = previous


def snapshot(cfg, run):
    scanned = engine.scan(cfg['limit'])
    if scanned['errors']:
        raise engine.Refused('scan has catalog errors')
    if not scanned['candidates']:
        return None
    label = scanned['candidates'][0]['catalog']
    candidates = [x for x in scanned['candidates'] if x['catalog'] == label]
    path = engine.catalog_path(label)
    raw = path.read_bytes(); rows = engine.load_catalog(raw); text = raw.decode()
    checks_raw = cfg['checks_path'].read_bytes()
    checks = engine.parse_json(checks_raw); engine.require_version(checks)
    if not isinstance(checks.get('checks'), dict):
        raise engine.Refused('trusted checks must be mapping')
    payload = {'schema_version': 1, 'role': 'proposer', 'catalog': label,
               'catalog_path': str(path), 'catalog_sha256': engine.digest(raw),
               'checks_sha256': engine.digest(checks_raw), 'trusted_checks': checks,
               'allowed_actions': cfg['allowed_actions'], 'candidates': [], 'evidence': []}
    for item in candidates:
        row = rows.get(item['id'])
        if row is None or engine.entry_hash(row) != item['entry_sha256']:
            raise engine.Refused('catalog changed during snapshot')
        start, end, _, _ = engine.entry_span(text, item['id'])
        exact = text[start:end]
        if len(exact.encode()) > MAX_ENTRY:
            raise engine.Refused('candidate exceeds bounded raw entry size; needs manual review')
        rawpath = run / ('entry-' + str(len(payload['candidates'])) + '.yaml')
        engine.atomic_write(rawpath, exact.encode())
        payload['candidates'].append(dict(item, raw_entry=exact,
            raw_snapshot_reference={'path': str(rawpath), 'sha256': engine.digest(exact.encode())}))
    # Only owner-configured evidence paths are read, never paths embedded in text.
    refs = cfg.get('evidence_paths', [])
    if not isinstance(refs, list) or len(refs) > 6:
        raise engine.Refused('evidence_paths must contain at most six trusted paths')
    for supplied in refs:
        ref = {'path': supplied} if isinstance(supplied, str) else supplied
        if not isinstance(ref, dict):
            raise engine.Refused('invalid trusted evidence reference')
        evidence_path = Path(engine.nonempty(ref, 'path')).expanduser()
        if not evidence_path.is_absolute():
            raise engine.Refused('evidence path must be absolute')
        evidence = evidence_path.read_bytes()
        source = evidence.decode('utf-8')
        if 'start_line' in ref or 'end_line' in ref:
            lo, hi = ref.get('start_line'), ref.get('end_line')
            if type(lo) is not int or type(hi) is not int or not 1 <= lo <= hi:
                raise engine.Refused('invalid trusted excerpt range')
            lines = source.splitlines(keepends=True)
            if hi > len(lines):
                raise engine.Refused('evidence excerpt outside file')
            source = ''.join(lines[lo-1:hi])
        if len(source.encode()) > MAX_EVIDENCE:
            raise engine.Refused('evidence exceeds bound; configure an exact line excerpt')
        payload['evidence'].append({'reference': {'path': str(evidence_path),
            'sha256': engine.digest(evidence)}, 'content': source,
            'line_range': [ref.get('start_line'), ref.get('end_line')],
            'verification_scope': 'Local file hash verified; source claims require independent semantic review.'})
    outcome_ids = {oid for item in payload['candidates'] for oid in item.get('outcome_ids', [])}
    available = {engine.entry_hash(record): record for record in engine.outcome_records()}
    if outcome_ids - set(available):
        raise engine.Refused('scan outcome snapshot is no longer available')
    payload['outcomes'] = [{'outcome_id': oid, 'record': available[oid]} for oid in sorted(outcome_ids)]
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAYLOAD:
        raise engine.Refused('snapshot exceeds total payload bound')
    return payload


def invoke(command, request, output, timeout):
    argv = [x.replace('{request}', str(request)).replace('{output}', str(output)) for x in command]
    env = dict(os.environ, RECALL_MAINTENANCE_WORKER='1', PYTHONDONTWRITEBYTECODE='1')
    with output.with_suffix('.stdout').open('wb') as stdout, output.with_suffix('.stderr').open('wb') as stderr:
        proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            raise engine.Refused('worker timed out')
    if code:
        raise engine.Refused('worker command failed: ' + str(code))
    if not output.is_file() or output.stat().st_size > MAX_OUTPUT:
        raise engine.Refused('missing or oversized worker output')
    result = engine.read_json(output)
    if not isinstance(result, dict):
        raise engine.Refused('worker output must be JSON object')
    engine.require_version(result)
    return result


def plan_from(cfg, payload, proposed):
    if set(proposed) != {'schema_version', 'actions'}:
        raise engine.Refused('proposer output fields invalid')
    actions = proposed.get('actions')
    if not isinstance(actions, list) or not 1 <= len(actions) <= cfg['limit']:
        raise engine.Refused('proposal deferred or has invalid action count')
    selected = {x['id']: x for x in payload['candidates']}
    evidence = {(e['reference']['path'], e['reference']['sha256']) for e in payload['evidence']}
    for action in actions:
        if not isinstance(action, dict) or action.get('id') not in selected:
            raise engine.Refused('proposal selected an unsnapshotted record')
        if action.get('type') not in cfg['allowed_actions']:
            raise engine.Refused('action outside trusted unattended scope')
        if action.get('entry_sha256') != selected[action['id']]['entry_sha256']:
            raise engine.Refused('proposal entry does not bind snapshot')
        if action['type'] == 'merge':
            target = selected.get(action.get('merge_into'))
            if target is None or action.get('merge_into_sha256') != target['entry_sha256']:
                raise engine.Refused('merge target must bind a selected snapshot record')
        allowed_refs = set(evidence)
        if action['type'] == 'keep':
            r = selected[action['id']]['raw_snapshot_reference']
            allowed_refs.add((r['path'], r['sha256']))
        refs = action.get('evidence')
        if not isinstance(refs, list) or not refs:
            raise engine.Refused('proposal needs snapshot evidence')
        for ref in refs:
            if not isinstance(ref, dict) or (ref.get('path'), ref.get('sha256')) not in allowed_refs:
                raise engine.Refused('proposal evidence was not in verified snapshot')
        changes = action.get('changes', {})
        if not isinstance(changes, dict):
            raise engine.Refused('changes must be mapping')
        for field, value in changes.items():
            if field in REMEDIES and (not isinstance(value, str) or len(value) > 300):
                raise engine.Refused('operative remedy must fit 300 characters')
    return {'schema_version': 1, 'author': cfg['author'], 'catalog': payload['catalog'],
            'catalog_sha256': payload['catalog_sha256'], 'checks_sha256': payload['checks_sha256'],
            'actions': actions,
            'outcome_ids': sorted({oid for action in actions
                                   for oid in selected[action['id']].get('outcome_ids', [])})}


def perform(cfg, run, state):
    if state.get('pending_plan'):
        with configured_checks(cfg['checks_path']):
            return engine.apply_plan(state['pending_plan'], state['pending_review'], apply=True)
    payload = snapshot(cfg, run)
    if payload is None:
        return {'state': 'empty'}
    request = run / 'request.json'; engine.write_json(request, payload)
    proposed = invoke(cfg['proposer_command'], request, run / 'proposed.json', cfg['timeout_seconds'])
    plan = plan_from(cfg, payload, proposed)
    planpath = run / 'plan.json'; engine.write_json(planpath, plan)
    plan_sha = engine.digest(planpath.read_bytes())
    context = {'schema_version': 1, 'role': 'reviewer', 'plan': plan, 'plan_sha256': plan_sha,
               'checks_sha256': payload['checks_sha256'], 'snapshot': payload}
    context_sha = engine.entry_hash(context)
    context['review_context_sha256'] = context_sha
    reviewrequest = run / 'review-request.json'; engine.write_json(reviewrequest, context)
    reviewed = invoke(cfg['reviewer_command'], reviewrequest, run / 'reviewed.json', cfg['timeout_seconds'])
    required = {'schema_version', 'approved', 'plan_sha256', 'checks_sha256', 'review_context_sha256', 'rationale'}
    if (set(reviewed) != required or reviewed.get('approved') is not True
            or reviewed.get('plan_sha256') != plan_sha
            or reviewed.get('checks_sha256') != payload['checks_sha256']
            or reviewed.get('review_context_sha256') != context_sha
            or not isinstance(reviewed.get('rationale'), str) or not reviewed['rationale'].strip()):
        raise engine.Refused('independent review refused or not bound to exact plan/check/context')
    review = dict(reviewed, reviewer=cfg['reviewer'])
    reviewpath = run / 'review.json'; engine.write_json(reviewpath, review)
    with configured_checks(cfg['checks_path']):
        # Engine rechecks current catalog, entry, evidence and check hashes at write.
        result = engine.apply_plan(planpath, reviewpath, apply=True)
    if result.get('state') == 'pending_index_repair':
        state.update(pending_plan=str(planpath), pending_review=str(reviewpath))
    return result


def run_tick(force=False):
    if os.environ.get('RECALL_MAINTENANCE_WORKER') == '1':
        return {'state': 'worker_recursion'}
    try:
        cfg = config_load()
        if cfg is None or not cfg['enabled']:
            return {'state': 'disabled'}
        cfg['lock_path'].parent.mkdir(parents=True, exist_ok=True)
        with cfg['lock_path'].open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'state': 'tick_busy'}
            if capture_busy(cfg):
                return {'state': 'capture_busy'}
            state = engine.read_json(cfg['state_path']) if cfg['state_path'].exists() else {
                'schema_version': 1, 'attempts': 0, 'completed': 0}
            current = time.time()
            if not force and current < state.get('retry_after', 0):
                return {'state': 'backoff'}
            if not force and current < state.get('next_due_at', 0) and not state.get('pending_plan'):
                return {'state': 'not_due'}
            run = cfg['runs_path'] / (str(int(current)) + '-' + uuid.uuid4().hex[:12])
            run.mkdir(parents=True)
            state.update(attempts=state.get('attempts', 0) + 1, last_attempt_at=current,
                         last_run=str(run), retry_after=current + cfg['backoff_seconds'])
            engine.write_json(cfg['state_path'], state)
            try:
                result = perform(cfg, run, state)
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                result = {'state': 'refused', 'error': str(exc)}
            if result.get('state') in {'applied', 'already_applied', 'empty'}:
                state.update(completed=state.get('completed', 0) + 1,
                             last_completed_at=time.time(), next_due_at=time.time() + cfg['period_seconds'])
                for key in ('retry_after', 'pending_plan', 'pending_review'):
                    state.pop(key, None)
            else:
                state['retry_after'] = time.time() + cfg['backoff_seconds']
            state['last_result'] = result.get('state')
            engine.write_json(run / 'result.json', result)
            engine.write_json(cfg['state_path'], state)
            return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'state': 'refused', 'error': str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='ignore due/backoff timing for an explicit local run')
    args = parser.parse_args(argv)
    result = run_tick(args.force)
    print(json.dumps(result, default=str))
    return 2 if result['state'] in {'refused', 'pending_index_repair'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
