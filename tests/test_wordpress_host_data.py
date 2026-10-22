#!/usr/bin/env python3
"""Host data copied into the WordPress container is scrubbed, and the page
that shows it is the site owner's.

The host copies status.json, the end of onionpress.log (recent-logs.txt)
and the results of settings-page actions into the WordPress container's
/var/lib/onionpress/, where every PHP plugin and theme can read them. The
log went in raw (home directory, user name, LAN addresses, anything secret
that reached the log), Linux's status.json carried wp_admin_password_path
(a path under $HOME), and /onionpress-status and /onionpress-status-json
served all of it to anyone who could reach the site — every visitor of the
onion service.

Now: recent-logs.txt passes redact.scrub_for_wordpress on both platforms,
status.json keeps only redact.WORDPRESS_STATUS_KEYS on all three writers,
the Mac's action results are scrubbed, and both status endpoints answer only
the site owner (is_super_admin).

The Linux tests run the launcher's own functions in bash with `docker`
stubbed as a shell function, so no daemon is ever contacted.
"""

import io
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

from onionpress import cli, redact  # noqa: E402

TOKEN = "eyJhIjoiY2xvdWRmbGFyZS10dW5uZWwtc2VjcmV0In0"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _between(text, start_marker, end_marker):
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start + len(start_marker))]


def _bash_function(text, name):
    # Ends at a column-0 `}` followed by a blank line: write_status embeds
    # `python3 -c` dict literals that close at column 0 too.
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n\n", start)
    return text[start:end] + "\n}\n"


class TestScrubForWordPress(unittest.TestCase):
    HOME = "/Users/alice"

    def setUp(self):
        self.app_support = tempfile.mkdtemp(prefix="op-wp-scrub-")

    def tearDown(self):
        shutil.rmtree(self.app_support, ignore_errors=True)

    def _scrub(self, text, home=HOME):
        return redact.scrub_for_wordpress(text, self.app_support, home=home)

    def test_home_directory_and_other_user_names_go(self):
        out = self._scrub(
            "mount /Users/alice/OnionPress:w\n"
            "lima: /Users/alice/.onionpress/colima/_lima/default\n"
            "home=/Users/alice\n"
            "other /Users/alicesmith/x and /home/pi/.onionpress/config\n")
        self.assertNotIn("alice", out)
        self.assertNotIn("/home/pi", out)
        self.assertIn("mount ~/OnionPress:w", out)
        self.assertIn("lima: ~/.onionpress/colima/_lima/default", out)
        self.assertIn("home=~\n", out)
        self.assertIn("/Users/<user>/x", out)
        self.assertIn("/home/<user>/.onionpress/config", out)

    def test_credentials_go(self):
        out = self._scrub(
            f"  CLOUDFLARE_TUNNEL_TOKEN: '' → '{TOKEN}'\n"
            "MYSQL_PASSWORD=hunter2\n"
            f"cloudflared --token {TOKEN} started\n"
            "GET /wp-login.php?action=rp&key=s3cr3tK3y\n"
            "Cookie: wordpress_logged_in=xyz\n")
        for secret in (TOKEN, "hunter2", "s3cr3tK3y", "xyz"):
            self.assertNotIn(secret, out)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN: <redacted>", out)
        self.assertIn("MYSQL_PASSWORD=<redacted>", out)
        self.assertIn("key=<redacted>", out)

    def test_addresses_other_than_loopback_are_pseudonymized(self):
        out = self._scrub(
            "bind 192.168.1.23:8080; peer 203.0.113.9 via 2001:db8::5\n"
            "listen 127.0.0.1:8080 0.0.0.0:80 [::1]:9050 v2.5.2 at 10:27:47\n")
        for real in ("192.168.1.23", "203.0.113.9", "2001:db8::5"):
            self.assertNotIn(real, out)
        salt = redact.get_daily_salt(self.app_support)
        self.assertIn(redact.pseudonymize_ip("192.168.1.23", salt), out)
        for kept in ("127.0.0.1:8080", "0.0.0.0:80", "[::1]:9050", "v2.5.2", "10:27:47"):
            self.assertIn(kept, out)

    def test_plain_lines_are_unchanged(self):
        line = "[2026-10-12 10:27:47] System fully operational (op2abc.onion)\n"
        self.assertEqual(self._scrub(line), line)
        self.assertFalse(os.path.exists(os.path.join(self.app_support, redact.SALT_DIR)),
                         "the salt is read only when there is an address to hide")

    def test_json_strings_scrubbed_structure_kept(self):
        data = {"success": False, "error": "No such file: /Users/alice/x.zip",
                "http_code": 200, "nested": ["peer 203.0.113.9", None, True]}
        out = redact.scrub_json_for_wordpress(data, self.app_support, home=self.HOME)
        self.assertEqual(out["error"], "No such file: ~/x.zip")
        self.assertEqual((out["success"], out["http_code"]), (False, 200))
        self.assertNotIn("203.0.113.9", json.dumps(out))
        self.assertEqual(out["nested"][1:], [None, True])


