#!/usr/bin/env python3
"""The onionname reaches WordPress as data, and only a valid one gets there.

provision_primary_subsite used to resolve the new subsite's blog_id with a
`wp eval` whose PHP source had the onionname spliced into a double-quoted
string, and setup_logic.validate_onionname accepted any non-empty name when
onionnames_client was missing or raised. Now:

  - the PHP is a constant and the name arrives on stdin (docker exec -i);
  - validate_onionname applies the charset/length rule itself, so it fails
    closed whatever onionnames_client does, and validates the whole string
    (a trailing newline is not "end of string");
  - install_fresh_wordpress and provision_existing_wordpress refuse an
    invalid name before touching WordPress or ~/.onionpress/config;
  - the Mac setup's collision prompt keeps the current name when the user
    types an invalid one, instead of carrying it into the install;
  - no `wp eval` in src/ is built from an f-string, % or .format().

Nothing here talks to Docker: subprocess is mocked.
"""

import ast
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import onionnames_client  # noqa: E402
from onionpress import setup_logic  # noqa: E402

# Each would have changed the PHP of the old blog_id lookup, or a config
# line, had it got through.
HOSTILE_NAMES = [
    'x"); system("id"); //',
    "alice/../../etc",
    "ali ce",
    "alice$HOME",
    "alice\n",
    "alice\nREGISTER_WITH_ONIONHEAVEN=yes",
    "{${phpinfo()}}",
    "alice'",
    "café1",
]


def _ok(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=stdout, stderr="")


class _BrokenClient:
    @staticmethod
    def validate_name(name):
        raise RuntimeError("wordlists unreadable")


class TestValidateFailsClosed(unittest.TestCase):

    def _check_all(self):
        for name in HOSTILE_NAMES + ["abcd", "", "12345", "a" * 41]:
            ok, reason = setup_logic.validate_onionname(name)
            self.assertFalse(ok, f"{name!r} must be rejected")
            self.assertTrue(reason, f"{name!r}: a reason is expected")
        for name in ("alice", "brewster-kahle", "a1.b2_c3"):
            self.assertEqual(setup_logic.validate_onionname(name), (True, ""))

    def test_with_client(self):
        self._check_all()

    def test_without_client(self):
        with mock.patch.object(setup_logic, "_onc", None):
            self._check_all()

    def test_when_client_raises(self):
        with mock.patch.object(setup_logic, "_onc", _BrokenClient):
            self._check_all()

    def test_reasons_match_the_client(self):
        for name in HOSTILE_NAMES + ["abcd", "12345", "a" * 41, "alice"]:
            with mock.patch.object(setup_logic, "_onc", None):
                local = setup_logic.validate_onionname(name)
            ok, reason = onionnames_client.validate_name(name)
            self.assertEqual(local, (ok, reason or ""), name)

    def test_non_string_is_empty(self):
        with mock.patch.object(setup_logic, "_onc", None):
            self.assertEqual(setup_logic.validate_onionname(None),
                             (False, "empty"))


class TestBlogIdLookupIsConstantPhp(unittest.TestCase):

    def _eval_call(self, onionname):
        with mock.patch("onionpress.setup_logic.subprocess.run",
                        return_value=_ok("7\n")) as mrun:
            setup_logic.provision_primary_subsite(
                onionname=onionname, site_title="T", onion_addr="abc.onion",
            )
        calls = [c for c in mrun.call_args_list
                 if "eval" in c.args[0]
                 and any("get_blog_id_from_url" in a for a in c.args[0])]
        self.assertEqual(len(calls), 1)
        return calls[0], mrun

    def test_name_goes_on_stdin_not_into_the_code(self):
        call, mrun = self._eval_call("alice-example")
        argv = call.args[0]
        self.assertEqual(argv[:3], ["docker", "exec", "-i"])
        self.assertEqual(call.kwargs.get("input"), "alice-example")
        php = argv[argv.index("eval") + 1]
        self.assertNotIn("alice-example", php)
        self.assertIn("php://stdin", php)
        # The resolved id still lands in primary_blog.
        meta = [c.args[0] for c in mrun.call_args_list
                if "primary_blog" in c.args[0]]
        self.assertEqual(meta[0][-1], "7")

    def test_php_is_identical_for_every_name(self):
        def php(name):
            argv = self._eval_call(name)[0].args[0]
            return argv[argv.index("eval") + 1]
        self.assertEqual(php("alice-example"), php("bob.example_2"))

    def test_other_wp_calls_do_not_read_stdin(self):
        with mock.patch("onionpress.setup_logic.subprocess.run",
                        return_value=_ok("7\n")) as mrun:
            setup_logic.provision_primary_subsite(
                onionname="alice", site_title="T", onion_addr="abc.onion",
            )
        for c in mrun.call_args_list:
            if "eval" not in c.args[0]:
                self.assertNotIn("-i", c.args[0][:3])
                self.assertIsNone(c.kwargs.get("input"))


