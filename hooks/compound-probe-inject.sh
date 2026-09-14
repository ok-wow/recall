#!/bin/sh
# Entry point for the probe injector on any host agent that runs shell hooks.
# Mirrors compound-live-signal.sh: resolve the core beside this script, so that
# a second copy of the hook installed under another agent's hook directory uses
# that copy's core instead of reaching across into the first install, and exit 0
# on any dependency problem -- a knowledge hook must never fail a turn.

if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

case "$0" in
  */*) SCRIPT_PARENT=${0%/*} ;;
  *) SCRIPT_PARENT=. ;;
esac
SCRIPT_DIR=$(CDPATH= cd -P "$SCRIPT_PARENT" 2>/dev/null && pwd -P) || exit 0
CORE="${COMPOUND_SKILL_DIR:-$SCRIPT_DIR}/compound-probe-inject.py"
[ -r "$CORE" ] || exit 0

exec python3 -I -S "$CORE"
