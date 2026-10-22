#!/usr/bin/env python3
"""Secrets never travel in a host process's argv.

`ps` shows every user's command lines (macOS `ps -axo args`, Linux
/proc/<pid>/cmdline), and several users can run OnionPress on one Mac.
So the onion private key (the heartbeat's and /online's arti_key_pem, and a
key being imported), the OnionHeaven request signatures and the WordPress
database password go to their child processes on stdin instead:

  - curl in onionpress-tor reads the hub payload with --data-binary @-
    (docker exec -i), on the Mac and in the Linux heartbeat daemon;
  - MariaDB clients in onionpress-db read the password from MYSQL_PWD,
    which a sh prelude fills from the first line of stdin (backup.py and
    the Linux launcher's db_client);
  - key_manager.py takes "-" for the key and reads it from stdin, and the
    Mac launcher's import-key takes "-" so the menubar can do the same.

Nothing here talks to Docker: subprocess is mocked, or `docker` is a stub.
"""
import base64
import importlib.util
import json
import os
import re
import secrets
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

from onionpress import backup, key_manager, onion_auth, onionheaven  # noqa: E402

KEY_MANAGER = os.path.join(PROJECT_DIR, "src", "onionpress", "key_manager.py")
HC = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
# Characters a shell or SQL layer could mangle; the launchers generate
# alphanumeric passwords, but wp-config.php's may be anything.
TRICKY_PASSWORD = "p a$s\\w'o\"rd`x"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _shell_function(rel, name):
    """The text of shell function *name* in launcher *rel*."""
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", _read(rel),
                  re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{rel}: {name}() not found")
    return m.group(0)


class _Recorder:
    """Stands in for subprocess.run: records argv and stdin, answers with
    *reply(argv)* (stdout), returncode 0."""

    def __init__(self, reply=lambda argv: ""):
        self.calls = []
        self._reply = reply

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("input")))
        out = self._reply(argv)
        if not kwargs.get("text"):
            out = out.encode()
        return subprocess.CompletedProcess(argv, 0, stdout=out,
                                           stderr="" if kwargs.get("text") else b"")

    def assert_secret_not_in_argv(self, test, secret):
        for argv, _ in self.calls:
            for arg in argv:
                test.assertNotIn(secret, arg, f"secret in argv: {argv}")


def _keypair():
    priv = secrets.token_bytes(64)
    pub = key_manager.derive_public_key(priv)  # sign_expanded takes only a·B
    pem_b64 = base64.b64encode(
        key_manager.build_openssh_key(priv, pub)).decode("ascii")
    return priv, pub, pem_b64


class _FakeApp:
    def __init__(self, onion_address, tmpdir):
        self.onion_address = onion_address
        self.healthcheck_address = HC
        self.version = "test"
        self.is_onionheaven = False
        self.app_support = tmpdir
        self.colima_home = os.path.join(tmpdir, "colima")
        self.bin_dir = os.path.join(tmpdir, "bin")
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def read_config_value(self, key, default=""):
        # Opted in to OnionHeaven: these tests are about what goes out once
        # the user has said yes (tests/test_onionheaven_opt_out.py covers no
        # and unset).
        return "yes" if key == "REGISTER_WITH_ONIONHEAVEN" else default


