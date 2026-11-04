#!/usr/bin/env python3
"""Remove anything shaped like a credential, and refuse when removal is uncertain.

A cloud session's digest leaves the container, so this runs before it does. It
keeps the PATH to a credential ("~/.aws/credentials", "/repo/.env") because that
is the useful part, and drops the value. It fails closed: `scrub_checked` returns
no text at all when it cannot be sure, and the caller then uploads nothing.

    scrub(text) -> str                         best-effort redaction
    residue(text) -> [why, ...]                what still looks like a credential
    scrub_checked(text) -> (text|None, why)    None means: do not send this
    scrub_obj(obj) / scrub_obj_checked(obj)    the same over nested JSON data

Shapes are the vendors' public formats plus three generic rules (auth headers,
SECRET-named assignments, uppercase `KEY=value` lines) and an entropy rule for
long opaque tokens. Plain hex (git shas), UUIDs, paths and identifiers survive.
"""
from __future__ import annotations

import math
import re

REDACTED = "[REDACTED]"

_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----", re.S)
_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")
_SHAPES = [re.compile(r"(?<![A-Za-z0-9_])" + p) for p in (
    r"sk-ant-[A-Za-z0-9_\-]{10,}", r"sk-[A-Za-z0-9_\-]{20,}", r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}", r"AKIA[0-9A-Z]{16}", r"xox[abprs]-[A-Za-z0-9\-]{10,}",
    r"AIza[0-9A-Za-z_\-]{30,}", r"ya29\.[0-9A-Za-z_\-]{20,}", r"npm_[A-Za-z0-9]{30,}", r"glpat-[A-Za-z0-9_\-]{20,}",
    r"sk_(?:live|test)_[A-Za-z0-9]{16,}",
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
)]
_AUTH = re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|x-auth-token)(\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"',;]+")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{8,}")
_URL_CRED = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)")
_SECRET_NAME = r"[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL|ACCESS_?KEY|AUTH)[A-Z0-9_]*"
_KV = re.compile(rf"\b({_SECRET_NAME})(\s*[=:]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;]+)")
_JSON_KV = re.compile(r"(?i)(\"[A-Za-z_]*(?:token|secret|password|api_?key|private_?key)[A-Za-z_]*\"\s*:\s*)\"[^\"]*\"")
_ENV_LINE = re.compile(r"(?m)^([A-Z][A-Z0-9_]{2,}=)(.+)$")
_OPAQUE = re.compile(r"[A-Za-z0-9+=_\-]{40,}")
_HEX = re.compile(r"[0-9a-fA-F]+")


def _entropy(s: str) -> float:
    return -sum((s.count(c) / len(s)) * math.log2(s.count(c) / len(s)) for c in set(s))


def _opaque(tok: str) -> bool:
    return (not _HEX.fullmatch(tok) and any(c.isdigit() for c in tok) and any(c.isupper() for c in tok)
            and any(c.islower() for c in tok) and _entropy(tok) > 3.8)


def scrub(text: str) -> str:
    s = _KEY_BLOCK.sub(REDACTED, text)
    for rx in _SHAPES:
        s = rx.sub(REDACTED, s)
    s = _AUTH.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
    s = _URL_CRED.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", s)
    s = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
    s = _JSON_KV.sub(lambda m: f'{m.group(1)}"{REDACTED}"', s)
    s = _ENV_LINE.sub(lambda m: f"{m.group(1)}{REDACTED}", s)
    return _OPAQUE.sub(lambda m: REDACTED if _opaque(m.group(0)) else m.group(0), s)


def residue(text: str) -> list:
    """What still looks like a credential. Empty after a successful scrub."""
    out = []
    if _KEY_BEGIN.search(text):
        out.append("private key block")
    out += [f"token shape {rx.pattern[:18]}" for rx in _SHAPES if rx.search(text)]
    if any(not m.group(0).endswith(REDACTED) for m in _AUTH.finditer(text)):
        out.append("authorization header")
    if any(not m.group(0).endswith(REDACTED + m.group(2)) for m in _URL_CRED.finditer(text)):
        out.append("credentialed url")
    if any(m.group(3).strip("'\"") != REDACTED for m in _KV.finditer(text)):
        out.append("secret-named assignment")
    if any(m.group(2) != REDACTED for m in _ENV_LINE.finditer(text)):
        out.append("env-style line")
    if any(_opaque(m.group(0)) for m in _OPAQUE.finditer(text)):
        out.append("opaque token")
    return out


def scrub_checked(text: str) -> tuple:
    """(clean text, "") or (None, reason). None means upload nothing."""
    try:
        if _KEY_BEGIN.search(_KEY_BLOCK.sub("", text)):
            return None, "scrubbing uncertain: a private key block has no end marker"
        clean = scrub(text)
        left = residue(clean)
        if left:
            return None, "scrubbing uncertain: still looks like " + ", ".join(left)
        return clean, ""
    except Exception as e:  # a scrubber that crashed has not scrubbed
        return None, f"scrubbing uncertain: {type(e).__name__}"


def scrub_obj(obj):
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _strings(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)


def scrub_obj_checked(obj) -> tuple:
    """Scrub every string in nested data; (clean obj, "") or (None, reason)."""
    for t in _strings(obj):
        if _KEY_BEGIN.search(_KEY_BLOCK.sub("", t)):
            return None, "scrubbing uncertain: a private key block has no end marker"
    clean = scrub_obj(obj)
    for t in _strings(clean):
        left = residue(t)
        if left:
            return None, "scrubbing uncertain: still looks like " + ", ".join(left)
    return clean, ""
