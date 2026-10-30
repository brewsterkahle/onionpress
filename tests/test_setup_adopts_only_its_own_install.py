#!/usr/bin/env python3
"""A setup adopts only a WordPress that OnionPress installed (issue #464).

Until WordPress is installed, wp-admin/install.php takes the first
visitor's choice of admin account (user 1, super admin after the multisite
conversion). A setup that found WordPress installed used to take the
"legacy" path, provision_existing_wordpress: rename and re-password the
existing admin, keeping whatever else that visitor added (another admin,
a plugin).

Now every OnionPress install writes `.wordpress-installed-by-onionpress`
in the data dir, holding user 1's registration time as WordPress records
it: setup_logic.install_fresh_wordpress (the Mac setup window, the Linux
tray and `onionpress provision`'s fresh path) and both launchers' bash
`onionpress setup` (note_wordpress_installed_by_onionpress, right after
their `wp core multisite-install`). provision_existing_wordpress refuses
an installed WordPress whose marker is missing or does not match the live
install (FOREIGN_INSTALL_MSG, False), unless adopt_existing is given:
`onionpress provision --adopt-existing` on Linux.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import setup_logic  # noqa: E402

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")
ONION = "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
REGISTERED = "2026-10-29 10:11:12"


def _read(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _bash_function(text, name):
    start = text.index(f"\n{name}() {{\n") + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


class _Recorder:
    """Stands in for subprocess.run: answers `wp user get 1
    --field=user_registered` with *registered*, everything else rc 0."""

    def __init__(self, registered=REGISTERED, fail=()):
        self.registered = registered
        self.fail = fail
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        rc, out = 0, ""
        if "--field=user_registered" in argv:
            out = self.registered + "\n" if self.registered is not None else ""
            rc = 0 if self.registered is not None else 1
        for word in self.fail:
            if word in argv:
                rc = 1
        return subprocess.CompletedProcess(argv, rc, out, "")

    def wp_verbs(self):
        """The wp-cli verb of every call, in order."""
        verbs = []
        for argv in self.calls:
            if "wp" in argv:
                i = argv.index("wp")
                verbs.append(" ".join(argv[i + 1:i + 3]))
        return verbs


class TestTheLiterals(unittest.TestCase):

    def test_one_marker_name(self):
        self.assertEqual(setup_logic.INSTALLED_BY_ONIONPRESS_MARKER,
                         ".wordpress-installed-by-onionpress")
        for rel in LAUNCHERS:
            self.assertIn('WP_INSTALLED_BY_ONIONPRESS_MARKER=".wordpress-installed-by-onionpress"',
                          _read(rel), rel)

    def test_the_launchers_write_it_alike(self):
        mac = _bash_function(_read(LAUNCHERS[0]), "note_wordpress_installed_by_onionpress")
        linux = _bash_function(_read(LAUNCHERS[1]), "note_wordpress_installed_by_onionpress")
        self.assertEqual(mac, linux)
        self.assertIn("wp_cli user get 1 --field=user_registered", mac)

    def test_each_bash_setup_writes_it_right_after_the_install(self):
        for rel, setup_fn in (("app/MacOS/onionpress", "setup_wordpress_cli"),
                              ("linux/onionpress", "setup_wordpress_interactive")):
            body = _bash_function(_read(rel), setup_fn)
            self.assertIn("core multisite-install", body, rel)
            self.assertRegex(
                body,
                r'installed successfully via WP-CLI"\n\s*note_wordpress_installed_by_onionpress\n',
                f"{rel}: {setup_fn} does not note the install")

    def test_the_bash_and_python_stamps_agree(self):
        """The same shape on both sides: WordPress's datetime, else 'unknown'."""
        self.assertEqual(setup_logic.INSTALLED_BY_UNKNOWN, "unknown")
        body = _bash_function(_read("linux/onionpress"), "note_wordpress_installed_by_onionpress")
        self.assertIn(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$", body)
        self.assertIn("|| stamp=unknown", body)
        self.assertEqual(setup_logic._USER_REGISTERED_RE.pattern,
                         r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")

    def test_linux_provision_takes_adopt_existing(self):
        text = _read("linux/onionpress")
        start = text.index("\n        provision)\n")
        end = text.index("\n            ;;\n", start)
        case = text[start:end]
        self.assertIn("provision_interactive(adopt_existing='--adopt-existing' in sys.argv[1:])", case)
        self.assertIn('"$ONIONPRESS_PYTHONPATH" "${@:2}"', case)
        self.assertIn("--adopt-existing", setup_logic.FOREIGN_INSTALL_MSG)


class TestPythonSide(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-adopt-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.marker = os.path.join(self.tmp, setup_logic.INSTALLED_BY_ONIONPRESS_MARKER)
        self.logs = []

    def _write_marker(self, stamp):
        with open(self.marker, "w") as f:
            f.write(stamp + "\n")

    def _install(self, rec):
        with mock.patch.object(setup_logic.subprocess, "run", rec), \
             mock.patch.object(setup_logic, "start_bundled_plugin_setup", return_value=True):
            return setup_logic.install_fresh_wordpress(
                site_title="My Blog", onionname="alice", password="hunter22",
                onion_addr=ONION, launcher_bin=None, data_dir=self.tmp,
                log_func=self.logs.append)

    def _provision(self, rec, **extra):
        with mock.patch.object(setup_logic.subprocess, "run", rec):
            return setup_logic.provision_existing_wordpress(
                site_title="t", onionname="alice", password="hunter22",
                data_dir=self.tmp, log_func=self.logs.append, **extra)

    def test_a_fresh_install_writes_the_marker_after_the_password(self):
        rec = _Recorder()
        self.assertTrue(self._install(rec))
        with open(self.marker) as f:
            self.assertEqual(f.read(), REGISTERED + "\n")
        verbs = rec.wp_verbs()
        # The password goes in straight after the install; the stamp is
        # read once that is done.
        self.assertEqual(verbs[0], "core install")
        self.assertTrue(verbs[1].startswith("eval "), verbs[1])
        self.assertIn("user get", verbs[2:])

    def test_a_failed_install_writes_no_marker(self):
        rec = _Recorder(fail=("install",))
        self.assertFalse(self._install(rec))
        self.assertFalse(os.path.exists(self.marker))

    def test_an_unreadable_stamp_is_unknown(self):
        rec = _Recorder(registered=None)
        self.assertTrue(self._install(rec))
        with open(self.marker) as f:
            self.assertEqual(f.read().strip(), "unknown")
        # ...and a junk one is unknown too: the stamp must be WordPress's
        # datetime, nothing a planted answer chooses.
        rec = _Recorder(registered="x\ny")
        with mock.patch.object(setup_logic.subprocess, "run", rec):
            self.assertTrue(
                setup_logic.mark_wordpress_installed_by_onionpress(self.tmp, "docker"))
        with open(self.marker) as f:
            self.assertEqual(f.read().strip(), "unknown")

    def test_whose_install_it_is(self):
        def ours(registered=REGISTERED):
            with mock.patch.object(setup_logic.subprocess, "run", _Recorder(registered)):
                return setup_logic.wordpress_installed_by_onionpress(self.tmp, "docker")
        self.assertFalse(ours(), "no marker")
        self._write_marker(REGISTERED)
        self.assertTrue(ours(), "the stamp matches the live install")
        self.assertFalse(ours("2026-10-30 00:00:00"), "another install's stamp")
        self.assertFalse(ours(None), "the live stamp cannot be read")
        self._write_marker("unknown")
        self.assertTrue(ours("whatever"), "an install whose stamp could not be read")
        self._write_marker("")
        self.assertFalse(ours(), "an empty marker")
        self._write_marker("yes")
        self.assertFalse(ours(), "junk in the marker")
        self._write_marker(REGISTERED + "\nextra")
        self.assertFalse(ours(), "a stamp with more after it")

    def test_a_foreign_install_is_refused(self):
        rec = _Recorder()
        self.assertFalse(self._provision(rec))
        self.assertIn(setup_logic.FOREIGN_INSTALL_MSG, self.logs)
        self.assertNotIn("user update", rec.wp_verbs(), "nothing was renamed")
        self.assertNotIn("eval", rec.wp_verbs(), "no password was set")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".setup_complete")))
        self.assertFalse(os.path.exists(self.marker), "a refusal writes no marker")

    def test_a_mismatched_marker_is_refused_too(self):
        self._write_marker("2026-01-01 00:00:00")
        rec = _Recorder()
        self.assertFalse(self._provision(rec))
        self.assertIn(setup_logic.FOREIGN_INSTALL_MSG, self.logs)

    def test_our_own_install_is_provisioned(self):
        self._write_marker(REGISTERED)
        rec = _Recorder()
        self.assertTrue(self._provision(rec))
        self.assertNotIn(setup_logic.FOREIGN_INSTALL_MSG, self.logs)
        self.assertIn("user update", rec.wp_verbs())
        self.assertTrue(os.path.exists(os.path.join(self.tmp, ".setup_complete")))

    def test_adopt_existing_overrides_and_marks(self):
        rec = _Recorder()
        self.assertTrue(self._provision(rec, adopt_existing=True))
        self.assertTrue(any("--adopt-existing" in m for m in self.logs))
        self.assertIn("user update", rec.wp_verbs())
        with open(self.marker) as f:
            self.assertEqual(f.read().strip(), REGISTERED)

    def test_an_invalid_onionname_is_refused_before_the_marker_is_looked_at(self):
        rec = _Recorder()
        with mock.patch.object(setup_logic.subprocess, "run", rec), \
             mock.patch.object(setup_logic, "_onc", None):
            self.assertFalse(setup_logic.provision_existing_wordpress(
                site_title="t", onionname="not valid!", password="hunter22",
                data_dir=self.tmp, log_func=self.logs.append))
        self.assertEqual(rec.calls, [])

    def test_provision_interactive_passes_the_choice_through(self):
        for adopt in (False, True):
            with self.subTest(adopt=adopt), \
                 mock.patch("builtins.input", side_effect=["My Blog", "alice"]), \
                 mock.patch("getpass.getpass", side_effect=["hunter22", "hunter22"]), \
                 mock.patch.object(setup_logic, "_wp_is_installed", return_value=True), \
                 mock.patch.object(setup_logic, "provision_existing_wordpress",
                                   return_value=True) as m:
                self.assertTrue(setup_logic.provision_interactive(
                    data_dir=self.tmp, adopt_existing=adopt))
            self.assertEqual(m.call_args.kwargs["adopt_existing"], adopt)


