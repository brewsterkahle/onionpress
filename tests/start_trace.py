"""Record what a launcher's start_containers does (issue #442.1).

A scratch install of a launcher (tests/launcher_harness.Launcher: the real
top level runs, under a scratch HOME) whose `main` is replaced by one call
of `start_containers`, with:

  - `docker`: the stateful fake in tests/start_trace_docker.py;
  - `bundled_python` / `op_py`: tests/start_trace_runner.py, which runs the
    start steps that are Python for real (against that docker) and records
    the WordPress-side ones;
  - the Docker wait, the VM repair, `sleep` and the background pieces
    (Mac `generate_vanity_address`; `run_detached`) as recorders; `curl` (the WordPress port wait) answers
    $STUB_RC_CURL;
  - the real install_plugin.sh / activate_plugin.sh in the scripts folder.

The trace is every `log` line, docker call, recorded Python step and
recorded function, in order, then the detached steps (sorted: they race
the rest), then the outcome: start_containers' status, START_ERRORS (Mac),
which markers are left in the data dir and which volumes exist. Paths
under the scratch tree read `$TMP`, temporary folders `$TEMPDIR`, image
digests `@sha256:PIN`.

Fixtures are tests/fixtures/start-traces/<platform>/<scenario>.trace.
OP_UPDATE_START_TRACES=1 rewrites them; a slice of #442 changes one only
on purpose and says why in its commit.
"""

import json
import os
import re
import shutil
import sys
import time

from launcher_harness import Launcher, PROJECT_ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "start-traces")
ADDRESS = "op2" + "a" * 53 + ".onion"
GENERATED = "op2" + "b" * 53 + ".onion"

# A temporary folder (bash mktemp -d, Python tempfile.mkdtemp), wherever
# this machine keeps them.
TEMP_DIR = re.compile(r"(?:[^\s=]*/)?tmp(?:\.[A-Za-z0-9]{6,}|[a-z0-9_]{8})(?=/|\s|$)")

# Image pins move with every re-pin (build/refresh-image-digests.sh); the
# trace says where a pinned image is used, not which digest it has today.
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")

MARKERS = (".import-key-pending", ".key-volume-migration-pending",
           ".install-from-backup", ".wordpress-installed")

STUBBED = {
    "mac": ("wait_for_docker_or_recover_vm", "ensure_vm_fsck_repair",
            "configure_site_timezones"),
    "linux": ("wait_for_docker",),
}

RUNNER_STUBS = r'''
bundled_python() {
    local tok rc=0
    tok=$(mktemp "$OP_PENDING/run.XXXXXX")
    "$OP_PYTHON" -I "$OP_RUNNER" "$@" || rc=$?
    rm -f "$tok"
    return $rc
}
op_py() { bundled_python "$@"; }
OP_PY=("$OP_PYTHON" -I "$OP_RUNNER")
run_detached() { _op_rec "detached py ${@: -1}"; }
sleep() { :; }
generate_vanity_address() {
    _op_rec "fn generate_vanity_address $*"
    [ -n "${FAKE_VANITY:-}" ] || return 1
    "$OP_PYTHON" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import start_trace_runner as r; sys.exit(r.write_vanity_key())' "$OP_TESTS"
    echo "$DATA_DIR/shared/vanity-keys/$FAKE_VANITY"
}
eval "_op_orig_$(declare -f log)"
log() { _op_rec "log $1"; _op_orig_log "$1"; }
_op_rec "TRACE-START"
set +e
(
    trap '_op_rec "START_ERRORS=[${START_ERRORS:-}]"' EXIT
    set -e
    start_containers
)
_op_rec "rc=$?"
exit 0
'''

PYTHON3_STUB = '''#!/bin/sh
case "$*" in
    *key_manager.py*) echo "bin python3 $*" >> "$OP_CALLS"; exit "${STUB_RC_PYTHON3:-0}" ;;
esac
exec /usr/bin/python3 "$@"
'''


class Scenario:
    """What the machine looks like before the start: the fake docker's
    state (start_trace_docker.py), the config's extra lines, files in the
    data dir, and environment (STUB_RC_*, FAKE_VANITY, OP_EGRESS_FAIL)."""

    def __init__(self, name, *, docker=None, config="", files=None, env=None,
                 platforms=("mac", "linux")):
        self.name = name
        self.docker = {"volumes": ["onionpress-onion-keys", "onionpress-tor-state"],
                       "wp_installed": True, "wp_port_host": "127.0.0.1",
                       "plugins": {"cache-enabler": "1.8.17",
                                   "internet-archive-wayback-machine-link-fixer": "1.4.2"}}
        self.docker.update(docker or {})
        self.config = config
        self.files = files or {}
        self.env = env or {}
        self.platforms = platforms


