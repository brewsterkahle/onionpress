#!/usr/bin/env python3
"""Which host addresses the stack's published ports listen on.

Two ports are published: WordPress (ONIONPRESS_WP_PORT) and Tor's SOCKS port
(ONIONPRESS_SOCKS_PORT). WordPress's reach is a setting: the Linux launcher
binds it to every interface by default, so a headless Pi can be set up from
another device on the LAN, and ONIONPRESS_BIND_HOST=127.0.0.1 in
~/.onionpress/config keeps it on the machine. Tor's SOCKS port is for this
machine only, on every platform, and is not a setting.

Until this was split, both ports followed ONIONPRESS_BIND_HOST, the config key
the launcher's comment documented was never read (only the environment
worked), and `onionpress update` carried a sed that rewrote the compose
bindings to 0.0.0.0. The launcher blocks below are extracted and run, not
re-typed, against stub `docker`/`hostname` binaries — nothing here talks to a
Docker daemon or reads the real ~/.onionpress.
"""

import glob
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_config_one_reader import config_value_function  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# What the .deb, install.sh and the DMG ship as the stack definition.
SHIPPED_COMPOSE = sorted(
    os.path.relpath(p, PROJECT_ROOT)
    for p in glob.glob(os.path.join(PROJECT_ROOT, "app/Resources/docker/*.yml")))

LINUX_LAUNCHER = "linux/onionpress"

# Every place that writes a fresh Linux ~/.onionpress/config.
LINUX_CONFIG_TEMPLATES = ("linux/onionpress", "linux/install.sh", "build/build-linux.sh")

# The rewrites older launchers' `onionpress update` (and, earlier, install.sh
# and the .deb postinst) applied to docker-compose.yml.
LEGACY_REWRITES = (
    re.compile(r"127\.0\.0\.1:\$\{ONIONPRESS_WP_PORT"),
    re.compile(r"127\.0\.0\.1:\$\{ONIONPRESS_SOCKS_PORT"),
)