class TestOnionHeavenPayloadOnStdin(unittest.TestCase):
    """src/onionpress/onionheaven.py: every POST to the hub."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="oh-argv-test-")
        self.priv, self.pub, _ = _keypair()
        self.addr = onion_auth.derive_onion_address(self.pub)
        self.app = _FakeApp(self.addr, self.tmpdir)
        with open(os.path.join(self.tmpdir, "onionheaven-registration.json"), "w") as f:
            json.dump({"registered": True, "content_address": self.addr}, f)
        # build_openssh_key salts each PEM with a random checkint; pin it so
        # the payload can be compared with what was built.
        pem = key_manager.build_openssh_key(self.priv, self.pub)
        for name, value in (("extract_keys", (self.priv, self.pub)),
                            ("build_openssh_key", pem)):
            p = mock.patch.object(key_manager, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.pem_b64 = base64.b64encode(pem).decode("ascii")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _post(self, fn, reply):
        rec = _Recorder(lambda argv: reply)
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            result = fn()
        self.assertTrue(result is None or result, self.app.logs)
        self.assertEqual(len(rec.calls), 1, rec.calls)
        argv, stdin = rec.calls[0]
        self.assertEqual(argv[1:4], ["exec", "-i", "onionpress-tor"])
        i = argv.index("--data-binary")
        self.assertEqual(argv[i + 1], "@-")
        self.assertNotIn("-d", argv)
        payload = json.loads(stdin)
        for arg in argv:
            self.assertNotIn(payload["signature"], arg)
        return rec, payload

    def test_heartbeat_sends_key_on_stdin(self):
        rec, payload = self._post(lambda: onionheaven._send_heartbeat(self.app),
                                  '{"online": true}')
        self.assertEqual(payload["arti_key_pem"], self.pem_b64)
        rec.assert_secret_not_in_argv(self, self.pem_b64)

    def test_online_notification_sends_key_on_stdin(self):
        rec, payload = self._post(
            lambda: onionheaven._send_onionheaven_notification(
                self.app, "online", "online"),
            '{"online": true}')
        self.assertEqual(payload["arti_key_pem"], self.pem_b64)
        rec.assert_secret_not_in_argv(self, self.pem_b64)

    def test_offline_notification_sends_signature_on_stdin(self):
        self._post(lambda: onionheaven.notify_onionheaven_offline(self.app),
                   '{"offline": true}')

    def test_unregister_sends_signature_on_stdin(self):
        self._post(lambda: onionheaven.unregister_from_onionheaven(self.app),
                   '{"unregistered": true}')

    def test_helpers_pass_input_through(self):
        rec = _Recorder(lambda argv: "ok")
        with mock.patch.object(onionheaven.subprocess, "run", rec):
            self.assertEqual(onionheaven._run_docker(self.app, ["x"], input="a"),
                             (True, "ok"))
            self.assertEqual(onionheaven._run_docker_rc(self.app, ["x"], input="b"),
                             (0, "ok"))
            onionheaven._run_docker(self.app, ["x"])
        self.assertEqual([c[1] for c in rec.calls], ["a", "b", None])


_HB_HOME = tempfile.mkdtemp(prefix="op-hb-argv-test-")


def _load_linux_heartbeat():
    """Import linux/onionpress-heartbeat.py under a throwaway HOME (it opens
    ~/.onionpress/onionpress.log at import), as test_linux_analytics_upload
    does."""
    os.makedirs(os.path.join(_HB_HOME, ".onionpress"), exist_ok=True)
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = _HB_HOME
    sys.modules.setdefault("key_manager", key_manager)
    sys.modules.setdefault("onion_auth", onion_auth)
    try:
        spec = importlib.util.spec_from_file_location(
            "onionpress_heartbeat_argv_test",
            os.path.join(PROJECT_DIR, "linux", "onionpress-heartbeat.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = saved_home
    return mod


class TestLinuxHeartbeatPayloadOnStdin(unittest.TestCase):
    """linux/onionpress-heartbeat.py: the docker-exec fallback of
    sign_and_post (first registration, and whenever the persistent
    connection is down)."""

    @classmethod
    def setUpClass(cls):
        cls.hb = _load_linux_heartbeat()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_HB_HOME, ignore_errors=True)

    def test_fallback_sends_key_on_stdin(self):
        priv, pub, pem_b64 = _keypair()
        addr = onion_auth.derive_onion_address(pub)
        rec = _Recorder(lambda argv: '{"online": true}')
        with mock.patch.object(self.hb, "_hub_conn", None), \
                mock.patch.object(self.hb.subprocess, "run", rec):
            ok, resp = self.hb.sign_and_post(
                "online", self.hb.DEFAULT_HUB, addr, HC, priv, pub,
                extra={"arti_key_pem": pem_b64})
        self.assertTrue(ok)
        self.assertEqual(resp, {"online": True})
        self.assertEqual(len(rec.calls), 1)
        argv, stdin = rec.calls[0]
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "onionpress-tor"])
        i = argv.index("--data-binary")
        self.assertEqual(argv[i + 1], "@-")
        self.assertEqual(json.loads(stdin)["arti_key_pem"], pem_b64)
        rec.assert_secret_not_in_argv(self, pem_b64)

    def test_docker_exec_without_input_is_unchanged(self):
        rec = _Recorder(lambda argv: "x")
        with mock.patch.object(self.hb.subprocess, "run", rec):
            self.assertEqual(self.hb.docker_exec("c", ["true"]), (True, "x"))
        self.assertEqual(rec.calls, [(["docker", "exec", "c", "true"], None)])


class TestBackupDbPasswordOnStdin(unittest.TestCase):
    """src/onionpress/backup.py: every MariaDB client run in onionpress-db."""

    CREDS = {"name": "wordpress", "user": "wordpress", "password": TRICKY_PASSWORD}

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="backup-argv-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _fake_client(self):
        path = os.path.join(self.tmpdir, "fake-client")
        with open(path, "w") as f:
            f.write('#!/bin/sh\nprintf "pwd=%s\\n" "$MYSQL_PWD"\n'
                    'for a in "$@"; do printf "arg=%s\\n" "$a"; done\n')
        os.chmod(path, 0o755)
        return path

    def test_prelude_hands_stdin_line_to_mysql_pwd(self):
        """Run what docker exec would run in the container, here."""
        argv = backup._db_client_argv(self._fake_client(), "-u", "wordpress",
                                      "wordpress")
        self.assertEqual(argv[:5], ["docker", "exec", "-i", "onionpress-db", "sh"])
        # The mariadb image's /bin/sh is dash; try it too where it exists.
        for shell in filter(None, ("sh", shutil.which("dash"))):
            with self.subTest(shell=shell):
                out = subprocess.run([shell] + argv[5:], input=TRICKY_PASSWORD + "\n",
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(out.stdout.splitlines(), [
                    "pwd=" + TRICKY_PASSWORD,
                    "arg=-u", "arg=wordpress", "arg=wordpress"])

    def test_workload_stats_query(self):
        rec = _Recorder(lambda argv: "1\t2\t3\t4\t5\t6\n")
        with mock.patch.object(backup.subprocess, "run", rec):
            stats = backup._query_workload_stats(self.CREDS)
        self.assertEqual(stats["db_bytes"], 1)
        argv, stdin = rec.calls[0]
        self.assertEqual(argv, backup._db_client_argv(
            "mariadb", "-B", "-N", "-u", "wordpress", "-D", "wordpress",
            "-e", argv[-1]))
        self.assertEqual(stdin, TRICKY_PASSWORD + "\n")
        rec.assert_secret_not_in_argv(self, TRICKY_PASSWORD)

    def test_restore_import_and_password_reconcile(self):
        # The DB password is the host secrets file's (source of truth, what
        # the containers run with), on stdin, never argv — and validated, so
        # it can be spliced into the ALTER USER literal. (A wp-config.php
        # value with quotes is refused elsewhere; see test_db_import_sandbox.)
        safe_password = "Restore" + secrets.token_hex(8)
        data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "secrets"), "w") as f:
            f.write(f"WORDPRESS_DB_PASSWORD='{safe_password}'\n")

        staging = os.path.join(self.tmpdir, "staging")
        os.makedirs(os.path.join(staging, "database"))
        with open(os.path.join(staging, "database", "wordpress.sql"), "wb") as f:
            f.write(b"SELECT 1;\n")

        def reply(argv):
            if argv[-2:] == ["get", "DB_PASSWORD"]:
                return "wpconfig-ignored\n"
            if argv[-2:] in (["get", "DB_NAME"], ["get", "DB_USER"]):
                return "wordpress\n"
            return ""
        rec = _Recorder(reply)
        logs = []
        with mock.patch.object(backup.subprocess, "run", rec):
            backup.restore_container_artifacts(staging, {}, logs.append,
                                               data_dir=data_dir)
        rec.assert_secret_not_in_argv(self, safe_password)

        # The dump goes in on stdin, through a sandboxed client, never
        # docker cp'd to /tmp and sourced.
        for argv, _ in rec.calls:
            self.assertNotIn("cp", argv, argv)
            self.assertNotIn("/tmp/wordpress.sql", " ".join(argv))
        imports = [(a, i) for a, i in rec.calls
                   if isinstance(i, (bytes, bytearray)) and "mariadb" in a
                   and "--sandbox" in a]
        self.assertEqual(len(imports), 1, rec.calls)
        argv, stdin = imports[0]
        for flag in ("--sandbox", "--binary-mode", "--local-infile=0"):
            self.assertIn(flag, argv)
        self.assertEqual(stdin, safe_password.encode() + b"\n" + b"SELECT 1;\n")

        alters = [(a, i) for a, i in rec.calls
                  if isinstance(i, str) and "ALTER USER" in i]
        self.assertEqual(len(alters), 1, rec.calls)
        argv, stdin = alters[0]
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "onionpress-db"])
        self.assertIn(f"IDENTIFIED BY '{safe_password}'", stdin)
        # Run the container side here, against a stub mariadb: the root
        # password comes from the container's environment, the SQL from stdin.
        bindir = os.path.join(self.tmpdir, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "mariadb"), "w") as f:
            f.write('#!/bin/sh\nprintf "pwd=%s args=%s\\n" "$MYSQL_PWD" "$*"\ncat\n')
        os.chmod(os.path.join(bindir, "mariadb"), 0o755)
        out = subprocess.run(
            argv[4:], input=stdin, capture_output=True, text=True, timeout=10,
            env={**os.environ, "MYSQL_ROOT_PASSWORD": "root-pw",
                 "PATH": bindir + os.pathsep + os.environ["PATH"]})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "pwd=root-pw args=-uroot --binary-mode --sandbox\n" + stdin)

    def test_create_backup_dump(self):
        """End to end through a stub `docker` that logs its argv and stdin."""
        db_password = "dbpw-" + secrets.token_hex(8)
        pem = os.path.join(self.tmpdir, "key.pem")
        with open(pem, "wb") as f:
            f.write(key_manager.build_openssh_key(secrets.token_bytes(64),
                                                  secrets.token_bytes(32)))
        argv_log = os.path.join(self.tmpdir, "argv.log")
        stdin_log = os.path.join(self.tmpdir, "stdin.log")
        bindir = os.path.join(self.tmpdir, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "docker"), "w") as f:
            f.write(f"""#!/bin/bash
