#!/usr/bin/env python3
"""scrub: nothing shaped like a credential leaves the machine; when unsure, nothing leaves.

Secret-shaped strings are assembled at runtime so no scanner (GitHub push
protection included) sees a literal in this file. Shapes come from the vendors'
public token formats, not from scrub.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import check, finish  # noqa: E402

import scrub  # noqa: E402

A36 = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6"
SECRETS = {
    "github classic token": "gh" + "p_" + A36,
    "github fine-grained": "github" + "_pat_" + "11AB" + A36 * 2,
    "anthropic key": "sk-" + "ant-api03-" + A36 + "xyz",
    "aws access key id": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "slack token": "xo" + "xb-1234567890-abcdefghijklmnop",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
    "bearer token": "Bearer " + A36 + "abc",
}

print("scrub")
for name, s in SECRETS.items():
    text = f"ran curl and got {s} back in the output"
    out, why = scrub.scrub_checked(text)
    check(f"{name}: removed", out is not None and s.split()[-1] not in out, f"{why!r} {out!r}")

out, _ = scrub.scrub_checked('curl -H "Authorization: Basic dXNlcjpwYXNzd29yZA==" https://x')
check("Authorization header value removed, header name kept",
      out is not None and "dXNlcjpwYXNzd29yZA" not in out and "Authorization" in out, str(out))

env_dump = "DATABASE_URL=postgres://u:hunter2pw@db:5432/app\nAPI_TOKEN=abc123def456\nPATH=/usr/bin\nSTRIPE_SECRET_KEY='zzzz9999'"
out, _ = scrub.scrub_checked(env_dump)
check(".env-style values removed", out is not None and not any(v in out for v in ("hunter2pw", "abc123def456", "zzzz9999")), str(out))
check(".env-style key names survive (what is configured, never the value)",
      out is not None and "API_TOKEN" in out and "STRIPE_SECRET_KEY" in out, str(out))

out, _ = scrub.scrub_checked("the key lives in ~/.aws/credentials and in /repo/.env, set it there")
check("a PATH to a credential file is kept, it is the useful part",
      out is not None and "~/.aws/credentials" in out and "/repo/.env" in out, str(out))

key_block = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\nabcdefgh\n-----END " + "RSA PRIVATE KEY-----"
out, _ = scrub.scrub_checked("found:\n" + key_block + "\nend")
check("a whole private-key block is removed", out is not None and "MIIEow" not in out and "abcdefgh" not in out, str(out))

# fail closed
out, why = scrub.scrub_checked("found:\n-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nno end marker")
check("an unterminated private-key block is uncertain -> nothing is returned", out is None and why, f"{out!r} {why!r}")
blob = "A" * 10 + "".join(chr(97 + (i * 7) % 26) + chr(48 + (i * 3) % 10) for i in range(100))
out, why = scrub.scrub_checked("payload " + blob)
check("a long opaque blob it cannot classify is uncertain or removed, never passed through",
      out is None or blob not in out, f"{out!r}")

# no over-scrubbing of ordinary engineering text
plain = ("Fixed the drain in hooks/recall-drain.sh, commit 3f2a9c1d8e7b6a5c4d3e2f1a0b9c8d7e6f5a4b3c "
         "and request 123e4567-e89b-12d3-a456-426614174000; see https://github.com/ok-wow/recall/pull/18 "
         "and run `python3 tests/run.py` (PYTHONUNBUFFERED=1). function computeNextMigrationRevisionIdentifier()")
out, why = scrub.scrub_checked(plain)
check("ordinary text, shas, uuids, urls and long identifiers pass through unchanged", out == plain, f"{why!r} {out!r}")

obj = {"a": ["x " + SECRETS["github classic token"]], "b": {"c": "API_TOKEN=abc123def456"}}
cleaned = scrub.scrub_obj(obj)
check("scrub_obj cleans every string in nested data",
      SECRETS["github classic token"] not in str(cleaned) and "abc123def456" not in str(cleaned), str(cleaned))
sys.exit(finish("scrub checks"))