# Run extracted shell with no Docker daemon reachable, whatever PATH holds.
NO_DOCKER = "unix:///var/empty/no-docker-for-tests.sock"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _code(text):
    """The text with comment lines dropped."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _services(compose_text):
    """{service name: its block, comments dropped}. No PyYAML in CI, so this
    leans on the file's two-space layout, like tests/test_image_pins.py."""
    body = re.split(r"^services:\n", compose_text, maxsplit=1, flags=re.M)[-1]
    body = re.split(r"^\S", body, maxsplit=1, flags=re.M)[0]
    out = {}
    for m in re.finditer(r"^  ([A-Za-z0-9_-]+):\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                         body, re.M | re.S):
        out[m.group(1)] = _code(m.group(2))
    return out


def _ports(service_block):
    """The service's `ports:` entries: a str for the short syntax, a dict for
    the long one."""
    m = re.search(r"^    ports:\n((?:      .*\n?)+)", service_block + "\n", re.M)
    if not m:
        return []
    entries, current = [], None
    for line in m.group(1).splitlines():
        item = re.match(r"^      - (.*)$", line)
        if item:
            current = None
            value = item.group(1).strip()
            kv = re.match(r"^([a-z_]+):\s*(.*)$", value)
            if kv:
                current = {kv.group(1): kv.group(2).strip().strip('"')}
                entries.append(current)
            else:
                entries.append(value.strip('"'))
        else:
            kv = re.match(r"^        ([a-z_]+):\s*(.*)$", line)
            if kv and current is not None:
                current[kv.group(1)] = kv.group(2).strip().strip('"')
    return entries


def _extract(pattern, text, what):
    m = re.search(pattern, text, re.S | re.M)
    if m is None:
        raise AssertionError(f"Could not find {what} in {LINUX_LAUNCHER} — "
                             "restructured? Update this test.")
    return m.group(0)


def _stub(bin_dir, name, body):
    path = os.path.join(bin_dir, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _run_bash(script, bin_dir=None, env=None):
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if bin_dir:
        path = bin_dir + os.pathsep + path
    full_env = {"PATH": path, "DOCKER_HOST": NO_DOCKER}
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=full_env, timeout=30)


class TestComposeBindings(unittest.TestCase):

    def test_socks_port_is_loopback_only(self):
        tor = _services(_read("app/Resources/docker/docker-compose.yml"))["tor"]
        ports = _ports(tor)
        self.assertEqual(1, len(ports), f"tor publishes {ports!r}")
        socks = ports[0]
        self.assertIsInstance(
            socks, dict,
            "Keep Tor's SOCKS mapping in long syntax: a short "
            "\"127.0.0.1:${ONIONPRESS_SOCKS_PORT...\" string is exactly what "
            "older launchers' `onionpress update` sed rewrites to 0.0.0.0.")
        self.assertEqual("127.0.0.1", socks.get("host_ip"))
        self.assertEqual("9050", socks.get("target"))
        self.assertIn("ONIONPRESS_SOCKS_PORT", socks.get("published", ""))
        self.assertNotIn(
            "ONIONPRESS_BIND_HOST", tor,
            "ONIONPRESS_BIND_HOST decides WordPress's reach only; Tor's SOCKS "
            "port must not follow it.")

    def test_wordpress_follows_the_bind_host_with_a_loopback_default(self):
        wp = _services(_read("app/Resources/docker/docker-compose.yml"))["wordpress"]
        self.assertEqual(
            ["${ONIONPRESS_BIND_HOST:-127.0.0.1}:${ONIONPRESS_WP_PORT:-8080}:80"],
            _ports(wp),
            "macOS never sets ONIONPRESS_BIND_HOST and relies on the "
            "127.0.0.1 default here.")

    def test_every_published_port_names_a_host(self):
        """A mapping without a host address listens on every interface. Only
        WordPress may be widened, and only through ONIONPRESS_BIND_HOST."""
        for rel in SHIPPED_COMPOSE:
            for name, block in _services(_read(rel)).items():
                for entry in _ports(block):
                    with self.subTest(file=rel, service=name, port=entry):
                        if isinstance(entry, dict):
                            self.assertEqual("127.0.0.1", entry.get("host_ip"))
                        elif name == "wordpress":
                            self.assertTrue(entry.startswith("${ONIONPRESS_BIND_HOST:-127.0.0.1}:"))
                        else:
                            self.assertTrue(entry.startswith("127.0.0.1:"))

    def test_nothing_is_bound_to_all_interfaces_literally(self):
        for rel in SHIPPED_COMPOSE:
            with self.subTest(file=rel):
                self.assertNotIn("0.0.0.0", _code(_read(rel)))

    def test_every_compose_file_in_the_repo_is_a_shipped_one(self):
        """linux/docker-compose.pi.yml was an unshipped override that bound
        WordPress to 0.0.0.0 whatever ONIONPRESS_BIND_HOST said, and none
        of the checks above read it. A compose file outside the shipped set
        would be just as unchecked."""
        found = set()
        for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
            # Hidden dirs too: .claude/worktrees/ holds other checkouts.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d not in ("node_modules", "OnionPress.app", "venv",
                                         "py2app_build", "py2app_dist")]
            for name in filenames:
                if "compose" in name and name.endswith((".yml", ".yaml")):
                    found.add(os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT))
        self.assertEqual(sorted(found), SHIPPED_COMPOSE)

    def test_legacy_rewrites_find_nothing_to_widen(self):
        for rel in SHIPPED_COMPOSE:
            text = _code(_read(rel))
            for pattern in LEGACY_REWRITES:
                with self.subTest(file=rel, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))


