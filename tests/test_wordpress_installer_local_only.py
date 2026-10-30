#!/usr/bin/env python3
"""WordPress's installer answers the owner's own requests only (issue #464).

Until WordPress is installed, wp-admin/install.php takes the first
visitor's choice of admin account and setup-config.php writes
wp-config.php. Tor serves the site as soon as the onion service is up,
days before a headless install's owner runs `onionpress setup`. The
WordPress image's onionpress-install-page.conf denies both files to every
source but loopback and this container's default gateway, where the
published port's requests arrive from (docker-proxy on Linux, Lima's
forward on a Mac: the same rule as auto-login's "local" scope). Tor
visitors arrive from the tor container, cloudflared's from its own
address. The entrypoint exports ONIONPRESS_WP_GATEWAY from the routing
table before Apache starts (the plugin's parse of /proc/net/route, in
bash); with none the conf admits loopback only. Ships in the WordPress
image: needs an image publish.
"""

import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WP_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "wordpress")
CONF = os.path.join(WP_DIR, "onionpress-install-page.conf")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")
ENTRYPOINT = os.path.join(WP_DIR, "onionpress-entrypoint.sh")

# /proc/net/route as the WordPress container sees it (the table
# tests/test_auto_login_off_argv.py runs the plugin's parser on).
ROUTE_TABLE = (
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)
GATEWAY = "172.18.0.1"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestTheConf(unittest.TestCase):

    def test_the_two_files_and_no_others(self):
        conf = _read(CONF)
        m = re.search(r'<Files ~ "([^"]+)">', conf)
        self.assertIsNotNone(m, "no <Files ~> section")
        pattern = re.compile(m.group(1))
        for name in ("install.php", "setup-config.php"):
            self.assertRegex(name, pattern)
        for name in ("index.php", "install-helper.php", "install.php.txt", "xinstall.php",
                     "admin.php", "wp-login.php"):
            self.assertNotRegex(name, pattern)

    def test_loopback_and_the_gateway_only(self):
        conf = _read(CONF)
        self.assertIn("Require ip 127.0.0.0/8 ::1", conf)
        self.assertIn("Require expr %{REMOTE_ADDR} == osenv('ONIONPRESS_WP_GATEWAY')", conf)
        self.assertNotIn("Require all granted", conf)
        self.assertNotIn("RequireAll", conf)

    def test_enabled_in_the_image(self):
        dockerfile = _read(DOCKERFILE)
        self.assertIn("COPY onionpress-install-page.conf /etc/apache2/conf-available/", dockerfile)
        enconf = re.search(r"^RUN a2enconf (.+)$", dockerfile, re.M)
        self.assertIsNotNone(enconf)
        self.assertIn("onionpress-install-page", enconf.group(1).split())
        # ...before the configtest that fails the build on a bad conf.
        self.assertLess(enconf.start(), dockerfile.index("RUN apache2ctl configtest"))


class TestTheEntrypoint(unittest.TestCase):

    def test_exports_the_gateway_before_apache(self):
        text = _read(ENTRYPOINT)
        self.assertIn("gateway_from_route() {", text)
        export = text.index("export ONIONPRESS_WP_GATEWAY")
        self.assertLess(export, text.index('exec docker-entrypoint.sh "$@"'))
        self.assertIn('ONIONPRESS_WP_GATEWAY=$(gateway_from_route) || ONIONPRESS_WP_GATEWAY=""', text)

    def test_the_variable_name_is_the_confs(self):
        self.assertIn("osenv('ONIONPRESS_WP_GATEWAY')", _read(CONF))
        self.assertIn("export ONIONPRESS_WP_GATEWAY", _read(ENTRYPOINT))


