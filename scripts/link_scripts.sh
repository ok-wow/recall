#!/usr/bin/env bash
# Point an installed skill's scripts at this repo, so there is ONE file.
#
# Copies drift. The catalogued entry is
# per-host-copies-drift-until-something-compares-them, and by 2026-09-21 it had
# fired three times: two hosts' sessionend hooks, then this repo against the
# installed okwow-compound skill, where five scripts had diverged in BOTH
# directions and three PRs landed against a tree nothing invoked. Its own fix
# is the choice this script implements — a symlink, so there is nothing to
# compare.
#
# The swap is atomic, and the order matters. Removing the live file and then
# creating the link leaves the registered path missing in between, which is the
# exact gap this is meant to close. So: create a temp link beside the target,
# then `mv -f` it over. A reader always sees the old file or the new link,
# never nothing. (harness-discipline:
# atomic-symlink-swap-nondestructive-version-control-migration-of-live-infra)
#
#   link_scripts.sh <target-scripts-dir> [--apply]
#
# Without --apply it prints what it would do and changes nothing.
set -euo pipefail

REPO_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
APPLY="${2:-}"
[ -n "$TARGET" ] || { echo "usage: link_scripts.sh <target-scripts-dir> [--apply]" >&2; exit 2; }
[ -d "$TARGET" ] || { echo "no such directory: $TARGET" >&2; exit 2; }

STAMP="$(date +%Y%m%d-%H%M%S)"
linked=0; already=0; kept=0

for src in "$REPO_SCRIPTS"/*.py; do
  name="$(basename "$src")"
  dst="$TARGET/$name"

  # Already pointing at us: nothing to do, and say so rather than relinking.
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    already=$((already + 1)); continue
  fi

  # A real file that differs from ours is somebody's work, not a stale copy.
  # Back it up before it stops being reachable — the whole point of this repo
  # is that a difference between two trees is information, not noise.
  if [ -f "$dst" ] && [ ! -L "$dst" ] && ! cmp -s "$src" "$dst"; then
    if [ "$APPLY" = "--apply" ]; then
      cp "$dst" "$dst.pre-link-$STAMP"
      echo "  DIFFERS, backed up to $(basename "$dst").pre-link-$STAMP: $name"
    else
      echo "  DIFFERS (would back up first): $name"
    fi
    kept=$((kept + 1))
  fi

  if [ "$APPLY" = "--apply" ]; then
    tmp="$dst.linking-$$"
    ln -s "$src" "$tmp"
    mv -f "$tmp" "$dst"          # atomic: never a moment with nothing there
  fi
  echo "  link $name -> $src"
  linked=$((linked + 1))
done

echo
echo "  $linked to link, $already already linked, $kept had local changes backed up"
[ "$APPLY" = "--apply" ] || echo "  dry run — pass --apply to write"