class TestNoComposeRewrites(unittest.TestCase):
    """Bindings are decided at compose-up time from the environment. A sed on
    the installed compose file is how the SOCKS port got widened before, so
    none may come back — in the launcher, the installer or the postinst."""

    def test_no_sed_touches_a_compose_file(self):
        for rel in (LINUX_LAUNCHER, "linux/install.sh", "build/build-linux.sh"):
            for i, line in enumerate(_read(rel).splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                with self.subTest(file=rel, line=i):
                    self.assertFalse(
                        re.search(r"\bsed\b", line) and "docker-compose" in line,
                        f"{rel}:{i} rewrites a compose file: {line.strip()}")


class TestBindHostResolution(unittest.TestCase):
    """The launcher's actual block, run under `set -e` against a temp
    DATA_DIR."""

    BLOCK = (r'^if \[ -z "\$\{ONIONPRESS_BIND_HOST:-\}" \] && \[ -f "\$DATA_DIR/config" \]; then\n'
             r'.*?^export ONIONPRESS_BIND_HOST$')

    def _resolve(self, config=None, env_value=None):
        block = _extract(self.BLOCK, _read(LINUX_LAUNCHER), "the bind-host block")
        with tempfile.TemporaryDirectory() as data_dir:
            if config is not None:
                with open(os.path.join(data_dir, "config"), "w") as f:
                    f.write(config)
            script = (
                "set -e\n"
                f"DATA_DIR={data_dir!r}\n"
                f"LOG_FILE={os.path.join(data_dir, 'onionpress.log')!r}\n"
                + config_value_function() + block + '\nprintf "%s" "$ONIONPRESS_BIND_HOST"\n'
            )
            env = {} if env_value is None else {"ONIONPRESS_BIND_HOST": env_value}
            result = _run_bash(script, env=env)
            leftovers = [n for n in os.listdir(data_dir) if n.startswith("PWNED")]
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], leftovers, "a config value was executed")
        return result.stdout, result.stderr

    def test_default_is_every_interface(self):
        self.assertEqual("0.0.0.0", self._resolve()[0])

    def test_config_without_the_key_survives_set_e(self):
        self.assertEqual("0.0.0.0", self._resolve("ADDRESS_PREFIX=op2\n")[0])

    def test_reads_the_config_file(self):
        self.assertEqual("127.0.0.1", self._resolve(
            "ADDRESS_PREFIX=op2\nONIONPRESS_BIND_HOST=127.0.0.1\n")[0])

    def test_commented_template_line_is_not_a_setting(self):
        self.assertEqual("0.0.0.0", self._resolve("#ONIONPRESS_BIND_HOST=127.0.0.1\n")[0])

    def test_tolerates_quotes_and_whitespace(self):
        self.assertEqual("127.0.0.1", self._resolve('ONIONPRESS_BIND_HOST="127.0.0.1" \n')[0])

    def test_localhost_means_loopback(self):
        self.assertEqual("127.0.0.1", self._resolve("ONIONPRESS_BIND_HOST=localhost\n")[0])

    def test_environment_wins_over_the_config(self):
        self.assertEqual("0.0.0.0", self._resolve(
            "ONIONPRESS_BIND_HOST=127.0.0.1\n", env_value="0.0.0.0")[0])

    def test_unusable_value_falls_back_to_loopback(self):
        for bad in ("lan", "::", "0.0.0.0:8080", "$(touch PWNED)", "`touch PWNED`"):
            with self.subTest(value=bad):
                value, stderr = self._resolve(f"ONIONPRESS_BIND_HOST={bad}\n")
                self.assertEqual("127.0.0.1", value)
                self.assertIn("WARNING", stderr)

    def test_unusable_environment_value_falls_back_too(self):
        value, stderr = self._resolve(env_value="all")
        self.assertEqual("127.0.0.1", value)
        self.assertIn("WARNING", stderr)


