#!/usr/bin/env python3
"""$COLIMA_HOME exists before Colima runs, and a VM whose Docker socket
forward is aimed elsewhere is restarted through Colima (issue #370).

Colima (v0.8.1, config/files.go configBaseDir) honours $COLIMA_HOME only
when the directory already exists; otherwise it uses ~/.colima for its
config directory, and the Docker socket forward it writes into the
instance's lima.yaml is ``<config dir>/default/docker.sock``. The macOS
launchers exported COLIMA_HOME=~/.onionpress/colima but, through 2.5.6,
created nothing under it before the first ``colima start``: a fresh
install's VM booted with Docker healthy and ``hostSocket:
~/.colima/default/docker.sock``, while the launcher waited at its own
path, ran the Lima-level wedge recovery (which boots the same lima.yaml)
and gave up. The QA of 2.5.3 (finding F2, 2026-10-14) and of the 2.5.6
build (2026-10-22) both hit it on throwaway HOMEs.

Now both launchers, the MenubarApp and the Python Colima wrapper create
$COLIMA_HOME/_lima where they set the variables, and the Docker wait,
finding a Running VM whose lima.yaml forwards the socket outside
$COLIMA_HOME, restarts the VM through Colima with OnionPress's own VM
settings (a bare ``colima start`` would give it Colima's defaults: the
whole home folder mounted) instead of restarting Lima.

The bash harness runs the launcher's own functions against stub
``docker``, ``limactl`` and ``colima`` binaries.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

LAUNCHER = "app/MacOS/onionpress"
LAUNCHER_SH = "app/MacOS/launcher.sh"
MENUBAR = "src/menubar.py"
MKDIR = 'mkdir -p "$LIMA_HOME"'


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _function_body(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def _line_of(text, index):
    return text[:index].count("\n") + 1


def _def_body(text, name):
    """Body of a method/function at any indentation, up to the next def."""
    start = text.index(name)
    end = text.find("\n    def ", start + 1)
    return text[start:end if end > 0 else len(text)]


class TestDirectoriesExistBeforeColimaRuns(unittest.TestCase):
    """Static: every place that sets COLIMA_HOME for Colima creates it."""

    def test_both_launchers_create_the_directories_where_they_export_them(self):
        for launcher in (LAUNCHER, LAUNCHER_SH):
            text = _read(launcher)
            with self.subTest(launcher=launcher):
                export = re.search(r'^export LIMA_HOME="\$COLIMA_HOME/_lima"$', text, re.M)
                self.assertIsNotNone(export, "LIMA_HOME is exported at top level")
                mkdir = re.search(r"^" + re.escape(MKDIR) + r"$", text, re.M)
                self.assertIsNotNone(mkdir, "the launcher creates $LIMA_HOME at top level")
                # Right after the export (a comment block between is fine)...
                between = text[export.end():mkdir.start()]
                self.assertTrue(all(line.startswith("#") or not line.strip()
                                    for line in between.splitlines()),
                                f"only comments between the export and the mkdir:\n{between}")
                # ...and before anything runs Colima.
                first_run = text.index('"$BIN_DIR/colima"')
                self.assertLess(mkdir.end(), first_run,
                                "the mkdir precedes the first colima call")
                self.assertLess(_line_of(text, mkdir.start()), _line_of(text, first_run))
                # No other way of running Colima than the bundled binary.
                for m in re.finditer(r"^(?!\s*#).*\bcolima\b", text, re.M):
                    line = m.group(0)
                    if "colima" in line and re.search(r"(^|[\s;|&(])colima\s+(start|stop|status|list|delete|version)\b", line):
                        self.fail(f"a bare colima call, not $BIN_DIR/colima: {line.strip()}")

    def test_menubar_creates_the_directories_before_its_first_colima_call(self):
        # The Colima calls are the daemon's (onionpress.daemon, #367): its
        # _setup_environment makes the directory before _resolve_ports_at_start,
        # its first. The menubar still makes it for its own children.
        makedirs = 'os.makedirs(os.environ["LIMA_HOME"], exist_ok=True)'
        for rel in (MENUBAR, "src/onionpress/daemon.py"):
            text = _read(rel)
            self.assertIn(makedirs, text, rel)
            self.assertLess(text.index('os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")'),
                            text.index(makedirs), rel)
        text = _read("src/onionpress/daemon.py")
        run = text[text.index("    def run(self):"):text.index("    def _main_loop_once(")]
        self.assertLess(run.index("self._setup_environment()"), run.index("self._resolve_ports_at_start()"))

class TestHarness(unittest.TestCase):
    """The launcher's functions under bash, with stub binaries."""

    FUNCTIONS = (
        "config_value", "docs_mount_args", "colima_start_with_vm_settings", "install_docker",
        "ensure_docker_socket", "docker_socket_forwards",
        "docker_socket_forward_is_foreign", "recover_docker_socket_forward",
        "wait_for_docker", "lima_vm_running", "recover_lima_vm",
        "wait_for_docker_or_recover_vm", "vm_shell_bounded",
        "save_previous_boot_journal",
    )
    DOCKER_STUB = """#!/bin/sh
printf 'docker %s\\n' "$*" >> "$STUB_CALLS"
[ -e "$STUB_DIR/docker-up" ] && exit 0
echo "Cannot connect to the Docker daemon at unix:///stub/docker.sock." >&2
exit 1
"""
    LIMACTL_STUB = """#!/bin/sh
printf 'limactl %s\\n' "$*" >> "$STUB_CALLS"
case "$1" in
    list) echo "$STUB_LIMA_STATUS" ;;
    start) [ "$STUB_LIMA_START" = heals ] && touch "$STUB_DIR/docker-up" ;;
esac
exit 0
"""
    # `start` records whether the directories existed when Colima was
    # called, and (heals) rewrites lima.yaml as Colima does once
    # $COLIMA_HOME exists, bringing Docker up.
    COLIMA_STUB = """#!/bin/sh
printf 'colima %s\\n' "$*" >> "$STUB_CALLS"
case "$1" in
    start)
        [ -d "$COLIMA_HOME" ] && [ -d "$LIMA_HOME" ] && echo "dirs-existed" >> "$STUB_DIR/notes"
        [ "$STUB_COLIMA_START" = fail ] && exit 1
        if [ "$STUB_COLIMA_START" = heals ]; then
            mkdir -p "$LIMA_HOME/colima"
            printf 'portForwards:\\n    - guestSocket: /var/run/docker.sock\\n      hostSocket: %s/default/docker.sock\\n' "$COLIMA_HOME" > "$LIMA_HOME/colima/lima.yaml"
            touch "$STUB_DIR/docker-up"
        fi
        ;;
esac
exit 0
"""
    FOREIGN_YAML = """\
vmType: vz
portForwards:
    - guestSocket: /var/run/docker.sock
      hostSocket: {home}/.colima/default/docker.sock
    - guestSocket: /var/run/docker.sock
      hostSocket: "{home}/.colima/docker.sock"
"""
    OWN_YAML = """\
portForwards:
    - guestSocket: /var/run/docker.sock
      hostSocket: {colima_home}/default/docker.sock
    - guestSocket: /var/run/docker.sock
      hostSocket: {colima_home}/docker.sock
"""

    def _run(self, snippet, *, lima_yaml=None, docker="down", lima="Running",
             lima_start="heals", colima_start="heals", config="", prologue=False):
        text = _read(LAUNCHER)
        functions = "".join(_function_body(text, name) + "\n}\n"
                            for name in self.FUNCTIONS)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = os.path.join(tmp, "bin")
        os.mkdir(bin_dir)
        for name, body in (("docker", self.DOCKER_STUB), ("limactl", self.LIMACTL_STUB),
                           ("colima", self.COLIMA_STUB)):
            path = os.path.join(bin_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(path, 0o755)
        home = os.path.join(tmp, "home")
        data_dir = os.path.join(home, ".onionpress")
        docs_dir = os.path.join(home, "OnionPress")
        colima_home = os.path.join(data_dir, "colima")
        lima_home = os.path.join(colima_home, "_lima")
        os.makedirs(os.path.join(data_dir, "shared"))
        os.makedirs(docs_dir)
        if config:
            with open(os.path.join(data_dir, "config"), "w", encoding="utf-8") as f:
                f.write(config)
        if lima_yaml is not None:
            os.makedirs(os.path.join(lima_home, "colima"))
            with open(os.path.join(lima_home, "colima", "lima.yaml"), "w", encoding="utf-8") as f:
                f.write(lima_yaml.format(home=home, colima_home=colima_home))
        if docker == "up":
            open(os.path.join(tmp, "docker-up"), "w").close()
        calls = os.path.join(tmp, "calls")
        log = os.path.join(tmp, "log")
        for name in (calls, log, os.path.join(tmp, "notes")):
            open(name, "w").close()
        setup = (
            "set -e\n"
            f'LOG_FILE="{log}"; BIN_DIR="{bin_dir}"; DATA_DIR="{data_dir}"\n'
            f'DOCUMENTS_DIR="{docs_dir}"; VM_ARCH=aarch64\n'
            f'COLIMA_HOME="{colima_home}"\n'
        )
        if prologue:
            # The launcher's own top-level lines: the exports and the mkdir.
            start = text.index('export COLIMA_HOME="$COLIMA_HOME"\n')
            end = text.index(MKDIR + "\n", start) + len(MKDIR) + 1
            setup += text[start:end]
        else:
            setup += f'LIMA_HOME="{lima_home}"\n'
        script = (
            setup
            + 'log() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
            + "sleep() { :; }\n"  # every wait budget passes instantly
            + functions + snippet + "\n")
        env = {"PATH": bin_dir + os.pathsep + "/usr/bin:/bin", "HOME": home,
               "STUB_DIR": tmp, "STUB_CALLS": calls,
               "STUB_LIMA_STATUS": lima, "STUB_LIMA_START": lima_start,
               "STUB_COLIMA_START": colima_start}
        out = subprocess.run(["bash", "-c", script], env=env,
                             capture_output=True, text=True, timeout=120)
        with open(calls, encoding="utf-8") as f:
            made = f.read().splitlines()
        with open(log, encoding="utf-8") as f:
            logged = f.read()
        with open(os.path.join(tmp, "notes"), encoding="utf-8") as f:
            notes = f.read().split()
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip(), made, logged, notes, tmp

    WAIT = "rc=0; wait_for_docker_or_recover_vm 180 start || rc=$?; echo $rc"
    INSTALL = "rc=0; install_docker || rc=$?; echo $rc"

    @staticmethod
    def _lima_recoveries(calls):
        return [c for c in calls if c in ("limactl stop -f colima", "limactl start colima")]

    @staticmethod
    def _colima_calls(calls):
        return [c for c in calls if c.startswith("colima ")]

    # --- the first start ---------------------------------------------------

    def test_first_start_creates_the_directories_before_calling_colima(self):
        rc, calls, logged, notes, tmp = self._run(self.INSTALL, prologue=True)
        self.assertEqual(rc, "0", logged)
        starts = [c for c in calls if c.startswith("colima start")]
        self.assertEqual(len(starts), 1, calls)
        self.assertEqual(notes, ["dirs-existed"],
                         "COLIMA_HOME and LIMA_HOME existed when colima start ran")
        words = starts[0].split()
        for flag in ("--activate=false", "--ssh-config=false", "--vm-type", "vz",
                     "--mount-type", "virtiofs", "--disk", "20", "--arch", "aarch64",
                     "--vz-rosetta=false"):
            self.assertIn(flag, words, starts[0])
        data_dir = os.path.join(tmp, "home", ".onionpress")
        self.assertIn(f"--mount {data_dir}/shared:w --mount {tmp}/home/OnionPress:w", starts[0])
        self.assertTrue(os.path.isfile(os.path.join(data_dir, "colima", ".initialized")))

    def test_first_start_takes_the_vm_size_from_the_config(self):
        rc, calls, _, _, _ = self._run(self.INSTALL, prologue=True,
                                       config="VM_CPU=4\nVM_MEMORY=6\nVM_DISK=50\n")
        self.assertEqual(rc, "0")
        start = next(c for c in calls if c.startswith("colima start"))
        self.assertIn("--cpu 4 --memory 6 --disk 50", start)

    # --- the forward check -------------------------------------------------

    CHECK = ("rc=0; docker_socket_forward_is_foreign || rc=$?; echo $rc; "
             "docker_socket_forwards")

    def test_foreign_forward_is_recognised_quoted_or_not(self):
        out, _, _, _, tmp = self._run(self.CHECK, lima_yaml=self.FOREIGN_YAML)
        lines = out.splitlines()
        self.assertEqual(lines[0], "0")
        home = os.path.join(tmp, "home")
        self.assertEqual(lines[1:], [f"{home}/.colima/default/docker.sock",
                                     f"{home}/.colima/docker.sock"])

    def test_own_forward_and_no_vm_are_not_foreign(self):
        out, _, _, _, _ = self._run(self.CHECK, lima_yaml=self.OWN_YAML)
        self.assertEqual(out.splitlines()[0], "1")
        out, _, _, _, _ = self._run(self.CHECK, lima_yaml=None)
        self.assertEqual(out.splitlines(), ["1"], "no lima.yaml: nothing to re-point")

    # --- the Docker wait's recovery ----------------------------------------

    def test_foreign_forward_on_a_running_vm_is_restarted_through_colima(self):
        rc, calls, logged, _, tmp = self._run(self.WAIT, lima_yaml=self.FOREIGN_YAML)
        self.assertEqual(rc, "0", logged)
        self.assertEqual(self._lima_recoveries(calls), [],
                         "a Lima restart boots the same lima.yaml; not tried")
        colima = self._colima_calls(calls)
        self.assertEqual(len(colima), 2, calls)
        self.assertEqual(colima[0], "colima stop")
        self.assertTrue(colima[1].startswith("colima start --activate=false --ssh-config=false "
                                             "--vm-type vz --mount-type virtiofs --mount "), colima[1])
        data_dir = os.path.join(tmp, "home", ".onionpress")
        self.assertIn(f"--mount {data_dir}/shared:w", colima[1])
        self.assertIn("--cpu 2 --memory 2 --disk 20 --arch aarch64", colima[1])
        # No wait for a socket that cannot appear: the first docker call
        # comes after the restart, and Lima was asked once.
        self.assertEqual(calls[0], "limactl list --format {{.Status}} colima")
        self.assertEqual(calls[-1], "docker info")
        self.assertEqual(calls.count("docker info"), 1)
        self.assertIn(f"{tmp}/home/.colima/default/docker.sock", logged)
        self.assertIn("Restarting the VM through Colima to re-point it", logged)
        self.assertIn(f"Lima now forwards the Docker socket to {data_dir}/colima/default/docker.sock", logged)

    def test_recovery_restarts_with_the_configured_vm_size(self):
        _, calls, _, _, _ = self._run(self.WAIT, lima_yaml=self.FOREIGN_YAML,
                                      config="VM_CPU=3\nVM_MEMORY=5\nVM_DISK=100\n")
        start = next(c for c in calls if c.startswith("colima start"))
        self.assertIn("--cpu 3 --memory 5 --disk 100", start)

    def test_gives_up_when_the_forward_stays_foreign(self):
        rc, calls, logged, _, _ = self._run(self.WAIT, lima_yaml=self.FOREIGN_YAML,
                                            colima_start="noop")
        self.assertEqual(rc, "1")
        self.assertEqual(len(self._colima_calls(calls)), 2)
        self.assertEqual(calls.count("docker info"), 0, "no wait after a failed re-point")
        self.assertIn("still forwards the Docker socket", logged)

    def test_gives_up_when_colima_cannot_start_the_vm(self):
        rc, calls, logged, _, _ = self._run(self.WAIT, lima_yaml=self.FOREIGN_YAML,
                                            colima_start="fail")
        self.assertEqual(rc, "1")
        self.assertIn("Colima could not start the VM", logged)
        self.assertEqual(self._lima_recoveries(calls), [])

    def test_forces_the_stop_when_colima_stop_fails(self):
        snippet = ('colima() { :; }\n'  # not used: the stub is on PATH
                   'cat > "$BIN_DIR/colima" <<\'EOF\'\n#!/bin/sh\n'
                   'printf \'colima %s\\n\' "$*" >> "$STUB_CALLS"\n'
                   '[ "$1" = stop ] && [ "$2" != -f ] && exit 1\n'
                   'if [ "$1" = start ]; then mkdir -p "$LIMA_HOME/colima"; '
                   'printf \'      hostSocket: %s/default/docker.sock\\n\' "$COLIMA_HOME" '
                   '> "$LIMA_HOME/colima/lima.yaml"; touch "$STUB_DIR/docker-up"; fi\n'
                   'exit 0\nEOF\n' + self.WAIT)
        rc, calls, logged, _, _ = self._run(snippet, lima_yaml=self.FOREIGN_YAML)
        self.assertEqual(rc, "0", logged)
        self.assertEqual(self._colima_calls(calls)[:2], ["colima stop", "colima stop -f"])
        self.assertIn("colima stop failed — forcing", logged)

    def test_a_stopped_vm_with_a_foreign_forward_is_left_to_colima_start(self):
        # detect_container_runtime's own `colima start` rewrites lima.yaml
        # for a stopped VM; the wait only reports, as before.
        rc, calls, logged, _, _ = self._run(self.WAIT, lima_yaml=self.FOREIGN_YAML, lima="Stopped")
        self.assertEqual(rc, "1")
        self.assertEqual(self._colima_calls(calls), [])
        self.assertEqual(self._lima_recoveries(calls), [])
        self.assertIn("not recovering", logged)

    def test_own_forward_keeps_the_lima_level_wedge_recovery(self):
        for lima_yaml in (self.OWN_YAML, None):
            with self.subTest(lima_yaml=bool(lima_yaml)):
                rc, calls, logged, _, _ = self._run(self.WAIT, lima_yaml=lima_yaml)
                self.assertEqual(rc, "0")
                self.assertEqual(self._colima_calls(calls), [])
                self.assertEqual(self._lima_recoveries(calls),
                                 ["limactl stop -f colima", "limactl start colima"])
                self.assertIn("recovering the wedged VM", logged)

    def test_no_lima_or_colima_calls_when_docker_answers(self):
        rc, calls, _, _, _ = self._run(self.WAIT, lima_yaml=self.OWN_YAML, docker="up")
        self.assertEqual(rc, "0")
        self.assertEqual(calls, ["docker info"])

    # --- the socket wait's diagnosis ----------------------------------------

    def test_socket_wait_names_the_foreign_forward(self):
        snippet = "rc=0; ensure_docker_socket 2 || rc=$?; echo $rc"
        rc, _, logged, _, tmp = self._run(snippet, lima_yaml=self.FOREIGN_YAML)
        self.assertEqual(rc, "1")
        self.assertIn("Docker socket never appeared", logged)
        self.assertIn(f"Lima forwards the VM's /var/run/docker.sock to {tmp}/home/.colima/default/docker.sock",
                      logged)
        self.assertNotIn("forward for the VM's /var/run/docker.sock is down", logged)
        rc, _, logged, _, _ = self._run(snippet, lima_yaml=self.OWN_YAML)
        self.assertEqual(rc, "1")
        self.assertIn("forward for the VM's /var/run/docker.sock is down", logged)

    def test_recovery_hint_never_recommends_a_bare_colima_start(self):
        for launcher in (LAUNCHER, LAUNCHER_SH):
            body = _function_body(_read(launcher), "ensure_docker_socket")
            with self.subTest(launcher=launcher):
                self.assertNotIn('stop && \\"$BIN_DIR/colima\\" start', body)
                self.assertIn("never a bare colima", body)


class TestWiring(unittest.TestCase):
    """The recovery is reached from both the start and the restart paths,
    and the first start uses the shared flag function."""

    def test_wait_checks_the_forward_before_waiting(self):
        body = _function_body(_read(LAUNCHER), "wait_for_docker_or_recover_vm")
        self.assertLess(body.index("if docker_socket_forward_is_foreign && lima_vm_running; then"),
                        body.index('if wait_for_docker "$limit"; then'))
        self.assertIn('recover_docker_socket_forward "$who" || return 1', body)

    def test_restart_dispatches_between_the_two_recoveries(self):
        text = _read(LAUNCHER)
        main = text[text.index("\nmain() {"):]
        restart = main[main.index("        restart)"):main.index("        status)")]
        self.assertIn("if docker_socket_forward_is_foreign && lima_vm_running; then\n"
                      "                    recover_docker_socket_forward restart || exit 1\n"
                      "                else\n"
                      "                    recover_lima_vm restart || exit 1", restart)

    def test_install_and_recovery_share_the_vm_settings(self):
        text = _read(LAUNCHER)
        self.assertIn('if colima_start_with_vm_settings "$VM_CPU" "$VM_MEMORY" "$VM_DISK"; then',
                      _function_body(text, "install_docker"))
        self.assertIn('colima_start_with_vm_settings "$cpu" "$memory" "$disk"',
                      _function_body(text, "recover_docker_socket_forward"))
        settings = _function_body(text, "colima_start_with_vm_settings")
        self.assertEqual(settings.count('"$BIN_DIR/colima" start'), 2, "one per backend")
        for flag in ("--vm-type vz", "--mount-type virtiofs", "--vm-type qemu",
                     "--mount-type sshfs", '--disk "$disk"', '--arch "$VM_ARCH"'):
            self.assertIn(flag, settings)


if __name__ == "__main__":
    unittest.main()
