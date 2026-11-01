#!/usr/bin/env python3
"""Passwords reach WordPress, curl and Python as data, never as code.

linux/onionpress's create_backup used to check the WordPress admin password
by splicing it into the PHP source of a `wp eval`, escaping `'` but not
`\\`, so a backslash in the password changed the PHP that ran and a
password ending in one could never make a backup. It now pipes the
password to `onionpress.cli verify-admin-password`, which hands it to PHP
on stdin. The same review moved the Archive.org login's password from
`python3 -c` source onto curl's stdin, and the remaining `python3 -c`
calls that spliced paths into code now pass them as argv.

The static checks keep both launchers free of the pattern. The bash
harnesses run the launcher's own functions against stub docker and curl
binaries, so the whole path (bash -> Python CLI -> docker exec) is
exercised without Docker.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

from onionpress import backup  # noqa: E402
from onionpress import cli  # noqa: E402

LINUX = "linux/onionpress"
MACOS = "app/MacOS/onionpress"

# Each one broke the old escaping or would break a naive one.
TRICKY_PASSWORDS = [
    "a'b",
    "ends-with-a-backslash\\",
    "x\\'); echo 'injected'; //",
    "two\\\\backslashes",
    'dq"dollar$HOME`tick`$(id)',
    "pässwörd\\'",
    "inner\nnewline",
]


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    """Source of a top-level bash function, closing brace included.

    Ends at the last `}` line before the next top-level definition rather
    than at the first one: create_backup's inline Python has a `}` line.
    """
    start = text.index(f"\n{name}() {{") + 1
    nxt = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{", re.M).search(text, start + 1)
    chunk = text[start:nxt.start() if nxt else len(text)]
    return chunk[:chunk.rindex("\n}\n") + 3]


def _double_quoted_after(text, pattern):
    """(line, body) for every double-quoted string that follows *pattern*."""
    for m in re.finditer(pattern, text):
        i = m.end()
        while i < len(text) and text[i] != '"':
            i += 2 if text[i] == "\\" else 1
        yield text.count("\n", 0, m.start()) + 1, text[m.end():i]


def _expands(body):
    """True when a double-quoted shell string expands something ($ or `)."""
    return re.search(r"(?<!\\)[$`]", body) is not None


class TestCreateBackupWiring(unittest.TestCase):

    def setUp(self):
        self.text = _read(LINUX)

    def test_verifier_takes_the_password_on_stdin_only(self):
        body = _function(self.text, "verify_wp_admin_password")
        self.assertIn("\n    op_py verify-admin-password\n", body)

    def test_archive_login_is_the_pythons(self):
        # multisite.fetch_archive_s3_keys (TestArchiveLoginPython) since #442.10.
        self.assertNotIn("fetch_archive_s3_keys", self.text)


class TestNoCodeSplicingInLaunchers(unittest.TestCase):
    """No double-quoted string that expands a variable is used as code."""

    def test_wp_eval_and_db_query_code_is_never_double_quoted(self):
        for path in (LINUX, MACOS):
            for line, body in _double_quoted_after(
                    _read(path), r"\bwp (?:eval|eval-file|db query|shell)\s+\""):
                self.fail(f"{path}:{line}: WP-CLI code in a double-quoted "
                          f"string: {body[:80]!r}")

    def test_python_dash_c_code_never_expands_a_variable(self):
        pattern = r"(?:\bpython3?|\bbundled_python|_BUNDLED_PYTHON_BIN)\s+-c\s+\""
        for path in (LINUX, MACOS):
            for line, body in _double_quoted_after(_read(path), pattern):
                self.assertFalse(
                    _expands(body),
                    f"{path}:{line}: python -c code expands a shell value; "
                    f"pass it as sys.argv or on stdin: {body[:120]!r}")

    def test_linux_sh_dash_c_code_is_never_double_quoted(self):
        # The macOS launcher's two (ensure_vm_fsck_repair's quoted heredoc and
        # the iptables rules built from `docker inspect` output) carry no
        # user input; the Linux launcher has no reason for any.
        for line, body in _double_quoted_after(
                _read(LINUX), r"\b(?:sh|bash)\s+-c\s+\""):
            self.fail(f"{LINUX}:{line}: sh -c code in a double-quoted "
                      f"string: {body[:80]!r}")


