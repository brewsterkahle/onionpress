#!/usr/bin/env python3
"""Setup credentials never travel in a host process's argv.

`ps` shows every user's command lines (macOS `ps -axo args`, Linux
/proc/<pid>/cmdline), and several users can run OnionPress on one Mac. A
subprocess error (TimeoutExpired, CalledProcessError) also quotes the whole
argv, and the callers log what they catch. So:

  - the WordPress admin password is never `--admin_password=` /
    `--user_pass=`: WordPress is installed without one (wp-cli generates a
    password, printed on a stdout nobody keeps) and the user's password is
    set by a fixed `wp eval` PHP that reads "<login>\\n<password>" from stdin
    (setup_logic.WP_SET_PASSWORD_PHP, and the launchers' identical
    wp_set_user_password);
  - the archive.org login form, its session cookie and the S3 keys go to
    curl / `wp option update` on stdin;
  - the onionnames registrar's signed body goes to curl on stdin.

Nothing here talks to Docker: subprocess is mocked, or `docker`/`curl` are
stubs on PATH.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import multisite, onion_proxy, onionnames_registrar, setup_logic  # noqa: E402

MAC = "app/MacOS/onionpress"
LINUX = "linux/onionpress"
LAUNCHERS = (MAC, LINUX)

# Characters a shell, PHP or URL layer could mangle.
TRICKY_PASSWORDS = [
    "hunter22",
    "p a$s\\w'o\"rd`x",
    "ends-with-a-backslash\\",
    "%s%n%d-and-$(id)",
    "pässwörd",
]


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    """Source of a top-level bash function, closing brace included: up to
    the last `}` line before the next top-level definition."""
    start = text.index(f"\n{name}() {{") + 1
    nxt = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{", re.M).search(text, start + 1)
    chunk = text[start:nxt.start() if nxt else len(text)]
    return chunk[:chunk.rindex("\n}\n") + 3]


# `docker exec -i` arguments that run wp-cli as www-data, never as root
# (onionpress.wp_files.wp_cli_args); without stdin, the same minus -i.
WP_AS_WWW_DATA = ["exec", "-i", "-u", "www-data", "-e",
                  "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache", "onionpress-wordpress", "wp"]


class _Recorder:
    """Stands in for subprocess.run: records (argv, stdin, kwargs) and
    answers with *reply(argv, stdin)* -> (returncode, stdout)."""

    def __init__(self, reply=lambda argv, stdin: (0, "")):
        self.calls = []
        self._reply = reply

    def __call__(self, argv, **kwargs):
        stdin = kwargs.get("input")
        self.calls.append((list(argv), stdin, kwargs))
        rc, out = self._reply(list(argv), stdin)
        if not kwargs.get("text"):
            out = out.encode()
        return subprocess.CompletedProcess(
            argv, rc, stdout=out, stderr="" if kwargs.get("text") else b"")

    def assert_not_in_argv(self, test, secret):
        for argv, _, _ in self.calls:
            for arg in argv:
                test.assertNotIn(secret, arg, f"secret in argv: {argv}")

    def eval_calls(self):
        return [(a, i) for a, i, _ in self.calls
                if "eval" in a and setup_logic.WP_SET_PASSWORD_PHP in a]


# ─── The PHP ─────────────────────────────────────────────────────────────


class TestSetPasswordPhp(unittest.TestCase):

    PHP = setup_logic.WP_SET_PASSWORD_PHP

    def test_fits_in_single_quotes_and_never_interpolates(self):
        self.assertNotIn("'", self.PHP)
        for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', self.PHP):
            self.assertNotIn("$", literal, f"PHP would interpolate: {literal!r}")
        # A `}` alone on a line would end the launchers' function early for
        # anything that scans for it.
        self.assertNotIn("\n}\n", self.PHP)

    def test_reads_stdin_and_names_no_secret(self):
        self.assertIn('file_get_contents("php://stdin")', self.PHP)
        self.assertIn("wp_set_password($pw, $u->ID)", self.PHP)
        self.assertIn('delete_user_meta($u->ID, "default_password_nag")', self.PHP)
        self.assertIn("exit(1)", self.PHP)

    @unittest.skipUnless(shutil.which("php"), "php not installed")
    def test_runs_against_stubbed_wordpress(self):
        prelude = (
            "<?php\n"
            'function get_user_by($f, $v) { return ($f === "login" && $v === "alice")'
            ' ? (object) array("ID" => 7, "user_login" => "alice") : false; }\n'
            'function wp_set_password($pw, $id) { echo "SET[", $pw, "] ", $id, "\\n"; }\n'
            'function delete_user_meta($id, $k) { echo "DEL ", $id, " ", $k, "\\n"; }\n'
            "eval($argv[1]);\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "run.php")
            with open(script, "w") as f:
                f.write(prelude)

            def run(stdin):
                return subprocess.run(["php", script, self.PHP], input=stdin,
                                      capture_output=True, text=True, timeout=30)

            for pw in TRICKY_PASSWORDS + ["inner\nnewline"]:
                with self.subTest(pw=pw):
                    out = run(f"alice\n{pw}")
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertIn(f"SET[{pw}] 7\n", out.stdout)
                    self.assertIn("DEL 7 default_password_nag\n", out.stdout)
            # trim(), as wp_install() and wp_authenticate() do.
            self.assertIn("SET[pw] 7", run(" alice \n  pw \n").stdout)
            for bad in ("bob\npw", "alice\n", "alice\n   ", "alice", ""):
                with self.subTest(stdin=bad):
                    out = run(bad)
                    self.assertEqual(out.returncode, 1, out.stdout)
                    self.assertNotIn("SET[", out.stdout)


# ─── setup_logic ─────────────────────────────────────────────────────────


class TestSetupLogicPasswordOnStdin(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="setup-argv-test-")
        self.logs = []
        # The background archive.org fetch would outlive the mock.
        p = mock.patch("threading.Thread")
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install(self, rec, password):
        with mock.patch.object(setup_logic.subprocess, "run", rec):
            return setup_logic.install_fresh_wordpress(
                site_title="My Blog", onionname="alice", password=password,
                onion_addr="abc.onion", docker_bin="/x/docker",
                launcher_bin="/x/onionpress", log_func=self.logs.append,
                data_dir=self.tmpdir)

    def test_install_sets_the_password_on_stdin(self):
        for pw in TRICKY_PASSWORDS:
            with self.subTest(pw=pw):
                rec = _Recorder()
                self.assertTrue(self._install(rec, pw), self.logs)
                rec.assert_not_in_argv(self, pw)
                install = rec.calls[0][0]
                self.assertEqual(install[1:10], WP_AS_WWW_DATA[:1] + WP_AS_WWW_DATA[2:]
                                 + ["core", "install"])
                self.assertFalse([a for a in install if "admin_password" in a])
                # Straight after the install, before anything else.
                argv, stdin, _ = rec.calls[1]
                self.assertEqual(argv, ["/x/docker"] + WP_AS_WWW_DATA + [
                    "eval", setup_logic.WP_SET_PASSWORD_PHP])
                self.assertEqual(stdin, f"alice\n{pw}")
                self.assertEqual(len(rec.eval_calls()), 1)
                for msg in self.logs:
                    self.assertNotIn(pw, msg)

    def test_install_reports_a_failed_password_set_but_finishes(self):
        def reply(argv, stdin):
            return (1, "") if setup_logic.WP_SET_PASSWORD_PHP in argv else (0, "")
        rec = _Recorder(reply)
        self.assertFalse(self._install(rec, "hunter22"))
        self.assertTrue(any("ERROR: setting the admin password failed" in m
                            for m in self.logs), self.logs)
        # The rest of setup (multisite, subsite, onboarding) still ran.
        self.assertIn(["/x/onionpress", "provision-post-install"],
                      [a for a, _, _ in rec.calls])

    def test_install_timeout_is_logged_without_argv(self):
        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv + ["SENTINEL-ARGV"], 60)
        with mock.patch.object(setup_logic.subprocess, "run", boom):
            ok = setup_logic.install_fresh_wordpress(
                site_title="t", onionname="alice", password="hunter22",
                launcher_bin=None, log_func=self.logs.append,
                data_dir=self.tmpdir)
        self.assertFalse(ok)
        self.assertTrue(any("timed out" in m for m in self.logs), self.logs)
        for msg in self.logs:
            self.assertNotIn("SENTINEL-ARGV", msg)

    def test_provision_existing_sets_the_password_on_stdin(self):
        rec = _Recorder()
        pw = TRICKY_PASSWORDS[1]
        with mock.patch.object(setup_logic.subprocess, "run", rec):
            self.assertTrue(setup_logic.provision_existing_wordpress(
                site_title="t", onionname="alice", password=pw,
                log_func=self.logs.append, data_dir=self.tmpdir,
                adopt_existing=True))
        rec.assert_not_in_argv(self, pw)
        self.assertEqual([i for _, i in rec.eval_calls()], [f"alice\n{pw}"])
        self.assertIn("Password updated", self.logs)

    def test_provision_existing_falls_back_to_admin(self):
        def reply(argv, stdin):
            if stdin and stdin.startswith("alice\n"):
                return 1, ""
            return 0, ""
        rec = _Recorder(reply)
        with mock.patch.object(setup_logic.subprocess, "run", rec):
            setup_logic.provision_existing_wordpress(
                site_title="t", onionname="alice", password="hunter22",
                log_func=self.logs.append, data_dir=self.tmpdir,
                adopt_existing=True)
        self.assertEqual([i for _, i in rec.eval_calls()],
                         ["alice\nhunter22", "admin\nhunter22"])
        self.assertIn("Password updated (via admin fallback)", self.logs)
        rec.assert_not_in_argv(self, "hunter22")

    def test_no_password_flags_left_on_any_host_command_line(self):
        paths = [MAC, LINUX, "linux/onionpress-tray", "src/menubar.py"]
        for base in ("src/onionpress", "linux"):
            for name in sorted(os.listdir(os.path.join(PROJECT_DIR, base))):
                if name.endswith(".py"):
                    paths.append(f"{base}/{name}")
        for rel in paths:
            with self.subTest(rel=rel):
                self.assertNotRegex(_read(rel), r"--(admin_password|user_pass)=")

    def test_menubar_logs_no_subprocess_argv(self):
        src = _read("src/menubar.py")
        start = src.index("    def _wp_core_install(self, sw):")
        body = src[start:src.index("\n    def ", start + 1)]
        self.assertIn("except subprocess.SubprocessError as e:", body)
        self.assertIn('self.log(f"wp core install error: {type(e).__name__}")', body)


# ─── archive.org and the proxy's /setup ────────────────────────────────


LOGIN_WITH_KEYS = json.dumps(
    {"success": True, "values": {"s3": {"access": "AK", "secret": "S3-SECRET"}}})
LOGIN_WITH_COOKIES = json.dumps(
    {"success": True, "values": {"cookies": {
        "logged-in-sig": "SIG-SECRET", "logged-in-user": "someone@example.org"}}})
S3_PAGE = json.dumps({"key": {"s3accesskey": "AK", "s3secretkey": "S3-SECRET"}})


def _handler(logs, docker_bin="/x/docker", data_dir=None):
    h = onion_proxy.OnionProxyHandler.__new__(onion_proxy.OnionProxyHandler)
    h.server = types.SimpleNamespace(docker_bin=docker_bin, docker_env={"A": "1"},
                                     log_func=logs.append, onion_address=None,
                                     launcher_script="/x/onionpress",
                                     data_dir=data_dir)
    return h


class TestArchiveLogin(unittest.TestCase):
    """multisite.fetch_archive_s3_keys, the one archive.org login (setup's
    background fetch and the per-launch reconcile)."""

    def _fetch(self, email, password, logs):
        return multisite.fetch_archive_s3_keys(
            email, password, docker_bin="/x/docker", env={"A": "1"},
            log_func=logs.append)

    def test_login_and_cookie_go_on_stdin(self):
        def reply(argv, stdin):
            return 0, (S3_PAGE if "@-" in argv and "-H" in argv else LOGIN_WITH_COOKIES)
        rec = _Recorder(reply)
        logs = []
        with mock.patch.object(multisite.subprocess, "run", rec):
            keys = self._fetch("me@example.org", "IA-P@ss&word", logs)
        self.assertEqual(keys, {"access": "AK", "secret": "S3-SECRET"})
        self.assertEqual(len(rec.calls), 2, rec.calls)
        (login, login_in, kw1), (page, page_in, kw2) = rec.calls
        self.assertEqual(login[1:4], ["exec", "-i", "onionheaven"])
        self.assertEqual(login[login.index("--data-binary") + 1], "@-")
        self.assertEqual(login_in, "email=me%40example.org&password=IA-P%40ss%26word")
        self.assertEqual(page[1:4], ["exec", "-i", "onionheaven"])
        self.assertEqual(page[page.index("-H") + 1], "@-")
        self.assertEqual(page_in, "Cookie: logged-in-sig=SIG-SECRET; "
                                  "logged-in-user=someone@example.org\n")
        self.assertEqual(kw1["env"], {"A": "1"})
        for secret in ("IA-P@ss&word", "IA-P%40ss%26word", "SIG-SECRET"):
            rec.assert_not_in_argv(self, secret)

    def test_timeout_is_logged_without_argv(self):
        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv + ["SENTINEL-ARGV"], 45)
        logs = []
        with mock.patch.object(multisite.subprocess, "run", boom):
            self.assertIsNone(self._fetch("e", "p", logs))
        self.assertEqual(logs, ["WARNING: archive.org login timed out"])

    def test_setup_logs_in_through_the_onion_like_the_reconcile(self):
        """Issue #322: setup went to clearnet archive.org while the
        per-launch reconcile went to its onion. Both are one function now,
        and the proxy's /setup has no login of its own (issue #481)."""
        rec = _Recorder(lambda argv, stdin: (0, LOGIN_WITH_KEYS))
        with mock.patch.object(multisite.subprocess, "run", rec):
            keys = self._fetch("me@example.org", "pw", [])
        self.assertEqual(keys, {"access": "AK", "secret": "S3-SECRET"})
        [(login, _, _)] = rec.calls
        self.assertEqual(login[-1], multisite._ARCHIVE_ONION + "/services/xauthn/?op=login")
        for path in ("src/onionpress/onion_proxy.py", "src/onionpress/multisite.py"):
            with self.subTest(path=path):
                self.assertNotIn("https://archive.org", _read(path))


