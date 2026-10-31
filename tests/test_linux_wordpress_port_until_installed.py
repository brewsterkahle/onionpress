#!/usr/bin/env python3
"""On Linux, WordPress's published port stays on 127.0.0.1 and the
heartbeat client is held until WordPress is installed (issue #464).

The Linux launcher binds WordPress's port to every interface by default
(ONIONPRESS_BIND_HOST=0.0.0.0), the .deb starts the units at once, and a
headless install may wait days for `onionpress setup`: until then
http://<host>:8080/wp-admin/install.php took the first LAN visitor's choice
of admin account, and the heartbeat client registered the address with
the OnionHeaven hub.

Now start_containers brings WordPress up bound to 127.0.0.1 unless a
start has seen it installed (the `.wordpress-installed` marker), then,
once `wp core is-installed` has answered, records the verdict and moves
the port where the config says (bind_wordpress_port_for_install_state:
one recreate of the wordpress container, once per install) or holds the
heartbeat client's user unit (a runtime mask, gone at reboot). Every
setup path ends in after_wordpress_installed: the launcher's
provision-post-install subcommand (what install_fresh_wordpress runs),
the `setup` case, and start_containers for an installed site. The
functions are extracted from the launcher and run under bash against
stub docker and systemctl binaries.
"""

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER = "linux/onionpress"
COMPOSE = "app/Resources/docker/docker-compose.yml"

