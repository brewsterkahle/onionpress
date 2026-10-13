#!/usr/bin/env python3
"""How Linux installs, updates and manages its services (issue #316).

- install.sh replaces every folder in /opt/onionpress (stage beside, swap by
  rename) instead of `cp -r` over the old one, which made docker/docker,
  lib/onionpress/onionpress and scripts/scripts on every re-run and left the
  first install's copies in use beside a new launcher.
- `onionpress update` installs a tagged release with that release's own
  installer (.deb or install.sh). It used to keep its own stale copy list
  (stopping halfway under set -e on a moved file), install main's HEAD, and
  `git reset --hard origin/main` the first checkout it found under /home/*.
- The heartbeat client is a systemd *user* unit; stop/restart/reset and
  START_ON_BOOT used system-scope systemctl, so /offline never went out.
- The settings watcher is a Type=oneshot unit; background work it starts
  (the derivative rebuild after a restore) goes to a transient user unit,
  not into the oneshot's cgroup, which systemd kills when it exits.

The shell is extracted from the scripts and run against stubs; nothing here
touches systemd, Docker, apt or the network.
"""

import os
import re
import stat
import subprocess
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = "linux/install.sh"
LAUNCHER = "linux/onionpress"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    return [l for l in text.splitlines() if not l.lstrip().startswith("#")]


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _case_branch(text, label, next_label):
    start = text.index(f"\n        {label})\n")
    return text[start:text.index(f"\n        {next_label})\n", start)]


def _run_bash(script, env=None, **kw):
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": tempfile.gettempdir()}
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], env=full_env, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL, **kw)


