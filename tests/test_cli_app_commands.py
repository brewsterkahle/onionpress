#!/usr/bin/env python3
"""The app's remaining functions as `onionpress` subcommands (issue #382,
src/onionpress/app_commands.py): version, address --url/--copy,
check-reachability, logs --app/--launcher/--web, open, onionname show|set
each against a fake docker and a sandbox data
dir; the launchers' import-key --file under bash; and the literals the
Mac daemon and the Linux tray share with the module."""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import app_commands, config, daemon, key_manager, onionheaven  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402
from onionpress.onionnames_registrar import RegistrarResult  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

ADDR = "a" * 56 + ".onion"
HOSTNAME_FILE = "/var/lib/tor/hidden_service/wordpress/hostname"


class FakeDocker:
    """answers: {"<args joined>": stdout} (missing: rc 1); calls recorded."""

    docker_bin = "/stub/docker"
    env = {"DOCKER_HOST": "unix:///stub"}

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def run(self, args, timeout=30, quiet=False, **kw):
        key = " ".join(str(a) for a in args)
        self.calls.append(key)
        if key in self.answers:
            return DockerResult(0, self.answers[key], "")
        return DockerResult(1, "", "no such container")

    def exec(self, container, command, timeout=30, quiet=False, **kw):
        return self.run(["exec", container, *command], timeout=timeout, quiet=quiet)


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def with_address(answers=None):
    return FakeDocker({f"exec onionpress-tor cat {HOSTNAME_FILE}": ADDR + "\n",
                       **(answers or {})})


class Sandbox(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-appcmd-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = resolve_paths(data_dir=self.dir, app_bundle="")
        self.out = io.StringIO()
        self.err = io.StringIO()
        self._err = mock.patch.object(sys, "stderr", self.err)
        self._err.start()
        self.addCleanup(self._err.stop)
        self.logged = []

    def log(self, msg):
        self.logged.append(msg)

    def config(self, **values):
        with open(self.paths.config_file, "w") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


class TestVersion(Sandbox):

    def test_text_and_json(self):
        from onionpress import __version__
        self.assertEqual(app_commands.version_command("macos", out=self.out), 0)
        self.assertEqual(self.out.getvalue(), f"OnionPress {__version__}\n")
        out = io.StringIO()
        app_commands.version_command("linux", json_output=True, out=out)
        self.assertEqual(json.loads(out.getvalue()), {"version": __version__, "platform": "linux"})


class TestAddress(Sandbox):

    def test_the_served_address_bare(self):
        rc = app_commands.address_command(self.paths, with_address(), "linux", out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, ADDR + "\n"))

    def test_url_carries_the_onionname_path_and_no_token(self):
        self.config(ONIONNAME="alice")
        rc = app_commands.address_command(self.paths, with_address(), "linux", url=True,
                                          out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, f"http://{ADDR}/alice/\n"))
        self.assertNotIn("op_login", self.out.getvalue())

    def test_url_without_an_onionname(self):
        app_commands.address_command(self.paths, with_address(), "linux", url=True, out=self.out)
        self.assertEqual(self.out.getvalue(), f"http://{ADDR}/\n")

    def test_falls_back_to_the_daemons_cache_then_fails(self):
        docker = FakeDocker()
        rc = app_commands.address_command(self.paths, docker, "linux", out=self.out)
        self.assertEqual(rc, 1)
        self.assertIn("No onion address", self.err.getvalue())
        with open(os.path.join(self.dir, app_commands.ADDRESS_CACHE), "w") as f:
            f.write(ADDR + "\n")
        rc = app_commands.address_command(self.paths, docker, "linux", out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, ADDR + "\n"))

    def test_a_damaged_hostname_is_not_an_address(self):
        docker = FakeDocker({f"exec onionpress-tor cat {HOSTNAME_FILE}": "not-an-address\n"})
        self.assertEqual(app_commands.served_address(docker, self.dir), "")

    def test_copy_uses_the_first_clipboard_tool_found(self):
        self.config(ONIONNAME="alice")
        runs = []

        def fake_run(argv, input=None, **kw):
            runs.append((argv, input))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(app_commands.shutil, "which",
                               side_effect=lambda n: "/usr/bin/xclip" if n == "xclip" else None), \
                mock.patch.object(app_commands.subprocess, "run", side_effect=fake_run):
            rc = app_commands.address_command(self.paths, with_address(), "linux", copy=True,
                                              out=self.out)
        self.assertEqual(rc, 0)
        self.assertEqual(runs, [(["xclip", "-selection", "clipboard"],
                                 f"http://{ADDR}/alice/".encode())])
        self.assertEqual(self.out.getvalue(), f"http://{ADDR}/alice/\n")
        self.assertIn("xclip", self.err.getvalue())

    def test_mac_copies_with_pbcopy(self):
        self.assertEqual(app_commands.clipboard_tools("macos"), [["pbcopy"]])

    def test_copy_without_a_tool_prints_the_url_and_fails(self):
        with mock.patch.object(app_commands.shutil, "which", return_value=None):
            rc = app_commands.address_command(self.paths, with_address(), "linux", copy=True,
                                              out=self.out)
        self.assertEqual(rc, 1)
        self.assertEqual(self.out.getvalue(), f"http://{ADDR}/\n")
        self.assertIn("wl-copy", self.err.getvalue())


