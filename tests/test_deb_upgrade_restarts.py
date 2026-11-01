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
the user manager and `try-restart`s onionpress.service and the heartbeat
through the `--machine=USER@.host` mechanism of the first-install path
(the heartbeat is ordered After= onionpress.service, so systemd stops it
first, as the launcher's own `restart` does, and starts it last), replaces
the tray the way a first install starts it, and says so. A failure leaves
the old code running and prints what to run; it never fails the install.

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
UNITS = ("linux/onionpress.service", "linux/onionpress-heartbeat.service")


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

    def test_upgrade_reloads_then_try_restarts_both_units_for_the_user(self):
        block = self._upgrade_block()
        reload_ = block.find('systemctl --user --machine="${REAL_USER}@.host" daemon-reload')
        self.assertGreaterEqual(reload_, 0, "the user manager must read the new unit files first")
        m = re.search(
            r'systemctl --user --machine="\$\{REAL_USER\}@\.host" try-restart \\\n\s*\$restart_units',
            block)
        self.assertIsNotNone(
            m, "the units are try-restarted (a stopped site stays stopped) through "
            "the same --machine mechanism")
        self.assertIn('restart_units="onionpress.service onionpress-heartbeat.service"', block,
                      "both units in one call: the heartbeat is After= the site")
        self.assertIn('restart_units="onionpress.service"', block,
                      "the site alone while the heartbeat is held (#506)")
        self.assertGreater(m.start(), reload_, "daemon-reload comes before the restart")
        self.assertNotIn("--no-block", block,
                         "the restart blocks, so `onionpress update`'s own restart "
                         "after apt-get never runs beside it")
        self.assertNotIn("enable --now", block, "an upgrade enables nothing anew")
        self.assertNotIn(" restart onionpress", block.replace("try-restart", ""),
                         "never a plain restart: that would start a site the user stopped")

    def test_upgrade_says_so_and_what_to_do_on_failure(self):
        block = self._upgrade_block()
        self.assertRegex(block, _m(r'^\s*echo "  OnionPress \$VERSION_INSTALLED installed over \$2\. Restarting the site'))
        self.assertIn("systemctl --user try-restart onionpress onionpress-heartbeat", block,
                      "the failure message names the commands to run by hand")
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

    def test_the_heartbeat_is_ordered_after_the_site(self):
        # `try-restart a b` relies on the units' own ordering: systemd stops
        # the heartbeat first (reverse of After=) and starts it last.
        heartbeat = _read("linux/onionpress-heartbeat.service")
        self.assertRegex(heartbeat, _m(r"^After=onionpress\.service$"))


class TestPostinstRuns(unittest.TestCase):
    """The generated postinst under bash, every system command a stub."""

    STUBS = {
        "systemctl": '''#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
case " $* " in
    *" --machine="*) [ "${STUB_MACHINE_FAILS:-}" = "1" ] && exit 1 ;;
esac
case "$*" in
    *" is-enabled onionpress-heartbeat.service") [ -z "${STUB_HEARTBEAT_ENABLED:-}" ] || echo "$STUB_HEARTBEAT_ENABLED" ;;
    *" is-active onionpress.service") echo "${STUB_SITE_ACTIVE:-active}" ;;
    *" is-active onionpress-heartbeat.service") echo "${STUB_HEARTBEAT_ACTIVE:-active}" ;;
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
        restart = calls.index(
            "systemctl --user --machine=opuser@.host try-restart onionpress.service onionpress-heartbeat.service")
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
        self.assertIn("systemctl --user --machine=opuser@.host try-restart onionpress.service onionpress-heartbeat.service", calls)

    def test_a_failed_restart_never_fails_the_install(self):
        r, calls = self._run("configure", "2.5.6", STUB_MACHINE_FAILS=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Could not restart OnionPress for opuser", r.stdout)
        self.assertIn("systemctl --user try-restart onionpress onionpress-heartbeat", r.stdout)
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


class TestHeartbeatHold(TestPostinstRuns):
    """Issue #506: the launcher's runtime mask of the heartbeat unit (its
    hold until WordPress is installed) outlived `apt purge`, so a reinstall
    in the same boot failed `enable --now` and started nothing, and the
    upgrade said "restarted where it was running" when nothing had."""

    def test_a_first_install_lifts_a_stale_hold_before_enabling(self):
        r, calls = self._run("configure")
        self.assertEqual(r.returncode, 0, r.stderr)
        unmask = calls.index("runuser -u opuser -- env XDG_RUNTIME_DIR=/run/user/1000 "
                             "systemctl --user unmask --runtime onionpress-heartbeat.service")
        enable = next(i for i, c in enumerate(calls) if "enable --now" in c)
        self.assertLess(unmask, enable)

    def test_an_upgrade_with_a_held_heartbeat_restarts_the_site_alone(self):
        r, calls = self._run("configure", "2.6.1", STUB_HEARTBEAT_ENABLED="masked-runtime",
                             STUB_HEARTBEAT_ACTIVE="inactive")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemctl --user --machine=opuser@.host try-restart onionpress.service", calls)
        self.assertFalse([c for c in calls if "try-restart" in c and "heartbeat" in c], calls)
        self.assertIn("OnionPress restarted", r.stdout)
        self.assertIn("onionpress.service active, onionpress-heartbeat.service inactive "
                      "(held until WordPress is set up)", r.stdout)
        self.assertNotIn("where it was running", r.stdout)

    def test_the_upgrade_message_reports_what_runs(self):
        r, _ = self._run("configure", "2.6.1")
        self.assertIn("onionpress.service active, onionpress-heartbeat.service active", r.stdout)
        r, _ = self._run("configure", "2.6.1", STUB_SITE_ACTIVE="inactive",
                         STUB_HEARTBEAT_ACTIVE="inactive")
        self.assertIn("onionpress.service inactive, onionpress-heartbeat.service inactive", r.stdout)
        self.assertIn("a stopped unit stays stopped", r.stdout)

    def test_prerm_and_postrm_lift_the_holds(self):
        build = _read(BUILD)
        prerm, postrm = _heredoc(build, "PRERM"), _heredoc(build, "POSTRM")
        fn = re.compile(r"^lift_heartbeat_holds\(\) \{\n.*?^\}\n", re.DOTALL | re.MULTILINE)
        a, b = fn.search(prerm), fn.search(postrm)
        self.assertIsNotNone(a); self.assertIsNotNone(b)
        self.assertEqual(a.group(0), b.group(0), "one function in both maintainer scripts")
        remove = re.search(r"^\s*remove\)\n(.*?)^\s*;;", _code(prerm), re.DOTALL | re.MULTILINE)
        self.assertIn("lift_heartbeat_holds", remove.group(1))
        purge = re.search(r"^\s*purge\)\n(.*?)^\s*;;", _code(postrm), re.DOTALL | re.MULTILINE)
        self.assertIn("lift_heartbeat_holds", purge.group(1))
        self.assertIn("systemctl --user unmask --runtime onionpress-heartbeat", _code(_read("linux/install.sh")),
                      "install.sh lifts a stale hold before its enable too")
        # Under bash: only a numeric /run/user/<uid> holding the mask is
        # unmasked, by that user's own manager.
        run = os.path.join(self.tmp, "run")
        for name, masked in (("1000", True), ("1001", False), ("lock", True)):
            d = os.path.join(run, name, "systemd", "user")
            os.makedirs(d)
            if masked:
                os.symlink("/dev/null", os.path.join(d, "onionpress-heartbeat.service"))
        getent = os.path.join(self.bin, "getent")
        with open(getent, "w") as f:
            f.write('#!/bin/bash\n[ "$2" = 1000 ] && echo "opuser:x:1000:1000::/h:/bin/bash"\n')
        os.chmod(getent, 0o755)
        script = a.group(0) + "lift_heartbeat_holds\necho rc=$?\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": self.bin, "STUB_LOG": self.log, "ONIONPRESS_RUN_USER": run})
        self.assertEqual(r.stdout.strip(), "rc=0", r.stderr)
        with open(self.log) as f:
            calls = [line.rstrip("\n") for line in f]
        self.assertEqual(calls, [f"runuser -u opuser -- env XDG_RUNTIME_DIR={run}/1000 "
                                 "systemctl --user unmask --runtime onionpress-heartbeat.service"])


if __name__ == "__main__":
    unittest.main()