class TestInstallersRefuseInvalidNames(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def _assert_refused(self, fn, **extra):
        for name in HOSTILE_NAMES:
            logs = []
            with mock.patch("onionpress.setup_logic.subprocess.run",
                            return_value=_ok()) as mrun, \
                    mock.patch.object(setup_logic, "_onc", None):
                ok = fn(site_title="T", onionname=name, password="hunter22",
                        data_dir=self.tmpdir, log_func=logs.append, **extra)
            self.assertFalse(ok, name)
            self.assertEqual(mrun.call_count, 0,
                             f"{name!r}: nothing may reach wp-cli")
            self.assertFalse(
                os.path.exists(os.path.join(self.tmpdir, "config")), name)
            self.assertTrue(any("invalid" in m for m in logs), name)

    def test_install_fresh_wordpress(self):
        self._assert_refused(setup_logic.install_fresh_wordpress,
                             launcher_bin=None)

    def test_provision_existing_wordpress(self):
        self._assert_refused(setup_logic.provision_existing_wordpress)


class TestCollisionPromptKeepsValidName(unittest.TestCase):
    """menubar._register_onionname_during_setup: an invalid name typed into
    the collision prompt is not adopted (it would become the install's
    admin user and the ONIONNAME config line)."""

    def test_invalid_entry_continues_before_adopting(self):
        with open(os.path.join(PROJECT_ROOT, "src", "menubar.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_register_onionname_during_setup")
        guards = [n for n in ast.walk(func)
                  if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "not ok"]
        self.assertEqual(len(guards), 1)
        self.assertIsInstance(guards[0].body[-1], ast.Continue)
        adopt = [n for n in ast.walk(func)
                 if isinstance(n, ast.Assign)
                 and ast.unparse(n) == "name = new_name"]
        self.assertEqual(len(adopt), 1)
        self.assertGreater(adopt[0].lineno, guards[0].lineno)


class TestNoWpEvalBuiltFromVariables(unittest.TestCase):
    """Every `wp eval` in src/ passes a code string that is not assembled
    from runtime values; data goes on stdin or as separate arguments."""

    @staticmethod
    def _is_built(node):
        if isinstance(node, ast.JoinedStr):
            return any(isinstance(v, ast.FormattedValue) for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            return not all(isinstance(s, ast.Constant)
                           for s in (node.left, node.right))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"):
            return True
        return False

    def test_no_interpolated_eval_code(self):
        offenders = []
        src = os.path.join(PROJECT_ROOT, "src")
        for root, _dirs, files in os.walk(src):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    seqs = []
                    if isinstance(node, ast.Call):
                        seqs.append(node.args)
                    if isinstance(node, (ast.List, ast.Tuple)):
                        seqs.append(node.elts)
                    for seq in seqs:
                        for i, el in enumerate(seq[:-1]):
                            if (isinstance(el, ast.Constant)
                                    and el.value in ("eval", "eval-file")
                                    and self._is_built(seq[i + 1])):
                                offenders.append(
                                    f"{os.path.relpath(path, PROJECT_ROOT)}:"
                                    f"{el.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