CURL = (f"exec onionheaven curl -s -o /dev/null -w %{{http_code}} --max-time 60 "
        f"--socks5-hostname 127.0.0.1:9050 http://{ADDR}/")


class TestCheckReachability(Sandbox):

    def test_reachable(self):
        docker = with_address({CURL: "200"})
        rc = app_commands.check_reachability_command(self.paths, docker, out=self.out)
        self.assertEqual(rc, 0)
        self.assertIn("Reachable", self.out.getvalue())
        self.assertIn(CURL, docker.calls)

    def test_not_reachable_json(self):
        docker = with_address({CURL: "000"})
        rc = app_commands.check_reachability_command(self.paths, docker, json_output=True,
                                                     out=self.out)
        self.assertEqual(rc, 1)
        result = json.loads(self.out.getvalue())
        self.assertEqual((result["reachable"], result["http_code"], result["address"]),
                         (False, "000", ADDR))
        self.assertTrue(result["tested_at"].endswith("Z"))

    def test_a_failed_curl_is_000(self):
        docker = with_address()   # curl's exec fails
        result = app_commands.reachability_test(docker, ADDR)
        self.assertEqual((result["reachable"], result["http_code"]), (False, "000"))

    def test_no_address_is_exit_2(self):
        rc = app_commands.check_reachability_command(self.paths, FakeDocker(), json_output=True,
                                                     out=self.out)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.out.getvalue())["reachable"], False)

    def test_the_settings_page_runs_the_same_probe(self):
        from onionpress import handle_action
        body = _read("src/onionpress/handle_action.py")
        branch = body[body.index("def _check_reachability("):body.index("# -- key import --")]
        self.assertIn("reachability_test(host.docker, addr)", branch)
        self.assertNotIn("curl", branch)
        self.assertTrue(hasattr(handle_action, "_check_reachability"))