# `docker exec` arguments of a `wp eval` reading stdin, run as www-data
# (onionpress.wp_files.wp_cli_args).
WP_EVAL_AS_WWW_DATA = ["exec", "-i", "-u", "www-data", "-e",
                       "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
                       "onionpress-wordpress", "wp", "eval"]


class _Harness(unittest.TestCase):
    """Runs launcher functions under bash with stub binaries on PATH.

    Each stub appends {"tool", "argv", "stdin"} to $STUB_CALLS. The docker
    stub answers the verifier's `docker exec -i -u www-data … wp eval`
    like WP-CLI would (an admin login and exit 0 when stdin equals
    $STUB_ACCEPT, exit 1 otherwise), a curl run in a container
    (`docker exec -i onionheaven curl ...`) like curl, `docker image
    inspect` (the key helpers' image is here) and `docker volume ls` (no
    volumes) with exit 0, and fails every other command. LINES are
    launcher lines, by their start, put before FUNCTIONS.
    """

    STUB = r'''
import json, os, sys
tool, args = sys.argv[1], sys.argv[2:]
reads_stdin = "-i" in args[:2] or "password@-" in args
data = sys.stdin.buffer.read().decode("utf-8") if reads_stdin else None
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"tool": tool, "argv": args, "stdin": data}) + "\n")
if tool == "docker" and args[:2] == ["exec", "-i"] and args[3:4] == ["curl"]:
    tool = "curl"
if tool == "docker" and args[:2] in (["image", "inspect"], ["volume", "ls"]):
    sys.exit(0)     # the key helpers' tor image is here; no key volume
if tool == "docker":
    if args[:9] == ["exec", "-i", "-u", "www-data", "-e",
                    "WP_CLI_CACHE_DIR=/tmp/wp-cli-cache",
                    "onionpress-wordpress", "wp", "eval"]:
        if data == os.environ.get("STUB_ACCEPT"):
            print("admin")
            sys.exit(0)
    sys.exit(1)
if tool == "curl":
    print(json.dumps({"success": True,
                      "values": {"s3": {"access": "AK", "secret": "SK"}}}))
'''

    FUNCTIONS = ()
    LINES = ()

    def run_bash(self, snippet, env_extra):
        text = _read(LINUX)
        functions = "".join(
            next(l for l in text.splitlines() if l.startswith(start)) + "\n"
            for start in self.LINES)
        functions += "".join(_function(text, name) for name in self.FUNCTIONS)
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            home = os.path.join(tmp, "home")
            data_dir = os.path.join(home, ".onionpress")
            os.makedirs(bin_dir)
            os.makedirs(data_dir)
            # An existing config keeps the CLI's ensure_config from copying
            # the template out of an installed app bundle.
            open(os.path.join(data_dir, "config"), "w").close()
            stub = os.path.join(tmp, "stub.py")
            with open(stub, "w", encoding="utf-8") as f:
                f.write(self.STUB)
            wrappers = {"python3": f'exec "{sys.executable}" "$@"\n'}
            for tool in ("docker", "curl"):
                wrappers[tool] = f'exec "{sys.executable}" "{stub}" {tool} "$@"\n'
            for name, body in wrappers.items():
                path = os.path.join(bin_dir, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\n" + body)
                os.chmod(path, 0o755)
            calls = os.path.join(tmp, "calls")
            log = os.path.join(data_dir, "onionpress.log")
            open(calls, "w").close()
            env = {
                "PATH": bin_dir + os.pathsep + "/usr/bin:/bin",
                "HOME": home, "TMPDIR": tmp,
                "DATA_DIR": data_dir, "LOG_FILE": log,
                "ONIONPRESS_PYTHONPATH": SRC,
                "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
                "WORDPRESS_DB_PASSWORD": "db-test-password",
                "STUB_CALLS": calls,
                "OUT": os.path.join(tmp, "backup.zip"),
                **env_extra,
            }
            script = ('log() { printf "%s\\n" "$*" >> "$LOG_FILE"; }\n'
                      + functions + snippet + "\n")
            out = subprocess.run(["bash", "-c", script], env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=120)
            with open(calls, encoding="utf-8") as f:
                made = [json.loads(line) for line in f]
            logged = ""
            if os.path.exists(log):
                with open(log, encoding="utf-8") as f:
                    logged = f.read()
        return out, made, logged


class TestArchiveLoginPython(unittest.TestCase):
    """The terminal setup's archive.org login is multisite.fetch_archive_s3_keys
    since #442.10 (Linux's bash fetch_archive_s3_keys is gone): the form,
    password included, is curl's stdin, intact whatever it holds."""

    def test_password_reaches_curl_on_stdin_intact(self):
        import urllib.parse
        from onionpress import multisite
        answer = json.dumps({"success": True, "values": {"s3": {"access": "AK", "secret": "SK"}}})
        for pw in TRICKY_PASSWORDS:
            with self.subTest(pw=pw), mock.patch.object(
                    multisite.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, answer, "")) as run:
                keys = multisite.fetch_archive_s3_keys("o'brien@example.org", pw)
                self.assertEqual(keys, {"access": "AK", "secret": "SK"})
                argv = run.call_args.args[0]
                self.assertEqual(argv[1:5], ["exec", "-i", "onionheaven", "curl"])
                form = urllib.parse.parse_qs(run.call_args.kwargs["input"])
                self.assertEqual(form, {"email": ["o'brien@example.org"], "password": [pw]})
                for arg in argv:
                    self.assertNotIn(pw, arg)


