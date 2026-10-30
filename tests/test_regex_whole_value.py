#!/usr/bin/env python3
"""Validators test the whole value, not the value up to a trailing newline.

`$` matches at the end of the string *or just before a final "\\n"*, in
Python's re and in PHP's PCRE alike. So `re.match(r"^[a-z2-7]{56}\\.onion$")`
and `preg_match('/^[a-z2-7]{56}\\.onion$/')` both accept "<address>.onion\\n",
which then becomes a path component, a database key or a command argument
that is not the value that was checked. Seen in:

  - the OnionHeaven hub (web-server.py): a signed /online with a
    healthcheck_address ending in "\\n" registered that address;
  - config.validate_address_prefix(), the setup form's username/email,
    the onionnames registry's address check, the analytics name checks;
  - every name/address preg_match in the WordPress plugins and theme;
  - the macOS launcher's prefix check, `echo | grep -qE '^...$'`, which
    tests each line on its own, and both launchers' `validate-oh-address`
    (62 characters with a line ending in ".onion").

Python uses re.fullmatch; PHP uses the D modifier (PCRE_DOLLAR_ENDONLY);
bash uses [[ =~ ]]. The scans below keep it that way. Denylists (a pattern
that refuses a match, like the Creations BLOCKED_EXT_RE) are left alone:
there a looser `$` refuses more, not less.
"""

import ast
import io
import os
import re
import subprocess
import sys
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

for fake in ("AppKit", "Foundation", "objc"):
    if fake not in sys.modules:
        sys.modules[fake] = types.ModuleType(fake)

from onionpress import config, derivatives, launcher_ops, log_rotation  # noqa: E402


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _python_sources():
    roots = ["src", "linux", os.path.join("app", "Resources", "docker", "tor"),
             os.path.join("app", "MacOS"), "build"]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(os.path.join(PROJECT_ROOT, root)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT)


def _php_sources():
    for dirpath, _, filenames in os.walk(os.path.join(PROJECT_ROOT, "app", "Resources")):
        for name in filenames:
            if name.endswith(".php"):
                yield os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT)


def _ends_in_dollar(node):
    """True if the pattern expression *node* ends in an unescaped `$`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        value = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
        return value.endswith("$") and not value.endswith("\\$")
    if isinstance(node, ast.JoinedStr) and node.values:
        return _ends_in_dollar(node.values[-1])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _ends_in_dollar(node.right)
    return False


def _starts_with_caret(node):
    """True if the pattern expression *node* starts with `^`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        value = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
        return value.startswith("^")
    if isinstance(node, ast.JoinedStr) and node.values:
        return _starts_with_caret(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _starts_with_caret(node.left)
    return False


def _prefix_matches_with_dollar(source):
    """(line, text) of every `.match(` against a `$`-terminated pattern, and
    every `.search(` against a `^...$` one: `re.match("...$", x)` directly,
    or `NAME.match(x)` where NAME was bound to `re.compile("...$")` anywhere
    in the module."""
    tree = ast.parse(source)
    compiled, anchored = set(), set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "compile" and node.value.args
                and _ends_in_dollar(node.value.args[0])):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    compiled.add(target.id)
                    if _starts_with_caret(node.value.args[0]):
                        anchored.add(target.id)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("match", "search")):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "re":
            if node.args and _ends_in_dollar(node.args[0]) and (
                    node.func.attr == "match" or _starts_with_caret(node.args[0])):
                found.append(node.lineno)
        elif isinstance(owner, ast.Name) and owner.id in compiled:
            if node.func.attr == "match" or owner.id in anchored:
                found.append(node.lineno)
    lines = source.splitlines()
    return [(n, lines[n - 1].strip()) for n in found]


# preg_* with a single-quoted pattern anchored at both ends: '/^...$/flags'.
_PHP_ANCHORED = re.compile(r"""preg_\w+\(\s*'([/#~])\^(?:(?!\1).|\\\1)*\$\1([A-Za-z]*)'""")


class TestNoPythonValidatorStopsAtANewline(unittest.TestCase):
    def test_scan_sees_the_old_bug(self):
        old = ('import re\nR = re.compile(r"^[a-z]+$")\nR.match(x)\nre.match(r"^a$", y)\n'
               'R.search(z)\nre.search(r"^b$", w)\nre.search(r"\\.php$", v)\n')
        self.assertEqual([n for n, _ in _prefix_matches_with_dollar(old)], [3, 4, 5, 6])
        new = 'import re\nR = re.compile(r"^[a-z]+$")\nR.fullmatch(x)\nre.fullmatch(r"a", y)\n'
        self.assertEqual(_prefix_matches_with_dollar(new), [])

    def test_no_match_against_a_dollar_terminated_pattern(self):
        offenders = []
        for rel in _python_sources():
            for lineno, text in _prefix_matches_with_dollar(_read(rel)):
                offenders.append(f"{rel}:{lineno}: {text}")
        self.assertEqual(
            offenders, [],
            "use .fullmatch(): `$` also matches before a trailing newline")


class TestNoPhpValidatorStopsAtANewline(unittest.TestCase):
    def test_scan_sees_the_old_bug(self):
        self.assertTrue(_PHP_ANCHORED.search("preg_match( '/^[a-z]+$/', $x )"))
        self.assertTrue(_PHP_ANCHORED.search("preg_match( '#^a/b$#i', $x )"))
        self.assertEqual(
            _PHP_ANCHORED.search("preg_match( '/^[a-z]+$/D', $x )").group(2), "D")

    def test_every_anchored_pattern_has_the_d_modifier(self):
        offenders = []
        for rel in _php_sources():
            for lineno, line in enumerate(_read(rel).splitlines(), 1):
                for m in _PHP_ANCHORED.finditer(line):
                    if "D" not in m.group(2):
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "add the D modifier: `$` also matches before a trailing newline")


