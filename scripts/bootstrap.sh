#!/bin/sh
# Create the virtualenv this skill needs, then verify a browser is reachable.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV="$ROOT/.venv"
PY="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
  echo "creating virtualenv at $VENV"
  "$PY" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip --quiet
"$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt" --quiet

set +e
"$VENV/bin/python" "$ROOT/scripts/doctor.py"
status=$?
set -e

if [ "$status" -ne 0 ]; then
  echo
  echo "No usable browser found. Either install Chrome/Edge, or let Playwright fetch Chromium:"
  echo "  \"$VENV/bin/python\" -m playwright install chromium"
fi

echo
echo "Try it:"
echo "  \"$ROOT/pixelvision\" https://example.com -o ./shots"
exit "$status"
