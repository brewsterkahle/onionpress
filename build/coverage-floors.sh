#!/bin/bash
# Hold the unit tests' coverage above its floors: the whole run, then the
# files whose logic the tests are meant to exercise (the CLI and the
# check/confirm, Settings-page and config code). Run after `coverage run`
# (`make coverage`, CI). Each floor sits a point or two under what the
# 2026-10-23 run measured; raise one when the tests catch up, never lower
# one to pass. Runs under macOS /bin/bash 3.2 (no associative arrays).
set -e
set -o pipefail  # coverage's exit code, not tail's
cd "$(dirname "$0")/.."

TOTAL_FLOOR=60

# "<file> <floor>" per line.
FILE_FLOORS="
src/onionpress/cli.py 60
src/onionpress/handle_action.py 85
src/onionpress/confirm.py 90
src/onionpress/config.py 85
"

# `python` is the interpreter that ran the tests (uv run / CI's venv).
PY="${PYTHON:-python}"
status=0
echo "== total (floor ${TOTAL_FLOOR}%)"
"$PY" -m coverage report --fail-under="$TOTAL_FLOOR" | tail -1 || status=1
echo "$FILE_FLOORS" | while read -r file floor; do
    [ -n "$file" ] || continue
    echo "== $file (floor ${floor}%)"
    "$PY" -m coverage report --include="$file" --fail-under="$floor" | tail -1 || exit 1
done || status=1
if [ "$status" -ne 0 ]; then
    echo "ERROR: coverage is under a floor (build/coverage-floors.sh)" >&2
    exit 1
fi
