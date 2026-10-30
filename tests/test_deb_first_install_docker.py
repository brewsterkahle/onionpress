#!/usr/bin/env python3
"""A first install of the .deb leaves OnionPress running, no manual step.

The e2e run of 2026-10-24 (Debian 13, Debian's docker.io 26.1.5) found
the package's postinst (a heredoc in build/build-linux.sh) printing "One
manual step needed to finish" on a fresh machine:

- Rootless Docker was never tried: the postinst asked `command -v
  dockerd-rootless-setuptool.sh`, and docker.io keeps the tool (and the
  dockerd-rootless.sh it looks for on PATH) in /usr/share/docker.io/contrib.
  Found there, the tool still exits 127 after setting the daemon up (its
  last step runs `$BIN/docker version`, and contrib has no docker CLI), so
  its exit status is not the verdict: the rootless daemon answering is.
  It also needs rootlesskit, which docker.io only suggests (now in
  Depends, as `rootlesskit | docker-ce-rootless-extras`).
- The docker-group fallback ran `daemon-reexec` on the user's manager,
  which keeps the groups it started with, so onionpress.service could not
  reach Docker. Now the manager is restarted, but only while the user has
  no login session (a restart ends everything it runs); with one, the
  manual step is printed, as before.
- Even with rootless Docker up, the final `systemctl --user
  --machine=USER@.host enable --now` failed: --machine goes through the
  user's session bus, and dbus-user-session came in with the same apt
  run, after the user's manager started, so there was no bus. The first
  install now talks to the manager over its private socket (runuser with
  XDG_RUNTIME_DIR), starts dbus.socket, and does not wait for the first
  start's image pulls (--no-block).

The e2e run of 2026-10-30 (Ubuntu 24.04 guest, Ubuntu's docker.io 29.1.3)
found two more, both the package's (issue #486):

- Ubuntu's docker.io ships no dockerd-rootless-setuptool.sh at all (its
  contrib holds check-config.sh and nuke-graph-directory.sh), so the
  postinst fell back to the docker group without a word. The package now
  carries Docker's own two scripts (linux/docker-rootless/, pinned by
  hash; tests/test_docker_rootless_vendored.py) as the last resort after
  PATH and docker.io's contrib, and every skipped or failed attempt says
  why, at the terminal and in ~/.onionpress/docker-rootless-setup.log.
- `docker compose` was missing: the Depends alternative
  `docker-compose-plugin | docker-compose` resolved to Ubuntu's
  docker-compose, the Python Compose v1 (Debian 13's docker-compose is
  v2). Every Compose alternative is now v2.

The behavioural part runs the generated postinst as this user under bash,
every command that reaches the system replaced by a stub that records its
arguments; runuser runs what it is given, so the setup tool and `docker
info` are the stubs' to answer.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest

from test_deb_upgrade_restarts import BUILD, _code, _heredoc, _read


def _control_depends():
    m = re.search(r"^Depends: (.*)$", _read(BUILD), re.MULTILINE)
    assert m, f"{BUILD}: no Depends line"
    return [d.strip() for d in m.group(1).split(",")]


class TestPostinstText(unittest.TestCase):
    def setUp(self):
        self.postinst = _code(_heredoc(_read(BUILD), "POSTINST"))

    def test_rootlesskit_is_a_dependency(self):
        self.assertIn("rootlesskit | docker-ce-rootless-extras", _control_depends(),
                      "docker.io only suggests rootlesskit; the rootless setup needs it")

    def test_compose_v1_never_satisfies_depends(self):
        compose = [d for d in _control_depends() if "compose" in d]
        self.assertEqual(
            compose, ["docker-compose-plugin | docker-compose-v2 | docker-compose (>= 2)"],
            "Ubuntu's docker-compose is the Python v1 with no `docker compose`; its v2 "
            "is docker-compose-v2, Debian 13's docker-compose is v2")

    def test_tor_browser_is_only_suggested(self):
        # As a Recommends it brought the system tor daemon (its own
        # Recommends) on to 127.0.0.1:9050 on Ubuntu, and the launcher then
        # moved the site to 18080 (e2e, 2026-10-30).
        control = _read(BUILD)
        self.assertIn("\nSuggests: torbrowser-launcher\n", control)
        self.assertNotRegex(control, r"^Recommends:", "nothing Recommended: apt installs it, "
                            "and what it Recommends, by default on Ubuntu")

    def test_the_setup_tool_is_looked_for_in_docker_ios_contrib_too(self):
        self.assertIn("/usr/share/docker.io/contrib", self.postinst)
        self.assertIn("command -v dockerd-rootless-setuptool.sh", self.postinst,
                      "Docker CE's on PATH still counts")

    def test_the_packages_own_scripts_are_the_last_resort(self):
        self.assertIn("/opt/onionpress/docker-rootless", self.postinst)
        self.assertLess(self.postinst.index("/usr/share/docker.io/contrib"),
                        self.postinst.index("/opt/onionpress/docker-rootless"),
                        "the distro's copy comes first")

    def test_the_reasons_reach_the_log_through_the_users_shell(self):
        # Issue #490's as_user_logging_to opens the log as the user; the
        # notes about a skipped or failed attempt go the same way.
        self.assertIn('as_user_logging_to "$_rootless_log" ">>" echo "$*"', self.postinst)
        self.assertIn('as_user_logging_to "$_rootless_log" ">" true', self.postinst,
                      "the log starts when an attempt starts, before the first note")

    def test_the_users_manager_is_never_only_reexeced(self):
        self.assertNotIn("daemon-reexec", self.postinst,
                         "daemon-reexec keeps the manager's old groups")

    def test_the_users_manager_is_restarted_only_without_a_session(self):
        m = re.search(r'systemctl restart "user@\$REAL_UID\.service"', self.postinst)
        self.assertIsNotNone(m)
        before = self.postinst[:m.start()]
        sessions = before.rfind('loginctl show-user "$REAL_USER" -p Sessions --value')
        self.assertGreaterEqual(sessions, 0, "the restart is decided on the user's sessions")
        self.assertIn('if [ -n "$_sessions" ]; then\n                bootstrap_ok=0',
                      self.postinst[sessions:m.start()])


class TestFirstInstallRuns(unittest.TestCase):
    """The generated postinst's first install under bash, every system
    command a stub."""

    STUBS = {
        "systemctl": '''#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
# No session bus: the user's manager predates dbus-user-session, so the
# --machine=USER@.host bridge fails (what Debian 13 did).
case " $* " in
    *" --machine="*) exit 1 ;;
esac
case "$*" in
    "--user enable --now docker.service")
        [ -e "$STUB_LOG.tool-ran" ] || exit 1
        [ "${STUB_ROOTLESS_FAILS:-}" = "1" ] || touch "$STUB_LOG.rootless-up"
        ;;
esac
exit 0
''',
        "loginctl": '''#!/bin/bash
echo "loginctl $*" >> "$STUB_LOG"
case "$*" in
    *"-p Sessions --value"*) echo "${STUB_SESSIONS:-}" ;;
    *) printf 'Linger=yes\\n' ;;
esac
''',
        "docker": '''#!/bin/bash
echo "docker DOCKER_HOST=${DOCKER_HOST:-} $*" >> "$STUB_LOG"
case "$1" in
    info) [ "$DOCKER_HOST" = "unix:///run/user/1000/docker.sock" ] && [ -e "$STUB_LOG.rootless-up" ] ;;
    ps) [ "${STUB_DOCKER_BUSY:-}" != "1" ] || echo 0123456789ab; exit 0 ;;
    *) exit 1 ;;
esac
''',
        "runuser": '''#!/bin/bash
echo "runuser $*" >> "$STUB_LOG"
while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
''',
        "pgrep": '''#!/bin/bash
exit 1
''',
        "sleep": '''#!/bin/bash
echo "sleep $*" >> "$STUB_LOG"
''',
        "getent": '''#!/bin/bash
if [ "$1" = passwd ] && [ "$2" = opuser ]; then
    echo "opuser:x:1000:1000::$STUB_HOME:/bin/bash"
    exit 0
fi
exit 2
''',
        "install": '''#!/bin/bash
for last; do :; done
mkdir -p "$last"
''',
        "chown": '''#!/bin/bash
echo "chown $*" >> "$STUB_LOG"
exit 0
''',
        "id": '''#!/bin/bash
case "$1" in
    -u) echo 1000 ;;
    -nG) echo "opuser" ;;
    *) exit 1 ;;
esac
''',
        # The postinst removes /var/run/docker.sock; never the real one
        # (a CI runner has Docker).
        "rm": '''#!/bin/bash
echo "rm $*" >> "$STUB_LOG"
for a; do
    case "$a" in /var/run/*|/run/*) exit 0 ;; esac
done
exec /bin/rm "$@"
''',
        "usermod": '''#!/bin/bash
echo "usermod $*" >> "$STUB_LOG"
''',
        # Ubuntu 24.04's RootlessKit 1.1.1 knows no --detach-netns;
        # STUB_ROOTLESSKIT_V2=1 answers like 2.x, which does.
        "rootlesskit": '''#!/bin/bash
echo "rootlesskit $*" >> "$STUB_LOG"
echo "   --copy-up value   mount a filesystem and copy-up the contents"
[ "${STUB_ROOTLESSKIT_V2:-}" != "1" ] || echo "   --detach-netns   detach network namespaces"
''',
        "logname": '''#!/bin/bash
exit 1
''',
    }

    # docker.io's setup tool: sets the daemon up, then fails on
    # `$BIN/docker version` (contrib has no docker CLI).
    SETUPTOOL = '''#!/bin/bash
echo "setuptool PATH=$PATH $*" >> "$STUB_LOG"
command -v dockerd-rootless.sh >/dev/null || exit 1
touch "$STUB_LOG.tool-ran"
echo "dockerd-rootless-setuptool.sh: 360: $BIN/docker: not found" >&2
exit 127
'''

    TOOLS = ("bash", "sh", "env", "grep", "awk", "cut", "cat", "mkdir",
             "tr", "head", "date", "chmod", "touch", "printf", "echo", "true")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-postinst-first-")
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
            self._write_exe(os.path.join(self.bin, name), body)
        self.contrib = os.path.join(self.tmp, "docker.io-contrib")
        os.mkdir(self.contrib)
        self.ce_bin = os.path.join(self.tmp, "ce-bin")
        os.mkdir(self.ce_bin)
        self.packaged = os.path.join(self.tmp, "opt-onionpress-docker-rootless")
        os.mkdir(self.packaged)
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.log = os.path.join(self.tmp, "calls.log")
        self.script = os.path.join(self.tmp, "postinst")
        with open(self.script, "w") as f:
            f.write(_heredoc(_read(BUILD), "POSTINST") + "\n")

    def _write_exe(self, path, body):
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def _docker_io_layout(self):
        self._write_exe(os.path.join(self.contrib, "dockerd-rootless-setuptool.sh"), self.SETUPTOOL)
        self._write_exe(os.path.join(self.contrib, "dockerd-rootless.sh"), "#!/bin/sh\n")

    def _docker_ce_layout(self):
        self._write_exe(os.path.join(self.ce_bin, "dockerd-rootless-setuptool.sh"), self.SETUPTOOL)
        self._write_exe(os.path.join(self.ce_bin, "dockerd-rootless.sh"), "#!/bin/sh\n")

    def _packaged_layout(self):
        """The package's own copy of Docker's scripts (/opt/onionpress/docker-rootless)."""
        self._write_exe(os.path.join(self.packaged, "dockerd-rootless-setuptool.sh"), self.SETUPTOOL)
        self._write_exe(os.path.join(self.packaged, "dockerd-rootless.sh"), "#!/bin/sh\n")

    def _rootless_log_text(self):
        with open(os.path.join(self.home, ".onionpress", "docker-rootless-setup.log")) as f:
            return f.read()

    def _run(self, path_extra=(), **stub_env):
        env = {
            "PATH": ":".join((self.bin, *path_extra)),
            "HOME": self.home, "SUDO_USER": "opuser", "LC_ALL": "C",
            "STUB_LOG": self.log, "STUB_HOME": self.home,
            "ONIONPRESS_DOCKER_IO_CONTRIB": self.contrib,
            "ONIONPRESS_DOCKER_ROOTLESS_DIR": self.packaged,
        }
        env.update({k: str(v) for k, v in stub_env.items()})
        r = subprocess.run(["bash", self.script, "configure"], capture_output=True,
                           text=True, env=env, timeout=60)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                calls = [line.rstrip("\n") for line in f]
        return r, calls

    def _enabled_onionpress(self, calls):
        return [c for c in calls if c.startswith(
            "systemctl --user enable --now --no-block onionpress.service")]

    def test_docker_ios_contrib_tool_sets_up_rootless_although_it_exits_127(self):
        self._docker_io_layout()
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        tool = [c for c in calls if c.startswith("setuptool ")]
        self.assertEqual(len(tool), 1, calls)
        self.assertTrue(tool[0].startswith(f"setuptool PATH={self.contrib}:"),
                        "contrib goes first on the tool's PATH, so it finds dockerd-rootless.sh")
        self.assertTrue(tool[0].endswith(" install --force"), tool[0])
        self.assertIn("systemctl disable --now docker.service docker.socket", calls)
        self.assertIn("rm -f /var/run/docker.sock", calls)
        self.assertIn("Docker mode: rootless (no logout required).", r.stdout)
        self.assertNotIn("One manual step needed", r.stdout)
        self.assertTrue(self._enabled_onionpress(calls), calls)
        self.assertFalse([c for c in calls if c.startswith("usermod")], calls)
        self.assertFalse([c for c in calls if "user@1000.service" in c], calls)
        self.assertNotIn("systemctl enable --now docker.socket docker.service", calls,
                         "the system daemon stays off once rootless runs")
        self.assertIn("docker: not found", self._rootless_log_text(), "the tool's output is kept")

    def test_the_packages_own_scripts_set_up_rootless_where_docker_io_ships_none(self):
        # Ubuntu 24.04's docker.io 29.1.3: no setup tool on PATH or in contrib.
        self._packaged_layout()
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        tool = [c for c in calls if c.startswith("setuptool ")]
        self.assertEqual(len(tool), 1, calls)
        self.assertTrue(tool[0].startswith(f"setuptool PATH={self.packaged}:"), tool[0])
        self.assertIn("Docker mode: rootless (no logout required).", r.stdout)
        self.assertNotIn("One manual step needed", r.stdout)
        self.assertFalse([c for c in calls if c.startswith("usermod")], calls)

    def test_an_old_rootlesskit_gets_the_no_detach_netns_drop_in(self):
        # Ubuntu 24.04: RootlessKit 1.1.1 refuses the --detach-netns that
        # Docker's wrapper passes by default, so the daemon never started
        # (e2e, 2026-10-30).
        self._packaged_layout()
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        drop_in = os.path.join(self.home, ".config", "systemd", "user", "docker.service.d",
                               "onionpress-rootlesskit.conf")
        with open(drop_in) as f:
            self.assertEqual(f.read(), "[Service]\nEnvironment=DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false\n")
        probe = calls.index("rootlesskit --help")
        tool = calls.index([c for c in calls if c.startswith("setuptool ")][0])
        self.assertLess(probe, tool, "the drop-in is in place before the tool starts the unit")
        self.assertIn("RootlessKit here predates --detach-netns", self._rootless_log_text())
        self.assertIn("Docker mode: rootless (no logout required).", r.stdout)

    def test_a_rootlesskit_with_detach_netns_gets_no_drop_in(self):
        self._docker_io_layout()
        r, calls = self._run(STUB_ROOTLESSKIT_V2=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user",
                                                     "docker.service.d")))
        self.assertNotIn("predates", r.stdout)

    def test_the_distros_copy_comes_before_the_packages(self):
        self._docker_io_layout()
        self._packaged_layout()
        r, calls = self._run()
        tool = [c for c in calls if c.startswith("setuptool ")]
        self.assertEqual(len(tool), 1, calls)
        self.assertTrue(tool[0].startswith(f"setuptool PATH={self.contrib}:"), tool[0])

    def test_a_link_at_the_logs_name_is_not_followed(self):
        self._docker_io_layout()
        data_dir = os.path.join(self.home, ".onionpress")
        os.mkdir(data_dir, 0o700)
        target = os.path.join(self.tmp, "somebody-elses-file")
        open(target, "w").close()
        os.symlink(target, os.path.join(data_dir, "docker-rootless-setup.log"))
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Docker mode: rootless (no logout required).", r.stdout,
                      "a bad log name costs the log, not the setup")
        with open(target) as f:
            self.assertEqual(f.read(), "", "nothing was written through the link")

    def test_the_first_install_needs_no_session_bus(self):
        self._docker_io_layout()
        r, calls = self._run()
        self.assertFalse([c for c in calls if "--machine=" in c], calls)
        self.assertIn("systemctl --user start dbus.socket", calls)
        reload_ = calls.index("systemctl --user daemon-reload")
        enable = calls.index(self._enabled_onionpress(calls)[0])
        self.assertLess(reload_, enable)
        self.assertTrue(self._enabled_onionpress(calls)[0].endswith(
            "onionpress.service onionpress-heartbeat.service onionpress-watcher.timer"))

    def test_docker_ces_tool_on_path_is_used(self):
        self._docker_ce_layout()
        r, calls = self._run(path_extra=(self.ce_bin,))
        self.assertEqual(r.returncode, 0, r.stderr)
        tool = [c for c in calls if c.startswith("setuptool ")]
        self.assertEqual(len(tool), 1, calls)
        self.assertTrue(tool[0].startswith(f"setuptool PATH={self.ce_bin}:"), tool[0])
        self.assertIn("Docker mode: rootless (no logout required).", r.stdout)

    def test_a_running_rootless_daemon_is_kept(self):
        self._docker_io_layout()
        open(self.log + ".rootless-up", "w").close()
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if c.startswith("setuptool ")], calls)
        self.assertNotIn("systemctl disable --now docker.service docker.socket", calls)
        self.assertIn("Docker mode: rootless (no logout required).", r.stdout)

    def test_failed_rootless_without_a_session_restarts_the_users_manager(self):
        self._docker_io_layout()
        r, calls = self._run(STUB_ROOTLESS_FAILS=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Rootless Docker did not start", r.stdout)
        disable = calls.index("systemctl disable --now docker.service docker.socket")
        reenable = calls.index("systemctl enable --now docker.socket docker.service")
        self.assertLess(disable, reenable, "the system daemon comes back for the group path")
        usermod = calls.index("usermod -aG docker opuser")
        restart = calls.index("systemctl restart user@1000.service")
        enable = calls.index(self._enabled_onionpress(calls)[0])
        self.assertLess(reenable, usermod)
        self.assertLess(usermod, restart)
        self.assertLess(restart, enable, "the new manager, with the group, starts the site")
        self.assertIn("Docker mode: system daemon (docker group).", r.stdout)
        self.assertNotIn("One manual step needed", r.stdout)
        self.assertEqual(len([c for c in calls if c.startswith("sleep")]), 29,
                         "the rootless daemon gets 30 tries a second apart")

    def test_failed_rootless_with_a_session_leaves_the_manager_alone(self):
        self._docker_io_layout()
        r, calls = self._run(STUB_ROOTLESS_FAILS=1, STUB_SESSIONS="3 7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usermod -aG docker opuser", calls)
        self.assertFalse([c for c in calls if "user@1000.service" in c],
                         "a restart would end the user's session's processes")
        self.assertFalse(self._enabled_onionpress(calls), calls)
        self.assertIn("One manual step needed to finish", r.stdout)

    def test_no_setup_tool_goes_to_the_group_path_with_docker_left_on_and_says_why(self):
        r, calls = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("systemctl disable --now docker.service docker.socket", calls)
        self.assertIn("usermod -aG docker opuser", calls)
        self.assertIn("systemctl restart user@1000.service", calls)
        self.assertIn("Docker mode: system daemon (docker group).", r.stdout)
        why = ("Rootless Docker was not tried: no dockerd-rootless-setuptool.sh on PATH, "
               f"in {self.contrib} or in {self.packaged}")
        self.assertIn(why, r.stdout)
        self.assertIn(why, self._rootless_log_text(), "the reason is kept for the evidence")

    def test_a_busy_system_daemon_is_left_alone_and_said_so(self):
        self._docker_io_layout()
        r, calls = self._run(STUB_DOCKER_BUSY=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if c.startswith("setuptool ")], calls)
        self.assertNotIn("systemctl disable --now docker.service docker.socket", calls)
        why = "Rootless Docker was not tried: the system Docker daemon is running other containers"
        self.assertIn(why, r.stdout)
        self.assertIn(why, self._rootless_log_text())
        self.assertIn("Docker mode: system daemon (docker group).", r.stdout)


if __name__ == "__main__":
    unittest.main()