class TestLogs(Sandbox):

    def _write(self, rel, lines):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("".join(f"{line}\n" for line in lines))
        return path

    def test_launcher_log_last_lines(self):
        self._write("onionpress.log", [f"line {i}" for i in range(10)])
        rc = app_commands.logs_command(self.paths, FakeDocker(), "linux", "launcher", lines=3,
                                       out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, "line 7\nline 8\nline 9\n"))

    def test_app_log_is_the_daemons_on_a_mac_and_the_flat_file_on_linux(self):
        self._write("onionpress.log", ["flat"])
        self.assertEqual(app_commands.log_file_for(self.paths, "app", "linux"),
                         self.paths.log_file)
        # No daemon log yet: the flat file
        self.assertEqual(app_commands.log_file_for(self.paths, "app", "macos"),
                         self.paths.log_file)
        rotating = self._write("logs/onionpress-2026-10-25-001.log", ["daemon"])
        self.assertEqual(app_commands.log_file_for(self.paths, "app", "macos"), rotating)

    def test_zero_means_all_and_a_missing_log_fails(self):
        self._write("onionpress.log", ["a", "b"])
        rc = app_commands.logs_command(self.paths, FakeDocker(), "linux", "app", lines=0,
                                       out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, "a\nb\n"))
        rc = app_commands.logs_command(self.paths, FakeDocker(), "macos", "launcher",
                                       out=io.StringIO())
        self.assertEqual(rc, 0)
        os.unlink(self.paths.log_file)
        rc = app_commands.logs_command(self.paths, FakeDocker(), "linux", "launcher",
                                       out=self.out)
        self.assertEqual(rc, 1)
        self.assertIn("No launcher log yet", self.err.getvalue())

    def test_follow_runs_tail(self):
        path = self._write("onionpress.log", ["x"])
        with mock.patch.object(app_commands.subprocess, "call", return_value=0) as call:
            rc = app_commands.logs_command(self.paths, FakeDocker(), "linux", "launcher",
                                           lines=20, follow=True, out=self.out)
        self.assertEqual(rc, 0)
        call.assert_called_once_with(["tail", "-n", "20", "-F", path])

    def test_web_log_prefers_the_daemons_capture(self):
        self._write("logs/wordpress-visitors-2026-10-25-001.log", ["GET /"])
        rc = app_commands.logs_command(self.paths, FakeDocker(), "macos", "web", out=self.out)
        self.assertEqual((rc, self.out.getvalue()), (0, "GET /\n"))

    def test_web_log_from_the_container_drops_the_apps_probes(self):
        docker = FakeDocker({"logs --tail 200 onionpress-wordpress":
                             'GET / "Mozilla"\nGET /.onionpress-health "OnionPress-HealthCheck"\n'
                             'GET /post/ "Mozilla"'})
        rc = app_commands.logs_command(self.paths, docker, "linux", "web", out=self.out)
        self.assertEqual(rc, 0)
        self.assertEqual(self.out.getvalue(), 'GET / "Mozilla"\nGET /post/ "Mozilla"')
        # The daemon's capture applies the same rule.
        src = _read("src/onionpress/daemon.py")
        self.assertIn(f'"{app_commands.HEALTHCHECK_LOG_MARK}" not in line', src)

    def test_web_log_without_the_container(self):
        rc = app_commands.logs_command(self.paths, FakeDocker(), "linux", "web", out=self.out)
        self.assertEqual(rc, 1)
        self.assertIn("not running", self.err.getvalue())