class TestOnionProxySetupPost(unittest.TestCase):
    """The proxy's /setup form goes through setup_logic.install_fresh_wordpress
    (issue #481): core install, then the password on stdin."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="setup-proxy-argv-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # The background archive.org fetch would outlive the mock; the
        # plugin setup would start a launcher.
        for p in (mock.patch("threading.Thread"),
                  mock.patch.object(setup_logic, "start_bundled_plugin_setup",
                                    return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def _post(self, password, reply=lambda argv, stdin: (0, ""), run=None):
        from urllib.parse import urlencode
        body = urlencode({"blog_title": "My Blog", "user_name": "alice",
                          "admin_password": password}).encode()
        logs, sent = [], []
        h = _handler(logs, data_dir=self.tmpdir)
        # Not installed yet: the form is refused on an installed site.
        installed = iter([False, True])
        h._wp_installed = lambda: next(installed)
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.send_response = sent.append
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        h.send_error = lambda *a: sent.append(a)
        rec = _Recorder(reply)
        with mock.patch.object(onion_proxy.subprocess, "run", run or rec):
            h._handle_setup_post()
        return rec, logs, sent, h.wfile.getvalue().decode()

    def test_password_is_stdin_only(self):
        pw = TRICKY_PASSWORDS[1]
        rec, logs, sent, page = self._post(pw)
        self.assertEqual(sent, [200], page)
        rec.assert_not_in_argv(self, pw)
        [install] = [a for a, _, _ in rec.calls if "--admin_user=alice" in a]
        self.assertIn("core", install)
        self.assertFalse([a for a in install if "admin_password" in a])
        [(argv, stdin)] = rec.eval_calls()
        self.assertEqual(argv[:9], ["/x/docker"] + WP_AS_WWW_DATA)
        self.assertEqual(stdin, f"alice\n{pw}")
        # The proxy's docker environment reaches every docker call.
        for argv, _, kwargs in rec.calls:
            if argv[0] == "/x/docker":
                self.assertEqual(kwargs.get("env"), {"A": "1"}, argv)
        for msg in logs:
            self.assertNotIn(pw, msg)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, ".setup_complete")))

    def test_failed_password_set_is_not_reported_as_success(self):
        def reply(argv, stdin):
            return (1, "") if setup_logic.WP_SET_PASSWORD_PHP in argv else (0, "")
        _, logs, sent, page = self._post("hunter22", reply)
        self.assertEqual(sent, [500])
        self.assertIn("admin password could not be set", page)
        self.assertTrue(any("ERROR: setting the admin password failed" in m
                            for m in logs), logs)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, ".setup_complete")))

    def test_install_timeout_is_reported_without_argv(self):
        def run(argv, **kwargs):
            if "site" in argv and "create" in argv:
                raise subprocess.TimeoutExpired(argv + ["SENTINEL-ARGV"], 30)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        _, logs, sent, page = self._post("hunter22", run=run)
        self.assertEqual(sent, [500])
        self.assertIn("Setup error: TimeoutExpired", logs)
        self.assertNotIn("SENTINEL-ARGV", page + "".join(logs))


# ─── onionnames registrar ────────────────────────────────────────────────


class TestRegistrarDefaultRunner(unittest.TestCase):

    def test_post_body_goes_on_stdin(self):
        rec = _Recorder(lambda argv, stdin: (0, '{"ok": true}\n__HTTP_STATUS__:200'))
        reg = onionnames_registrar.Registrar(
            docker_bin="/x/docker",
            key_source=lambda: (b"\x01" * 64, b"\x02" * 32))
        with mock.patch.object(onionnames_registrar.subprocess, "run", rec), \
                mock.patch.object(onionnames_registrar.onion_auth,
                                  "sign_name_payload", return_value="SIG-SECRET"):
            self.assertTrue(reg.register("alice", "x.onion").ok)
            self.assertTrue(reg.check("alice").ok)
        (post, post_in, _), (get, get_in, _) = rec.calls
        self.assertEqual(post[:4], ["/x/docker", "exec", "-i", "onionpress-tor"])
        self.assertEqual(post[post.index("--data-binary") + 1], "@-")
        self.assertEqual(json.loads(post_in)["signature"], "SIG-SECRET")
        rec.assert_not_in_argv(self, "SIG-SECRET")
        # Unsigned GETs keep a plain docker exec.
        self.assertEqual(get[:3], ["/x/docker", "exec", "onionpress-tor"])
        self.assertIsNone(get_in)


# ─── The launchers ───────────────────────────────────────────────────────


class _LauncherHarness(unittest.TestCase):
    """Runs launcher functions under bash (set -e, like the launchers) with
    stub `docker` and `curl` on PATH. Each stub call appends {"tool",
    "argv", "stdin"} to $STUB_CALLS; stdin is read for `docker exec -i` and
    for curl with `@-`. `wp core multisite-install` prints a generated
    password the way wp-cli does; `wp eval` exits $STUB_EVAL_RC. A curl
    run in a container (`docker exec -i <c> curl ...`) answers like curl."""

    STUB = r'''
import json, os, sys
tool, args = sys.argv[1], sys.argv[2:]
reads_stdin = (tool == "docker" and args[:2] == ["exec", "-i"]) or \
    (tool == "curl" and any(a.endswith("@-") for a in args))
data = sys.stdin.buffer.read().decode("utf-8") if reads_stdin else None
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"tool": tool, "argv": args, "stdin": data}) + "\n")
if tool == "docker" and args[:2] == ["exec", "-i"] and args[3:4] == ["curl"]:
    tool, args = "curl", args[4:]
if tool == "docker":
    if "multisite-install" in args:
        print("Success: Network installed.")
        print("Admin password: GENERATED-BY-WP-CLI")
    if "eval" in args:
        rc = int(os.environ.get("STUB_EVAL_RC", "0"))
        if rc:
            print("Error: no such user, or an empty password", file=sys.stderr)
        else:
            print("Success: password set for alice")
        sys.exit(rc)
    sys.exit(0)
if tool == "curl":
    if "-H" in args and "@-" in args:
        print(os.environ["STUB_S3_PAGE"])
    else:
        print(os.environ["STUB_LOGIN"])
'''

    def run_bash(self, rel, functions, snippet, env_extra=None, prelude=""):
        text = _read(rel)
        source = "".join(_function(text, name) + "\n"
                         for name in ("wp_cli", "wp_cli_in") + tuple(functions))
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            stub = os.path.join(tmp, "stub.py")
            with open(stub, "w", encoding="utf-8") as f:
                f.write(self.STUB)
            for tool in ("docker", "curl"):
                path = os.path.join(bin_dir, tool)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" {tool} "$@"\n')
                os.chmod(path, 0o755)
            calls = os.path.join(tmp, "calls")
            log = os.path.join(tmp, "onionpress.log")
            open(calls, "w").close()
            open(log, "w").close()
            env = {
                "PATH": bin_dir + os.pathsep + "/usr/bin:/bin",
                "HOME": tmp, "TMPDIR": tmp, "LOG_FILE": log, "PY": sys.executable,
                "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
                "STUB_CALLS": calls, "STUB_LOGIN": LOGIN_WITH_KEYS,
                "STUB_S3_PAGE": S3_PAGE, "ONIONPRESS_WP_PORT": "8080",
                **(env_extra or {}),
            }
            script = ("set -e\n"
                      'log() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                      'python3() { "$PY" "$@"; }\n'
                      "wp_access_host() { echo localhost; }\n"
                      + prelude + source + snippet + "\n")
            out = subprocess.run(["bash", "-c", script], env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=120)
            with open(calls, encoding="utf-8") as f:
                made = [json.loads(line) for line in f]
            with open(log, encoding="utf-8") as f:
                logged = f.read()
        return out, made, logged

    def assert_not_in_argv(self, made, secret):
        for c in made:
            for arg in c["argv"]:
                self.assertNotIn(secret, arg, f"secret in argv: {c}")

    @staticmethod
    def eval_calls(made):
        return [c for c in made if c["tool"] == "docker" and "eval" in c["argv"]]


class TestWpSetUserPassword(_LauncherHarness):

    def test_identical_in_both_launchers_and_matches_python(self):
        mac, linux = (_function(_read(r), "wp_set_user_password") for r in LAUNCHERS)
        self.assertEqual(mac, linux)
        self.assertIn("| wp_cli_in eval '" + setup_logic.WP_SET_PASSWORD_PHP + "'\n}",
                      mac)
        self.assertNotIn("allow-root", mac)

    def test_password_reaches_php_on_stdin_only(self):
        for rel in LAUNCHERS:
            for pw in TRICKY_PASSWORDS:
                with self.subTest(rel=rel, pw=pw):
                    out, made, _ = self.run_bash(
                        rel, ["wp_set_user_password"],
                        'wp_set_user_password alice "$PW"', {"PW": pw})
                    self.assertEqual(out.returncode, 0, out.stderr)
                    [call] = made
                    self.assertEqual(call["argv"], WP_AS_WWW_DATA + [
                        "eval", setup_logic.WP_SET_PASSWORD_PHP])
                    self.assertEqual(call["stdin"], f"alice\n{pw}")


class TestLauncherSetup(_LauncherHarness):
    """Mac setup_wordpress_cli and Linux setup_wordpress_interactive."""

    CASES = (
        (MAC, "setup_wordpress_cli"),
        (LINUX, "setup_wordpress_interactive"),
    )

    def _setup(self, rel, fn, pw, extra_args="", env=None, prelude=""):
        return self.run_bash(
            rel, ["wp_set_user_password", fn],
            f'rc=0; {fn} --user alice --pass "$PW" --title T {extra_args} || rc=$?; '
            'echo "rc=$rc"',
            {"PW": pw, **(env or {})}, prelude)

    def test_install_then_password_on_stdin(self):
        for rel, fn in self.CASES:
            for pw in TRICKY_PASSWORDS:
                with self.subTest(rel=rel, pw=pw):
                    out, made, logged = self._setup(rel, fn, pw)
                    self.assertEqual(out.stdout.strip().splitlines()[-1], "rc=0",
                                     out.stdout + out.stderr)
                    self.assert_not_in_argv(made, pw)
                    [install] = [c for c in made if "multisite-install" in c["argv"]]
                    self.assertIn("--admin_user=alice", install["argv"])
                    self.assertFalse([a for a in install["argv"] if "admin_password" in a])
                    [call] = self.eval_calls(made)
                    self.assertEqual(call["stdin"], f"alice\n{pw}")
                    self.assertGreater(made.index(call), made.index(install))
                    # wp-cli's generated password is not kept anywhere.
                    self.assertNotIn("GENERATED-BY-WP-CLI", logged)
                    self.assertNotIn("GENERATED-BY-WP-CLI", out.stdout)
                    self.assertIn("WordPress installed successfully!", out.stdout)

    def test_failed_password_set_fails_setup(self):
        for rel, fn in self.CASES:
            with self.subTest(rel=rel):
                out, _, logged = self._setup(rel, fn, "hunter22",
                                             env={"STUB_EVAL_RC": "1"})
                self.assertEqual(out.stdout.strip().splitlines()[-1], "rc=1")
                self.assertIn("ERROR: setting the WordPress admin password failed",
                              logged)
                self.assertNotIn("WordPress installed successfully!", out.stdout)

    def test_linux_s3_keys_go_on_stdin(self):
        prelude = ('fetch_archive_s3_keys() { ia_s3_access=AK; '
                   'ia_s3_secret=S3-SECRET; }\n')
        out, made, _ = self._setup(
            LINUX, "setup_wordpress_interactive", "hunter22",
            '--ia-account me@example.org --ia-password "$IA_PW"',
            {"IA_PW": "IA-SECRET"}, prelude)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "rc=0", out.stderr)
        options = {c["argv"][c["argv"].index("update") + 1]: c["stdin"]
                   for c in made if "option" in c["argv"]}
        self.assertEqual(options, {"onionpress_archive_s3_access": "AK",
                                   "onionpress_archive_s3_secret": "S3-SECRET"})
        for secret in ("S3-SECRET", "IA-SECRET", "hunter22"):
            self.assert_not_in_argv(made, secret)


class TestLinuxArchiveCookieFallback(_LauncherHarness):

    def test_session_cookie_goes_to_curl_on_stdin(self):
        out, made, _ = self.run_bash(
            LINUX, ["fetch_archive_s3_keys"],
            'fetch_archive_s3_keys me@example.org "$PW"; '
            'echo "$ia_s3_access $ia_s3_secret"',
            {"PW": "IA-SECRET", "STUB_LOGIN": LOGIN_WITH_COOKIES})
        self.assertEqual(out.stdout.strip(), "AK S3-SECRET", out.stderr)
        login, page = made
        self.assertEqual(login["stdin"], "IA-SECRET")
        self.assertEqual(page["argv"][-3:-1], ["-H", "@-"])
        self.assertEqual(page["stdin"], "Cookie: logged-in-sig=SIG-SECRET; "
                                        "logged-in-user=someone@example.org\n")
        for secret in ("IA-SECRET", "SIG-SECRET"):
            self.assert_not_in_argv(made, secret)


class TestLinuxArchiveLoginOverTor(_LauncherHarness):
    """The Linux setup's archive.org login runs curl in the onionheaven
    container through its Tor SOCKS port, to archive.org's onion, as the
    Mac's does (multisite.ensure_archive_s3_keys): from the host it would
    tie the publisher's IP address to their archive.org account."""

    ONION = re.search(r"https://archive[a-z2-7]{49}\.onion",
                      _read("src/onionpress/multisite.py")).group(0)

    def test_no_curl_runs_on_the_host(self):
        for stub_login in (LOGIN_WITH_KEYS, LOGIN_WITH_COOKIES):
            with self.subTest(login=stub_login):
                out, made, _ = self.run_bash(
                    LINUX, ["fetch_archive_s3_keys"],
                    'fetch_archive_s3_keys me@example.org "$PW"; '
                    'echo "$ia_s3_access $ia_s3_secret"',
                    {"PW": "IA-SECRET", "STUB_LOGIN": stub_login})
                self.assertEqual(out.stdout.strip(), "AK S3-SECRET", out.stderr)
                self.assertTrue(made)
                for call in made:
                    self.assertEqual(call["tool"], "docker", call)
                    argv = call["argv"]
                    self.assertEqual(argv[:6], ["exec", "-i", "onionheaven", "curl",
                                                "-sk", "--socks5-hostname"])
                    self.assertEqual(argv[6], "127.0.0.1:9050")
                    self.assertTrue(argv[-1].startswith(self.ONION + "/"), argv[-1])

    def test_function_names_no_clearnet_archive_org(self):
        text = _read(LINUX)
        body = _function(text, "fetch_archive_s3_keys")
        self.assertNotIn("https://archive.org", body)
        self.assertIn(self.ONION, body)

    def test_unreachable_tor_fails_with_a_log_line(self):
        out, _, logged = self.run_bash(
            LINUX, ["fetch_archive_s3_keys"],
            'rc=0; fetch_archive_s3_keys me@example.org pw || rc=$?; echo "rc=$rc"',
            prelude='docker() { return 1; }\n')
        self.assertEqual(out.stdout.strip(), "rc=1", out.stderr)
        self.assertIn("could not reach archive.org's onion service through Tor", logged)


if __name__ == "__main__":
    unittest.main()
