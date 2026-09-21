#!/usr/bin/env python3
"""normalize_catalog_yaml.py — keep okWOW knowledge YAML strict-parseable.

okwow-compound appends entries to FAILURE_MODES.yaml / PROCESS_FAILURES.yaml /
learnings.yaml / extracted_registry.yaml. These files are NOT strict-YAML by
default — a `probe_when:` item that STARTS with a backtick (`` `gh pr list` ``),
a value that opens with a quote then continues unquoted (`"my PRs" scoped...`),
or a multi-line plain value whose continuation contains a colon all break
`yaml.safe_load`. This tool fixes exactly those scalars by wrapping them in a
double-quoted scalar — meaning preserved (the literal text, including any
backticks/quotes, is kept verbatim). It NEVER touches block scalars (`>` / `|`),
already-valid lines, or non-string scalars (ints/bools/null).

It does NOT fix structural drift (column-0 entries, orphaned blocks) — those
come from malformed appends; this tool's job is the recurring scalar-quoting
issue. If a file is structurally broken in a way quoting can't fix, --apply
refuses to write and reports the stuck line.

USAGE
  normalize_catalog_yaml.py FILE [FILE ...]            # --check (report only; exit 1 if any file is broken/would-change)
  normalize_catalog_yaml.py FILE [FILE ...] --apply    # back up (.bak-<ts>), fix in place, validate

okwow-compound Stage 4 runs this with --apply on every catalog/state file it
touched, and confirms strict parse before finishing.
"""
import sys, re, yaml, datetime, shutil

KEYWORD    = r'[A-Za-z0-9_][A-Za-z0-9_ -]*'          # word-ish keys only
SEQ_SCALAR = re.compile(r'^(\s*)-\s+(.*)$')
MAP_VALUE  = re.compile(rf'^(\s*)({KEYWORD}):[ \t]+(.+)$')
NESTED_KEY = re.compile(rf'^{KEYWORD}:(\s|$)')
NEW_NODE   = re.compile(rf'^\s*(-\s|{KEYWORD}:(\s|$))')


def quote(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def is_block_opener(val):
    return re.match(r'^[>|][+-]?\d*\s*$', val) is not None


def gather(lines, i, anchor_indent):
    """Join continuation lines of a multi-line scalar starting at line i."""
    parts, j = [], i + 1
    while j < len(lines):
        ln = lines[j]
        st = ln.strip()
        ind = len(ln) - len(ln.lstrip(' '))
        if st == '':
            k = j + 1
            while k < len(lines) and lines[k].strip() == '':
                k += 1
            if k >= len(lines) or (len(lines[k]) - len(lines[k].lstrip(' '))) <= anchor_indent:
                break
            parts.append(''); j += 1; continue
        if ind <= anchor_indent or NEW_NODE.match(ln):
            break
        parts.append(st); j += 1
    return parts, j - 1


def fix_at(lines, err_line):
    """Re-quote the scalar node whose (possibly multi-line) value covers err_line."""
    for cand in range(min(err_line, len(lines) - 1), max(-1, err_line - 8), -1):
        if cand < 0:
            continue
        line = lines[cand]
        prefix = content = None
        anchor = 0
        m = SEQ_SCALAR.match(line)
        if m:
            lead, c = m.group(1), m.group(2)
            if NESTED_KEY.match(c) or is_block_opener(c):
                m = None
            else:
                prefix, content, anchor = f"{lead}- ", c, len(lead)
        if m is None:
            mp = MAP_VALUE.match(line)
            if not mp or is_block_opener(mp.group(3)):
                continue
            lead, key, content = mp.group(1), mp.group(2), mp.group(3)
            prefix, anchor = f"{lead}{key}: ", len(lead)
        cont, last = gather(lines, cand, anchor)
        if last < err_line:
            continue
        logical = content if not cont else (content + ' ' + ' '.join(c for c in cont if c != '')).strip()
        return lines[:cand] + [prefix + quote(logical)] + lines[last + 1:]
    return None


def normalize(text, max_iter=500):
    """Iteratively quote-fix until safe_load passes. Returns (lines, iters) or
    ('FAIL', bad_line, lines, iters)."""
    lines = text.split('\n')
    for it in range(max_iter):
        try:
            yaml.safe_load('\n'.join(lines))
            return lines, it
        except yaml.YAMLError as e:
            mark = getattr(e, 'problem_mark', None)
            if mark is None:
                return ('FAIL', -1, lines, it)
            new = fix_at(lines, mark.line)
            if new is None or new == lines:
                return ('FAIL', mark.line, lines, it)
            lines = new
    return ('FAIL', -1, lines, max_iter)


def process(path, apply):
    text = open(path).read()
    try:
        yaml.safe_load(text)
        print(f"  ✓ {path}: already strict-parseable (no change)")
        return True
    except yaml.YAMLError:
        pass
    res = normalize(text)
    if isinstance(res, tuple) and res[0] == 'FAIL':
        _, bad, lines, _ = res
        print(f"  ✗ {path}: could NOT auto-fix (likely structural, not scalar-quoting).")
        if bad >= 0:
            for k in range(max(0, bad - 1), min(len(lines), bad + 2)):
                print(f"      {k+1}: {lines[k]}")
        return False
    lines, iters = res
    new_text = '\n'.join(lines)
    diffs = sum(1 for a, b in zip(text.split('\n'), lines) if a != b)
    if apply:
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        shutil.copy(path, f"{path}.bak-{ts}")
        open(path, 'w').write(new_text if new_text.endswith('\n') else new_text + '\n')
        print(f"  ✓ {path}: FIXED {diffs} scalar(s) in {iters} pass(es); backup .bak-{ts}")
    else:
        print(f"  ! {path}: NOT strict — would quote-fix {diffs} scalar(s) in {iters} pass(es). Re-run with --apply.")
    return apply


def main():
    args = [a for a in sys.argv[1:] if a != '--apply']
    apply = '--apply' in sys.argv
    if not args:
        print(__doc__.split('USAGE')[1] if 'USAGE' in __doc__ else "usage: normalize_catalog_yaml.py FILE [--apply]")
        sys.exit(2)
    print(f"normalize_catalog_yaml ({'apply' if apply else 'check'} mode):")
    ok = all(process(p, apply) for p in args)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
