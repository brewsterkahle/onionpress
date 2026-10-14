#!/usr/bin/env python3
"""`onionpress update --from-checkout` installs exactly the shipped file
set, from a source no other user controls. (Plain `onionpress update`
installs a release's checked .deb: tests/test_linux_release_update.py.)

linux/install-tree.sh holds the one list of what goes into /opt/onionpress;
the .deb (build/build-linux.sh), linux/install.sh and `onionpress update`
all install with it. `update` had its own copy commands: it copied
src/onion_auth.py and src/key_manager.py long after they moved (the cp
failed unnoticed), never refreshed lib/onionpress, and its `cp -r` of
docker/ and plugins/ landed inside the existing folders. Its source was
the first git checkout in ANY /home/*/, or a `reset --hard` to whatever
origin/main said, installed as root.

The scripts run for real here: install-tree.sh against scratch checkouts,
the launcher's update_source_checkout with git and sudo stubbed. Nothing
runs git, sudo or Docker for real.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
INSTALL_TREE = os.path.join(PROJECT_DIR, "linux", "install-tree.sh")
LINUX = "linux/onionpress"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


def _case_branch(text, label):
    start = text.index(f"\n        {label})\n")
    return text[start:text.index("\n            ;;\n", start)]


def _manifest():
    out = subprocess.run(["bash", INSTALL_TREE, "list"], capture_output=True,
                         text=True, check=True, timeout=30).stdout
    return [line.split() for line in out.splitlines()]


def _tree(path):
    out = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, path)] = f.read()
    return out


def _run(*args):
    return subprocess.run(["bash", INSTALL_TREE, *args], capture_output=True,
                          text=True, timeout=120)


class TestTheList(unittest.TestCase):

    def test_every_entry_is_in_the_repo(self):
        entries = _manifest()
        self.assertGreater(len(entries), 10)
        for kind, src, *_ in entries:
            with self.subTest(src):
                path = os.path.join(PROJECT_DIR, src)
                self.assertTrue(os.path.isdir(path) if kind == "tree" else os.path.isfile(path))

    def test_what_the_launcher_and_daemon_need(self):
        dests = {e[2]: e for e in _manifest()}
        for dest in ("onionpress", "onionpress-tray", "docker", "plugins", "themes",
                     "lib/onionpress", "assets", "scripts/onion_auth.py",
                     "scripts/key_manager.py", "scripts/onionpress-heartbeat.py",
                     "onionpress-wordpress-egress", "VERSION",
                     # install.sh copies these on to the user's systemd folder
                     "onionpress.service", "onionpress-heartbeat.service",
                     "onionpress-watcher.service", "onionpress-watcher.timer",
                     "app-icon.png"):
            with self.subTest(dest):
                self.assertIn(dest, dests)
        # The moved modules come from the package, not their old place.
        self.assertEqual(dests["scripts/key_manager.py"][1], "src/onionpress/key_manager.py")
        self.assertEqual(dests["scripts/onion_auth.py"][1], "src/onionpress/onion_auth.py")

    def test_every_plugin_script_is_listed(self):
        listed = {e[1] for e in _manifest()}
        scripts = os.path.join(PROJECT_DIR, "app", "Resources", "scripts")
        for name in os.listdir(scripts):
            if name.endswith(".sh"):
                with self.subTest(name):
                    self.assertIn(f"app/Resources/scripts/{name}", listed)


class TestEveryWayInUsesIt(unittest.TestCase):

    def test_the_deb(self):
        build = _read("build/build-linux.sh")
        fn = _shell_function(build, "collect_files")
        self.assertIn('bash "$PROJECT_DIR/linux/install-tree.sh" stage "$PROJECT_DIR" "$1"', fn)
        self.assertNotIn("cp ", fn)

    def test_install_sh(self):
        text = _read("linux/install.sh")
        self.assertIn('$SUDO bash "$REPO_DIR/linux/install-tree.sh" install "$REPO_DIR" "$INSTALL_DIR"',
                      text)
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r'cp (-r )?"\$REPO_DIR/(app|src|linux)/[^"]*" "\$INSTALL_DIR')

    def test_update(self):
        branch = _shell_function(_read(LINUX), "update_from_checkout")
        self.assertIn('sudo bash "$repo_dir/linux/install-tree.sh" install "$repo_dir" "$INSTALL_DIR"',
                      branch)
        code = "\n".join(l for l in branch.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("sudo cp", code)
        self.assertNotIn("src/onion_auth.py", code)
        self.assertNotIn("src/key_manager.py", code)
        self.assertNotIn('sudo tee "$INSTALL_DIR/VERSION"', code)

    def test_the_deb_and_an_install_are_the_same_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            deb = os.path.join(tmp, "deb")
            env = {"PROJECT_DIR": PROJECT_DIR, "PATH": "/usr/bin:/bin"}
            fn = _shell_function(_read("build/build-linux.sh"), "collect_files")
            subprocess.run(["bash", "-c", "set -e\n" + fn + '\ncollect_files "$1"', "bash", deb],
                           check=True, env=env, capture_output=True, timeout=120)
            opt = os.path.join(tmp, "opt")
            os.mkdir(opt)
            r = _run("install", PROJECT_DIR, os.path.join(opt, "onionpress"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(_tree(deb), _tree(os.path.join(opt, "onionpress")))


class _Checkout:
    """A scratch source checkout holding every listed source, small."""

    def __init__(self, case):
        self.tmp = tempfile.mkdtemp(prefix="op-install-tree-")
        case.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "src")
        for kind, path, *_ in _manifest():
            full = os.path.join(self.src, path)
            if kind == "tree":
                os.makedirs(os.path.join(full, "sub"), exist_ok=True)
                self.write(os.path.join(path, "sub", "file.txt"), f"{path}\n")
                os.makedirs(os.path.join(full, "__pycache__"), exist_ok=True)
                self.write(os.path.join(path, "__pycache__", "x.cpython-311.pyc"), "pyc")
            elif kind == "egress":
                self.write(path, 'RULES_SCRIPT = r"""\ncase "$1" in\nesac\n"""\n')
            elif kind == "version":
                self.write(path, '        self.version = "9.8.7"\n')
            else:
                self.write(path, f"{path}\n")

    def write(self, rel, text):
        full = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)


class TestInstall(unittest.TestCase):

    def setUp(self):
        self.co = _Checkout(self)
        self.opt = os.path.join(self.co.tmp, "opt")
        os.mkdir(self.opt)
        self.target = os.path.join(self.opt, "onionpress")

    def _leftovers(self):
        return [n for n in os.listdir(self.opt) if n != "onionpress"]

    def test_stage_lays_out_the_list(self):
        dest = os.path.join(self.co.tmp, "staged")
        r = _run("stage", self.co.src, dest)
        self.assertEqual(r.returncode, 0, r.stderr)
        tree = _tree(dest)
        self.assertEqual(tree["VERSION"], b"9.8.7\n")
        self.assertEqual(tree["onionpress-wordpress-egress"], b'#!/bin/sh\ncase "$1" in\nesac\n')
        self.assertEqual(tree["lib/onionpress/sub/file.txt"], b"src/onionpress\n")
        self.assertEqual(tree["scripts/key_manager.py"], b"src/onionpress/key_manager.py\n")
        self.assertFalse([p for p in tree if "__pycache__" in p or p.endswith(".pyc")])
        for kind, src, dest_rel, *mode in _manifest():
            if mode:
                with self.subTest(dest_rel):
                    self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dest, dest_rel)).st_mode),
                                     int(mode[0], 8))

    def test_an_install_replaces_the_whole_tree(self):
        os.makedirs(os.path.join(self.target, "docker"))
        with open(os.path.join(self.target, "docker", "docker-compose.yml"), "w") as f:
            f.write("old compose")
        with open(os.path.join(self.target, "stale-file"), "w") as f:
            f.write("from an older version")
        r = _run("install", self.co.src, self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Installed OnionPress 9.8.7", r.stdout)
        tree = _tree(self.target)
        self.assertNotIn("stale-file", tree)
        self.assertNotIn("docker/docker-compose.yml", tree)
        self.assertNotIn("docker/docker/sub/file.txt", tree)
        self.assertIn("docker/sub/file.txt", tree)
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o755)
        self.assertEqual(self._leftovers(), [])
        # Again, over itself.
        self.assertEqual(_run("install", self.co.src, self.target).returncode, 0)
        self.assertEqual(_tree(self.target), tree)
        self.assertEqual(self._leftovers(), [])

    def test_a_fresh_install(self):
        r = _run("install", self.co.src, self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_tree(self.target)["VERSION"], b"9.8.7\n")

    def _old_install(self):
        os.makedirs(self.target)
        with open(os.path.join(self.target, "onionpress"), "w") as f:
            f.write("the old launcher")
        return _tree(self.target)

    def test_a_missing_source_installs_nothing(self):
        before = self._old_install()
        os.remove(os.path.join(self.co.src, "src", "onionpress", "key_manager.py"))
        shutil.rmtree(os.path.join(self.co.src, "app", "Resources", "themes"))
        r = _run("install", self.co.src, self.target)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("src/onionpress/key_manager.py", r.stderr)
        self.assertIn("app/Resources/themes", r.stderr)
        self.assertEqual(_tree(self.target), before)
        self.assertEqual(self._leftovers(), [])

    def test_links_and_odd_files_install_nothing(self):
        before = self._old_install()
        os.symlink("/etc/passwd", os.path.join(self.co.src, "app", "Resources", "docker", "sub", "x"))
        r = _run("install", self.co.src, self.target)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a plain file", r.stderr)
        self.assertEqual(_tree(self.target), before)
        os.remove(os.path.join(self.co.src, "app", "Resources", "docker", "sub", "x"))
        launcher = os.path.join(self.co.src, "linux", "onionpress")
        os.remove(launcher)
        os.symlink("/etc/passwd", launcher)
        r = _run("install", self.co.src, self.target)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(_tree(self.target), before)
        self.assertEqual(self._leftovers(), [])

    def test_no_version_installs_nothing(self):
        before = self._old_install()
        self.co.write("src/menubar.py", "no version here\n")
        self.assertNotEqual(_run("install", self.co.src, self.target).returncode, 0)
        self.assertEqual(_tree(self.target), before)
        self.assertEqual(self._leftovers(), [])

    def test_stage_wants_a_new_or_empty_folder(self):
        dest = os.path.join(self.co.tmp, "full")
        os.makedirs(dest)
        open(os.path.join(dest, "x"), "w").close()
        self.assertNotEqual(_run("stage", self.co.src, dest).returncode, 0)

    def test_a_linked_install_dir_is_refused(self):
        real = os.path.join(self.co.tmp, "elsewhere")
        os.makedirs(real)
        os.symlink(real, self.target)
        self.assertNotEqual(_run("install", self.co.src, self.target).returncode, 0)
        self.assertEqual(os.listdir(real), [])


# ─── Where `update` takes its source from ───────────────────────────────

UPDATE_PRELUDE = r'''
log() { echo "[log] $*" >> "$LOG_FILE"; }
git() {
    echo "git $*" >> "$CALLS"
    case "$*" in
        *"status --porcelain"*) printf '%s' "${GIT_STATUS:-}"; return "${STATUS_RC:-0}" ;;
        *"pull --ff-only"*) return "${PULL_RC:-0}" ;;
    esac
    return 0
}
sudo() { echo "sudo $*" >> "$CALLS"; return "${SUDO_RC:-1}"; }
'''


class TestUpdateSource(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-update-src-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.calls = os.path.join(self.tmp, "calls")
        open(self.calls, "w").close()

    def checkout(self, rel="onionpress"):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.join(path, ".git"))
        with open(os.path.join(path, "file"), "w") as f:
            f.write("x")
        return path

    def _run(self, **env):
        text = _read(LINUX)
        script = ("set -e\n" + UPDATE_PRELUDE
                  + _shell_function(text, "update_source_checkout") + "\n"
                  + _shell_function(text, "update_checkout_trusted") + "\n"
                  'rc=0; update_source_checkout || rc=$?\n'
                  'echo "rc=$rc"; echo "dir=$UPDATE_REPO_DIR"; echo "error=$UPDATE_ERROR"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": os.environ["PATH"], "HOME": self.home,
                                "LOG_FILE": os.path.join(self.tmp, "log"),
                                "CALLS": self.calls, **env})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.calls) as f:
            calls = f.read().splitlines()
        return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l), calls

    def test_the_users_own_checkout_moves_forward_only(self):
        path = self.checkout()
        out, calls = self._run()
        self.assertEqual((out["rc"], out["dir"]), ("0", path))
        self.assertEqual(calls, [f"git -C {path} status --porcelain",
                                 f"git -C {path} pull --ff-only"])

    def test_home_tmp_comes_first(self):
        self.checkout()
        first = self.checkout("tmp/onionpress")
        out, _ = self._run()
        self.assertEqual(out["dir"], first)

    def test_local_changes_are_refused(self):
        self.checkout()
        out, calls = self._run(GIT_STATUS=" M linux/onionpress\n")
        self.assertEqual(out["rc"], "1")
        self.assertIn("local changes", out["error"])
        self.assertFalse([c for c in calls if "pull" in c])

    def test_an_unreadable_state_is_refused(self):
        self.checkout()
        out, calls = self._run(STATUS_RC="128")
        self.assertEqual(out["rc"], "1")
        self.assertFalse([c for c in calls if "pull" in c])

    def test_no_fast_forward_no_reset(self):
        self.checkout()
        out, calls = self._run(PULL_RC="1")
        self.assertEqual(out["rc"], "1")
        self.assertIn("--ff-only", out["error"])
        self.assertFalse([c for c in calls if "reset" in c or "fetch" in c], calls)

    def test_a_file_anyone_may_write_is_refused(self):
        path = self.checkout()
        os.chmod(os.path.join(path, "file"), 0o666)
        out, calls = self._run()
        self.assertEqual(out["rc"], "1")
        self.assertIn("writable by everyone", out["error"])
        self.assertEqual(calls, [])

    def test_a_folder_above_anyone_may_write_is_refused(self):
        self.checkout()
        os.chmod(self.home, 0o777)
        self.addCleanup(os.chmod, self.home, 0o755)
        out, calls = self._run()
        self.assertEqual(out["rc"], "1")
        self.assertIn(self.home.split(os.sep)[-1], out["error"])
        self.assertEqual(calls, [])

    def test_a_linked_checkout_is_not_taken(self):
        real = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(real, ".git"))
        os.symlink(real, os.path.join(self.home, "onionpress"))
        out, calls = self._run()
        # Not the link: root's checkout, cloned with sudo (refused here).
        self.assertEqual(out["rc"], "1")
        self.assertEqual(out["dir"], "/opt/onionpress-src")
        self.assertTrue(calls and calls[0].startswith("sudo git clone "), calls)

    def test_no_other_users_home(self):
        body = _shell_function(_read(LINUX), "update_source_checkout")
        code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("/home/", code)
        self.assertIn('for d in "$HOME/tmp/onionpress" "$HOME/onionpress"; do', code)
        launcher = "\n".join(l for l in _read(LINUX).splitlines()
                             if not l.lstrip().startswith("#"))
        self.assertNotIn("reset --hard", launcher)
        self.assertIn('update_checkout_trusted "$UPDATE_REPO_DIR" "$uid" || return 1\n'
                      "    return 0", body)

    def test_update_starts_with_it(self):
        branch = _shell_function(_read(LINUX), "update_from_checkout")
        self.assertLess(branch.index("update_source_checkout"), branch.index("install-tree.sh"))


class TestHeartbeatClientBothScopes(unittest.TestCase):
    """stop, restart and update stopped and started the heartbeat client at
    system scope only (sudo systemctl), while installs run it as a user
    unit: it kept beating with the site down."""

    def test_stop_restart_update(self):
        text = _read(LINUX)
        for label in ("stop", "restart", "update"):
            with self.subTest(label):
                # update restarts in update_finish, after either install
                branch = (_shell_function(text, "update_finish") if label == "update"
                          else _case_branch(text, label))
                code = "\n".join(l for l in branch.splitlines() if not l.lstrip().startswith("#"))
                self.assertNotIn("systemctl", code)
                self.assertLess(code.index("stop_heartbeat_client"), code.index("stop_containers"))
                if label != "stop":
                    self.assertLess(code.index("start_containers"),
                                    code.index("start_heartbeat_client"))


if __name__ == "__main__":
    unittest.main()
