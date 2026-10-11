"""OnionHeaven registration is opt-in.

REGISTER_WITH_ONIONHEAVEN is tri-state: "yes" / "no" / unset. Only an
explicit "yes" may register, because registering ships the onion private
key to the hub. These tests pin that no code path treats a missing or
empty value as consent, on either platform.
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

    def test_unset_or_garbage_is_undecided(self):
        for v in (None, "", "   ", "maybe", "y", "yse"):
            self.assertIsNone(registration_choice(v), repr(v))


class TestDefaultsAreOptIn(unittest.TestCase):
    def test_python_default_is_not_yes(self):
        self.assertNotEqual(
            registration_choice(op_config.DEFAULTS["REGISTER_WITH_ONIONHEAVEN"]), "yes")

    def test_shipped_configs_do_not_preset_yes(self):
        for rel in ("app/Resources/config-template.txt",
                    "linux/install.sh", "linux/onionpress"):
            text = _read(rel)
            self.assertNotRegex(
                text, re.compile(r"^REGISTER_WITH_ONIONHEAVEN=yes\s*$", re.M),
                f"{rel} must not opt users in by default")
            self.assertRegex(
                text, re.compile(r"^REGISTER_WITH_ONIONHEAVEN=\s*$", re.M),
                f"{rel} should ship the key unset (undecided)")

    def test_settings_form_default_is_unchecked(self):
        text = _read("src/onionpress/settings_ui.py")
        self.assertIn('("REGISTER_WITH_ONIONHEAVEN", "no")', text)


class TestNoCodePathTreatsUnsetAsYes(unittest.TestCase):
    """The old gate was `!= "no"`, which registered on a missing key."""

    FILES = (
        "src/onionpress/onionheaven.py",
        "src/menubar.py",
        "linux/onionpress-heartbeat.py",
        "linux/onionpress-service.py",
        "linux/onionpress-tray",
        "linux/onionpress",
    )

    def test_no_not_equal_no_gate(self):
        pat = re.compile(r'REGISTER_WITH_ONIONHEAVEN[^\n]*!=\s*["\']no["\']')
        for rel in self.FILES:
            self.assertIsNone(pat.search(_read(rel)), f"{rel}: unset must not count as yes")

    def test_no_yes_default_when_reading(self):
        # A read with a "yes" fallback: config.get(KEY, "yes") or
        # read_config_value(KEY, "yes"). Writes of "yes" (the prompt's
        # Enable button) are fine and do not match this shape.
        pat = re.compile(
            r'(get|read_config_value|read_value)\([^)]*["\']REGISTER_WITH_ONIONHEAVEN["\']'
            r'\s*,\s*["\']yes["\']')
        for rel in self.FILES:
            self.assertIsNone(pat.search(_read(rel)), f"{rel}: must not default the key to yes")

    def test_bash_status_default_is_not_yes(self):
        self.assertNotIn("oh_config_enabled:-yes", _read("linux/onionpress"))


class TestPromptsExist(unittest.TestCase):
    def test_mac_prompt_wired_in(self):
        text = _read("src/menubar.py")
        self.assertIn("def prompt_enable_onionheaven", text)
        self.assertIn("get_registration_choice(self)", text)

    def test_linux_tray_prompt_and_cli(self):
        self.assertIn("def _maybe_prompt_onionheaven", _read("linux/onionpress-tray"))
        launcher = _read("linux/onionpress")
        self.assertIn("onionheaven_cmd", launcher)
        self.assertIn("onionpress onionheaven enable", _read("linux/install.sh"))


if __name__ == "__main__":
    unittest.main()