class TestOpen(Sandbox):

    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "18080"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_targets(self):
        self.config(ONIONNAME="alice")
        docker = with_address()
        self.assertEqual(app_commands.open_target("site", self.paths, docker),
                         ("http://localhost:18080/alice/", False))
        self.assertEqual(app_commands.open_target("admin", self.paths, docker),
                         ("http://localhost:18080/wp-admin/", False))
        self.assertEqual(app_commands.open_target("settings", self.paths, docker),
                         ("http://localhost:18080/wp-admin/admin.php?page=onionpress-settings",
                          False))
        self.assertEqual(app_commands.open_target("onion", self.paths, docker),
                         (f"http://{ADDR}/alice/", True))

    def test_site_without_an_onionname(self):
        self.assertEqual(app_commands.open_target("site", self.paths, FakeDocker()),
                         ("http://localhost:18080/", False))

    def test_the_port_comes_from_the_running_stack_without_the_export(self):
        os.environ.pop("ONIONPRESS_WP_PORT")
        with mock.patch.object(config, "resolve_port_offset",
                               return_value=mock.Mock(wp_port=28080)):
            self.assertEqual(app_commands.open_target("admin", self.paths, FakeDocker())[0],
                             "http://localhost:28080/wp-admin/")

    def test_open_hands_the_browser_the_login_page_not_the_token(self):
        with mock.patch.object(app_commands.launcher_ops, "login_target",
                               return_value="file:///home/x/OnionPress/.auto-login/login-1.html") as lt, \
                mock.patch.object(app_commands.launcher_ops, "open_in_browser") as ob:
            rc = app_commands.open_command("onion", self.paths, with_address(), out=self.out)
        self.assertEqual(rc, 0)
        lt.assert_called_once_with(f"http://{ADDR}/")
        ob.assert_called_once_with("file:///home/x/OnionPress/.auto-login/login-1.html",
                                   prefer_tor=True)
        self.assertEqual(self.out.getvalue(), f"Opening http://{ADDR}/\n")

    def test_onion_without_an_address_fails(self):
        with mock.patch.object(app_commands.launcher_ops, "open_in_browser") as ob:
            rc = app_commands.open_command("onion", self.paths, FakeDocker(), out=self.out)
        self.assertEqual(rc, 1)
        ob.assert_not_called()
        self.assertIn("No onion address", self.err.getvalue())

    def test_mac_tor_browser_branch(self):
        from onionpress import browser, launcher_ops
        self.assertIn(browser.TOR_BROWSER_PATH, launcher_ops.TOR_BROWSER_APPS)
        with mock.patch.object(launcher_ops.sys, "platform", "darwin"), \
                mock.patch.object(launcher_ops, "_tor_browser_running", return_value=None), \
                mock.patch.object(launcher_ops.shutil, "which", return_value=None), \
                mock.patch.object(launcher_ops.os.path, "isdir",
                                  side_effect=lambda p: p == browser.TOR_BROWSER_PATH), \
                mock.patch.object(launcher_ops.subprocess, "Popen") as popen:
            launcher_ops.open_in_browser("file:///x.html", prefer_tor=True)
        self.assertEqual(popen.call_args.args[0],
                         ["open", "-a", browser.TOR_BROWSER_PATH, "file:///x.html"])


class StubRegistrar:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def register(self, name, address):
        self.calls.append((name, address))
        return self.result


