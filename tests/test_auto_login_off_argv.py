#!/usr/bin/env python3
"""The one-time auto-login token never sits on a host command line.

The menubar, the Linux tray and `onionpress dashboard` open the site with
`?op_login=<token>`, which the onionpress-auto-login.php mu-plugin trades
once, within two minutes, for an admin session. `ps` shows every user's
argv (macOS `ps -axo args`, Linux /proc/<pid>/cmdline), and several users can
share one Mac, so:

  - the token reaches WordPress on stdin, through a fixed `wp eval` PHP
    (auto_login.MINT_PHP), never as code or as an argument;
  - on the Mac the URL goes to the browser through LaunchServices from the
    menubar process (browser.open_url), not through `open`;
  - on Linux the browser's command line gets the path of a 0600 page that
    redirects to the URL (launcher_ops.login_target);
  - Brave on the Mac, whose Tor window takes URLs only on its command line,
    gets the site without a token;
  - logs get the URL without the token.

Nothing here talks to Docker: subprocess is mocked.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

from onionpress import auto_login, browser, launcher_ops  # noqa: E402

PLUGIN = "app/Resources/plugins/onionpress-auto-login.php"
ONION = "a" * 52 + "qd2a.onion"

# /proc/net/route as the WordPress container sees it on onionpress-network
# (as in test_op_proxy_access.py): default route via 172.18.0.1, hex in
# little-endian host order, and the bridge subnet itself.
ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)
GATEWAY = "172.18.0.1"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


class _Recorder:
    """Stands in for subprocess.run / Popen: records (argv, stdin)."""

    def __init__(self, rc=0, stderr=""):
        self.calls = []
        self.rc = rc
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("input")))
        return subprocess.CompletedProcess(argv, self.rc, stdout="", stderr=self.stderr)

    def assert_not_in_argv(self, test, secret):
        for argv, _ in self.calls:
            for arg in argv:
                test.assertNotIn(secret, arg, f"token in argv: {argv}")


# ─── The PHP that stores the token ───────────────────────────────────────


class TestMintPhp(unittest.TestCase):

    PHP = auto_login.MINT_PHP

    def test_reads_the_token_from_stdin(self):
        self.assertIn('file_get_contents("php://stdin")', self.PHP)
        self.assertIn('set_site_transient("op_login_" . $token, '
                      'array("uid" => $uid, "scope" => $scope), '
                      f'{auto_login.TOKEN_TTL});', self.PHP)
        self.assertIn('array("local", "onion")', self.PHP)
        self.assertEqual(tuple(auto_login.SCOPES), ("local", "onion"))
        self.assertIn("exit(1)", self.PHP)

    def test_fits_in_single_quotes_and_never_interpolates(self):
        self.assertNotIn("'", self.PHP)
        for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', self.PHP):
            self.assertNotIn("$", literal, f"PHP would interpolate: {literal!r}")

    def test_token_format_matches_the_plugin(self):
        """MINT_PHP and the plugin accept the same tokens, and new_token()
        makes one of them."""
        plugin = _read(PLUGIN)
        self.assertIn("preg_match( '/^[A-Za-z0-9_-]{32,64}$/D', $token )", plugin)
        self.assertIn('preg_match("/^[A-Za-z0-9_-]{32,64}\\z/", $token)', self.PHP)
        for _ in range(50):
            self.assertRegex(auto_login.new_token(), r"\A[A-Za-z0-9_-]{32,64}\Z")

    @unittest.skipUnless(shutil.which("php"), "php not installed")
    def test_runs_against_stubbed_wordpress(self):
        prelude = (
            "<?php\n"
            'function get_users($a) { return getenv("NO_ADMIN") ? array() : array("3"); }\n'
            'function is_multisite() { return true; }\n'
            'function get_super_admins() { return array("root"); }\n'
            'function get_user_by($f, $v) { return (object) array("ID" => 9); }\n'
            'function set_site_transient($k, $v, $t) { echo "SET ", $k, " ", '
            '$v["uid"], " ", $v["scope"], " ", $t, "\\n"; }\n'
            "eval($argv[1]);\n"
        )
        token = auto_login.new_token()
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "run.php")
            with open(script, "w") as f:
                f.write(prelude)

            def run(stdin, **env):
                return subprocess.run(["php", script, self.PHP], input=stdin,
                                      capture_output=True, text=True, timeout=30,
                                      env={**os.environ, **env})

            out = run(f"{token}\nlocal\n")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(f"SET op_login_{token} 3 local 120\n", out.stdout)
            out = run(f"{token}\nonion\n", NO_ADMIN="1")
            self.assertIn(f"SET op_login_{token} 9 onion 120\n", out.stdout)
            for bad in (f"{token}\nanywhere\n", "short\nlocal\n",
                        f"{token}x'\nlocal\n", f"{token}\n", ""):
                with self.subTest(stdin=bad):
                    out = run(bad)
                    self.assertEqual(out.returncode, 1, out.stdout)
                    self.assertNotIn("SET", out.stdout)


# ─── auto_login ──────────────────────────────────────────────────────────


class TestMint(unittest.TestCase):

    def test_token_goes_on_stdin_only(self):
        for scope in auto_login.SCOPES:
            rec = _Recorder()
            with mock.patch.object(auto_login.subprocess, "run", rec):
                token = auto_login.mint(scope, docker_bin="/x/docker")
            self.assertRegex(token, r"\A[A-Za-z0-9_-]{43}\Z")
            [(argv, stdin)] = rec.calls
            self.assertEqual(argv, ["/x/docker", "exec", "-i", "onionpress-wordpress",
                                    "wp", "eval", auto_login.MINT_PHP, "--allow-root"])
            self.assertEqual(stdin, f"{token}\n{scope}\n")
            rec.assert_not_in_argv(self, token)

    def test_each_token_is_fresh(self):
        rec = _Recorder()
        with mock.patch.object(auto_login.subprocess, "run", rec):
            tokens = {auto_login.mint("local") for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    def test_failure_returns_none_and_logs_no_argv(self):
        logs = []
        with mock.patch.object(auto_login.subprocess, "run",
                               _Recorder(rc=1, stderr="Error: no administrator")):
            self.assertIsNone(auto_login.mint("local", log_func=logs.append))
        self.assertEqual(logs, ["Auto-login token failed: Error: no administrator"])

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 15)
        logs = []
        with mock.patch.object(auto_login.subprocess, "run", timeout):
            self.assertIsNone(auto_login.mint("onion", log_func=logs.append))
        self.assertEqual(logs, ["Auto-login token error: TimeoutExpired"])

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            auto_login.mint("anywhere")

    def test_scope_follows_the_url_host(self):
        cases = {
            "http://localhost:8080/name/": "local",
            "http://127.0.0.1:18080/": "local",
            "http://[::1]:8080/wp-admin/": "local",
            f"http://{ONION}/name/": "onion",
            "http://example.com/": None,
            "http://localhost.example.com/": None,
            "not a url": None,
        }
        for url, scope in cases.items():
            with self.subTest(url=url):
                self.assertEqual(auto_login.scope_for(url), scope)

    def test_login_url_mints_only_for_our_hosts(self):
        rec = _Recorder()
        with mock.patch.object(auto_login.subprocess, "run", rec):
            self.assertIsNone(auto_login.login_url("http://example.com/"))
            self.assertEqual(rec.calls, [])
            url = auto_login.login_url("http://localhost:8080/wp-admin/admin.php?page=x")
        [(_, stdin)] = rec.calls
        token = stdin.split("\n")[0]
        self.assertEqual(url, f"http://localhost:8080/wp-admin/admin.php?page=x&op_login={token}")


class TestRedirectPage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auto-login-test-")
        self.dir = os.path.join(self.tmp, "OnionPress", ".auto-login")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_page_is_private_and_named_without_the_token(self):
        token = auto_login.new_token()
        url = f"http://localhost:8080/my&site/?op_login={token}"
        path = auto_login.write_redirect_page(url, self.dir)
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertNotIn(token, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        page = _read(path)
        escaped = f"http://localhost:8080/my&amp;site/?op_login={token}"
        self.assertIn(f'<meta http-equiv="refresh" content="0;url={escaped}">', page)
        self.assertIn('<meta name="referrer" content="no-referrer">', page)

    def test_quotes_in_the_url_cannot_leave_the_attribute(self):
        path = auto_login.write_redirect_page('http://localhost/"><script>x</script>', self.dir)
        self.assertNotIn("<script>", _read(path))

    def test_existing_dir_is_tightened_and_a_symlink_refused(self):
        os.makedirs(self.dir, mode=0o755)
        os.chmod(self.dir, 0o755)
        auto_login.write_redirect_page("http://localhost/", self.dir)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        link = os.path.join(self.tmp, "link")
        os.symlink(elsewhere, link)
        with self.assertRaises(OSError):
            auto_login.write_redirect_page("http://localhost/", link)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_stale_pages_are_swept(self):
        old = auto_login.write_redirect_page("http://localhost/", self.dir)
        os.utime(old, (0, 0))
        new = auto_login.write_redirect_page("http://localhost/", self.dir)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_remove_later_names_only_the_path(self):
        rec = _Recorder()
        with mock.patch.object(auto_login.subprocess, "Popen", rec):
            auto_login.remove_later("/home/u/OnionPress/.auto-login/login-x.html")
        [(argv, _)] = rec.calls
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertEqual(argv[-2:], [str(auto_login.TOKEN_TTL),
                                     "/home/u/OnionPress/.auto-login/login-x.html"])

    def test_redirect_dir_is_a_snap_readable_home_path(self):
        """Snap browsers read nothing in /tmp, $XDG_RUNTIME_DIR or a hidden
        top-level directory of $HOME."""
        rel = os.path.relpath(auto_login.REDIRECT_DIR, os.path.expanduser("~"))
        self.assertFalse(rel.startswith((".", "snap")), rel)
        self.assertFalse(rel.startswith(os.pardir), rel)


# ─── Linux: launcher_ops, the tray, `onionpress dashboard` ───────────────


class TestLinuxLoginTarget(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="login-target-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_browser_gets_a_file_uri_and_no_token(self):
        run, popen = _Recorder(), _Recorder()
        with mock.patch.object(auto_login.subprocess, "run", run), \
                mock.patch.object(auto_login.subprocess, "Popen", popen), \
                mock.patch.object(launcher_ops.subprocess, "Popen", popen), \
                mock.patch.object(launcher_ops, "_launch_default_web_browser",
                                  return_value=False), \
                mock.patch.object(launcher_ops.sys, "platform", "linux"):
            target = launcher_ops.login_target("http://localhost:8080/wp-admin/",
                                               page_dir=self.tmp)
            launcher_ops.open_in_browser(target)
        [(_, stdin)] = run.calls
        token = stdin.split("\n")[0]
        self.assertTrue(target.startswith("file://" + self.tmp), target)
        self.assertNotIn(token, target)
        page = os.path.join(self.tmp, os.path.basename(target))
        self.assertIn(f"op_login={token}", _read(page))
        self.assertIn(["xdg-open", target], [argv for argv, _ in popen.calls])
        run.assert_not_in_argv(self, token)
        popen.assert_not_in_argv(self, token)

    def test_no_token_means_the_plain_url(self):
        with mock.patch.object(auto_login.subprocess, "run", _Recorder(rc=1)):
            self.assertEqual(launcher_ops.login_target("http://localhost:8080/",
                                                       page_dir=self.tmp),
                             "http://localhost:8080/")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_a_file_uri_goes_to_the_web_browser_not_the_html_handler(self):
        popen = _Recorder()
        with mock.patch.object(launcher_ops, "_launch_default_web_browser",
                               return_value=True) as gio, \
                mock.patch.object(launcher_ops.subprocess, "Popen", popen), \
                mock.patch.object(launcher_ops.sys, "platform", "linux"):
            launcher_ops.open_in_browser("file:///home/u/OnionPress/.auto-login/login-x.html")
            launcher_ops.open_in_browser("http://localhost:8080/")
        gio.assert_called_once_with("file:///home/u/OnionPress/.auto-login/login-x.html")
        self.assertEqual(popen.calls, [(["xdg-open", "http://localhost:8080/"], None)])

    def test_no_token_minting_left_in_launcher_ops(self):
        self.assertFalse(hasattr(launcher_ops, "mint_op_login_token"))
        self.assertNotIn("op_login", _read("src/onionpress/launcher_ops.py"))

    def test_tray_hands_browsers_only_login_target(self):
        src = _read("linux/onionpress-tray")
        self.assertNotIn("_build_login_url", src)
        self.assertNotIn("op_login=", src)
        # Every login_url (a token-bearing URL) goes to the copy dialog only.
        for m in re.finditer(r"launcher_ops\.login_url\(", src):
            line = src[src.rindex("\n", 0, m.start()):src.index("\n", m.end())]
            self.assertIn("_show_url_copy_dialog(", line)
        self.assertGreaterEqual(src.count("launcher_ops.login_target("), 3)
        # The cold-launch thread mints when Tor Browser is up, not at click time.
        body = src[src.index("def _wait_then_open():"):]
        body = body[:body.index("threading.Thread(target=_wait_then_open")]
        self.assertIn("target = launcher_ops.login_target(base)", body)
        self.assertIn('cmd = [wrapper, "-new-tab", target]', body)

    def test_dashboard_uses_open_login(self):
        src = _read("linux/onionpress")
        start = src.index("        dashboard)")
        body = src[start:src.index(";;", start)]
        self.assertIn('op_py open-login "http://localhost:${ONIONPRESS_WP_PORT:-8080}/wp-admin/"',
                      body)
        self.assertNotIn("op_login", body)
        self.assertNotIn("transient", body)

    def test_cli_open_login_skips_setup(self):
        from onionpress import cli
        with mock.patch.object(launcher_ops, "open_login") as open_login, \
                mock.patch.object(cli, "OnionPressCLI") as full_cli:
            self.assertEqual(cli.main(["open-login", "http://localhost:8080/wp-admin/"]), 0)
        open_login.assert_called_once_with("http://localhost:8080/wp-admin/")
        full_cli.assert_not_called()


# ─── Mac: LaunchServices, not `open` ─────────────────────────────────────


def _fake_appkit(main_thread=True):
    calls = []

    class NSURL:
        @staticmethod
        def URLWithString_(s):
            return ("url", s)

        @staticmethod
        def fileURLWithPath_(p):
            return ("file", p)

    class _WS:
        def openURL_(self, u):
            calls.append(("openURL", u))
            return True

        def openURLs_withApplicationAtURL_configuration_completionHandler_(self, urls, app, cfg, h):
            calls.append(("openURLs", urls, app))

        def URLForApplicationWithBundleIdentifier_(self, bid):
            return ("bundle", bid)

    ws = _WS()
    queued = []
    appkit = types.SimpleNamespace(
        NSURL=NSURL,
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: ws),
        NSWorkspaceOpenConfiguration=types.SimpleNamespace(configuration=lambda: "cfg"),
        NSThread=types.SimpleNamespace(isMainThread=lambda: main_thread),
        NSOperationQueue=types.SimpleNamespace(mainQueue=lambda: types.SimpleNamespace(
            addOperationWithBlock_=queued.append)),
    )
    return appkit, calls, queued


class TestMacOpenUrl(unittest.TestCase):

    URL = "http://localhost:8080/name/?op_login=SECRET-TOKEN-0123456789-abcdefghijklmn"

    def test_default_browser_through_launch_services(self):
        appkit, calls, _ = _fake_appkit()
        rec = _Recorder()
        with mock.patch.dict(sys.modules, {"AppKit": appkit}), \
                mock.patch.object(browser.subprocess, "run", rec):
            browser.open_url(self.URL)
        self.assertEqual(calls, [("openURL", ("url", self.URL))])
        self.assertEqual(rec.calls, [])

    def test_named_app(self):
        appkit, calls, _ = _fake_appkit()
        with mock.patch.dict(sys.modules, {"AppKit": appkit}), \
                mock.patch.object(browser.os.path, "isdir",
                                  lambda p: p == "/Applications/Tor Browser.app"):
            browser.open_url(self.URL, browser.TOR_BROWSER_PATH)
            browser.open_url(self.URL, "Firefox")
        self.assertEqual(calls, [
            ("openURLs", [("url", self.URL)], ("file", "/Applications/Tor Browser.app")),
            ("openURLs", [("url", self.URL)], ("bundle", "org.mozilla.firefox")),
        ])

    def test_off_the_main_thread_it_is_dispatched_there(self):
        appkit, calls, queued = _fake_appkit(main_thread=False)
        with mock.patch.dict(sys.modules, {"AppKit": appkit}):
            browser.open_url(self.URL)
            self.assertEqual(calls, [])
            [block] = queued
            block()
        self.assertEqual(calls, [("openURL", ("url", self.URL))])

    def test_onion_token_only_for_launch_services_browsers(self):
        base = f"http://{ONION}/name/"
        for btype, bname in (("tor_browser", "Tor Browser"), ("extension", "Firefox"),
                             ("brave", "Brave Browser")):
            with self.subTest(btype=btype):
                rec, opened, logs, minted = _Recorder(), [], [], []

                def add_login(url):
                    minted.append(url)
                    return url + "?op_login=SECRET"
                with mock.patch.object(browser, "detect_best_browser",
                                       return_value=(btype, bname)), \
                        mock.patch.object(browser, "open_url",
                                          lambda u, app=None, log_func=None:
                                          opened.append((u, app))), \
                        mock.patch.object(browser.subprocess, "run", rec):
                    self.assertTrue(browser.open_onion_url(base, "/x", logs.append,
                                                           add_login=add_login))
                rec.assert_not_in_argv(self, "SECRET")
                for line in logs:
                    self.assertNotIn("SECRET", line)
                if btype == "brave":
                    self.assertEqual(minted, [])
                    self.assertEqual(opened, [])
                    self.assertIn(base, rec.calls[0][0])
                else:
                    self.assertEqual(minted, [base])
                    self.assertEqual(rec.calls, [])
                    self.assertEqual(opened[0][0], base + "?op_login=SECRET")


class TestMenubar(unittest.TestCase):

    SRC = _read("src/menubar.py")

    def _method(self, name):
        start = self.SRC.index(f"    def {name}(")
        end = re.compile(r"\n    (?:def |@)").search(self.SRC, start + 1).start()
        return textwrap.dedent(self.SRC[start:end])

    def test_open_local_site_keeps_the_token_out_of_argv_and_log(self):
        ns = {}
        opened, logs = [], []
        rec = _Recorder()
        ns["op_browser"] = types.SimpleNamespace(
            open_url=lambda url, app=None, log_func=None: opened.append(url))
        ns["auto_login"] = auto_login
        ns["os"] = os
        ns["subprocess"] = types.SimpleNamespace(run=rec)
        exec(self._method("_generate_login_url"), ns)
        exec(self._method("open_local_site"), ns)
        app = types.SimpleNamespace(
            read_config_value=lambda k, d="": "alice", local_url="http://localhost:8080",
            bin_dir="/x/bin", log=logs.append)
        app._generate_login_url = types.MethodType(ns["_generate_login_url"], app)
        with mock.patch.object(auto_login.subprocess, "run", rec):
            ns["open_local_site"](app, None)
        [(argv, stdin)] = rec.calls
        token = stdin.split("\n")[0]
        self.assertEqual(argv[0], "/x/bin/docker")
        self.assertEqual(stdin, f"{token}\nlocal\n")
        self.assertEqual(opened, [f"http://localhost:8080/alice/?op_login={token}"])
        rec.assert_not_in_argv(self, token)
        self.assertEqual(logs, ["Opened local site: http://localhost:8080/alice/"])

    def test_every_token_url_goes_to_launch_services(self):
        """A _generate_login_url result is only ever handed to
        op_browser.open_url or open_onion_url (add_login=), never logged or
        given to subprocess."""
        self.assertNotIn("op_login", self.SRC)
        for m in re.finditer(r"_generate_login_url\b", self.SRC):
            line = self.SRC[self.SRC.rindex("\n", 0, m.start()) + 1:
                            self.SRC.index("\n", m.end())]
            if line.lstrip().startswith("def _generate_login_url"):
                continue
            prev = self.SRC[self.SRC.rindex("\n", 0, self.SRC.rindex("\n", 0, m.start())):
                            m.start()]
            self.assertRegex(prev + line,
                             r"op_browser\.open_url\(|add_login=self\._generate_login_url",
                             line)


# ─── The mu-plugin ───────────────────────────────────────────────────────


class TestPlugin(unittest.TestCase):

    SRC = _read(PLUGIN)

    def test_single_use_network_wide_and_scoped(self):
        self.assertIn("get_site_transient( $key )", self.SRC)
        # Only the request whose delete removed the token goes on.
        self.assertIn("if ( false === $stored || ! delete_site_transient( $key ) ) {", self.SRC)
        self.assertLess(self.SRC.index("delete_site_transient( $key )"),
                        self.SRC.index("onionpress_auto_login_scope_ok( $stored['scope'] )"))
        self.assertLess(self.SRC.index("onionpress_auto_login_scope_ok( $stored['scope'] )"),
                        self.SRC.index("wp_set_current_user("))
        self.assertNotRegex(self.SRC, r"\bget_transient\(")

    def test_local_scope_admits_loopback_and_the_gateway_only(self):
        """An allowlist, as /__op_proxy.php's: refusing the tor container's
        address let cloudflared and every other container in."""
        fn = self.SRC[self.SRC.index("function onionpress_auto_login_scope_ok"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("array( 'localhost', '127.0.0.1', '[::1]' )", fn)
        self.assertIn("$_SERVER['REMOTE_ADDR']", fn)
        self.assertIn("'/^[a-z2-7]{56}\\.onion$/D'", fn)
        self.assertIn("if ( '127.0.0.1' === $remote || '::1' === $remote ) {", fn)
        self.assertIn("$gateway = onionpress_auto_login_gateway();", fn)
        self.assertTrue(fn.rstrip().endswith("return '' !== $gateway && $remote === $gateway;"))
        self.assertNotIn("gethostbyname", self.SRC)
        self.assertNotIn("172.", self.SRC)

    def test_open_redirect_fix_kept(self):
        """PR #273: sanitize redirect_to before validating it
        (tests/test-auto-login-redirect.php exercises the block)."""
        self.assertIn("$requested = wp_sanitize_redirect( wp_unslash( $_GET['redirect_to'] ) );",
                      self.SRC)
        self.assertIn("preg_match( '#^/($|[^/\\\\\\\\])#', $requested )", self.SRC)
        self.assertRegex(self.SRC, re.compile(
            r"\$redirect_to = remove_query_arg.*?(?=\n\s*wp_redirect)", re.S))

    @unittest.skipUnless(shutil.which("php"), "php not installed")
    def test_redirect_regression_script(self):
        out = subprocess.run(["php", os.path.join(TESTS_DIR, "test-auto-login-redirect.php")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    @unittest.skipUnless(shutil.which("php"), "php not installed")
    def test_plugin_against_stubbed_wordpress(self):
        """Run the shipped plugin's init closure with stubbed WordPress."""
        token = auto_login.new_token()
        prelude = r'''<?php
define('ABSPATH', '/'); define('YEAR_IN_SECONDS', 1); define('AUTH_COOKIE', 'a');
define('LOGGED_IN_COOKIE', 'l'); define('PLUGINS_COOKIE_PATH', '/'); define('ADMIN_COOKIE_PATH', '/');
define('COOKIEPATH', '/'); define('SITECOOKIEPATH', '/');
$GLOBALS['store'] = json_decode(getenv('STORE'), true);
$GLOBALS['init'] = null;
function add_action($h, $f) { $GLOBALS['init'] = $f; }
function wp_unslash($v) { return $v; }
function get_site_transient($k) { return array_key_exists($k, $GLOBALS['store']) ? $GLOBALS['store'][$k] : false; }
function delete_site_transient($k) { if (!array_key_exists($k, $GLOBALS['store'])) return false; unset($GLOBALS['store'][$k]); return true; }
function get_user_by($f, $v) { return (object) array('ID' => (int) $v); }
function wp_set_current_user($id) { echo "LOGIN ", $id, "\n"; }
function wp_generate_auth_cookie() { return 'c'; }
function op_test_setcookie() {}
function nocache_headers() {}
function remove_query_arg($a) { return '/clean'; }
function wp_sanitize_redirect($v) { return $v; }
function wp_redirect($u) { echo "REDIRECT ", $u, "\n"; }
'''
        with tempfile.TemporaryDirectory() as tmp:
            plugin = os.path.join(tmp, "plugin.php")
            routes = os.path.join(tmp, "route")
            with open(routes, "w") as f:
                f.write(ROUTE_TABLE)
            self.assertEqual(self.SRC.count("'/proc/net/route'"), 1)
            src = self.SRC.replace("'/proc/net/route'", "getenv('ROUTE')")
            src = src.replace("setcookie(", "op_test_setcookie(")  # a PHP builtin
            src = src.replace("    exit;\n} );", "    return;\n} );")
            self.assertEqual(src.count("op_test_setcookie("), 4)
            self.assertIn("    return;\n} );", src)
            with open(plugin, "w") as f:
                f.write(src)
            runner = os.path.join(tmp, "run.php")
            with open(runner, "w") as f:
                f.write(prelude + f"require '{plugin}';\n"
                        "$_GET = array('op_login' => getenv('TOKEN'));\n"
                        "$_SERVER = array('HTTP_HOST' => getenv('HOST'), "
                        "'REMOTE_ADDR' => getenv('REMOTE'));\n"
                        "call_user_func($GLOBALS['init']);\n"
                        "call_user_func($GLOBALS['init']);\n")

            def run(scope, host, remote=GATEWAY, tok=token, route=routes):
                import json
                store = {f"op_login_{token}": {"uid": 7, "scope": scope}}
                return subprocess.run(
                    ["php", runner], capture_output=True, text=True, timeout=30,
                    env={**os.environ, "STORE": json.dumps(store), "TOKEN": tok,
                         "HOST": host, "REMOTE": remote, "ROUTE": route}).stdout

            # Redeemed once: the second init call finds nothing.
            ok = "LOGIN 7\nREDIRECT /clean\n"
            self.assertEqual(run("local", "localhost:8080"), ok)
            self.assertEqual(run("local", "127.0.0.1:18080"), ok)
            self.assertEqual(run("onion", ONION), ok)
            for remote in ("::ffff:" + GATEWAY, "127.0.0.1", "::1", "::ffff:127.0.0.1"):
                with self.subTest(remote=remote):
                    self.assertEqual(run("local", "localhost:8080", remote), ok)
            # The onion scope does not look at the address: Tor Browser's
            # requests arrive from the tor container.
            self.assertEqual(run("onion", ONION, "172.18.0.5"), ok)
            for scope, host, remote in (
                    ("local", "localhost:8080", "172.18.0.5"),   # through Tor
                    ("local", "localhost:8080", "172.18.0.4"),   # cloudflared
                    ("local", "localhost:8080", "172.17.0.1"),   # another network's gateway
                    ("local", "localhost:8080", "172.18.0.0"),
                    ("local", "localhost:8080", "10.0.2.2"),
                    ("local", "localhost:8080", "192.168.1.20"),
                    ("local", "localhost:8080", ""),
                    ("local", ONION, GATEWAY),
                    ("onion", "localhost:8080", GATEWAY),
                    ("other", "localhost:8080", GATEWAY)):
                with self.subTest(scope=scope, host=host, remote=remote):
                    self.assertEqual(run(scope, host, remote), "")
            self.assertEqual(run("local", "localhost:8080", tok=token + "'"), "")
            # No route table, or no default route in it: loopback only.
            missing = os.path.join(tmp, "missing")
            self.assertEqual(run("local", "localhost:8080", route=missing), "")
            self.assertEqual(run("local", "localhost:8080", "127.0.0.1", route=missing), ok)
            no_default = os.path.join(tmp, "no-default")
            with open(no_default, "w") as f:
                f.write(ROUTE_TABLE.splitlines(True)[0] + ROUTE_TABLE.splitlines(True)[2])
            self.assertEqual(run("local", "localhost:8080", route=no_default), "")

    @unittest.skipUnless(shutil.which("php"), "php not installed")
    def test_gateway_parser_matches_op_proxy(self):
        """The plugin and /__op_proxy.php (src/onion-forward.php) read the
        gateway the same way: run each parser on the same route tables."""
        forwarder = _read("src/onion-forward.php")
        parsers = {
            "onionpress_auto_login_gateway": self.SRC,
            "op_proxy_gateway": forwarder,
        }
        tables = {
            "docker": ROUTE_TABLE,
            "no default": ROUTE_TABLE.splitlines(True)[0] + ROUTE_TABLE.splitlines(True)[2],
            "default without gateway flag": ROUTE_TABLE.replace("\t0003\t", "\t0001\t", 1),
            "default with zero gateway": ROUTE_TABLE.replace("010012AC", "00000000", 1),
            "malformed gateway": ROUTE_TABLE.replace("010012AC", "010012AG", 1),
            "second network first": (
                ROUTE_TABLE.splitlines(True)[0]
                + "eth1\t000013AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
                + ROUTE_TABLE.splitlines(True)[1]),
            "empty": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            results = {}
            for fn, src in parsers.items():
                m = re.search(r"\nfunction " + fn + r"\(\) \{\n.*?\n\}\n", src, re.S)
                self.assertIsNotNone(m, fn)
                body = m.group(0).replace("'/proc/net/route'", "getenv('ROUTE')")
                self.assertIn("getenv('ROUTE')", body)
                script = os.path.join(tmp, fn + ".php")
                with open(script, "w") as f:
                    f.write("<?php\n" + body + f"echo json_encode({fn}());\n")
                for name, table in tables.items():
                    route = os.path.join(tmp, "route")
                    with open(route, "w") as f:
                        f.write(table)
                    out = subprocess.run(["php", script], capture_output=True, text=True,
                                         timeout=30, env={**os.environ, "ROUTE": route})
                    results.setdefault(name, {})[fn] = out.stdout
            for name, by_fn in results.items():
                with self.subTest(table=name):
                    self.assertEqual(len(set(by_fn.values())), 1, by_fn)
            self.assertEqual(results["docker"]["op_proxy_gateway"], f'"{GATEWAY}"')
            self.assertEqual(results["no default"]["op_proxy_gateway"], '""')


if __name__ == "__main__":
    unittest.main()