class TestPythonVerifierInput(unittest.TestCase):
    """verify_wp_admin_password_any and the CLI subcommand the launcher runs."""

    def test_password_goes_to_php_on_stdin_verbatim(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "admin\n", "")

        with mock.patch("onionpress.backup.subprocess.run", side_effect=fake_run):
            for pw in TRICKY_PASSWORDS:
                ok, user = backup.verify_wp_admin_password_any(pw)
                self.assertEqual((ok, user), (True, "admin"))
        php = {argv[10] for argv, _ in seen}
        self.assertEqual(len(php), 1, "the PHP source must not vary with the password")
        for (argv, kwargs), pw in zip(seen, TRICKY_PASSWORDS):
            # wp-cli as www-data, never as root.
            self.assertEqual(argv[:10], ["docker"] + WP_EVAL_AS_WWW_DATA)
            self.assertEqual(kwargs["input"], pw)
            for arg in argv:
                self.assertNotIn(pw, arg)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_cli_reads_the_password_from_stdin(self, _cli):
        for pw in TRICKY_PASSWORDS:
            with self.subTest(pw=pw), \
                    mock.patch("sys.stdin", io.StringIO(pw + "\n")), \
                    mock.patch("onionpress.backup.verify_wp_admin_password_any",
                               return_value=(True, "admin")) as verify:
                self.assertEqual(cli.main(["verify-admin-password"]), 0)
                verify.assert_called_once_with(pw)

    @mock.patch("onionpress.cli.OnionPressCLI")
    def test_cli_rejects_an_empty_password_without_asking_wordpress(self, _cli):
        with mock.patch("sys.stdin", io.StringIO("")), \
                mock.patch("onionpress.backup.verify_wp_admin_password_any") as verify:
            self.assertEqual(cli.main(["verify-admin-password"]), 1)
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
