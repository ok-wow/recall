#!/usr/bin/env python3
"""Review and maintain private FM/PF catalogs. Application is explicit and local.

The shared sidecar lock serializes cooperating writers only. Catalog hashes
detect intervening edits; neither mechanism controls arbitrary external editors.
No plan or check configuration can supply executable commands.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

from bump_recurrence import DupCatch, entries, entry_span, _field_span, dump_field

CATALOGS = {'FM': 'FAILURE_MODES.yaml', 'PF': 'PROCESS_FAILURES.yaml'}
INACTIVE = {'retired', 'superseded', 'archived', 'removed'}
EDITABLE = {'fix_pattern', 'fix', 'summary', 'trigger', 'probe_when', 'symptom', 'failure_mode', 'name',
            'title', 'description', 'remedy', 'guidance'}
RESULTS = {'helpful', 'partial', 'failed', 'corrected', 'rejected', 'unknown'}


class Refused(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def entry_hash(entry):
    return digest(json.dumps(entry, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), default=str).encode('utf-8'))


def guidance_revision(row):
    """Keep semantic/provenance fields; omit only known recurrence telemetry.

    The full entry hash still guards writes and review suppression. Use counts
    stay conservative because their semantics are not standardized here.
    """
    return entry_hash({key: value for key, value in row.items()
                       if key not in {'recurrences', 'recurrence'}
                       and not re.fullmatch(r'recurrence[_-]\d{4}[_-]\d{2}[_-]\d{2}', str(key))})


def outcome_scheme(record):
    scheme = record.get('revision_scheme', 'entry_v1')
    if scheme not in {'entry_v1', 'guidance_v1'}:
        raise Refused('unknown outcome revision_scheme: ' + str(scheme))
    return scheme


def lesson_revision(row, scheme):
    return guidance_revision(row) if scheme == 'guidance_v1' else entry_hash(row)


def now():
    return datetime.now(timezone.utc).isoformat()


def env_path(primary, legacy, default):
    return Path(os.environ.get(primary) or os.environ.get(legacy) or str(default)).expanduser()


def home():
    return env_path('RECALL_HOME', 'OKWOW_HOME', Path.home() / '.recall')


def catalog_path(label):
    if label not in CATALOGS:
        raise Refused('needs-review: only FM/PF catalogs are writable')
    root = env_path('RECALL_CATALOG_DIR', 'OKWOW_CATALOG_DIR', home() / 'catalogs').resolve()
    path = root / CATALOGS[label]
    if path.is_symlink() or path.resolve().parent != root:
        raise Refused('catalog symlink/outside path is not a writable target')
    return path


@contextmanager
def catalog_lock(path):
    """Shared protocol for catalog writers: hold this through re-read and replace."""
    lock = Path(str(Path(path).resolve()) + '.maintenance.lock')
    with lock.open('a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, str(path))
        dirfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n').encode())


def parse_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Refused('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def read_json(path):
    return parse_json(Path(path).read_bytes())


def require_version(obj):
    if not isinstance(obj, dict):
        raise Refused('JSON document must be an object')
    if type(obj.get('schema_version')) is not int or obj['schema_version'] != 1:
        raise Refused('schema_version must be 1')


def nonempty(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refused('required nonempty string: ' + key)
    return value


def load_catalog(raw):
    try:
        rows = entries(yaml.load(raw.decode('utf-8'), DupCatch))
    except (yaml.YAMLError, SystemExit, UnicodeError) as exc:
        raise Refused('broken catalog: ' + str(exc))
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise Refused('every catalog row must have a string id')
        if row['id'] in result:
            raise Refused('duplicate catalog id: ' + row['id'])
        result[row['id']] = row
    return result


def verify_evidence(refs):
    if not isinstance(refs, list) or not refs:
        raise Refused('independent local evidence is required')
    for ref in refs:
        path = Path(nonempty(ref, 'path')).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise Refused('evidence must be an existing absolute local file')
        if digest(path.read_bytes()) != ref.get('sha256'):
            raise Refused('evidence hash changed: ' + str(path))


def load_checks(plan):
    configured = os.environ.get('RECALL_MAINTENANCE_CHECKS') or os.environ.get('OKWOW_MAINTENANCE_CHECKS')
    if not configured:
        raise Refused('RECALL_MAINTENANCE_CHECKS must name owner-managed trusted checks')
    path = Path(configured).expanduser()
    raw = path.read_bytes()
    if digest(raw) != plan.get('checks_sha256'):
        raise Refused('trusted check configuration hash mismatch')
    config = parse_json(raw)
    require_version(config)
    checks = config.get('checks')
    if not isinstance(checks, dict):
        raise Refused('trusted checks must be a mapping')
    for check in checks.values():
        if check.get('kind') not in {'field_contains', 'field_equals', 'field_absent'}:
            raise Refused('unsupported trusted check kind')
        nonempty(check, 'field')
    return checks


def run_checks(action, row, checks):
    names = action.get('checks')
    if not isinstance(names, list) or not names:
        raise Refused('each action requires named trusted deterministic checks')
    for name in names:
        if name not in checks:
            raise Refused('unknown trusted check: ' + str(name))
        check = checks[name]
        field, kind = check['field'], check['kind']
        value = row.get(field)
        passed = ((kind == 'field_contains' and isinstance(value, str)
                   and isinstance(check.get('value'), str) and check['value'] in value)
                  or (kind == 'field_equals' and value == check.get('value'))
                  or (kind == 'field_absent' and field not in row))
        if not passed:
            raise Refused('failed deterministic check: ' + name)


def replace_fields(text, eid, changes):
    """Use existing spans and preserve bytes outside the named fields."""
    for field, value in changes.items():
        start, end, id_end, indent = entry_span(text, eid)
        body = text[start:end]
        if re.search(r'^' + re.escape(indent + field) + r':', body, re.M):
            lo, hi = _field_span(body, field, indent)
            # The recurrence helper includes inter-field comments in its span.
            # Keep these bytes: they can document the next provenance field.
            tail = body[lo:hi].splitlines(keepends=True)
            while len(tail) > 1 and (not tail[-1].strip() or tail[-1].lstrip().startswith('#')):
                hi -= len(tail.pop())
            body = body[:lo] + dump_field(field, value, indent) + body[hi:]
        else:
            at = id_end - start
            body = body[:at] + dump_field(field, value, indent) + body[at:]
        text = text[:start] + body + text[end:]
    return text


def prepare(planpath, reviewpath):
    plan_bytes = Path(planpath).read_bytes()
    review_bytes = Path(reviewpath).read_bytes()
    plan = parse_json(plan_bytes)
    require_version(plan)
    author = nonempty(plan, 'author')
    plan_sha = digest(plan_bytes)
    review = parse_json(review_bytes)
    require_version(review)
    if (review.get('approved') is not True or review.get('plan_sha256') != plan_sha
            or nonempty(review, 'reviewer').strip().casefold() == author.strip().casefold()):
        raise Refused('approved independent review must bind exact plan bytes')
    checks = load_checks(plan)
    path = catalog_path(plan.get('catalog'))
    actions = plan.get('actions')
    if not isinstance(actions, list) or not 1 <= len(actions) <= 3:
        raise Refused('one to three actions required')
    seen = set()
    for action in actions:
        eid = nonempty(action, 'id')
        if eid in seen:
            raise Refused('duplicate action id')
        seen.add(eid)
        if action.get('type') not in {'keep', 'rewrite', 'retire', 'merge'}:
            raise Refused('unsupported action; needs-review')
        for key in ('rationale', 'trigger', 'nearby_non_trigger', 'entry_sha256'):
            nonempty(action, key)
        verify_evidence(action.get('evidence'))
    validate_outcome_ids(plan)
    return plan, plan_sha, path, checks, plan_bytes, review_bytes


def validate_outcome_ids(plan, rows=None):
    ids = plan.get('outcome_ids')
    if (not isinstance(ids, list) or any(not isinstance(oid, str) for oid in ids)
            or len(ids) != len(set(ids))):
        raise Refused('reviewed plan requires explicit unique outcome_ids (empty allowed)')
    affected = {action['id'] for action in plan['actions']}
    outcomes = {entry_hash(record): record for record in outcome_records()}
    for oid in ids:
        record = outcomes.get(oid)
        if (record is None or record.get('catalog') != plan['catalog']
                or record.get('entry_id') not in affected):
            raise Refused('reviewed outcome must match affected entry and exact lesson revision: ' + oid)
        scheme = outcome_scheme(record)
        if rows is not None:
            row = rows.get(record['entry_id'])
            if row is None or record.get('lesson_revision') != lesson_revision(row, scheme):
                raise Refused('reviewed outcome must match affected entry and exact lesson revision: ' + oid)


def patched_catalog(plan, raw, checks):
    if digest(raw) != plan.get('catalog_sha256'):
        raise Refused('stale catalog hash; review a new plan')
    before = load_catalog(raw)
    action_types = {a['id']: a['type'] for a in plan['actions']}
    for action in plan['actions']:
        if action['type'] == 'merge' and action_types.get(action.get('merge_into')) in {'merge', 'retire'}:
            raise Refused('merge target cannot be retired or superseded by this plan')
    text = raw.decode('utf-8')
    expected = dict(before)
    for action in plan['actions']:
        eid = action['id']
        if eid not in before or entry_hash(before[eid]) != action['entry_sha256']:
            raise Refused('stale or wrong entry: ' + eid)
        changes = action.get('changes', {})
        if not isinstance(changes, dict) or set(changes) - EDITABLE:
            raise Refused('changes may edit guidance fields only; preserve ID/provenance')
        if action['type'] == 'rewrite':
            if not changes:
                raise Refused('rewrite requires guidance changes')
        elif changes:
            raise Refused('only rewrite accepts changes')
        if action['type'] == 'retire':
            changes = {'status': 'retired'}
        elif action['type'] == 'merge':
            target = nonempty(action, 'merge_into')
            if ':' in target or target == eid or target not in before:
                raise Refused('needs-review: merge target must be another entry in same catalog')
            if entry_hash(before[target]) != action.get('merge_into_sha256'):
                raise Refused('stale merge target')
            if str(before[target].get('status', '')).strip().lower() in INACTIVE:
                raise Refused('merge target must be active')
            changes = {'status': 'superseded', 'superseded_by': target}
        updated = dict(before[eid], **changes)
        run_checks(action, updated, checks)
        expected[eid] = updated
        text = replace_fields(text, eid, changes)
    after = load_catalog(text.encode('utf-8'))
    if after != expected or set(after) != set(before):
        raise Refused('unexpected semantic change or lost ID; refusing write')
    return text.encode('utf-8')


def git_dirty(path):
    result = subprocess.run(['git', '-C', str(path.parent), 'status', '--porcelain', '--', str(path)],
                            capture_output=True, text=True)
    return result.returncode != 0 or bool(result.stdout.strip())


def rebuild_index():
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('build_probe_index.py'))],
                            capture_output=True, text=True, timeout=120)
    return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def apply_plan(planpath, reviewpath, apply=False):
    plan, operation, path, checks, plan_bytes, review_bytes = prepare(planpath, reviewpath)
    journalpath = home() / 'maintenance' / 'operations' / operation / 'journal.json'
    backuppath = journalpath.with_name('catalog.before.yaml')
    if not apply:
        raw = path.read_bytes()
        validate_outcome_ids(plan, load_catalog(raw))
        patched = patched_catalog(plan, raw, checks)
        return {'state': 'dry_run', 'operation_id': operation,
                'before_sha256': digest(raw), 'after_sha256': digest(patched)}
    with catalog_lock(path):
        if (Path(planpath).read_bytes() != plan_bytes
                or Path(reviewpath).read_bytes() != review_bytes):
            raise Refused('plan or review changed while waiting; obtain review of exact current bytes')
        # Validation can wait behind another writer. Bind evidence and trusted
        # predicates again at the actual application boundary.
        for action in plan['actions']:
            verify_evidence(action['evidence'])
        checks = load_checks(plan)
        raw = path.read_bytes()
        if journalpath.exists():
            journal = read_json(journalpath)
            if (journal.get('operation_id') != operation or journal.get('plan') != plan
                    or journal.get('review') != parse_json(review_bytes)
                    or journal.get('catalog_path') != str(path)
                    or journal.get('backup') != str(backuppath)
                    or journal.get('outcome_ids_reviewed') != plan['outcome_ids']
                    or journalpath.with_name('plan.json').read_bytes() != plan_bytes
                    or journalpath.with_name('review.json').read_bytes() != review_bytes):
                raise Refused('transaction does not bind the exact reviewed operation; manual recovery required')
            if digest(raw) != journal['after_sha256']:
                raise Refused('operation exists but current catalog differs; manual recovery required')
            preimage = backuppath.read_bytes()
            if (digest(preimage) != journal['before_sha256']
                    or journal['before_sha256'] != plan['catalog_sha256']):
                raise Refused('transaction backup does not match reviewed pre-image')
            # An index-only retry must judge the original reviewed lesson, not
            # the post-image it already wrote. Verify that exact transition in
            # memory; never replace catalog bytes or consume later outcomes.
            validate_outcome_ids(plan, load_catalog(preimage))
            if patched_catalog(plan, preimage, checks) != raw:
                raise Refused('transaction post-image differs from the reviewed change')
            if journal['state'] == 'applied':
                return dict(journal, state='already_applied')
            if journal['state'] not in {'catalog_written', 'pending_index_repair'}:
                raise Refused('incomplete transaction needs manual recovery')
        else:
            validate_outcome_ids(plan, load_catalog(raw))
            patched = patched_catalog(plan, raw, checks)
            dirty = git_dirty(path)
            journal = {'schema_version': 1, 'operation_id': operation, 'state': 'prepared',
                       'catalog': plan['catalog'], 'catalog_path': str(path),
                       'before_sha256': digest(raw), 'after_sha256': digest(patched),
                       'backup': str(backuppath), 'journal': str(journalpath),
                       'plan': plan, 'review': parse_json(review_bytes), 'time': now(),
                       'outcome_ids_reviewed': list(plan['outcome_ids']),
                       'dispositions': [{'id': a['id'], 'type': a['type'],
                                         'entry_sha256': entry_hash(load_catalog(patched)[a['id']])}
                                        for a in plan['actions']],
                       'commit': 'deferred: target dirty or not in git' if dirty else 'pending'}
            atomic_write(backuppath, raw)
            atomic_write(journalpath.with_name('plan.json'), plan_bytes)
            atomic_write(journalpath.with_name('review.json'), review_bytes)
            write_json(journalpath, journal)
            # A cooperating writer holds the sidecar too. Detect known external
            # changes before replacement, without claiming arbitrary editors lock.
            if digest(path.read_bytes()) != plan['catalog_sha256']:
                raise Refused('catalog changed under lock; transaction retained for inspection')
            atomic_write(path, patched)
            journal['state'] = 'catalog_written'
            write_json(journalpath, journal)
            if not dirty:
                from store_commit import commit_paths
                committed, note = commit_paths([path], 'Recall maintenance ' + operation[:12])
                journal['commit'] = note
                journal['committed'] = committed
        try:
            ok, detail = rebuild_index()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, detail = False, str(exc)
        journal['index_result'] = detail
        journal['state'] = 'applied' if ok else 'pending_index_repair'
        journal['updated_at'] = now()
        write_json(journalpath, journal)
        return journal


def outcome_records():
    root = home() / 'maintenance' / 'outcomes'
    return [read_json(p) for p in sorted(root.glob('*.json'))]


def ingest_outcome(path):
    record = read_json(path)
    require_version(record)
    outcome_scheme(record)
    author, verifier = nonempty(record, 'author'), nonempty(record, 'verifier')
    if author == verifier:
        raise Refused('outcome needs an independent verifier')
    if record.get('result') not in RESULTS or record.get('catalog') not in CATALOGS:
        raise Refused('unsupported outcome result/catalog')
    for key in ('entry_id',):
        nonempty(record, key)
    for key in ('lesson_revision', 'run', 'task', 'arm', 'session', 'time', 'actual_tokens'):
        if key not in record:
            raise Refused('outcome field required (nullable): ' + key)
    tokens = record['actual_tokens']
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise Refused('actual_tokens must be null or a nonnegative integer')
    verify_evidence(record.get('evidence'))
    oid = entry_hash(record)
    dest = home() / 'maintenance' / 'outcomes' / (oid + '.json')
    if dest.exists():
        return {'state': 'already_recorded', 'outcome_id': oid}
    write_json(dest, record)
    return {'state': 'recorded', 'outcome_id': oid, 'path': str(dest)}


def scan(limit=3):
    if not 1 <= limit <= 100:
        raise Refused('limit must be 1..100')
    failures = {}
    negative_outcomes = []
    for record in outcome_records():
        scheme = outcome_scheme(record)
        if record.get('result') in {'failed', 'corrected', 'rejected'}:
            key = (record['catalog'], record['entry_id'], scheme, record.get('lesson_revision'))
            failures.setdefault(key, set()).add(entry_hash(record))
            negative_outcomes.append(record)
    reviewed = {}
    for journalpath in sorted((home() / 'maintenance' / 'operations').glob('*/journal.json')):
        journal = read_json(journalpath)
        if journal.get('state') == 'applied':
            for action in journal.get('dispositions', []):
                key = (journal['catalog'], action['id'], action['entry_sha256'])
                reviewed.setdefault(key, set()).update(journal.get('outcome_ids_reviewed', []))
    candidates = []
    errors = []
    current_revisions = {}
    for label in CATALOGS:
        path = catalog_path(label)
        if not path.exists():
            continue
        raw = path.read_bytes()
        try:
            rows = load_catalog(raw)
        except Refused as exc:
            errors.append({'catalog': label, 'error': str(exc)})
            continue
        signatures = Counter(' '.join(str(row.get('fix_pattern', '')).lower().split())
                             for row in rows.values()
                             if str(row.get('status', '')).strip().lower() not in INACTIVE)
        for eid, row in rows.items():
            current_revisions[(label, eid, 'entry_v1')] = entry_hash(row)
            current_revisions[(label, eid, 'guidance_v1')] = guidance_revision(row)
            if str(row.get('status', '')).strip().lower() in INACTIVE:
                continue
            reasons = []
            known = reviewed.get((label, eid, entry_hash(row)))
            current_failures = failures.get((label, eid, 'entry_v1', entry_hash(row)), set()) | failures.get(
                (label, eid, 'guidance_v1', guidance_revision(row)), set())
            pending_failures = current_failures - (known or set())
            if known is not None and not pending_failures:
                continue
            if pending_failures:
                reasons.append('failed_independent_outcome')
            notes = ' '.join(str(v) for k, v in row.items() if str(k).startswith('recurrence_'))
            if re.search(r'correct|instead|counterexample|qualif|wrong|narrow', notes, re.I):
                reasons.append('hidden_corrective_note')
            if row.get('recurrences'):
                reasons.append('recurrence')
            signature = ' '.join(str(row.get('fix_pattern', '')).lower().split())
            if signature and signatures[signature] > 1:
                reasons.append('duplicate_signature')
            if reasons:
                candidates.append({'catalog': label, 'id': eid, 'catalog_path': str(path),
                                   'catalog_sha256': digest(raw), 'entry_sha256': entry_hash(row),
                                   'guidance_revision': guidance_revision(row), 'revision_scheme': 'guidance_v1',
                                   'outcome_ids': sorted(pending_failures),
                                   'reasons': reasons})
    rank = {'failed_independent_outcome': 0, 'hidden_corrective_note': 1,
            'recurrence': 2, 'duplicate_signature': 3}
    candidates.sort(key=lambda r: (min(rank[x] for x in r['reasons']), r['catalog'], r['id']))
    historical = [{'outcome_id': entry_hash(r), 'catalog': r['catalog'], 'id': r['entry_id'],
                   'revision_scheme': outcome_scheme(r),
                   'lesson_revision': r.get('lesson_revision'), 'reason': 'not_current_revision'}
                  for r in negative_outcomes
                  if current_revisions.get((r['catalog'], r['entry_id'], outcome_scheme(r))) != r.get('lesson_revision')]
    return {'schema_version': 1, 'candidates': candidates[:limit], 'total_candidates': len(candidates),
            'historical_outcomes': historical,
            'errors': errors, 'scope': 'FM/PF; other guidance requires review'}


def report():
    records = outcome_records()
    return {'schema_version': 1, 'counts': dict(Counter(r['result'] for r in records)),
            'total': len(records), 'interpretation': 'Descriptive independently evidenced outcomes; not causal proof from exposures.'}


def revision(catalog, eid):
    rows = load_catalog(catalog_path(catalog).read_bytes())
    if eid not in rows:
        raise Refused('entry not found: ' + eid)
    return {'schema_version': 1, 'catalog': catalog, 'id': eid,
            'entry_sha256': entry_hash(rows[eid]), 'guidance_revision': guidance_revision(rows[eid]),
            'revision_scheme': 'guidance_v1'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('scan'); p.add_argument('--limit', type=int, default=3)
    p = sub.add_parser('revision')
    p.add_argument('--catalog', choices=tuple(CATALOGS), required=True)
    p.add_argument('--id', required=True)
    for command in ('validate', 'apply'):
        p = sub.add_parser(command)
        p.add_argument('--plan', required=True)
        p.add_argument('--review', required=True)
        if command == 'apply':
            p.add_argument('--apply', action='store_true', help='write the reviewed plan (otherwise dry-run)')
    p = sub.add_parser('outcome'); p.add_argument('--file', required=True)
    sub.add_parser('report')
    args = parser.parse_args(argv)
    try:
        if args.command == 'scan':
            result = scan(args.limit)
        elif args.command == 'revision':
            result = revision(args.catalog, args.id)
        elif args.command == 'outcome':
            result = ingest_outcome(args.file)
        elif args.command == 'report':
            result = report()
        else:
            result = apply_plan(args.plan, args.review, getattr(args, 'apply', False))
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 2 if result.get('state') == 'pending_index_repair' else 0
    except (Refused, OSError, ValueError, KeyError, TypeError, SystemExit) as exc:
        print(json.dumps({'state': 'refused', 'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
