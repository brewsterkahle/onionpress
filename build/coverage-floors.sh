#!/bin/bash
# Hold the unit tests' coverage above its floors: the whole run, then the
# files whose logic the tests are meant to exercise (the CLI and the
# check/confirm, Settings-page and config code). Run after `coverage run`
# (`make coverage`, CI). Each floor sits a point or two under what the
# run that set it measured (2026-10-23; the CLI's raised 2026-10-25 with
# tests/test_cli_commands.py and tests/test_cli_edges.py; the modules split
# out by #474-#484 added 2026-10-29; the daemon, launcher_ops, scrub,
# key_manager, updater and colima added and the total raised the same day
# with their tests; start, bundled_plugins and containers added and the
# total and the CLI's raised 2026-10-30 with #442's; the updater's raised
# 2026-10-31 with #525's Linux release tests; the total, the daemon's and reachability's raised 2026-11-01 with #407's Linux daemon tests); raise one when
# the tests catch up, never lower one to pass. Runs under macOS /bin/bash 3.2 (no associative arrays).
set -e
set -o pipefail  # coverage's exit code, not tail's
cd "$(dirname "$0")/.."

TOTAL_FLOOR=74

# "<file> <floor>" per line.
FILE_FLOORS="
src/onionpress/cli.py 98
src/onionpress/app_commands.py 94
src/onionpress/handle_action.py 95
src/onionpress/confirm.py 98
src/onionpress/config.py 85
src/onionpress/backup_archive.py 92
src/onionpress/db_import.py 93
src/onionpress/hub_backup.py 85
src/onionpress/ports.py 86
src/onionpress/secrets_file.py 96
src/onionpress/onion_hostname.py 98
src/onionpress/reachability.py 98
src/onionpress/image_overrides.py 95
src/onionpress/daemon.py 83
src/onionpress/launcher_ops.py 84
src/onionpress/scrub.py 90
src/onionpress/key_manager.py 87
src/onionpress/updater.py 89
src/onionpress/start.py 86
src/onionpress/bundled_plugins.py 95
src/onionpress/containers.py 93
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