class TestAddressPrefix(unittest.TestCase):
    def test_validator_refuses_a_trailing_newline(self):
        for bad in ("op2\n", "abcde\n", "abcdef\n"):
            ok, _, suggestion = config.validate_address_prefix(bad)
            self.assertFalse(ok, repr(bad))
            self.assertNotIn("\n", suggestion)

    def test_mkp224o_is_never_started_for_one(self):
        with mock.patch.object(launcher_ops.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                launcher_ops.generate_vanity_in_container("op2\n", "/nonexistent")
        run.assert_not_called()

    def _mac_launcher_check(self, prefix):
        body = _read("app/MacOS/onionpress")
        start = body.index('        _prefix_problem=""\n')
        end = body.index("        unset _prefix_problem", start)
        script = ("log() { :; }\nDATA_DIR=/nonexistent\nADDRESS_PREFIX=\"$1\"\n"
                  + body[start:end] + 'printf %s "$ADDRESS_PREFIX"\n')
        out = subprocess.run(["/bin/bash", "-c", script, "bash", prefix],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_mac_launcher_tests_the_whole_value(self):
        # Two ADDRESS_PREFIX= lines in the config read as "ab\ncd".
        self.assertEqual(self._mac_launcher_check("ab\ncd"), "op2")
        self.assertEqual(self._mac_launcher_check("ab\n"), "op2")
        self.assertEqual(self._mac_launcher_check("abc"), "abc")
        self.assertEqual(self._mac_launcher_check("ab0"), "op2")


def _bash_function(text, name):
    """Source of a top-level bash function, closing brace included."""
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


class TestLauncherHubAddressCheck(unittest.TestCase):
    """`validate-oh-address` (the menubar settings dialog's hub check) took
    62 characters with a line ending in ".onion" and handed them to curl."""

    VALID = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"

    def _run(self, launcher, addr):
        import tempfile
        body = _read(launcher)
        script = (_bash_function(body, "is_onion_address")
                  + _bash_function(body, "_validate_oh_address")
                  + '_validate_oh_address "$1"\n')
        with tempfile.TemporaryDirectory() as tmp:
            stub = os.path.join(tmp, "docker")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(stub, 0o755)
            env = {"PATH": f"{tmp}:/usr/bin:/bin", "DATA_DIR": tmp,
                   "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock"}
            out = subprocess.run(["/bin/bash", "-c", script, "bash", addr],
                                 capture_output=True, text=True, env=env, timeout=30)
        return out.stdout

    def test_only_a_whole_onion_address_gets_past_the_format_check(self):
        bad = ("evil.example.com/" + "a" * 39 + ".onion",
               "x" * 55 + "\n.onion",
               "abc\n" + self.VALID[4:],
               self.VALID.upper())
        self.assertEqual(len(bad[0]), 62)
        for launcher in ("app/MacOS/onionpress", "linux/onionpress"):
            for addr in bad:
                with self.subTest(launcher=launcher, addr=addr):
                    self.assertIn('"status":"invalid_format"', self._run(launcher, addr))
            with self.subTest(launcher=launcher, addr="valid"):
                self.assertIn('"status":"unreachable"', self._run(launcher, self.VALID))


class TestOtherNameChecks(unittest.TestCase):
    def test_rotated_log_names(self):
        self.assertEqual(log_rotation.extract_log_type("onionpress-2026-04-21-001.log"),
                         "onionpress")
        self.assertIsNone(log_rotation.extract_log_type("onionpress-2026-04-21-001.log\n"))

    def test_derivative_names(self):
        self.assertTrue(derivatives.derivative_base_candidates("photo-300x200.jpg"))
        self.assertEqual(derivatives.derivative_base_candidates("photo-300x200.jpg\n"), [])

    def test_setup_form_refuses_a_trailing_newline(self):
        from urllib.parse import urlencode
        from onionpress import onion_proxy

        def post(user, email):
            body = urlencode({"blog_title": "My Blog", "user_name": user,
                              "admin_password": "hunter22",
                              "admin_email": email}).encode()
            h = onion_proxy.OnionProxyHandler.__new__(onion_proxy.OnionProxyHandler)
            h.server = types.SimpleNamespace(docker_bin="/x/docker", docker_env={},
                                             log_func=lambda *_: None)
            h.headers = {"Content-Length": str(len(body))}
            h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
            sent = []
            h.send_response = sent.append
            h.send_header = lambda *a: None
            h.end_headers = lambda: None
            h.send_error = lambda *a: sent.append(a)
            # The first-run gate (`wp core is-installed`) runs before the form
            # is read; report "not installed" so the name checks are reached.
            h._wp_installed = lambda: False
            with mock.patch.object(onion_proxy.subprocess, "run") as run:
                h._handle_setup_post()
            return sent, run, h.wfile.getvalue().decode()

        # The form has no email field since issue #481 (setup_logic uses a
        # fixed one); the user name is the onionname.
        for user, email in (("alice\n", "alice@example.org"),):
            sent, run, page = post(user, email)
            self.assertEqual(sent, [400], (user, email, page))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
