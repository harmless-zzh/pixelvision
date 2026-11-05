#!/bin/sh
# Expose this skill to local agents by symlinking it into their skill directories.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
REMOVE=""
[ "${1:-}" = "--remove" ] && REMOVE=1

for target in "$HOME/.agents/skills/pixelvision" "$HOME/.dsh/skills/pixelvision"; do
  if [ -n "$REMOVE" ]; then
    [ -L "$target" ] && rm "$target" && echo "removed  $target"
    continue
  fi
  mkdir -p "$(dirname "$target")"
  [ -L "$target" ] && rm "$target"
  if [ -e "$target" ]; then
    echo "already exists and is not a link, skipping: $target" >&2
    continue
  fi
  ln -s "$ROOT" "$target"
  echo "linked   $target -> $ROOT"
done

[ -n "$REMOVE" ] || sh "$ROOT/scripts/bootstrap.sh"
