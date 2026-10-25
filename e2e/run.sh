#!/bin/bash
# The OnionPress end-to-end suite (docs/E2E-TESTING.md).
#
#   e2e/run.sh --probe                  what this host could test; starts nothing
#   e2e/run.sh [--tier t0[,t1…]] [options]
#
# Options are the harness's (see `e2e/run.sh --help-options`):
#   --platforms macos,debian   default: everything the host offers
#   --mac-app PATH | --mac-dmg PATH | --deb PATH | --release TAG|latest
#   --max-nodes N  --keep  --scratch-root DIR  --limactl PATH
# and any pytest option (-k, -x, -v, …).
#
# The report (report.md, results.json, junit.xml, per-node logs) goes to
# e2e-results/<date>-<host>/ at the top of the checkout.
set -euo pipefail

E2E_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$E2E_DIR")"
PY=(uv run --no-project --python 3.14 --with 'pytest>=8' --with 'pynacl>=1.5.0')

if ! command -v uv >/dev/null 2>&1; then
    echo "e2e/run.sh needs uv (https://docs.astral.sh/uv/): brew install uv" >&2
    exit 1
fi

case "${1:-}" in
    --probe)
        shift
        exec "${PY[@]}" python "$E2E_DIR/probe.py" "$@" ;;
    --help-options)
        exec "${PY[@]}" python -m pytest -c "$E2E_DIR/pytest.ini" --rootdir "$E2E_DIR" --help | sed -n '/onionpress e2e/,/^$/p' ;;
esac

tiers="t0"
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --tier) tiers="$2"; shift 2 ;;
        --tier=*) tiers="${1#--tier=}"; shift ;;
        *) args+=("$1"); shift ;;
    esac
done

files=()
IFS=',' read -r -a tier_list <<< "$tiers"
for t in "${tier_list[@]}"; do
    found=0
    for f in "$E2E_DIR"/test_"$t"_*.py; do
        [ -f "$f" ] && files+=("$f") && found=1
    done
    if [ "$found" = 0 ]; then
        echo "e2e/run.sh: no tests for tier $t yet (e2e/test_${t}_*.py)" >&2
        exit 2
    fi
done

# A folder of its own even for two runs started in the same second.
mkdir -p "$PROJECT_DIR/e2e-results"
base="$PROJECT_DIR/e2e-results/$(date +%Y%m%d-%H%M%S)-$(hostname -s)"
results="$base"
n=1
until mkdir "$results" 2>/dev/null; do
    n=$((n + 1))
    results="$base-$n"
done
# Run from the caller's directory, so relative --mac-dmg/--deb paths mean
# what they say.
exec "${PY[@]}" python -m pytest -c "$E2E_DIR/pytest.ini" --rootdir "$E2E_DIR" -rA --results-dir "$results" --junitxml "$results/junit.xml" \
    "${args[@]+"${args[@]}"}" "${files[@]}"