class TestOnionname(Sandbox):

    def test_show(self):
        rc = app_commands.onionname_show(self.paths, out=self.out)
        self.assertEqual(rc, 0)
        self.assertIn("No onionname is set", self.out.getvalue())
        self.config(ONIONNAME="alice", ONIONNAME_ADDRESS=ADDR, ONIONNAME_REGISTERED="yes")
        out = io.StringIO()
        app_commands.onionname_show(self.paths, json_output=True, out=out)
        self.assertEqual(json.loads(out.getvalue()),
                         {"name": "alice", "address": ADDR, "registered": True,
                          "url": f"http://{ADDR}/alice/"})

    def test_set_registers_and_records(self):
        reg = StubRegistrar(RegistrarResult(status="ok"))
        rc = app_commands.onionname_set("bobby", self.paths, with_address(), "macos", self.log,
                                        out=self.out, registrar=reg)
        self.assertEqual(rc, 0, self.out.getvalue())
        self.assertEqual(reg.calls, [("bobby", ADDR)])
        self.assertEqual(config.read_config(self.paths.config_file),
                         {"ONIONNAME": "bobby", "ONIONNAME_ADDRESS": ADDR,
                          "ONIONNAME_REGISTERED": "yes"})
        self.assertIn(f"http://{ADDR}/bobby/", self.out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.dir, app_commands.ONIONNAME_RETRY_MARKER)))

    def test_a_taken_name_is_refused_with_suggestions_and_not_recorded(self):
        self.config(ONIONNAME="alice", ONIONNAME_REGISTERED="yes", ONIONNAME_ADDRESS=ADDR)
        reg = StubRegistrar(RegistrarResult(status="collision", suggestions=["bobby2", "bobby3"]))
        rc = app_commands.onionname_set("bobby", self.paths, with_address(), "linux", self.log,
                                        json_output=True, out=self.out, registrar=reg)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.out.getvalue()),
                         {"error": "taken", "name": "bobby", "suggestions": ["bobby2", "bobby3"]})
        self.assertEqual(config.read_value(self.paths.config_file, "ONIONNAME"), "alice")

    def test_an_invalid_name_is_refused_before_anything(self):
        reg = StubRegistrar(RegistrarResult(status="ok"))
        rc = app_commands.onionname_set("x", self.paths, with_address(), "linux", self.log,
                                        out=self.out, registrar=reg)
        self.assertEqual(rc, 2)
        self.assertEqual(reg.calls, [])
        self.assertFalse(os.path.exists(self.paths.config_file))

    def test_not_running_records_and_the_mac_daemon_retries(self):
        reg = StubRegistrar(RegistrarResult(status="ok"))
        rc = app_commands.onionname_set("carol", self.paths, FakeDocker(), "macos", self.log,
                                        json_output=True, out=self.out, registrar=reg)
        self.assertEqual(rc, 0)
        self.assertEqual(reg.calls, [])
        answer = json.loads(self.out.getvalue())
        self.assertEqual((answer["ok"], answer["name"], answer["registered"]), (True, "carol", False))
        self.assertIn("start OnionPress", answer["note"])
        self.assertEqual(config.read_value(self.paths.config_file, "ONIONNAME_REGISTERED"), "no")
        self.assertTrue(os.path.exists(os.path.join(self.dir, app_commands.ONIONNAME_RETRY_MARKER)))

    def test_hub_not_answering_records_unregistered(self):
        reg = StubRegistrar(RegistrarResult(status="unreachable"))
        rc = app_commands.onionname_set("davey", self.paths, with_address(), "linux", self.log,
                                        out=self.out, registrar=reg)
        self.assertEqual(rc, 0)
        self.assertIn("did not answer", self.out.getvalue())
        self.assertEqual(config.read_value(self.paths.config_file, "ONIONNAME"), "davey")
        self.assertEqual(config.read_value(self.paths.config_file, "ONIONNAME_REGISTERED"), "no")
        self.assertFalse(os.path.exists(os.path.join(self.dir, app_commands.ONIONNAME_RETRY_MARKER)))

    def test_the_markers_are_the_daemons(self):
        self.assertEqual(app_commands.ONIONNAME_RETRY_MARKER, daemon.ONIONNAME_RETRY_MARKER)


# ---- import-key --file, under bash ----

def expanded_key(seed):
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] = (h[31] & 127) | 64
    return bytes(h)


KEY = expanded_key(b"a key for --file")
KEY_ADDR = key_manager.derive_onion_address(key_manager.derive_public_key(KEY))
CTOR_FILE = b"== ed25519v1-secret: type0 ==\x00\x00\x00" + KEY


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


