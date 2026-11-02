"""Run a launcher's real `main` (app/MacOS/onionpress, linux/onionpress)
against stubs: the shared harness of tests/test_launcher_dispatch_*.py.

A copy of the launcher goes into a scratch tree laid out as the real one
(Mac: <tmp>/App/MacOS/onionpress beside App/Resources; Linux:
<tmp>/opt/onionpress with ONIONPRESS_INSTALL_DIR=<tmp>/opt), under a
scratch HOME, so its top level runs for real and makes its data dir
there. One line changes: before the closing `main "$@"` the copy sources
a stubs file, which replaces the functions a test names with recorders
and the Python CLI (`bundled_python`, `op_py`) with one that records its
argv and the backup password it was handed. PATH holds only a stub bin
dir (docker, colima, osascript, systemctl, ...: each records its argv)
and the system's own /usr/bin, /bin, /usr/sbin, /sbin: no real docker,
Colima or Python CLI can be reached from here.

Every call lands in one file, one line each: `fn NAME ARGS` for a stubbed
function, `py ARGS` (+ `py-env PASSWORD`) for the CLI, `bin NAME ARGS`
for a stub executable. A stub's exit status is STUB_RC_<NAME> (an
environment variable a test sets; 0 by default). main()'s first CLI
call, `launcher-env` (issue #405), is answered by the stub from
$STUB_LAUNCHER_ENV (offset 0's ports by default; FAIL for no answer);
setup_db_passwords' `ensure-secrets` (#529) by writing a stub secrets
file (STUB_RC_ENSURE_SECRETS its exit).
"""

import os
import shutil
import stat
import subprocess
import tempfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAC = os.path.join(PROJECT_ROOT, "app", "MacOS", "onionpress")
LINUX = os.path.join(PROJECT_ROOT, "linux", "onionpress")
BASH = "/bin/bash"  # macOS's 3.2 runs the Mac launcher; Linux's runs anything newer

# Executables on the stub PATH. docker answers `compose ps --format json`
# with $STUB_PS and everything else with STUB_RC_DOCKER_<FIRST WORDS>.
STUB_BINS = ("colima", "osascript", "systemctl", "systemd-notify", "xdg-open",
             "open", "pbcopy", "limactl", "caffeinate", "sudo", "curl")

DOCKER_STUB = r'''#!/bin/sh
echo "bin docker $*" >> "$OP_CALLS"
case "$*" in
    info*) exit "${STUB_RC_DOCKER_INFO:-0}" ;;
    "compose ps --format json"*)
        [ -n "${STUB_PS:-}" ] && printf '%s\n' "$STUB_PS"
        exit 0 ;;
    *" down"*) exit "${STUB_RC_DOCKER_DOWN:-0}" ;;
    *"up -d tor"*) exit "${STUB_RC_DOCKER_UP_TOR:-0}" ;;
esac
exit 0
'''

BIN_STUB = '''#!/bin/sh
echo "bin {name} $*" >> "$OP_CALLS"
exit "${{STUB_RC_{var}:-0}}"
'''

STUBS_HEAD = r'''
_op_rec() { printf '%s\n' "$*" >> "$OP_CALLS"; }
_op_py() {
    _op_rec "py $*"
    _op_rec "py-env ${ONIONPRESS_BACKUP_PASSWORD:-<none>}"
    # main's first call, `launcher-env` (issue #405): answered from
    # $STUB_LAUNCHER_ENV (the lines the Python would print; FAIL: exit 1
    # with no answer), by default the ports of offset 0; its own knob,
    # so STUB_RC_PY keeps meaning the verb's own CLI call.
    if [ "$3" = launcher-env ]; then
        [ "${STUB_LAUNCHER_ENV-}" = FAIL ] && return 1
        printf '%s\n' "${STUB_LAUNCHER_ENV-$STUB_LAUNCHER_ENV_DEFAULT}"
        return 0
    fi
    # setup_db_passwords' `ensure-secrets` (issue #529): the file the
    # Python would make, so the bash `source` that follows has one; its
    # own knob, STUB_RC_ENSURE_SECRETS.
    if [ "$3" = ensure-secrets ]; then
        [ -f "$DATA_DIR/secrets" ] || printf "WORDPRESS_DB_PASSWORD='stub-wp'\nMYSQL_PASSWORD='stub-wp'\nMYSQL_ROOT_PASSWORD='stub-root'\n" > "$DATA_DIR/secrets"
        return "${STUB_RC_ENSURE_SECRETS:-0}"
    fi
    return "${STUB_RC_PY:-0}"
}
STUB_LAUNCHER_ENV_DEFAULT='ONIONPRESS_PORT_OFFSET=0
ONIONPRESS_WP_PORT=8080
ONIONPRESS_SOCKS_PORT=9050
ONIONPRESS_PROXY_PORT=9077'
bundled_python() {
    # The Mac's `bundled_python -m onionpress.cli ARGS` is recorded as ARGS.
    if [ "$1 $2" = "-m onionpress.cli" ]; then shift 2; else _op_rec "py-other $*"; return 99; fi
    _op_py "$@"
}
op_py() { _op_py "$@"; }
'''