FUNCTIONS = (
    "wordpress_bind_host_for_start",
    "wordpress_port_bind_host",
    "bind_wordpress_port_for_install_state",
    "heartbeat_client_held",
    "hold_heartbeat_client_until_installed",
    "release_heartbeat_client_hold",
    "after_wordpress_installed",
    "start_heartbeat_client",
)


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{\n") + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _case(text, name):
    start = text.index(f"\n        {name})\n")
    end = text.index("\n            ;;\n", start)
    return text[start:end]


DOCKER_STUB = r"""#!/bin/bash
# docker port onionpress-wordpress 80/tcp: the mapping in $STATE/port
# (empty file: no container). docker compose up -d wordpress: records the
# bind host it was given and makes it the mapping.
echo "docker $* [ONIONPRESS_BIND_HOST=${ONIONPRESS_BIND_HOST-unset}]" >> "$CALLS"
case "$1 $2" in
    "port onionpress-wordpress")
        [ -s "$STATE/port" ] || exit 1
        cat "$STATE/port" ;;
    "compose up")
        printf '%s:8080\n' "$ONIONPRESS_BIND_HOST" > "$STATE/port" ;;
esac
exit 0
"""

SYSTEMCTL_STUB = r"""#!/bin/bash
# systemctl --user <verb> <unit>, stateful: $STATE/enabled holds what
# is-enabled prints (enabled | masked-runtime), $STATE/active whether
# is-active succeeds.
echo "systemctl $*" >> "$CALLS"
[ "$1" = "--user" ] || { echo "not --user" >&2; exit 2; }
verb="$2"
case "$verb" in
    is-enabled) cat "$STATE/enabled"; [ "$(cat "$STATE/enabled")" = enabled ] ;;
    is-active) [ "$(cat "$STATE/active")" = active ] ;;
    mask) echo masked-runtime > "$STATE/enabled" ;;
    unmask) echo enabled > "$STATE/enabled" ;;
    start) echo active > "$STATE/active" ;;
    stop) echo inactive > "$STATE/active" ;;
    *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestUnderBash(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-port-until-installed-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name, body in (("docker", DOCKER_STUB), ("systemctl", SYSTEMCTL_STUB)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.state = os.path.join(self.tmp, "state")
        os.mkdir(self.state)
        self.data_dir = os.path.join(self.tmp, "data")
        os.mkdir(self.data_dir)
        self.calls = os.path.join(self.tmp, "calls")
        self.log = os.path.join(self.tmp, "onionpress.log")
        self.marker = os.path.join(self.data_dir, ".wordpress-installed")
        self._set(port="0.0.0.0:8080\n[::]:8080\n", enabled="enabled", active="active")

    def _set(self, **state):
        for name, value in state.items():
            with open(os.path.join(self.state, name), "w") as f:
                f.write(value)

    def _get(self, name):
        with open(os.path.join(self.state, name)) as f:
            return f.read().strip()

    def _run(self, body, bind_host="0.0.0.0"):
        text = _read(LAUNCHER)
        script = (
            f"DATA_DIR={shlex.quote(self.data_dir)}\n"
            f"LOG_FILE={shlex.quote(self.log)}\n"
            f"ONIONPRESS_BIND_HOST={bind_host}\n"
            "ONIONPRESS_WP_PORT=8080\n"
            'WP_INSTALLED_MARKER=".wordpress-installed"\n'
            "HEARTBEAT_UNIT=onionpress-heartbeat.service\n"
            'log() { echo "$1" >> "$LOG_FILE"; }\n'
            + "".join(_bash_function(text, f) for f in FUNCTIONS)
            + body
        )
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                   CALLS=self.calls, STATE=self.state)
        r = subprocess.run(["bash", "-c", script], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def _logged(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as f:
            return f.read()

    # --- the start's bind host ---

    def test_a_start_binds_loopback_until_a_start_has_seen_wordpress_installed(self):
        self.assertEqual(self._run("wordpress_bind_host_for_start\n"), "127.0.0.1\n")
        open(self.marker, "w").close()
        self.assertEqual(self._run("wordpress_bind_host_for_start\n"), "0.0.0.0\n")
        self.assertEqual(self._run("wordpress_bind_host_for_start\n", "192.168.1.5"),
                         "192.168.1.5\n")

    def test_start_containers_composes_with_it(self):
        body = _bash_function(_read(LAUNCHER), "start_containers")
        # The core `compose up` is start-containers' (#442.8), handed the
        # address in its environment (tests/test_start_core.py).
        self.assertIn('ONIONPRESS_BIND_HOST="$(wordpress_bind_host_for_start)" \\\n'
                      '        op_py --data-dir "$DATA_DIR" start-containers --until-core', body)
        # Once the verdict is in: installed sites end as every setup does,
        # the rest stay on loopback with the heartbeat client held.
        self.assertRegex(body, r'if \$wp_installed; then\n[^\n]*\n\s*after_wordpress_installed\n')
        self.assertRegex(body, r'\n\s*bind_wordpress_port_for_install_state false\n'
                               r'\s*hold_heartbeat_client_until_installed\n')
        # ...before Tor starts (in start-post-up, #442.7).
        self.assertLess(body.index("hold_heartbeat_client_until_installed"),
                        body.index('start_post_up "$wp_installed"'))

    def test_only_wordpress_s_port_follows_the_variable(self):
        """The per-command override reaches only the service it is for."""
        compose = _read(COMPOSE)
        self.assertEqual(compose.count("${ONIONPRESS_BIND_HOST"), 1)
        self.assertIn('"${ONIONPRESS_BIND_HOST:-127.0.0.1}:${ONIONPRESS_WP_PORT:-8080}:80"', compose)

    # --- moving the port once the verdict is in ---

    def test_not_installed_moves_a_wide_port_to_loopback(self):
        open(self.marker, "w").close()
        self._run("bind_wordpress_port_for_install_state false\n")
        self.assertFalse(os.path.exists(self.marker))
        self.assertIn("docker compose up -d wordpress [ONIONPRESS_BIND_HOST=127.0.0.1]", self._calls())
        self.assertEqual(self._get("port"), "127.0.0.1:8080")
        self.assertIn("not installed: keeping its port 8080 on 127.0.0.1 (was 0.0.0.0)", self._logged())

    def test_not_installed_on_loopback_already_changes_nothing(self):
        self._set(port="127.0.0.1:8080\n")
        self._run("bind_wordpress_port_for_install_state false\n")
        self.assertFalse(any("compose up" in c for c in self._calls()))

    def test_installed_moves_the_port_where_the_config_says(self):
        self._set(port="127.0.0.1:8080\n")
        self._run("bind_wordpress_port_for_install_state true\n")
        self.assertTrue(os.path.exists(self.marker))
        self.assertIn("docker compose up -d wordpress [ONIONPRESS_BIND_HOST=0.0.0.0]", self._calls())
        self.assertEqual(self._get("port"), "0.0.0.0:8080")
        self.assertIn("installed: moving its port 8080 from 127.0.0.1 to 0.0.0.0", self._logged())

    def test_installed_with_a_loopback_config_stays(self):
        self._set(port="127.0.0.1:8080\n")
        self._run("bind_wordpress_port_for_install_state true\n", bind_host="127.0.0.1")
        self.assertTrue(os.path.exists(self.marker))
        self.assertFalse(any("compose up" in c for c in self._calls()))

    def test_installed_where_the_config_says_changes_nothing(self):
        self._run("bind_wordpress_port_for_install_state true\n")
        self.assertTrue(os.path.exists(self.marker))
        self.assertFalse(any("compose up" in c for c in self._calls()))

    def test_no_container_records_the_verdict_only(self):
        self._set(port="")
        self._run("bind_wordpress_port_for_install_state true\n")
        self.assertTrue(os.path.exists(self.marker))
        self.assertFalse(any("compose up" in c for c in self._calls()))
        self._run("bind_wordpress_port_for_install_state false\n")
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(any("compose up" in c for c in self._calls()))

    # --- the heartbeat client ---

    def test_held_until_installed(self):
        self._run("hold_heartbeat_client_until_installed\n")
        calls = self._calls()
        self.assertIn("systemctl --user mask --runtime onionpress-heartbeat.service", calls)
        self.assertIn("systemctl --user stop onionpress-heartbeat.service", calls)
        self.assertEqual(self._get("enabled"), "masked-runtime")
        self.assertEqual(self._get("active"), "inactive")
        self.assertIn("held until WordPress is installed", self._logged())
        # Held again on the next start: no second mask, nothing to stop.
        os.unlink(self.calls)
        self._run("hold_heartbeat_client_until_installed\n")
        self.assertFalse(any("mask" in c or "stop" in c for c in self._calls()))

    def test_start_heartbeat_client_respects_the_hold(self):
        self._set(enabled="masked-runtime", active="inactive")
        self._run("start_heartbeat_client\n")
        self.assertFalse(any(" start " in c for c in self._calls()))
        self.assertIn("stays held", self._logged())
        self._set(enabled="enabled")
        self._run("start_heartbeat_client\n")
        self.assertIn("systemctl --user start onionpress-heartbeat", self._calls())

    def test_released_once_installed(self):
        self._set(enabled="masked-runtime", active="inactive")
        self._run("release_heartbeat_client_hold\n")
        calls = self._calls()
        self.assertIn("systemctl --user unmask --runtime onionpress-heartbeat.service", calls)
        self.assertIn("systemctl --user start onionpress-heartbeat", calls)
        self.assertLess(calls.index("systemctl --user unmask --runtime onionpress-heartbeat.service"),
                        calls.index("systemctl --user start onionpress-heartbeat"))
        self.assertEqual(self._get("active"), "active")

    def test_release_without_a_hold_does_nothing(self):
        self._run("release_heartbeat_client_hold\n")
        self.assertFalse(any("unmask" in c or " start " in c for c in self._calls()))

    def test_after_wordpress_installed_does_both(self):
        self._set(port="127.0.0.1:8080\n", enabled="masked-runtime", active="inactive")
        self._run("after_wordpress_installed\n")
        calls = self._calls()
        self.assertIn("docker compose up -d wordpress [ONIONPRESS_BIND_HOST=0.0.0.0]", calls)
        self.assertIn("systemctl --user start onionpress-heartbeat", calls)
        self.assertTrue(os.path.exists(self.marker))

    def test_a_hostile_port_answer_runs_nothing(self):
        pwned = os.path.join(self.tmp, "pwned")
        self._set(port="a[$(>" + pwned + ")]:8080\n")
        self._run("bind_wordpress_port_for_install_state true\n")
        self.assertFalse(os.path.exists(pwned))


class TestEverySetupPathEndsThere(unittest.TestCase):

    def test_provision_post_install_subcommand(self):
        case = _case(_read(LAUNCHER), "provision-post-install")
        self.assertRegex(case, r"op_py provision-post-install \\\n[^\n]*\n[^\n]*\n(\s*#[^\n]*\n)*"
                               r"\s*if wp_is_installed; then\n\s*after_wordpress_installed\n"
                               r"\s*start_cloudflare_tunnel true\n\s*fi$")

    def test_the_setup_case(self):
        # `onionpress setup` installs through install_fresh_wordpress
        # (#442.10), which runs this launcher's provision-post-install case:
        # that one ends with after_wordpress_installed.
        case = _case(_read(LAUNCHER), "setup")
        self.assertIn('run_setup "$@" || setup_rc=$?', case)
        provision = _case(_read(LAUNCHER), "provision-post-install")
        self.assertIn("after_wordpress_installed", provision)
        # --from-backup restarts the containers: start_containers decides.
        self.assertIn('stop_containers || log "WARNING: setup: the stop failed; starting the '
                      'containers anyway"\n                    start_containers', case)

    def test_the_marker_is_removed_with_its_sibling(self):
        """A removal of ~/.onionpress state must not leave only this one."""
        text = _read(LAUNCHER)
        self.assertIn('WP_INSTALLED_MARKER=".wordpress-installed"', text)
        self.assertIn('rm -f "${DATA_DIR:?}/${WP_INSTALLED_MARKER:?}"', text)



class TestTorStartsLeaveWordPressAlone(unittest.TestCase):
    """tor depends on wordpress in docker-compose.yml: a plain `compose up
    tor` recreates WordPress under the environment's ONIONPRESS_BIND_HOST
    (the config's, exported by the launcher), so the start that bound the
    port to 127.0.0.1 until setup published the installer to the network
    as soon as Tor came up (found by the live Linux checks of #499; the
    bash start did the same). Every Python start of Tor says --no-deps."""

    def test_every_tor_up_says_no_deps(self):
        import re
        src = os.path.join(PROJECT_ROOT, "src", "onionpress")
        found = []
        for name in sorted(os.listdir(src)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(src, name), encoding="utf-8") as f:
                text = f.read()
            for m in re.finditer(r'\["up", "-d"(?:, "[^"]+")*, "tor"', text):
                found.append((name, m.group(0)))
        self.assertTrue(found, "no Tor start found: the pattern is stale")
        self.assertEqual([f for f in found if '"--no-deps"' not in f[1]], [])
        with open(os.path.join(PROJECT_ROOT, "linux", "onionpress"), encoding="utf-8") as f:
            self.assertNotRegex(f.read(), r"(?m)^[^#]*compose[^\n#]*\bup -d tor\b")

if __name__ == "__main__":
    unittest.main()