class TestInstallReplacesFolders(unittest.TestCase):

    def setUp(self):
        self.script = _read(INSTALL_SH)
        self.helpers = "".join(_bash_function(self.script, n) for n in
                               ("staging_name", "swap_in_dir", "replace_dir", "replace_file"))

    def test_nothing_is_copied_over_a_folder_in_the_install_dir(self):
        for line in _code_lines(self.script):
            with self.subTest(line=line.strip()):
                self.assertNotRegex(line, r'cp\s+-r\b.*"\$INSTALL_DIR/',
                                    "a `cp -r` into an existing folder nests a copy "
                                    "inside it on every re-run; use replace_dir")

    def test_every_folder_goes_through_replace_dir(self):
        for src, dst in (("app/Resources/docker", "docker"),
                         ("app/Resources/plugins", "plugins"),
                         ("app/Resources/themes", "themes"),
                         ("src/onionpress", "lib/onionpress"),
                         ("linux/assets", "assets")):
            with self.subTest(dst):
                self.assertIn(f'replace_dir "$REPO_DIR/{src}" "$INSTALL_DIR/{dst}"', self.script)

    def test_launcher_and_tray_are_renamed_into_place(self):
        # bash reads a script as it runs it: `onionpress update` runs this
        # installer, and a launcher overwritten in place would change under it.
        for name in ("onionpress", "onionpress-tray"):
            with self.subTest(name):
                self.assertIn(f'replace_file "$REPO_DIR/linux/{name}" "$INSTALL_DIR/{name}" 0755',
                              self.script)

    def _tree(self, root):
        out = set()
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                out.add(os.path.relpath(os.path.join(dirpath, n), root))
        return out

    def test_a_second_install_replaces_instead_of_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src", "docker")
            install = os.path.join(tmp, "opt")
            os.makedirs(os.path.join(src, "tor", "__pycache__"))
            os.makedirs(install)
            for rel in ("docker-compose.yml", "tor/web-server.py", "tor/__pycache__/x.pyc",
                        "gone-next-time.txt"):
                with open(os.path.join(src, rel), "w") as f:
                    f.write("v1\n")
            run = (self.helpers + 'set -e\nSUDO=""\n'
                   'replace_dir "$1" "$2/docker"\n')
            for round_ in (1, 2):
                if round_ == 2:
                    os.remove(os.path.join(src, "gone-next-time.txt"))
                    with open(os.path.join(src, "docker-compose.yml"), "w") as f:
                        f.write("v2\n")
                r = subprocess.run(["bash", "-c", run, "bash", src, install],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
            dst = os.path.join(install, "docker")
            self.assertEqual(self._tree(dst),
                             {"docker-compose.yml", "tor", "tor/web-server.py"})
            with open(os.path.join(dst, "docker-compose.yml")) as f:
                self.assertEqual(f.read(), "v2\n")
            self.assertEqual(os.listdir(install), ["docker"], "staging folders left behind")

    def test_replace_file_renames_over_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "new")
            dst = os.path.join(tmp, "onionpress")
            for path, text in ((src, "new\n"), (dst, "old\n")):
                with open(path, "w") as f:
                    f.write(text)
            before = os.stat(dst).st_ino
            r = subprocess.run(["bash", "-c", self.helpers + 'set -e\nSUDO=""\n'
                                'replace_file "$1" "$2" 0755\n', "bash", src, dst],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotEqual(os.stat(dst).st_ino, before, "overwritten in place")
            self.assertTrue(os.stat(dst).st_mode & stat.S_IXUSR)
            self.assertEqual(sorted(os.listdir(tmp)), ["new", "onionpress"])

    def test_scripts_folder_is_rebuilt_without_the_old_nesting(self):
        start = self.script.index("# Copy shared scripts")
        end = self.script.index('swap_in_dir "$_scripts_stage" "$INSTALL_DIR/scripts"\n', start)
        block = self.script[start:end + len('swap_in_dir "$_scripts_stage" "$INSTALL_DIR/scripts"\n')]
        with tempfile.TemporaryDirectory() as install:
            os.makedirs(os.path.join(install, "scripts", "scripts"))
            with open(os.path.join(install, "scripts", "scripts", "install_plugin.sh"), "w") as f:
                f.write("stale\n")
            r = subprocess.run(
                ["bash", "-c", self.helpers + 'set -e\nSUDO=""\n' + block],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "REPO_DIR": PROJECT_ROOT, "INSTALL_DIR": install},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            names = set(os.listdir(os.path.join(install, "scripts")))
            self.assertNotIn("scripts", names)
            for name in ("key_manager.py", "onion_auth.py", "onionpress-heartbeat.py",
                         "install_plugin.sh", "activate_plugin.sh"):
                self.assertIn(name, names)
            self.assertEqual(os.listdir(install), ["scripts"])


class TestUpdate(unittest.TestCase):
    """`onionpress update` runs a release's own installer."""

    def setUp(self):
        self.launcher = _read(LAUNCHER)
        self.update = "".join(_bash_function(self.launcher, n) for n in
                              ("update_onionpress", "_update_run", "_update_from_deb",
                               "_update_from_install_sh"))

    def test_touches_no_checkout_and_no_branch_head(self):
        code = "\n".join(_code_lines(self.update))
        for bad in ("git reset", "git pull", "origin/main", "/home/", "cp -r", "cp \""):
            with self.subTest(bad):
                self.assertNotIn(bad, code)

    def test_installs_a_tag_with_its_own_installer(self):
        self.assertIn('--branch "$tag"', self.update)
        self.assertIn('bash "$tmp/onionpress/linux/install.sh"', self.update)
        self.assertIn("/releases/download/$tag/onionpress.deb", self.update)
        self.assertIn("apt-get install -y", self.update)
        self.assertIn(r'^v[0-9]+\.[0-9]+\.[0-9]+$', self.update)

    def test_the_command_and_the_settings_button_use_it(self):
        branch = _case_branch(self.launcher, "update", "write-status")
        self.assertIn('update_onionpress "${2:-}"', branch)
        handle = _case_branch(self.launcher, "handle-action", "reset")
        self.assertIn('"$0" update', handle)

    STUBS = r"""
log() { echo "log: $*" >> "$OUT/log"; }
curl() { printf '{"tag_name": "%s"}\n' "$LATEST"; }
jq() { python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name") or "")'; }
sudo() { [ "$SUDO_OK" = yes ]; }
dpkg-query() { [ "$DEB" = yes ] && echo "install ok installed"; }
_update_from_deb() { echo "deb $1" >> "$OUT/calls"; }
_update_from_install_sh() { echo "install.sh $1" >> "$OUT/calls"; }
"""

    def _run(self, current, latest, arg="", sudo_ok="yes", deb="no"):
        tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", tmp])
        with open(os.path.join(tmp, "VERSION"), "w") as f:
            f.write(current + "\n")
        script = (self.update + self.STUBS
                  + 'set -e\nrc=0\nupdate_onionpress "$1" || rc=$?\necho "rc=$rc"\n')
        r = subprocess.run(
            ["bash", "-c", script, "bash", arg],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OUT": tmp,
                 "INSTALL_DIR": tmp, "LOG_FILE": os.path.join(tmp, "log"),
                 "LATEST": latest, "SUDO_OK": sudo_ok, "DEB": deb},
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        calls = ""
        if os.path.exists(os.path.join(tmp, "calls")):
            with open(os.path.join(tmp, "calls")) as f:
                calls = f.read()
        m = re.search(r"rc=(\d+)", r.stdout)
        self.assertIsNotNone(m, r.stderr)
        return int(m.group(1)), calls

    def test_newer_release_goes_through_install_sh(self):
        self.assertEqual(self._run("2.5.2", "v2.5.3"), (0, "install.sh v2.5.3\n"))

    def test_deb_install_goes_through_the_deb(self):
        self.assertEqual(self._run("2.5.2", "v2.5.3", deb="yes"), (0, "deb v2.5.3\n"))

    def test_current_or_newer_install_is_left_alone(self):
        self.assertEqual(self._run("2.5.3", "v2.5.3"), (0, ""))
        self.assertEqual(self._run("2.6.0", "v2.5.3"), (0, ""))

    def test_a_named_tag_is_installed_even_if_older(self):
        self.assertEqual(self._run("2.6.0", "v9.9.9", arg="2.5.3"), (0, "install.sh v2.5.3\n"))

    def test_anything_but_a_release_tag_is_refused(self):
        for tag in ("", "main", "v2.5.3; rm -rf /", "v2.5.3\nv2.5.4", "../v2.5.3"):
            with self.subTest(tag=tag):
                self.assertEqual(self._run("2.5.2", tag), (1, ""))

    def test_no_terminal_and_no_passwordless_sudo_says_so(self):
        self.assertEqual(self._run("2.5.2", "v2.5.3", sudo_ok="no"), (3, ""))
        handle = _case_branch(self.launcher, "handle-action", "reset")
        self.assertIn('elif [ "$update_rc" -eq 3 ]; then', handle)


class TestSystemdUserScope(unittest.TestCase):
    """Both installers set the services up as user units."""

    def setUp(self):
        self.launcher = _read(LAUNCHER)

    def _commands(self, text):
        """systemctl command lines (not log/echo text) outside `uninstall`,
        which also cleans up the system units of very old installs."""
        start = text.index("\n        uninstall)\n")
        end = text.index("\n        reset-onionheaven)\n", start)
        text = text[:start] + text[end:]
        out = []
        for line in _code_lines(text):
            s = line.strip()
            if s.startswith(("log ", "echo ")):
                continue
            if re.search(r"(^|[;&|(]\s*|\bif\s+|\bthen\s+|\bdo\s+)(sudo\s+)?systemctl\b", s):
                out.append(s)
        return out

    def test_every_systemctl_is_user_scope(self):
        cmds = self._commands(self.launcher)
        self.assertTrue(cmds, "no systemctl calls found; update this test")
        for c in cmds:
            with self.subTest(c):
                self.assertNotRegex(c, r"\bsudo\s+systemctl\b")
                self.assertRegex(c, r"\bsystemctl\s+--user\b")

    def test_heartbeat_goes_down_before_the_containers(self):
        for label, nxt in (("stop", "restart"), ("restart", "status"), ("reset", "validate-oh-address")):
            with self.subTest(label):
                body = _case_branch(self.launcher, label, nxt)
                self.assertLess(body.index("stop_heartbeat_client"), body.index("stop_containers"))
        handle = _case_branch(self.launcher, "handle-action", "reset")
        for action in ("stop)", "restart)"):
            with self.subTest(action):
                sub = handle[handle.index(f"\n                {action}\n"):]
                sub = sub[:sub.index(";;")]
                self.assertLess(sub.index("stop_heartbeat_client"), sub.index("stop_containers"))

    def test_the_service_brings_the_heartbeat_back(self):
        start = _case_branch(self.launcher, "start", "stop")
        notify = start.index('if [ "${NOTIFY_SOCKET:-}" ]; then')
        self.assertIn("start_heartbeat_client --no-block", start[notify:])

    def test_start_on_boot_enables_the_user_units(self):
        body = _bash_function(self.launcher, "apply_config_updates")
        self.assertIn("systemctl --user enable onionpress.service onionpress-heartbeat.service", body)
        self.assertIn("systemctl --user disable onionpress.service onionpress-heartbeat.service", body)

    def test_stop_heartbeat_client_asks_the_user_manager(self):
        fn = _bash_function(self.launcher, "stop_heartbeat_client")
        with tempfile.TemporaryDirectory() as tmp:
            script = (fn + 'log() { :; }\n'
                      'systemctl() { echo "$*" >> "$OUT/calls"; [ "$ACTIVE" = yes ]; }\n'
                      'if stop_heartbeat_client; then echo was-running; fi\n')
            for active, expect in (("yes", True), ("no", False)):
                calls = os.path.join(tmp, "calls")
                if os.path.exists(calls):
                    os.remove(calls)
                r = _run_bash(script, env={"OUT": tmp, "ACTIVE": active,
                                           "LOG_FILE": os.path.join(tmp, "log")})
                with open(calls) as f:
                    got = f.read().splitlines()
                self.assertEqual("was-running" in r.stdout, expect)
                self.assertEqual(got[0], "--user is-active --quiet onionpress-heartbeat.service")
                if expect:
                    self.assertEqual(got[1], "--user stop onionpress-heartbeat.service")
                else:
                    self.assertEqual(len(got), 1)


class TestWatcherBackgroundWork(unittest.TestCase):

    def setUp(self):
        self.launcher = _read(LAUNCHER)
        self.fn = _bash_function(self.launcher, "run_detached")

    def test_handle_action_marks_the_watcher(self):
        handle = _case_branch(self.launcher, "handle-action", "reset")
        self.assertLess(handle.index("ONIONPRESS_IN_WATCHER=true"), handle.index("case \"$action\" in"))

    def test_derivative_rebuild_is_detached_through_it(self):
        body = _bash_function(self.launcher, "start_containers")
        self.assertRegex(body, r"\n\s*run_detached env PYTHONPATH=\"\$ONIONPRESS_PYTHONPATH\" "
                               r"python3 -m onionpress\.cli regenerate-derivatives\n")
        self.assertNotRegex(body, r"\(\s*op_py regenerate-derivatives")

    def _run(self, in_watcher):
        tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", tmp])
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "systemd-run"), "w") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > "$OUT/systemd-run"\n')
        os.chmod(os.path.join(bin_dir, "systemd-run"), 0o755)
        script = (self.fn + 'log() { :; }\n'
                  'ONIONPRESS_IN_WATCHER="$IN_WATCHER"\n'
                  'run_detached sh -c \'echo ran > "$OUT/ran"\'\n')
        r = _run_bash(script, env={
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "OUT": tmp, "IN_WATCHER": in_watcher, "LOG_FILE": os.path.join(tmp, "log"),
            "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
            "MYSQL_ROOT_PASSWORD": "s3cret-not-for-argv",
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        return tmp

    def test_from_the_watcher_it_goes_to_a_transient_user_unit(self):
        tmp = self._run("true")
        with open(os.path.join(tmp, "systemd-run")) as f:
            argv = f.read().splitlines()
        self.assertIn("--user", argv)
        self.assertIn("--collect", argv)
        self.assertIn("--setenv=DOCKER_HOST=unix:///run/user/1000/docker.sock", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["sh", "-c", 'echo ran > "$OUT/ran"'])
        self.assertFalse(any("s3cret" in a for a in argv))
        self.assertFalse(os.path.exists(os.path.join(tmp, "ran")))

    def test_elsewhere_it_runs_in_a_detached_subshell(self):
        tmp = self._run("")
        self.assertFalse(os.path.exists(os.path.join(tmp, "systemd-run")))
        for _ in range(100):
            if os.path.exists(os.path.join(tmp, "ran")):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(tmp, "ran")))


if __name__ == "__main__":
    unittest.main()