def _var(name):
    return name.upper().replace("-", "_")


class Launcher:
    """One scratch install of a launcher. *platform* is "mac" or "linux";
    *stub* names the launcher functions replaced by recorders (each
    returns STUB_RC_<NAME>)."""

    def __init__(self, test, platform, stub=()):
        self.platform = platform
        self.tmp = tempfile.mkdtemp(prefix="op-launcher-")
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.data_dir = os.path.join(self.home, ".onionpress")
        self.calls_file = os.path.join(self.tmp, "calls")
        source = MAC if platform == "mac" else LINUX
        if platform == "mac":
            self.script = os.path.join(self.tmp, "App", "MacOS", "onionpress")
            self.resources = os.path.join(self.tmp, "App", "Resources")
            self.docker_dir = os.path.join(self.resources, "docker")
            os.makedirs(os.path.join(self.resources, "bin"))
        else:
            self.script = os.path.join(self.tmp, "opt", "onionpress")
            self.docker_dir = os.path.join(self.tmp, "opt", "docker")
        os.makedirs(os.path.dirname(self.script), exist_ok=True)
        os.makedirs(self.docker_dir)
        with open(source, encoding="utf-8") as f:
            text = f.read()
        tail = '\nmain "$@"\n'
        assert text.endswith(tail), f"{source} no longer ends in main \"$@\""
        text = text[:-len(tail)] + '\n. "$OP_TEST_STUBS"' + tail
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(self.script, 0o755)

        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self._write_bin("docker", DOCKER_STUB)
        for name in STUB_BINS:
            self._write_bin(name, BIN_STUB.format(name=name, var=_var(name)))

        self.stubs = os.path.join(self.tmp, "stubs.sh")
        body = [STUBS_HEAD]
        for fn in stub:
            body.append(f'{fn}() {{ _op_rec "fn {fn}" "$@"; '
                        f'return "${{STUB_RC_{_var(fn)}:-0}}"; }}\n')
        with open(self.stubs, "w", encoding="utf-8") as f:
            f.write("".join(body))
        self.extra_stubs = ""

    def _write_bin(self, name, text):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, stat.S_IRWXU)

    def add_stubs(self, text):
        """More bash, sourced after the recorders (a stub with a body)."""
        with open(self.stubs, "a", encoding="utf-8") as f:
            f.write("\n" + text + "\n")

    def run(self, *argv, env=None, input=None, timeout=60):
        if os.path.exists(self.calls_file):
            os.unlink(self.calls_file)
        full_env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": self.home,
            "TMPDIR": self.tmp,
            "LANG": "C",
            "ONIONPRESS_PORT_OFFSET": "0",
            "OP_TEST_STUBS": self.stubs,
            "OP_CALLS": self.calls_file,
        }
        if self.platform == "linux":
            full_env["ONIONPRESS_INSTALL_DIR"] = os.path.dirname(self.script)
            full_env["ONIONPRESS_PYTHONPATH"] = os.path.join(self.tmp, "no-lib")
        full_env.update(env or {})
        return subprocess.run(
            [BASH, self.script, *argv], env=full_env, capture_output=True, text=True,
            input=input, stdin=None if input is not None else subprocess.DEVNULL,
            timeout=timeout)

    def calls(self, env=False):
        """The recorded calls, in order. The `launcher-env` call main()
        opens with (issue #405; tests/test_launcher_env.py has its own)
        and its `py-env` line are left out unless *env*."""
        try:
            with open(self.calls_file, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        if env:
            return lines
        kept, skip_env = [], False
        for line in lines:
            if line.startswith("py ") and line.endswith(" launcher-env"):
                skip_env = True
                continue
            if skip_env and line.startswith("py-env "):
                skip_env = False
                continue
            skip_env = False
            kept.append(line)
        return kept

    def py_calls(self, env=False):
        """The Python CLI's argvs (each a string), in order (see calls)."""
        return [c[3:] for c in self.calls(env) if c.startswith("py ")]

    def fn_calls(self):
        """The stubbed functions called, by name, in order."""
        return [c.split()[1] for c in self.calls() if c.startswith("fn ")]

    @property
    def pidfile(self):
        return os.path.join(self.data_dir, "onionpress.pid")