def _gateway_function():
    text = _read(ENTRYPOINT)
    start = text.index("gateway_from_route() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestGatewayParseUnderBash(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-gateway-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pwned = os.path.join(self.tmp, "pwned")

    def _run(self, table):
        path = os.path.join(self.tmp, "route")
        with open(path, "w") as f:
            f.write(table)
        script = _gateway_function() + f"gateway_from_route {shlex.quote(path)}\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           timeout=30, env=dict(os.environ, OP_PWNED=self.pwned))
        self.assertEqual(r.stderr, "")
        return r.returncode, r.stdout

    def test_the_default_gateway(self):
        self.assertEqual(self._run(ROUTE_TABLE), (0, GATEWAY + "\n"))
        # Another subnet: 192.168.65.1 is 0141A8C0 in the table.
        self.assertEqual(self._run(ROUTE_TABLE.replace("010012AC", "0141A8C0")),
                         (0, "192.168.65.1\n"))

    def test_no_default_route_is_none(self):
        lines = ROUTE_TABLE.splitlines(True)
        self.assertEqual(self._run(lines[0] + lines[2]), (1, ""))
        self.assertEqual(self._run(""), (1, ""))

    def test_a_default_without_the_gateway_flag_is_none(self):
        self.assertEqual(self._run(ROUTE_TABLE.replace("\t0003\t", "\t0001\t", 1)), (1, ""))

    def test_a_missing_table_is_none(self):
        script = _gateway_function() + f"gateway_from_route {shlex.quote(self.tmp)}/absent\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_hostile_fields_run_nothing(self):
        """Each field is checked before the arithmetic sees it."""
        hostile = "a[$(>$OP_PWNED)]"
        for column in (2, 3):
            fields = ROUTE_TABLE.splitlines()[1].split("\t")
            fields[column] = hostile
            table = ROUTE_TABLE.splitlines(True)[0] + "\t".join(fields) + "\n"
            with self.subTest(column=column):
                self.assertEqual(self._run(table), (1, ""))
                self.assertFalse(os.path.exists(self.pwned))

    def test_it_agrees_with_the_plugin(self):
        """The same answer as onionpress_auto_login_gateway() for the same
        tables, where php is installed."""
        php = shutil.which("php")
        if not php:
            self.skipTest("php not installed")
        plugin = os.path.join(PROJECT_ROOT, "app", "Resources", "plugins",
                              "onionpress-auto-login.php")
        src = _read(plugin)
        start = src.index("function onionpress_auto_login_gateway()")
        end = src.index("\n}\n", start) + len("\n}\n")
        fn = src[start:end].replace("'/proc/net/route'", "getenv('ROUTE')")
        for table in (ROUTE_TABLE, ROUTE_TABLE.replace("010012AC", "0141A8C0"),
                      ROUTE_TABLE.replace("\t0003\t", "\t0001\t", 1)):
            path = os.path.join(self.tmp, "route-php")
            with open(path, "w") as f:
                f.write(table)
            r = subprocess.run([php, "-r", fn + " echo onionpress_auto_login_gateway();"],
                               capture_output=True, text=True, timeout=30,
                               env=dict(os.environ, ROUTE=path))
            self.assertEqual(r.stdout, self._run(table)[1].strip(), table)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


APACHE_MODULES = "/usr/lib/apache2/modules"


@unittest.skipUnless(shutil.which("apache2") and os.path.isdir(APACHE_MODULES),
                     "needs a Debian/Ubuntu apache2")
class TestUnderApache(unittest.TestCase):
    """The conf as the system's Apache 2.4 reads it: loopback passes, the
    gateway's address passes, and (the conf's Require ip line left out, so
    the loopback client stands for a Tor visitor) any other source is
    refused. Other files are untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-apache-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        docroot = os.path.join(self.tmp, "docroot", "wp-admin")
        os.makedirs(docroot)
        for name in ("install.php", "setup-config.php", "index.php", "install-helper.php"):
            with open(os.path.join(docroot, name), "w") as f:
                f.write(name)
        self.port = _free_port()
        self.conf = os.path.join(self.tmp, "httpd.conf")
        self.proc = None

    def _start(self, include, gateway):
        with open(self.conf, "w") as f:
            f.write(
                f'ServerRoot "{self.tmp}"\nServerName test.local\n'
                f"PidFile {self.tmp}/httpd.pid\nErrorLog {self.tmp}/error.log\n"
                f"Listen 127.0.0.1:{self.port}\n"
                f"LoadModule mpm_event_module {APACHE_MODULES}/mod_mpm_event.so\n"
                f"LoadModule authz_core_module {APACHE_MODULES}/mod_authz_core.so\n"
                f"LoadModule authz_host_module {APACHE_MODULES}/mod_authz_host.so\n"
                f"DocumentRoot {self.tmp}/docroot\n"
                f"<Directory {self.tmp}/docroot>\n    Require all granted\n</Directory>\n"
                f"Include {include}\n")
        env = dict(os.environ, ONIONPRESS_WP_GATEWAY=gateway)
        check = subprocess.run(["apache2", "-t", "-f", self.conf], env=env,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.proc = subprocess.Popen(["apache2", "-f", self.conf, "-DFOREGROUND"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail("apache2 did not listen")

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def _status(self, name):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/wp-admin/{name}",
                                        timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_loopback_always_passes(self):
        self._start(CONF, "10.9.9.9")
        for name in ("install.php", "setup-config.php", "index.php", "install-helper.php"):
            self.assertEqual(self._status(name), 200, name)

    def test_only_the_gateway_passes_otherwise(self):
        without_loopback = os.path.join(self.tmp, "only-gateway.conf")
        with open(without_loopback, "w") as f:
            f.write("".join(l for l in _read(CONF).splitlines(True) if "Require ip" not in l))
        self._start(without_loopback, "127.0.0.1")
        self.assertEqual(self._status("install.php"), 200)
        self.assertEqual(self._status("setup-config.php"), 200)
        self._stop()
        for gateway in ("10.9.9.9", ""):
            with self.subTest(gateway=gateway):
                self._start(without_loopback, gateway)
                self.assertEqual(self._status("install.php"), 403)
                self.assertEqual(self._status("setup-config.php"), 403)
                self.assertEqual(self._status("index.php"), 200)
                self.assertEqual(self._status("install-helper.php"), 200)
                self._stop()


if __name__ == "__main__":
    unittest.main()