printf '%s\\n' "$*" >> '{argv_log}'
if [[ "$1 $2" == "exec -i" ]]; then cat >> '{stdin_log}'; fi
case "$*" in
  *ks_hs_id.ed25519_expanded_private*) cat '{pem}' ;;
  *"wp config get DB_PASSWORD"*) echo '{db_password}' ;;
  *"wp config get DB_"*) echo wordpress ;;
  *mariadb-dump*) echo "CREATE TABLE wp_posts (id int);" ;;
  *"mariadb -B -N"*) printf '1\\t2\\t3\\t4\\t5\\t6\\n' ;;
  *"tar -cf -"*) d=$(mktemp -d); tar -cf - -C "$d" .; rm -rf "$d" ;;
  "exec onionpress-tor sh -c "*) echo no ;;     # not OnionHome
  "exec onionheaven sh -c "*) echo none ;;      # not a hub
esac
exit 0
""")
        os.chmod(os.path.join(bindir, "docker"), 0o755)
        data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(data_dir)
        out_zip = os.path.join(self.tmpdir, "backup.zip")
        logs = []
        with mock.patch.dict(os.environ,
                             {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            backup.create_backup("x.onion", "admin", "zip-password", out_zip,
                                 "test", logs.append, data_dir=data_dir)
        self.assertTrue(os.path.exists(out_zip), logs)
        with open(argv_log) as f:
            argv_lines = f.read()
        with open(stdin_log) as f:
            stdin_lines = f.read().splitlines()
        self.assertIn("mariadb-dump -u wordpress wordpress", argv_lines)
        self.assertNotIn(db_password, argv_lines)
        # Once for the stats query, once for the dump.
        self.assertEqual(stdin_lines, [db_password, db_password])


class TestLauncherDbClient(unittest.TestCase):
    """The Linux launcher's db_client(), run in bash against a stub `docker`
    that runs the container side here. The Mac launcher has none: its
    database work goes through onionpress.cli (backup.py)."""

    LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")
    DB_CLIENT_LAUNCHERS = ("linux/onionpress",)

    def test_no_password_flag_left(self):
        for rel in self.LAUNCHERS:
            with self.subTest(rel=rel):
                self.assertNotRegex(_read(rel), r'"-p\$\{?db_pass')

    def test_db_client_uses_backups_prelude(self):
        for rel in self.DB_CLIENT_LAUNCHERS:
            with self.subTest(rel=rel):
                # The same container-side prelude as backup.py's.
                self.assertIn(f"sh -c '{backup._DB_PASSWORD_FROM_STDIN}' sh",
                              _shell_function(rel, "db_client"))

    def test_db_client_feeds_password_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv_log = os.path.join(tmp, "argv.log")
            client = os.path.join(tmp, "client")
            with open(client, "w") as f:
                f.write('#!/bin/sh\nprintf "pwd=%s args=%s\\n" "$MYSQL_PWD" "$*"\n')
            os.chmod(client, 0o755)
            for rel in self.DB_CLIENT_LAUNCHERS:
                with self.subTest(rel=rel):
                    script = (
                        "set -e\n"
                        'docker() {\n'
                        '    printf "%s\\n" "$*" >> "$ARGV_LOG"\n'
                        '    [ "$1 $2 $3" = "exec -i onionpress-db" ] || return 99\n'
                        '    shift 3\n'
                        '    "$@"\n'
                        '}\n'
                        + _shell_function(rel, "db_client") + "\n"
                        'db_client "$PW" "$CLIENT" -u wordpress wordpress\n'
                    )
                    out = subprocess.run(
                        ["bash", "-c", script], capture_output=True, text=True,
                        timeout=30, env={**os.environ, "ARGV_LOG": argv_log,
                                         "PW": TRICKY_PASSWORD, "CLIENT": client})
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertEqual(out.stdout.strip(),
                                     f"pwd={TRICKY_PASSWORD} args=-u wordpress wordpress")
                    with open(argv_log) as f:
                        self.assertNotIn(TRICKY_PASSWORD, f.read())


class TestConfigSetTokenOnStdin(unittest.TestCase):
    """`onionpress config set CLOUDFLARE_TUNNEL_TOKEN` (issue #365): the
    token is taken on stdin only; the menubar's write_config_value, which
    runs the command through the launcher, puts it there."""

    TOKEN = "eyJhIjoiMGYxZTJkM2M0YjVhNjk3ODhmN2U2ZDVjNGIzYTI5MTgiLCJ0IjoidHVubmVsIn0="

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="config-argv-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_cli_refuses_the_token_on_argv(self):
        from onionpress import cli
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--data-dir", self.tmpdir, "config", "set",
                           "CLOUDFLARE_TUNNEL_TOKEN", self.TOKEN])
        self.assertEqual(rc, 2)
        self.assertNotIn(self.TOKEN, out.getvalue() + err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "config")))
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(self.TOKEN)):
            rc = cli.main(["--data-dir", self.tmpdir, "config", "set",
                           "CLOUDFLARE_TUNNEL_TOKEN", "-"])
        self.assertEqual(rc, 0)
        self.assertNotIn(self.TOKEN, out.getvalue() + err.getvalue())

    def test_menubar_sends_the_token_on_stdin(self):
        import ast
        import textwrap
        from onionpress import config as op_config
        with open(os.path.join(PROJECT_DIR, "src", "menubar.py")) as f:
            src = f.read()
        node = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.FunctionDef) and n.name == "write_config_value")
        ns = {"os": os, "subprocess": subprocess, "op_config": op_config}
        exec(textwrap.dedent(ast.get_source_segment(src, node)), ns)
        app = mock.Mock()
        app.config_file = os.path.join(self.tmpdir, "config")
        open(app.config_file, "w").close()
        app.app_support = self.tmpdir
        app.launcher_script = "/Applications/OnionPress.app/Contents/MacOS/onionpress"
        app.lines = []
        app.log = app.lines.append
        rec = _Recorder()
        with mock.patch("subprocess.run", rec):
            self.assertTrue(ns["write_config_value"](app, "CLOUDFLARE_TUNNEL_TOKEN", self.TOKEN))
        rec.assert_secret_not_in_argv(self, self.TOKEN)
        argv, stdin = rec.calls[0]
        self.assertEqual(argv, [app.launcher_script, "config", "set",
                                "CLOUDFLARE_TUNNEL_TOKEN", "-"])
        self.assertEqual(stdin, self.TOKEN + "\n")
        self.assertNotIn(self.TOKEN, "\n".join(app.lines))


class TestKeyImportKeyOnStdin(unittest.TestCase):
    """The key-import path: menubar -> Mac launcher -> key_manager.py, and
    the Linux launcher -> key_manager.py."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="import-argv-test-")
        # A random key as Tor and mkp224o make one: clamped (an import
        # refuses a secret scalar with bit 255 set).
        priv = bytearray(secrets.token_bytes(64))
        priv[0] &= 248
        priv[31] = (priv[31] & 127) | 64
        priv = bytes(priv)
        self.key_b32 = base64.b32encode(priv).decode("ascii")
        self.addr = key_manager.derive_onion_address(
            key_manager.derive_public_key(priv))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_key_manager_reads_dash_from_stdin(self):
        by_arg = subprocess.run([sys.executable, KEY_MANAGER, "decode-key", self.key_b32],
                                capture_output=True, text=True, timeout=30)
        by_stdin = subprocess.run([sys.executable, KEY_MANAGER, "decode-key", "-"],
                                  input=self.key_b32 + "\n",
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(by_stdin.returncode, 0, by_stdin.stderr)
        self.assertEqual(by_stdin.stdout.strip(), self.addr)
        self.assertEqual(by_arg.stdout, by_stdin.stdout)

        out_dir = os.path.join(self.tmpdir, "keys")
        imported = subprocess.run([sys.executable, KEY_MANAGER, "import-key", "-", out_dir],
                                  input=self.key_b32 + "\n",
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(os.path.isfile(os.path.join(
            out_dir, self.addr, "ks_hs_id.ed25519_expanded_private")))

    # The key import's own helpers (replace_onion_key and what it calls),
    # where the launcher has them.
    HELPERS = ("is_onion_address", "vanity_key_dir", "docker_volume_state",
               "replace_onion_key", "set_address_prefix_for")

    def _run_import(self, rel, function, wrapper, call):
        data_dir = os.path.join(self.tmpdir, rel.replace("/", "_"))
        argv_log = os.path.join(self.tmpdir, "argv.log")
        # No Docker: every `docker` (the launcher's and key_manager.py's
        # own reads of the tor container) is a stub that fails, except the
        # volume listing, which says there are none.
        bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write('#!/bin/sh\n[ "$1 $2" = "volume ls" ] && exit 0\nexit 1\n')
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        text = _read(rel)
        helpers = "".join(_shell_function(rel, n) + "\n" for n in self.HELPERS
                          if re.search(r"^" + n + r"\(\)", text, re.M))
        script = (
            "set -e\n"
            "log() { :; }\n"
            "sed() { :; }\n"  # ADDRESS_PREFIX is test_address_prefix.py's
            "stop_containers() { :; }\n"
            # Nothing registered with an OnionHeaven hub (that path is
            # tests/test_onionheaven_lifecycle.py's).
            "onionheaven_release() { :; }\n"
            "start_heartbeat_client() { :; }\n"
            f'{wrapper}() {{ printf "%s\\n" "$*" >> "$ARGV_LOG"; "$PY" "$@"; }}\n'
            + helpers + _shell_function(rel, function) + "\n" + call + "\n"
        )
        out = subprocess.run(
            ["bash", "-c", script], input=self.key_b32 + "\n",
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "ARGV_LOG": argv_log, "PY": sys.executable,
                 "KEY": self.key_b32, "DATA_DIR": data_dir,
                 "PATH": bin_dir + os.pathsep + os.environ["PATH"],
                 "LOG_FILE": os.path.join(self.tmpdir, "log"),
                 "SCRIPTS_DIR": os.path.dirname(KEY_MANAGER)})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(os.path.isfile(os.path.join(
            data_dir, "shared", "vanity-keys", self.addr, "hostname")),
            out.stdout + out.stderr)
        with open(argv_log) as f:
            argv_lines = f.read()
        self.assertIn("key_manager.py import-key -", argv_lines)
        self.assertNotIn(self.key_b32, argv_lines)

    def test_mac_launcher_import_key_dash(self):
        self._run_import("app/MacOS/onionpress", "import_key", "bundled_python",
                         "import_key -")

    def test_linux_launcher_import_key(self):
        self._run_import("linux/onionpress", "import_key", "python3",
                         'import_key "$KEY"')

    def test_linux_launcher_replace_onion_key(self):
        """What the Settings page's import-key-file runs."""
        self._run_import("linux/onionpress", "replace_onion_key",
                         "python3", 'replace_onion_key "$KEY"')

    def test_menubar_passes_key_on_stdin(self):
        src = _read("src/menubar.py")
        self.assertIn('[self.launcher_script, "import-key", "-"],', src)
        self.assertNotIn('"import-key", key_data', src)


if __name__ == "__main__":
    unittest.main()