WP_CLI_STUB = r"""
wp_cli() {
    echo "wp_cli $*" >> "$CALLS"
    printf '%s\n' "$STUB_REGISTERED"
}
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestBashMarkerUnderBash(unittest.TestCase):
    """note_wordpress_installed_by_onionpress from each launcher, run under
    bash against a wp_cli stub, and read back by setup_logic."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-adopt-bash-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = os.path.join(self.tmp, "data")
        os.mkdir(self.data_dir)
        self.calls = os.path.join(self.tmp, "calls")
        self.marker = os.path.join(self.data_dir, ".wordpress-installed-by-onionpress")
        self.pwned = os.path.join(self.tmp, "pwned")

    def _run(self, rel, registered):
        text = _read(rel)
        script = (
            "set -e\n"
            f"DATA_DIR={shlex.quote(self.data_dir)}\n"
            f"CALLS={shlex.quote(self.calls)}\n"
            + WP_CLI_STUB
            + 'WP_INSTALLED_BY_ONIONPRESS_MARKER=".wordpress-installed-by-onionpress"\n'
            + _bash_function(text, "note_wordpress_installed_by_onionpress")
            + "note_wordpress_installed_by_onionpress\n"
        )
        env = dict(os.environ, STUB_REGISTERED=registered, OP_PWNED=self.pwned)
        r = subprocess.run(["bash", "-c", script], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.marker) as f:
            return f.read()

    def test_the_stamp_is_wordpress_s(self):
        for rel in LAUNCHERS:
            with self.subTest(launcher=rel):
                self.assertEqual(self._run(rel, REGISTERED), REGISTERED + "\n")
                with open(self.calls) as f:
                    self.assertIn("wp_cli user get 1 --field=user_registered", f.read())
                # ...and setup_logic reads it as its own.
                with mock.patch.object(setup_logic, "_wp_user_1_registered", return_value=REGISTERED):
                    self.assertTrue(setup_logic.wordpress_installed_by_onionpress(self.data_dir))
                with mock.patch.object(setup_logic, "_wp_user_1_registered",
                                       return_value="2026-10-30 00:00:00"):
                    self.assertFalse(setup_logic.wordpress_installed_by_onionpress(self.data_dir))

    def test_anything_else_is_unknown(self):
        for rel in LAUNCHERS:
            for answer in ("", "Error: no user", "a[$(>$OP_PWNED)]", REGISTERED + " extra",
                           "2026-10-29T10:11:12"):
                with self.subTest(launcher=rel, answer=answer):
                    self.assertEqual(self._run(rel, answer), "unknown\n")
                    self.assertFalse(os.path.exists(self.pwned))
                    with mock.patch.object(setup_logic, "_wp_user_1_registered", return_value=REGISTERED):
                        self.assertTrue(setup_logic.wordpress_installed_by_onionpress(self.data_dir))


if __name__ == "__main__":
    unittest.main()