def run(test, platform, scenario):
    """The scenario's trace on *platform*'s launcher, as text."""
    launcher = Launcher(test, platform, stub=STUBBED[platform])
    tmp = launcher.tmp
    with open(os.path.join(launcher.bin, "docker"), "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec "$OP_PYTHON" -I "{os.path.join(HERE, "start_trace_docker.py")}" "$@"\n')
    with open(os.path.join(launcher.bin, "python3"), "w", encoding="utf-8") as f:
        f.write(PYTHON3_STUB)
    os.chmod(os.path.join(launcher.bin, "python3"), 0o755)
    scripts = (os.path.join(launcher.resources, "scripts") if platform == "mac"
               else os.path.join(os.path.dirname(launcher.script), "scripts"))
    shutil.copytree(os.path.join(PROJECT_ROOT, "app", "Resources", "scripts"), scripts)
    launcher.add_stubs(RUNNER_STUBS)
    # The compose files, present as in a real install (the Python side
    # adds the Cloudflare one only when it is there); the fake docker never
    # reads them.
    for name in ("docker-compose.yml", "docker-compose.cloudflare.yml"):
        open(os.path.join(launcher.docker_dir, name), "w").close()

    os.makedirs(launcher.data_dir, mode=0o700)
    with open(os.path.join(launcher.data_dir, "config"), "w", encoding="utf-8") as f:
        # The scenario's lines first: a key's first line is its value.
        f.write(scenario.config + "ADDRESS_PREFIX=op2\nINSTALL_IA_PLUGIN=yes\n")
    for rel, body in scenario.files.items():
        path = os.path.join(launcher.data_dir, rel.format(tmp=tmp))
        if body is None:
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body.format(tmp=tmp))
    state = os.path.join(tmp, "docker-state.json")
    with open(state, "w", encoding="utf-8") as f:
        json.dump(scenario.docker, f)
    pending = os.path.join(tmp, "pending")
    os.makedirs(pending)

    env = {"OP_PYTHON": sys.executable, "OP_RUNNER": os.path.join(HERE, "start_trace_runner.py"),
           "OP_TESTS": HERE, "OP_SRC": os.path.join(PROJECT_ROOT, "src"),
           "OP_TMP": tmp, "OP_DATA_DIR": launcher.data_dir, "OP_PENDING": pending,
           "OP_TRACE_OS": platform, "FAKE_DOCKER_STATE": state}
    env.update(scenario.env)
    result = launcher.run("start", env=env, timeout=180)
    # The detached steps: started in the background, they may finish
    # after the launcher.
    deadline = time.monotonic() + 30
    time.sleep(0.2)
    while os.listdir(pending) and time.monotonic() < deadline:
        time.sleep(0.1)
    calls = launcher.calls()
    test.assertIn("TRACE-START", calls, result.stdout + result.stderr)
    calls = calls[calls.index("TRACE-START") + 1:]
    test.assertTrue(any(c.startswith("rc=") for c in calls), result.stdout + result.stderr)

    with open(state, encoding="utf-8") as f:
        volumes = json.load(f).get("volumes", [])
    left = [m for m in MARKERS if os.path.lexists(os.path.join(launcher.data_dir, m))]
    return render(calls, tmp, platform, volumes, left)


def render(calls, tmp, platform, volumes, markers):
    body, detached, tail = [], [], []
    for line in calls:
        line = TEMP_DIR.sub("$TEMPDIR", line.replace(tmp, "$TMP"))
        line = DIGEST.sub("@sha256:PIN", line)
        if line.startswith("bin curl"):
            continue
        if line.startswith("detached "):
            detached.append(line)
        elif line.startswith(("rc=", "START_ERRORS=")):
            tail.append(line)
        else:
            body.append(line)
    if platform != "mac":
        tail = [t for t in tail if not t.startswith("START_ERRORS=")]
    tail.sort(reverse=True)  # rc= first
    out = body + sorted(detached) + tail + [
        "markers=" + " ".join(markers), "volumes=" + " ".join(sorted(volumes))]
    return "\n".join(out) + "\n"


def fixture_path(platform, name):
    return os.path.join(FIXTURES, platform, name + ".trace")


def check(test, platform, scenario):
    """The scenario's trace equals its fixture (rewritten under
    OP_UPDATE_START_TRACES=1)."""
    got = run(test, platform, scenario)
    path = fixture_path(platform, scenario.name)
    if os.environ.get("OP_UPDATE_START_TRACES") == "1":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(got)
        return
    with open(path, encoding="utf-8") as f:
        want = f.read()
    test.maxDiff = None
    test.assertEqual(got, want, f"{platform}/{scenario.name}: the start's trace changed "
                     f"(OP_UPDATE_START_TRACES=1 rewrites {os.path.relpath(path, PROJECT_ROOT)})")
