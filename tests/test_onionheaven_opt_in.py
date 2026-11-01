"""OnionHeaven registration is on by default, and nothing asks first.

REGISTER_WITH_ONIONHEAVEN: "yes" or unset registers, "no" opts out, and
any other value (a typo) does neither, so a mistyped "no" never
registers. Fresh configs say yes; there is no startup prompt on either
platform. What happens when the choice changes while OnionPress runs
(heartbeats stop at once; an explicit "no" unregisters) is in
tests/test_onionheaven_opt_out.py.
"""

import os
import re
import sys
import unittest

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, "src"))

from onionpress import config as op_config  # noqa: E402
from onionpress.onionheaven import registration_choice  # noqa: E402


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


class TestRegistrationChoice(unittest.TestCase):
    def test_explicit_yes(self):
        for v in ("yes", "YES", " Yes ", "true", "1", "on"):
            self.assertEqual(registration_choice(v), "yes", v)

    def test_explicit_no(self):
        for v in ("no", "NO", " No ", "false", "0", "off"):
            self.assertEqual(registration_choice(v), "no", v)

    def test_unset_is_yes(self):
        for v in (None, "", "   "):
            self.assertEqual(registration_choice(v), "yes", repr(v))

    def test_garbage_is_neither(self):
        for v in ("maybe", "y", "yse", "nope"):
            self.assertIsNone(registration_choice(v), repr(v))


class TestDefaultsAreOn(unittest.TestCase):
    def test_python_default_is_yes(self):
        self.assertEqual(
            registration_choice(op_config.DEFAULTS["REGISTER_WITH_ONIONHEAVEN"]), "yes")

    # Every place that writes a fresh ~/.onionpress/config.
    CONFIG_WRITERS = ("app/Resources/config-template.txt",
                      "linux/install.sh", "linux/onionpress",
                      "build/build-linux.sh")

    def test_shipped_configs_say_yes(self):
        for rel in self.CONFIG_WRITERS:
            self.assertRegex(
                _read(rel), re.compile(r"^REGISTER_WITH_ONIONHEAVEN=yes\s*$", re.M), rel)

    def test_python_config_writer_writes_yes(self):
        import tempfile
        from onionpress.platform import resolve_paths
        with tempfile.TemporaryDirectory() as tmp:
            paths = resolve_paths(data_dir=tmp, app_bundle="")
            op_config.ensure_config(paths)
            with open(paths.config_file, encoding="utf-8") as f:
                self.assertIn("REGISTER_WITH_ONIONHEAVEN=yes\n", f.read())

    def test_settings_dialog_shows_unset_as_on(self):
        text = _read("src/onionpress/settings_ui.py")
        self.assertIn('("REGISTER_WITH_ONIONHEAVEN", "yes")', text)
        self.assertIn('if old_values["REGISTER_WITH_ONIONHEAVEN"].strip() == "":', text)

    def test_settings_page_has_no_undecided_choice(self):
        php = _read("app/Resources/plugins/onionpress-settings.php")
        start = php.index("'REGISTER_WITH_ONIONHEAVEN' => array(")
        block = php[start:php.index("),\n", php.index("'options'", start)) + 2]
        self.assertIn("'options'     => array( 'yes' => 'Enabled (recommended)', 'no' => 'Disabled' )",
                      block)


class TestTyposNeverCountAsYes(unittest.TestCase):
    """Unset is yes; a value that is neither yes nor no must not be."""

    FILES = (
        "src/onionpress/onionheaven.py",
        "src/menubar.py",
        "src/onionpress/daemon.py",
        "linux/onionpress-heartbeat.py",
        "linux/onionpress-tray",
        "linux/onionpress",
    )

    def test_no_not_equal_no_gate(self):
        # `!= "no"` would register on "nope".
        pat = re.compile(r'REGISTER_WITH_ONIONHEAVEN[^\n]*!=\s*["\']no["\']')
        for rel in self.FILES:
            self.assertIsNone(pat.search(_read(rel)), f"{rel}: a typo must not count as yes")

    def test_bash_status_only_yes_or_empty_is_enabled(self):
        src = _read("linux/onionpress")
        self.assertIn("oh_config_enabled=${oh_config_enabled:-yes}", src)
        self.assertIn('[ "$oh_config_enabled" = "yes" ] || oh_config_enabled=no', src)

    def test_linux_daemon_reads_the_choice_through_one_normalizer(self):
        # Registration, the per-cycle re-read and the opt-out all gate on
        # _registration_choice (the system-sleep hook's one-shot /offline,
        # which no hook called, went on 2026-10-31).
        src = _read("linux/onionpress-heartbeat.py")
        self.assertIn("def _registration_choice(config):", src)
        self.assertEqual(src.count('config.get("REGISTER_WITH_ONIONHEAVEN"'), 1)
        self.assertEqual(src.count("_registration_choice(config)"), 3)

    def test_mac_gates_go_through_get_registration_choice(self):
        # The loop, every /online and every /offline re-read the choice.
        # OnionHeavenClient's methods (issue #479).
        src = _read("src/onionpress/onionheaven.py")
        for fn in ("    def heartbeat_loop(", "    def _send_heartbeat_locked(",
                   "    def _send_notification("):
            body = src[src.index(fn):]
            body = body[:body.index("\n    def ", 1)]
            self.assertIn('get_registration_choice(self.app)', body, fn)
            self.assertIn('!= "yes"', body, fn)


class TestNoStartupPrompt(unittest.TestCase):
    def test_mac_registers_without_asking(self):
        text = _read("src/menubar.py")
        self.assertNotIn("prompt_enable_onionheaven", text)
        self.assertNotIn("_onionheaven_prompt_shown", text)
        # The gate is the daemon's status pass (onionpress.daemon), through
        # its OnionHeavenClient's tick.
        daemon = _read("src/onionpress/daemon.py")
        self.assertIn("self.hub_client.tick(", daemon)
        src = _read("src/onionpress/onionheaven.py")
        tick = src[src.index("    def tick("):]
        tick = tick[:tick.index("\n    def ", 1)]
        self.assertIn('if get_registration_choice(self.app) == "yes":', tick)
        self.assertNotIn("prompt_enable_onionheaven", daemon)
        self.assertNotIn("REGISTRATION_PROMPT", _read("src/onionpress/onionheaven.py"))

    def test_linux_tray_does_not_ask(self):
        tray = _read("linux/onionpress-tray")
        self.assertNotIn("_maybe_prompt_onionheaven", tray)
        self.assertNotIn("_show_onionheaven_prompt", tray)

    def test_linux_cli_and_installer_say_how_to_opt_out(self):
        self.assertIn("onionheaven_cmd", _read("linux/onionpress"))
        self.assertIn("onionpress onionheaven disable", _read("linux/install.sh"))


if __name__ == "__main__":
    unittest.main()