class TestWordPressLogExcerpt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-wp-excerpt-")
        self.log = os.path.join(self.dir, "onionpress.log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _excerpt(self, content, **kw):
        with open(self.log, "wb") as f:
            f.write(content)
        return redact.wordpress_log_excerpt(self.log, self.dir, home="/Users/alice", **kw)

    def test_last_hundred_lines_scrubbed(self):
        lines = [f"[{i:05d}] /Users/alice/.onionpress line\n" for i in range(5000)]
        out = self._excerpt("".join(lines).encode())
        got = out.splitlines(keepends=True)
        self.assertEqual(len(got), redact.WORDPRESS_LOG_LINES)
        self.assertEqual(got[0], "[04900] ~/.onionpress line\n")
        self.assertEqual(got[-1], "[04999] ~/.onionpress line\n")

    def test_short_log_and_missing_final_newline(self):
        self.assertEqual(self._excerpt(b"a\nb\nc"), "a\nb\nc")
        self.assertEqual(self._excerpt(b""), "")
        self.assertEqual(self._excerpt(b"a\nb\nc\n", lines=2), "b\nc\n")
        # Lines end at \n only, as for tail(1): pull progress is \r-separated.
        self.assertEqual(self._excerpt(b"a\npull 1%\rpull 2%\rdone\n", lines=1),
                         "pull 1%\rpull 2%\rdone\n")

    def test_runaway_line_is_bounded(self):
        out = self._excerpt(b"x" * (4 * 1024 * 1024) + b"\nlast\n")
        self.assertLess(len(out), 512 * 1024)
        self.assertTrue(out.endswith("last\n"))

    def test_missing_log_raises_oserror(self):
        with self.assertRaises(OSError):
            redact.wordpress_log_excerpt(os.path.join(self.dir, "nope"), self.dir)


class TestStatusForWordPress(unittest.TestCase):
    LINUX_STATUS = {
        "state": "running", "version": "2.5.2", "onion_address": "op2abc.onion",
        "uptime_seconds": 5, "bootstrap_pct": 100, "containers": {"onionpress-tor": "running"},
        "wayback_queue_count": 0, "updated_at": "2026-10-12T00:00:00Z", "platform": "linux",
        "load_avg": [0.1, 0.2, 0.3], "host_uptime_seconds": 99,
        "onionheaven": {"server_active": False, "client_hub": "hub.onion"},
        "wp_installed": True, "onboarded": True, "mkp224o_available": True,
        "wp_admin_password_path": "/home/pi/.onionpress/wp-admin-password",
        "vanity_in_progress": False, "serving_from_wayback": False, "wp_port": 8080,
    }

    def test_host_only_fields_dropped(self):
        out = redact.status_for_wordpress(dict(self.LINUX_STATUS, config={"A": "b"}))
        self.assertNotIn("wp_admin_password_path", out)
        self.assertNotIn("config", out)
        self.assertNotIn("/home/pi", json.dumps(out))
        self.assertLessEqual(set(out), redact.WORDPRESS_STATUS_KEYS)
        self.assertEqual(out["onionheaven"], self.LINUX_STATUS["onionheaven"])

    def test_every_field_the_pages_read_is_kept(self):
        status_php = _read("app/Resources/plugins/onionpress-status.php")
        settings_php = _read("app/Resources/plugins/onionpress-settings.php")
        read = set(re.findall(r"\$status\[\s*'(\w+)'", status_php))
        read |= set(re.findall(r"\bs\.([a-z_]+)", status_php))  # the page's poll()
        read |= set(re.findall(r"\$(?:status|sd|s)\[\s*'(\w+)'", settings_php))
        self.assertIn("host_uptime_seconds", read)
        self.assertIn("onionname", read)
        self.assertEqual(read - redact.WORDPRESS_STATUS_KEYS, set(),
                         "a field the status/settings pages read is withheld "
                         "from the WordPress copy of status.json")


class TestCliCommands(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-wp-cli-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, argv, stdin=""):
        buf = io.StringIO()
        # Read-only and run on every Linux status write: none of
        # OnionPressCLI's setup may run for them.
        with mock.patch.object(cli, "OnionPressCLI", side_effect=AssertionError("setup ran")), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = cli.main(["--data-dir", self.dir] + argv)
        return rc, buf.getvalue()

    def test_wordpress_logs(self):
        log = os.path.join(self.dir, "onionpress.log")
        home = os.path.expanduser("~")
        with open(log, "w") as f:
            f.write(f"old\n{home}/.onionpress/config MYSQL_PASSWORD=pw 192.168.1.23\n")
        rc, out = self._run(["wordpress-logs", "--log-file", log])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("old\n~/.onionpress/config MYSQL_PASSWORD=<redacted>"), out)
        self.assertNotIn("192.168.1.23", out)

    def test_wordpress_logs_missing_file_fails(self):
        rc, out = self._run(["wordpress-logs", "--log-file", os.path.join(self.dir, "nope")])
        self.assertEqual((rc, out), (1, ""))

    def test_wordpress_status(self):
        status = dict(TestStatusForWordPress.LINUX_STATUS)
        rc, out = self._run(["wordpress-status"], stdin=json.dumps(status))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), redact.status_for_wordpress(status))

    def test_wordpress_status_rejects_garbage(self):
        for stdin in ("", "not json", "[1, 2]"):
            rc, out = self._run(["wordpress-status"], stdin=stdin)
            self.assertEqual((rc, out), (1, ""), stdin)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestLinuxLauncher(unittest.TestCase):
    """Run linux/onionpress's write_status() / write_recent_logs() and read
    back what they wrote into the (stubbed) WordPress volume."""

    DOCKER_STUB = r"""
docker() {
    # wp_put's `docker exec -i -u www-data onionpress-wordpress sh -c <script>
    # sh /var/lib/onionpress/<f> <mode>` runs its script here against
    # $OUT/<f>; every other docker call fails, as with no daemon.
    if [ "$1 $2 $3 $4 $5 $6 $7" = "exec -i -u www-data onionpress-wordpress sh -c" ]; then
        sh -c "$8" sh "$OUT/${10##*/}" "${11}"
        return
    fi
    return 1
}
log() { :; }
wp_is_installed() { return 1; }
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-wp-launcher-")
        self.data = os.path.join(self.tmp, "data")
        self.out = os.path.join(self.tmp, "volume")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (self.data, self.out, bin_dir):
            os.makedirs(d)
        # The launcher runs `python3`; make that this interpreter, which can
        # import the package (the system python3 may be too old for it).
        os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
        with open(os.path.join(self.data, "wp-admin-password"), "w") as f:
            f.write("not-read-by-this-test\n")
        self.log_file = os.path.join(self.tmp, "onionpress.log")
        with open(self.log_file, "w") as f:
            f.write(f"[t] Wrote {self.data}/config\n"
                    f"[t] cloudflared --token {TOKEN}\n"
                    "[t] Bound WordPress to 192.168.1.23:8080\n")
        self.env = {
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.tmp,
            "DATA_DIR": self.data,
            "DOCKER_DIR": self.tmp,
            "LOG_FILE": self.log_file,
            "ONIONPRESS_PYTHONPATH": SRC,
            "ONIONPRESS_VERSION": "0.0.0-test",
            "OUT": self.out,
            "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
        }
        self.launcher = _read("linux/onionpress")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, call, extra=""):
        script = "".join(_bash_function(self.launcher, n)
                         for n in ("op_py", "wp_put", "write_status", "write_recent_logs"))
        script += self.DOCKER_STUB + extra + "\n" + call + "\n"
        return subprocess.run(["bash", "-c", script], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def _volume(self, name):
        path = os.path.join(self.out, name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    def test_write_status_scrubs_logs_and_status(self):
        result = self._run("write_status")
        logs = self._volume("recent-logs.txt")
        self.assertIsNotNone(logs, result.stderr)
        self.assertIn("Wrote ~/data/config", logs)
        for leaked in (self.tmp, TOKEN, "192.168.1.23"):
            self.assertNotIn(leaked, logs)

        wp_status = json.loads(self._volume("status.json"))
        self.assertNotIn("wp_admin_password_path", wp_status)
        self.assertLessEqual(set(wp_status), redact.WORDPRESS_STATUS_KEYS)
        self.assertEqual(wp_status["platform"], "linux")
        # Written by wp_put: owner-only, and no temporary file left behind.
        for name in ("status.json", "recent-logs.txt", "version", "config-current.json"):
            self.assertEqual(os.stat(os.path.join(self.out, name)).st_mode & 0o777, 0o600, name)
        self.assertEqual(sorted(os.listdir(self.out)),
                         ["config-current.json", "recent-logs.txt", "status.json", "version"])
        # The tray's host copy keeps it.
        with open(os.path.join(self.data, "status.json")) as f:
            host_status = json.load(f)
        self.assertEqual(host_status["wp_admin_password_path"],
                         os.path.join(self.data, "wp-admin-password"))

    def test_helper_failure_never_copies_raw_data(self):
        self._run("write_status", extra="op_py() { return 1; }\n")
        logs = self._volume("recent-logs.txt")
        self.assertIn("Recent logs unavailable", logs)
        self.assertNotIn(TOKEN, logs)
        self.assertIsNone(self._volume("status.json"))

    def test_idle_watcher_tick_uses_the_scrubbed_copy(self):
        # The quiet tick is the shared handler's (onionpress.handle_action
        # write_recent_logs, Linux only): the scrubbed excerpt, as the
        # launcher's write_recent_logs writes it.
        handler = _read("src/onionpress/handle_action.py")
        body = handler[handler.index("def write_recent_logs("):handler.index("def apply_config(")]
        self.assertIn("redact.wordpress_log_excerpt(log_file, host.paths.data_dir)", body)
        self.assertNotIn("tail", body)
        self.assertIn('if host.os_name == "linux":\n            write_recent_logs(host)', handler)
        self._run("write_recent_logs")
        self.assertNotIn(TOKEN, self._volume("recent-logs.txt"))

    def test_no_raw_copy_of_the_log_remains(self):
        writers = [ln for ln in self.launcher.splitlines()
                   if "/var/lib/onionpress/recent-logs.txt" in ln]
        self.assertEqual(len(writers), 1, writers)
        self.assertIn('printf \'%s\\n\' "$logs"', writers[0])


class TestMacMenubar(unittest.TestCase):
    def setUp(self):
        self.src = _read("src/menubar.py")

    def test_status_and_logs_written_scrubbed(self):
        body = _between(self.src, "    def write_status_to_volume(self):",
                        "    def poll_settings_page(self):")
        self.assertIn("json.dumps(redact.status_for_wordpress(status), indent=2)", body)
        self.assertIn("redact.wordpress_log_excerpt(log_file, self.app_support)", body)
        self.assertNotIn("readlines()", body)
        # The analytics uploader still gets the full payload (scrubbed there).
        self.assertIn("self._last_status_payload = status\n", body)

    def test_action_results_scrubbed(self):
        # The page's action results are the shared handler's (issue #366).
        handler = _read("src/onionpress/handle_action.py")
        body = _between(handler, "    def write_result(self, filename, data):",
                        "    # -- the containers --")
        self.assertIn("redact.scrub_json_for_wordpress(data, self.paths.data_dir)", body)
        self.assertNotIn("def _write_result", self.src)

    def test_only_these_writers_reach_the_shared_files(self):
        for name in ("recent-logs.txt", "status.json"):
            self.assertEqual(self.src.count(f"/var/lib/onionpress/{name}"), 1, name)


class TestStatusPageIsTheOwners(unittest.TestCase):
    """Both endpoints check the viewer before they read a shared file or
    queue an action; there is no PHP in the test environment, so this reads
    the source."""

    def setUp(self):
        self.php = _read("app/Resources/plugins/onionpress-status.php")

    def test_viewer_must_be_super_admin(self):
        fn = _between(self.php, "function onionpress_status_viewer_allowed()", "\n}\n")
        self.assertIn("is_user_logged_in() && is_super_admin()", fn)

    def _handler(self, path):
        start = self.php.index(f"if ( $path !== '{path}' )")
        return self.php[start:self.php.index("\n} );\n", start)]

    def test_json_feed_refuses_before_reading_or_requesting(self):
        body = self._handler("onionpress-status-json")
        gate = body.index("if ( ! onionpress_status_viewer_allowed() )")
        self.assertLess(gate, body.index("/var/lib/onionpress/"))
        refusal = body[gate:body.index("/var/lib/onionpress/")]
        self.assertIn("status_header( 403 )", refusal)
        self.assertIn("exit;", refusal)

    def test_page_refuses_before_reading(self):
        body = self._handler("onionpress-status")
        gate = body.index("if ( ! onionpress_status_viewer_allowed() )")
        self.assertLess(gate, body.index("/var/lib/onionpress/"))
        refusal = body[gate:body.index("/var/lib/onionpress/")]
        self.assertIn("wp_safe_redirect( '/wp-login.php?redirect_to='", refusal)
        self.assertIn("'response' => 403", refusal)

    def test_settings_page_links_it_only_for_the_owner(self):
        php = _read("app/Resources/plugins/onionpress-settings.php")
        link = php.index('<a href="/onionpress-status">')
        self.assertIn("if ( is_super_admin() ) :", php[link - 200:link])


if __name__ == "__main__":
    unittest.main()
