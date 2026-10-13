"""OnionHeaven registration is opt-in.

REGISTER_WITH_ONIONHEAVEN is tri-state: "yes" / "no" / unset. Only an
explicit "yes" may register, because registering ships the onion private
key to the hub. These tests pin that no code path treats a missing or
empty value as consent, on either platform. What happens when the choice
changes while OnionPress runs (heartbeats stop at once; an explicit "no"
unregisters, unset sends nothing) is in tests/test_onionheaven_opt_out.py.
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

    # Every place that writes a fresh ~/.onionpress/config. The .deb's
    # postinst (build/build-linux.sh) wrote REGISTER_WITH_ONIONHEAVEN=yes
    # until 2026-10-13: every new apt install shipped its key to the hub
    # without being asked.
    CONFIG_WRITERS = ("app/Resources/config-template.txt",
                      "linux/install.sh", "linux/onionpress",
                      "build/build-linux.sh")

    def test_shipped_configs_do_not_preset_yes(self):
        for rel in self.CONFIG_WRITERS:
            text = _read(rel)
            self.assertNotRegex(
                text, re.compile(r"^REGISTER_WITH_ONIONHEAVEN=yes\s*$", re.M),
                f"{rel} must not opt users in by default")
            self.assertRegex(
                text, re.compile(r"^REGISTER_WITH_ONIONHEAVEN=\s*$", re.M),
                f"{rel} should ship the key unset (undecided)")

    def test_mac_launcher_fallback_config_leaves_it_unset(self):
        # The launcher's inline config (no config-template.txt in the
        # bundle) does not name the key at all: unset, undecided.
        src = _read("app/MacOS/onionpress")
        start = src.index('if [ ! -f "$DATA_DIR/config" ]; then')
        block = src[start:src.index("\nfi\n", start)]
        self.assertIn('cat > "$DATA_DIR/config" <<EOF', block)
        self.assertNotIn("REGISTER_WITH_ONIONHEAVEN", block)

    def test_python_config_writer_skips_the_empty_default(self):
        # ensure_config writes DEFAULTS when there is no template, leaving
        # out empty values: the key stays absent.
        import tempfile
        from onionpress.platform import resolve_paths
        with tempfile.TemporaryDirectory() as tmp:
            paths = resolve_paths(data_dir=tmp, app_bundle="")
            op_config.ensure_config(paths)
            with open(paths.config_file, encoding="utf-8") as f:
                self.assertNotIn("REGISTER_WITH_ONIONHEAVEN", f.read())

    def test_nothing_shipped_presets_yes(self):
        # A census of every shipped text file, so a new config writer
        # cannot opt a new install in either: no line sets the key to a
        # value registration_choice() reads as yes. (Turning it on is the
        # user's: the prompts' Enable buttons, `onionpress onionheaven
        # enable`, the Settings dialog and page.)
        # A config line (template, heredoc) or an echo/printf of one; not
        # prose such as "REGISTER_WITH_ONIONHEAVEN=yes|no".
        pat = re.compile(
            r"(^[ \t]*(export[ \t]+)?|(echo|printf)[ \t]+['\"]?)"
            r"REGISTER_WITH_ONIONHEAVEN[ \t]*=[ \t]*['\"]?(yes|true|1|on)['\"]?"
            r"[ \t]*($|>|\\n|\")", re.I | re.M)
        found = []
        for top in ("app", "linux", "build", "src"):
            for dirpath, dirnames, filenames in os.walk(os.path.join(REPO, top)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    try:
                        with open(path, encoding="utf-8") as f:
                            text = f.read()
                    except (UnicodeDecodeError, OSError):
                        continue
                    if pat.search(text):
                        found.append(os.path.relpath(path, REPO))
        self.assertEqual(found, [])

    def test_python_writes_of_yes_are_the_prompts_enable_button(self):
        # The only Python write of "yes": the Mac startup prompt's Enable.
        pat = re.compile(r'["\']REGISTER_WITH_ONIONHEAVEN["\']\s*,\s*["\']yes["\']')
        hits = []
        for rel in ("src/menubar.py", "src/onionpress/config.py",
                    "src/onionpress/settings_ui.py", "src/onionpress/cli.py",
                    "src/onionpress/onionheaven.py", "linux/onionpress-tray",
                    "linux/onionpress-service.py", "linux/onionpress-heartbeat.py"):
            text = _read(rel)
            for m in pat.finditer(text):
                fn = text.rfind("\n    def ", 0, m.start())
                hits.append((rel, text[fn:text.index("(", fn)].strip()))
        self.assertEqual(hits, [("src/menubar.py", "def prompt_enable_onionheaven")])

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

    def test_linux_daemon_reads_the_choice_through_one_normalizer(self):
        # Registration, the per-cycle re-read, the opt-out and the
        # system-sleep hook's /offline all gate on _registration_choice.
        src = _read("linux/onionpress-heartbeat.py")
        self.assertIn("def _registration_choice(config):", src)
        self.assertEqual(src.count('config.get("REGISTER_WITH_ONIONHEAVEN"'), 1)
        self.assertEqual(src.count("_registration_choice(config)"), 4)

    def test_mac_gates_go_through_get_registration_choice(self):
        # The loop, every /online and every /offline re-read the choice.
        src = _read("src/onionpress/onionheaven.py")
        for fn in ("def _heartbeat_loop(", "def _send_heartbeat_locked(",
                   "def _send_onionheaven_notification("):
            body = src[src.index(fn):]
            body = body[:body.index("\ndef ", 1)]
            self.assertIn('get_registration_choice(app)', body, fn)
            self.assertIn('!= "yes"', body, fn)


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
