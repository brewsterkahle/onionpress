#!/usr/bin/env python3
"""Upgrading the .deb restarts the installing user's OnionPress (issue #357).

The package's postinst (a heredoc in build/build-linux.sh) writes the new
files, runs `systemctl --global enable` and the system manager's
daemon-reload, but through 2.5.6 reloaded and started the USER units only
on a first install (`$2` empty), and prerm does nothing on an upgrade. So
after an upgrade the old onionpress.service (a long-running Type=notify
user unit whose `start` stays in its status loop), the old heartbeat
daemon and the tray kept running the old code, the containers the old
pinned images, and the status reported the old version (the Settings page
kept offering the update just installed), while the watcher timer ran the
new `handle-action` every 10 s against all of it.

Now postinst's upgrade path, for the user it already works out, reloads
the user manager, stops the heartbeat and watcher units of a release
before #407 (their files are gone; the heartbeat's runtime mask, the old
install hold of #464/#506, is lifted), `try-restart`s onionpress.service
and restarts onionpress-daemon.service (an upgrade from before #407 has
none running) through the `--machine=USER@.host` mechanism of the
first-install path (the daemon is ordered Before= onionpress.service, as
the launcher's own `restart` orders them), replaces the tray the way a
first install starts it, and says so. A failure leaves the old code
running and prints what to run; it never fails the install.

The static part reads the heredoc; the behavioural part runs the generated
postinst under bash as this user, with every command that reaches the
system (systemctl, loginctl, pgrep, pkill, runuser, su, getent, install,
chown, id) replaced by a stub that records its arguments.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUILD = "build/build-linux.sh"
UNITS = ("linux/onionpress.service", "linux/onionpress-daemon.service")


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _heredoc(text, tag):
    m = re.search(r"<<'" + tag + r"'\n(.*?)\n" + tag + r"\n", text, re.DOTALL)
    if m is None:
        raise AssertionError(f"{BUILD}: no <<'{tag}' heredoc")
    return m.group(1)


def _code(text):
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _m(pattern):
    return re.compile(pattern, re.MULTILINE)


class TestPostinstText(unittest.TestCase):
    def setUp(self):
        self.postinst = _code(_heredoc(_read(BUILD), "POSTINST"))

    def _upgrade_block(self):
        m = re.search(
            r'^if \[ "\$1" = "configure" \] && \[ -n "\$2" \] && \[ "\$REAL_USER" != "root" \]; then\n(.*?)^fi$',
            self.postinst, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(
            m, f"{BUILD}: postinst has no block for the upgrade "
            "(`configure` with `$2`, the version being replaced) of a real user")
        return m.group(1)

    def test_upgrade_reloads_then_restarts_the_site_and_the_daemon_for_the_user(self):
        block = self._upgrade_block()
        reload_ = block.find('systemctl --user --machine="${REAL_USER}@.host" daemon-reload')
        self.assertGreaterEqual(reload_, 0, "the user manager must read the new unit files first")
        m = re.search(r'systemctl --user --machine="\$\{REAL_USER\}@\.host" try-restart onionpress\.service',
                      block)
        self.assertIsNotNone(m, "the site is try-restarted (a stopped site stays stopped) through "
                                "the same --machine mechanism")
        d = re.search(r'systemctl --user --machine="\$\{REAL_USER\}@\.host" restart onionpress-daemon\.service',
                      block)
        self.assertIsNotNone(d, "the daemon unit is restarted (started, after an upgrade from before #407)")
        self.assertGreater(m.start(), reload_, "daemon-reload comes before the restart")
        self.assertLess(m.start(), d.start(), "the site first: its ExecStop asks the daemon for /offline")
        # The old units of a release before #407, stopped and their hold lifted.
        old = re.search(r'systemctl --user --machine="\$\{REAL_USER\}@\.host" stop onionpress-heartbeat\.service onionpress-watcher\.timer',
                        block)
        self.assertIsNotNone(old)
        self.assertLess(reload_, old.start())
        self.assertLess(old.start(), m.start())
        self.assertIn('unmask --runtime onionpress-heartbeat.service', block)
        self.assertNotIn("--no-block", block,
                         "the restart blocks, so `onionpress update`'s own restart "
                         "after apt-get never runs beside it")
        self.assertNotIn("enable --now", block, "an upgrade enables nothing anew")
        self.assertNotIn(" restart onionpress.service", block.replace("try-restart", ""),
                         "never a plain restart of the site: that would start one the user stopped")
        self.assertNotIn("restart_units", block)
        self.assertNotIn("masked-runtime", block)

    def test_upgrade_says_so_and_what_to_do_on_failure(self):
        block = self._upgrade_block()
        self.assertRegex(block, _m(r'^\s*echo "  OnionPress \$VERSION_INSTALLED installed over \$2\. Restarting the site'))
        self.assertIn("systemctl --user try-restart onionpress\n", block,
                      "the failure message names the commands to run by hand")
        self.assertIn("systemctl --user restart onionpress-daemon\n", block)
        self.assertNotRegex(block, _m(r"^\s*exit [1-9]"), "a failed restart never fails the install")

    def test_upgrade_replaces_the_tray_the_way_a_first_install_starts_it(self):
        block = self._upgrade_block()
        self.assertRegex(block, _m(r"^\s*if pkill -u \"\$REAL_USER\" -f '/onionpress-tray\$' 2>/dev/null; then\n\s*sleep 1\n\s*fi$"))
        self.assertRegex(block, _m(r"^\s*spawn_tray$"))
        spawn = re.search(r"^spawn_tray\(\)\s*\{.*?^\}", self.postinst, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(spawn, "spawn_tray() is the one tray starter")
        self.assertIn("setsid /usr/local/bin/onionpress-tray", spawn.group(0))
        self.assertIn("pgrep -u \"$REAL_USER\" -f '/onionpress-tray$'", spawn.group(0))
        first_install = self.postinst[self.postinst.index('[ -z "$2" ] && [ "$REAL_USER" != "root" ]'):]
        self.assertIn("\n    spawn_tray\n", first_install, "the first install uses the same function")
        self.assertEqual(self.postinst.count("setsid /usr/local/bin/onionpress-tray"), 2,
                         "the tray command line lives in spawn_tray only (runuser and su)")

    def test_prerm_keeps_the_site_running_across_the_upgrade(self):
        prerm = _code(_heredoc(_read(BUILD), "PRERM"))
        m = re.search(r"^\s*upgrade\|deconfigure\|failed-upgrade\)\n(.*?)^\s*;;", prerm, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "", "prerm does nothing on upgrade: postinst restarts")

    def test_the_daemon_is_ordered_before_the_site(self):
        # The site's ExecStop asks the daemon for /offline while the tor
        # container is up: systemd stops the site first (reverse of
        # Before=) and starts the daemon first, as the Mac's `start` does.
        daemon = _read("linux/onionpress-daemon.service")
        self.assertRegex(daemon, _m(r"^Before=onionpress\.service$"))
        self.assertRegex(daemon, _m(r"^Wants=onionpress\.service$"))
        self.assertNotIn("After=onionpress", daemon)


class TestPostinstRuns(unittest.TestCase):
    """The generated postinst under bash, every system command a stub."""

    STUBS = {
        "systemctl": '''#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
case " $* " in
    *" --machine="*) [ "${STUB_MACHINE_FAILS:-}" = "1" ] && exit 1 ;;
esac
case "$*" in
    *" is-active onionpress.service") echo "${STUB_SITE_ACTIVE:-active}" ;;
    *" is-active onionpress-daemon.service") echo "${STUB_DAEMON_ACTIVE:-active}" ;;
esac
exit 0
''',
        "loginctl": '''#!/bin/bash
echo "loginctl $*" >> "$STUB_LOG"
printf 'Linger=yes\\n'
[ "${STUB_NO_DISPLAY:-}" = "1" ] || printf 'Display=:0\\n'
''',
        "pgrep": '''#!/bin/bash
echo "pgrep $*" >> "$STUB_LOG"
[ "${STUB_TRAY_RUNNING:-}" = "1" ] && [ ! -e "$STUB_LOG.killed" ]
''',
        "pkill": '''#!/bin/bash
echo "pkill $*" >> "$STUB_LOG"
[ "${STUB_TRAY_RUNNING:-}" = "1" ] || exit 1
touch "$STUB_LOG.killed"
''',
        "runuser": '''#!/bin/bash
echo "runuser $*" >> "$STUB_LOG"
exit 0
''',
        "su": '''#!/bin/bash
echo "su $*" >> "$STUB_LOG"
exit 0
''',
        "sleep": '''#!/bin/bash
echo "sleep $*" >> "$STUB_LOG"
''',
        "getent": '''#!/bin/bash
# Only the two users the tests name; a listing (the last resort) is empty.
if [ "$1" = passwd ] && [ "$2" = opuser ]; then
    echo "opuser:x:1000:1000::$STUB_HOME:/bin/bash"
    exit 0
fi
if [ "$1" = passwd ] && [ "$2" = root ]; then
    echo "root:x:0:0::$STUB_HOME:/bin/bash"
    exit 0
fi
exit 2
''',
        "install": '''#!/bin/bash
echo "install $*" >> "$STUB_LOG"
for last; do :; done
mkdir -p "$last"
''',
        "chown": '''#!/bin/bash
echo "chown $*" >> "$STUB_LOG"
''',
        "id": '''#!/bin/bash
case "$1" in
    -u) echo 1000 ;;
    -nG) echo "opuser docker" ;;
    *) exit 1 ;;
esac
''',
        "docker": '''#!/bin/bash
exit 1
''',
        "usermod": '''#!/bin/bash
echo "usermod $*" >> "$STUB_LOG"
''',
        "logname": '''#!/bin/bash
exit 1
''',
    }

    # What the script needs from the system, by name: PATH holds the stubs
    # and links to these alone, so a machine with rootless Docker set up
    # (dockerd-rootless-setuptool.sh on PATH) is not asked to set it up again.
    TOOLS = ("bash", "sh", "env", "grep", "awk", "cut", "cat", "mkdir", "rm",
             "tr", "head", "date", "chmod", "touch")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-postinst-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", "--", self.tmp], check=False))
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for tool in self.TOOLS:
            for d in ("/usr/bin", "/bin"):
                if os.path.exists(os.path.join(d, tool)):
                    os.symlink(os.path.join(d, tool), os.path.join(self.bin, tool))
                    break
            else:
                self.skipTest(f"{tool} not found")
        for name, body in self.STUBS.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.log = os.path.join(self.tmp, "calls.log")
        self.script = os.path.join(self.tmp, "postinst")
        with open(self.script, "w") as f:
            f.write(_heredoc(_read(BUILD), "POSTINST") + "\n")

    def _run(self, *args, **stub_env):
        env = {
            "PATH": self.bin,
            "HOME": self.home, "SUDO_USER": "opuser", "LC_ALL": "C",
            "STUB_LOG": self.log, "STUB_HOME": self.home,
        }
        env.update({k: str(v) for k, v in stub_env.items()})
        if os.path.exists(self.log):
            os.unlink(self.log)
        r = subprocess.run(["bash", self.script, *args], capture_output=True,
                           text=True, env=env, timeout=60)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                calls = [line.rstrip("\n") for line in f]
        return r, calls

    def test_an_upgrade_reloads_restarts_and_replaces_the_tray(self):
        r, calls = self._run("configure", "2.5.6", STUB_TRAY_RUNNING=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("installed over 2.5.6. Restarting the site for opuser", r.stdout)
        self.assertIn("OnionPress restarted", r.stdout)
        reload_ = calls.index("systemctl --user --machine=opuser@.host daemon-reload")
        restart = calls.index("systemctl --user --machine=opuser@.host try-restart onionpress.service")
        daemon = calls.index("systemctl --user --machine=opuser@.host restart onionpress-daemon.service")
        self.assertLess(restart, daemon)
        old = calls.index("systemctl --user --machine=opuser@.host stop onionpress-heartbeat.service onionpress-watcher.timer")
        self.assertLess(reload_, old)
        self.assertLess(old, restart)
        kill = calls.index("pkill -u opuser -f /onionpress-tray$")
        spawn = next(i for i, c in enumerate(calls)
                     if c.startswith("runuser -u opuser -- bash -lc setsid /usr/local/bin/onionpress-tray"))
        self.assertLess(reload_, restart)
        self.assertLess(restart, kill)
        self.assertLess(kill, spawn)
        self.assertIn("sleep 1", calls[kill:spawn], "the killed tray's D-Bus name gets a moment to go")
        # The first-install bootstrap is not run again on an upgrade.
        self.assertFalse([c for c in calls if "enable --now" in c or "daemon-reexec" in c], calls)
        self.assertFalse([c for c in calls if c.startswith("usermod")], calls)

    def test_an_upgrade_without_a_tray_running_just_starts_one(self):
        r, calls = self._run("configure", "2.5.6")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("sleep 1", calls)
        self.assertTrue([c for c in calls if "setsid /usr/local/bin/onionpress-tray" in c], calls)

    def test_an_upgrade_on_a_headless_box_starts_no_tray(self):
        r, calls = self._run("configure", "2.5.6", STUB_NO_DISPLAY=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "onionpress-tray" in c and not c.startswith("pkill")], calls)
        self.assertIn("systemctl --user --machine=opuser@.host try-restart onionpress.service", calls)
        self.assertIn("systemctl --user --machine=opuser@.host restart onionpress-daemon.service", calls)

    def test_a_failed_restart_never_fails_the_install(self):
        r, calls = self._run("configure", "2.5.6", STUB_MACHINE_FAILS=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Could not restart OnionPress for opuser", r.stdout)
        self.assertIn("systemctl --user try-restart onionpress\n", r.stdout)
        self.assertIn("systemctl --user restart onionpress-daemon\n", r.stdout)
        self.assertNotIn("OnionPress restarted", r.stdout)

    def test_a_first_install_restarts_nothing(self):
        r, calls = self._run("configure")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "try-restart" in c], calls)
        self.assertNotIn("Restarting the site", r.stdout)
        self.assertTrue([c for c in calls if "enable --now" in c], "the first install enables and starts")
        self.assertTrue([c for c in calls if "setsid /usr/local/bin/onionpress-tray" in c],
                        "the first install still starts the tray")

    def test_an_upgrade_for_root_restarts_nothing(self):
        r, calls = self._run("configure", "2.5.6", SUDO_USER="root")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "--machine" in c], calls)


class TestRetiredUnits(TestPostinstRuns):
    """The heartbeat and watcher units of a release before #407, and the
    heartbeat's runtime mask (the launcher's hold until WordPress was
    installed, issue #464, which outlived `apt purge`, issue #506): an
    upgrade stops them and lifts the mask, a first install lifts a stale
    mask before enabling, and the maintainer scripts carry no hold code
    (nothing holds the daemon's unit)."""

    def test_a_first_install_lifts_a_stale_mask_before_enabling(self):
        r, calls = self._run("configure")
        self.assertEqual(r.returncode, 0, r.stderr)
        unmask = calls.index("runuser -u opuser -- env XDG_RUNTIME_DIR=/run/user/1000 "
                             "systemctl --user unmask --runtime onionpress-heartbeat.service")
        enable = next(i for i, c in enumerate(calls) if "enable --now" in c)
        self.assertLess(unmask, enable)
        self.assertTrue(calls[enable].endswith("onionpress.service onionpress-daemon.service"), calls[enable])

    def test_the_upgrade_message_reports_what_runs(self):
        r, _ = self._run("configure", "2.6.1")
        self.assertIn("onionpress.service active, onionpress-daemon.service active", r.stdout)
        r, _ = self._run("configure", "2.6.1", STUB_SITE_ACTIVE="inactive", STUB_DAEMON_ACTIVE="inactive")
        self.assertIn("onionpress.service inactive, onionpress-daemon.service inactive", r.stdout)
        self.assertIn("a stopped site stays stopped", r.stdout)

    def test_no_hold_code_is_left(self):
        build = _read(BUILD)
        for tag in ("POSTINST", "PRERM", "POSTRM"):
            with self.subTest(tag=tag):
                text = _code(_heredoc(build, tag))
                self.assertNotIn("lift_heartbeat_holds", text)
                self.assertNotIn("masked-runtime", text)
                self.assertNotIn("mask --runtime onionpress-daemon", text)
        prerm = _code(_heredoc(build, "PRERM"))
        remove = re.search(r"^\s*remove\)\n(.*?)^\s*;;", prerm, re.DOTALL | re.MULTILINE)
        self.assertIn("systemctl --global disable onionpress.service onionpress-daemon.service "
                      "onionpress-heartbeat.service onionpress-watcher.timer", remove.group(1))
        self.assertIn("systemctl --user unmask --runtime onionpress-heartbeat", _code(_read("linux/install.sh")),
                      "install.sh lifts a stale mask too")


if __name__ == "__main__":
    unittest.main()