class TestEncodeKeyFile(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-keyfile-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _file(self, data, name="hs_ed25519_secret_key"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_ctor_file_and_bare_key_decode_to_the_address(self):
        for data in (CTOR_FILE, KEY):
            b32 = key_manager.encode_key_file(self._file(data))
            self.assertEqual(key_manager.decode_base32_key(b32), KEY)
            self.assertEqual(b32, base64.b32encode(data).decode())

    def test_a_wrong_size_or_unusable_key_is_refused(self):
        with self.assertRaises(ValueError):
            key_manager.encode_key_file(self._file(b"x" * 10))
        bad = bytearray(KEY)
        bad[31] |= 128   # the top bit Tor clears
        with self.assertRaises(ValueError):
            key_manager.encode_key_file(self._file(bytes(bad)))

    def test_the_script_subcommand(self):
        r = subprocess.run([sys.executable, os.path.join(PROJECT_ROOT, "src", "onionpress",
                                                         "key_manager.py"),
                            "encode-key-file", self._file(CTOR_FILE)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), base64.b32encode(CTOR_FILE).decode())
        r = subprocess.run([sys.executable, os.path.join(PROJECT_ROOT, "src", "onionpress",
                                                         "key_manager.py"),
                            "encode-key-file", self._file(b"short")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("96 bytes", r.stderr)


class TestImportKeyFileFlag(unittest.TestCase):
    """Both launchers' import_key take --file <hs_ed25519_secret_key> (and
    Linux's `-` on stdin now too): the key reaches replace_onion_key as
    the file's base32, after key_manager's check."""

    PRELUDE = r'''
log() { echo "[log] $*" >> "$LOG_FILE"; }
bundled_python() { "$PY" "$@"; }
python3() { "$PY" "$@"; }
replace_onion_key() { printf '%s\n' "$1" >> "$CALLS"; return 0; }
start_heartbeat_client() { :; }
docker() { return 1; }
'''

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-ikfile-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.key_file = os.path.join(self.dir, "hs_ed25519_secret_key")
        with open(self.key_file, "wb") as f:
            f.write(CTOR_FILE)
        self.calls = os.path.join(self.dir, "calls")

    def _run(self, launcher, args, stdin=""):
        text = _read(launcher)
        funcs = "\n".join(_shell_function(text, n) for n in ("is_onion_address", "import_key"))
        script = self.PRELUDE + funcs + f"\nimport_key {args}\n"
        env = {**os.environ, "PY": sys.executable,
               "SCRIPTS_DIR": os.path.join(PROJECT_ROOT, "src", "onionpress"),
               "DATA_DIR": os.path.join(self.dir, "data"), "LOG_FILE": os.path.join(self.dir, "log"),
               "CALLS": self.calls, "_BUNDLED_PYTHONHOME": "", "_BUNDLED_PYTHON_BIN": sys.executable}
        env.pop("ONIONPRESS_CONFIRMED", None)
        return subprocess.run(["bash", "-c", script], input=stdin, capture_output=True,
                              text=True, timeout=120, env=env)

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().split()

    def test_mac(self):
        r = self._run("app/MacOS/onionpress", f'--file "{self.key_file}" --json')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._calls(), [base64.b32encode(CTOR_FILE).decode()])
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                         {"ok": True, "address": KEY_ADDR})

    def test_linux(self):
        r = self._run("linux/onionpress", f'--file "{self.key_file}"')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._calls(), [base64.b32encode(CTOR_FILE).decode()])
        self.assertIn(f"Imported key for: {KEY_ADDR}", r.stdout)

    def test_linux_reads_the_key_from_stdin(self):
        r = self._run("linux/onionpress", "-", stdin=base64.b32encode(KEY).decode() + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._calls(), [base64.b32encode(KEY).decode()])

    def test_a_bad_file_is_refused_before_anything(self):
        with open(self.key_file, "wb") as f:
            f.write(b"garbage")
        for launcher in ("app/MacOS/onionpress", "linux/onionpress"):
            with self.subTest(launcher=launcher):
                r = self._run(launcher, f'--file "{self.key_file}"')
                self.assertEqual(r.returncode, 1)
                self.assertIn("96 bytes", r.stderr)
                self.assertEqual(self._calls(), [])

    def test_the_launchers_list_the_new_commands(self):
        mac = _read("app/MacOS/onionpress")
        linux = _read("linux/onionpress")
        for name in ("version", "check-reachability", "open", "onionname"):
            self.assertIn(f"  {name}", mac + "\n")
            self.assertIn(name + "|", linux)
        self.assertIn('--data-dir "$DATA_DIR" address "${@:2}"', mac)
        self.assertIn('--data-dir "$DATA_DIR" address "${@:2}"', linux)
        self.assertIn('--data-dir "$DATA_DIR" logs "${@:2}"', mac)
        self.assertIn('--data-dir "$DATA_DIR" logs "${@:2}"', linux)


if __name__ == "__main__":
    unittest.main()