class TestPrintedWordPressUrls(unittest.TestCase):
    """With WordPress on loopback, a LAN URL would not open."""

    FUNC = r"^wp_access_host\(\) \{\n.*?^\}$"

    def _host(self, bind_host, hostname_output):
        func = _extract(self.FUNC, _read(LINUX_LAUNCHER), "wp_access_host()")
        with tempfile.TemporaryDirectory() as bin_dir:
            _stub(bin_dir, "hostname", f"printf '%s' {hostname_output!r}")
            result = _run_bash(func + "\nwp_access_host\n", bin_dir=bin_dir,
                               env={"ONIONPRESS_BIND_HOST": bind_host})
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def test_every_interface_prints_the_lan_address(self):
        self.assertEqual("192.168.1.20", self._host("0.0.0.0", "192.168.1.20 fd00::2 \n"))

    def test_every_interface_without_an_address_prints_localhost(self):
        self.assertEqual("localhost", self._host("0.0.0.0", ""))

    def test_loopback_prints_localhost(self):
        self.assertEqual("localhost", self._host("127.0.0.1", "192.168.1.20\n"))

    def test_a_specific_address_prints_itself(self):
        self.assertEqual("10.0.0.5", self._host("10.0.0.5", "192.168.1.20\n"))

    def test_launcher_urls_use_it(self):
        offenders = [l.strip() for l in _read(LINUX_LAUNCHER).splitlines()
                     if "hostname -I" in l and "ONIONPRESS_WP_PORT" in l]
        self.assertEqual([], offenders,
                         "Build WordPress URLs with $(wp_access_host).")


class TestSocksExposureWarning(unittest.TestCase):
    """Re-running install.sh leaves an older docker-compose.yml in place, and
    that one still publishes SOCKS on ONIONPRESS_BIND_HOST. The launcher asks
    Docker for the real mapping after Tor starts and warns."""

    FUNC = r"^warn_if_socks_port_exposed\(\) \{\n.*?^\}$"

    def _warn(self, docker_body):
        func = _extract(self.FUNC, _read(LINUX_LAUNCHER), "warn_if_socks_port_exposed()")
        with tempfile.TemporaryDirectory() as bin_dir:
            _stub(bin_dir, "docker", docker_body)
            script = ("set -e\n"
                      'log() { echo "$1"; }\n'
                      "DOCKER_DIR=/opt/onionpress/docker\n"
                      + func + "\nwarn_if_socks_port_exposed\n")
            result = _run_bash(script, bin_dir=bin_dir)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_loopback_mapping_is_quiet(self):
        self.assertEqual("", self._warn("echo 127.0.0.1:19050"))

    def test_wider_mapping_warns(self):
        out = self._warn(r"printf '0.0.0.0:9050\n[::]:9050\n'")
        self.assertIn("WARNING", out)
        self.assertIn("0.0.0.0:9050", out)

    def test_no_container_is_quiet(self):
        self.assertEqual("", self._warn("exit 1"))

    def test_start_containers_runs_it_after_tor_starts(self):
        text = _read(LINUX_LAUNCHER)
        self.assertRegex(
            text,
            r"docker compose up -d tor onionheaven [^\n]*\n\s*warn_if_socks_port_exposed\n")


class TestDocumentedSetting(unittest.TestCase):

    def test_linux_config_templates_document_it_without_pinning_it(self):
        """Commented out, so the launcher's default still decides for anyone
        who never touches it."""
        for rel in LINUX_CONFIG_TEMPLATES:
            text = _read(rel)
            with self.subTest(file=rel):
                self.assertIn("#ONIONPRESS_BIND_HOST=127.0.0.1\n", text)
                self.assertIsNone(re.search(r"^ONIONPRESS_BIND_HOST=", text, re.M))

    def test_mac_never_sets_it(self):
        """Mac relies on the compose file's 127.0.0.1 default for WordPress."""
        paths = [os.path.join("app/MacOS", n) for n in os.listdir(os.path.join(PROJECT_ROOT, "app/MacOS"))]
        paths += [os.path.relpath(p, PROJECT_ROOT) for p in
                  glob.glob(os.path.join(PROJECT_ROOT, "src", "**", "*.py"), recursive=True)]
        for rel in paths:
            with self.subTest(file=rel):
                self.assertNotIn("ONIONPRESS_BIND_HOST", _read(rel))


if __name__ == "__main__":
    unittest.main()
