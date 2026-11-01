#!/usr/bin/env python3
"""`onionpress config get|set|unset|list` and `onionpress onionheaven
enable|disable|status` (issue #365, CLI-first phase 1).

Every setting a person may change is reachable through `config set` on
both platforms, with the check its value gets on the Settings page; the
hand-edit-only keys (image overrides, ports, paths, hub admission) are
refused; a secret is taken on stdin only and shown as <set>/<empty>; a
change leaves a marker the running menubar takes; the login item and the
Linux user units follow LAUNCH_ON_LOGIN / START_ON_BOOT. The GUIs write
config only through the command (a census of the sources).

Nothing here touches Docker: the config and onionheaven subcommands run
before OnionPressCLI's setup, and the test fails if that setup runs.
"""

import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)

from onionpress import cli, login_item, onionheaven  # noqa: E402
from onionpress import config as op_config  # noqa: E402

TOKEN = "eyJhIjoiMGYxZTJkM2M0YjVhNjk3ODhmN2U2ZDVjNGIzYTI5MTgiLCJ0IjoidHVubmVsIn0="
ONION = "a" * 56 + ".onion"
HUB = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"

CONFIG = """\
# OnionPress config
ADDRESS_PREFIX=op2
VM_MEMORY=2
ONIONPRESS_TOR_IMAGE=onionpress-tor:dev
"""

# A good and a bad value for every key `config set` accepts.
VALUES = {
    "ADDRESS_PREFIX": ("op3", "op2abc"),
    "LAUNCH_ON_LOGIN": ("no", "1"),
    "START_ON_BOOT": ("no", "true"),
    "PREVENT_SLEEP": ("never", "always"),
    "VM_MEMORY": ("4", "0"),
    "VM_CPU": ("4", "65"),
    "VM_DISK": ("40", "5"),
    "INSTALL_IA_PLUGIN": ("no", "y"),
    "INSTALL_CACHE_ENABLER": ("no", "n"),
    "REPUBLISH_ON_UNREACHABLE": ("no", "off"),
    "QUIT_KEEPS_SITE_RUNNING": ("yes", "maybe"),
    "REGISTER_WITH_ONIONHEAVEN": ("no", "later"),
    "ONIONHEAVEN_ADDRESS": (ONION, "evil.example/" + "a" * 43 + ".onion"),
    "ONIONHEAVEN_MAX_SERVICES": ("20", "501"),
    "ONIONHEAVEN_PROPAGATION_DELAY": ("120", "30"),
    "SHARE_ANALYTICS_WITH_ONIONHOME": ("yes", "sometimes"),
    "ONIONHOME_ADDRESS": (ONION, ONION.upper()),
    "ONIONNAME": ("brewster", "x" * 61),
    "ONIONNAME_ADDRESS": (ONION, "not-an-address"),
    "ONIONNAME_REGISTERED": ("yes", "registered"),
    # The token's good value goes on stdin (TestSecretOnStdin).
    "CLOUDFLARE_TUNNEL_TOKEN": (None, "short"),
}

HAND_EDIT = [
    "ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE", "ONIONPRESS_BIND_HOST",
    "ONIONPRESS_PORT_OFFSET", "ONIONPRESS_WP_PORT", "ONIONPRESS_DOCUMENTS_DIR",
    "ONIONHEAVEN_ACCEPT_REGISTRATIONS",
]


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


class _CliCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-cli-config-")
        self.config = os.path.join(self.dir, "config")
        with open(self.config, "w") as f:
            f.write(CONFIG)
        # Side effects (login item, systemctl) are tested on their own;
        # here they only record what they were asked.
        self.effects = []
        p = mock.patch.object(
            op_config, "apply_side_effects",
            side_effect=lambda keys, *a, **k: self.effects.append(list(keys)) or [])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cli, "OnionPressCLI",
                              side_effect=AssertionError("OnionPressCLI setup ran"))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            rc = cli.main(["--data-dir", self.dir, *args])
        return rc, out.getvalue(), err.getvalue()

    def file(self):
        return op_config.read_config(self.config)

    def marker(self):
        path = os.path.join(self.dir, op_config.CONFIG_CHANGED_MARKER)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read().split()

    def log(self):
        path = os.path.join(self.dir, "onionpress.log")
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()


class TestConfigSet(_CliCase):

    def test_every_key_has_values_in_the_table(self):
        self.assertEqual(set(VALUES), set(op_config.CLI_CONFIG_KEYS))

    def test_good_values_are_written(self):
        for key, (good, _bad) in VALUES.items():
            if good is None:
                continue
            with self.subTest(key=key):
                # A prefix the (unknown) address may lack is asked about
                # (tests/test_cli_confirm_protocol.py); answered here.
                flags = ["--confirmed", "prefix-change"] if key == "ADDRESS_PREFIX" else []
                rc, out, err = self.run_cli("config", "set", key, good, *flags)
                self.assertEqual((rc, err), (0, ""), out)
                self.assertEqual(self.file()[key], good)
                self.assertIn(key, out)
                self.assertIn(key, self.marker())
        # The rest of the file is untouched.
        self.assertEqual(self.file()["ONIONPRESS_TOR_IMAGE"], "onionpress-tor:dev")
        with open(self.config) as f:
            self.assertTrue(f.read().startswith("# OnionPress config\n"))

    def test_bad_values_are_refused(self):
        before = self.file()
        for key, (_good, bad) in VALUES.items():
            with self.subTest(key=key):
                if key in op_config.SECRET_CONFIG_KEYS:
                    rc, out, err = self.run_cli("config", "set", key, "-", stdin=bad)
                else:
                    rc, out, err = self.run_cli("config", "set", key, bad)
                self.assertEqual(rc, 2, out + err)
                self.assertIn(f"{key} must be", err + out)
                self.assertEqual(self.file(), before)
        self.assertIsNone(self.marker())
        self.assertEqual(self.effects, [])

    def test_page_checks_are_the_cli_checks(self):
        for key, check in op_config.SETTINGS_PAGE_KEYS.items():
            self.assertIs(op_config.CLI_CONFIG_KEYS[key], check, key)

    def test_hand_edit_only_keys_are_refused(self):
        for key in HAND_EDIT + ["ONIONPRESS_ANYTHING_NEW"]:
            with self.subTest(key=key):
                rc, out, err = self.run_cli("config", "set", key, "x")
                self.assertEqual(rc, 2)
                self.assertIn("editing the config file", err)
                rc, out, err = self.run_cli("config", "unset", key)
                self.assertEqual(rc, 2)
        self.assertEqual(self.file()["ONIONPRESS_TOR_IMAGE"], "onionpress-tor:dev")

    def test_unknown_key_is_refused(self):
        rc, out, err = self.run_cli("config", "set", "TOR_IMPL", "arti")
        self.assertEqual(rc, 2)
        self.assertIn("not a setting", err)
        self.assertNotIn("TOR_IMPL", self.file())

    def test_control_characters_are_refused(self):
        # A newline would add a config line: refused even where the rest
        # of the value is fine (via stdin, the only way to pass one).
        rc, out, err = self.run_cli("config", "set", "PREVENT_SLEEP", "-",
                                    stdin="never\nONIONPRESS_TOR_IMAGE=evil\n")
        self.assertEqual(rc, 2)
        self.assertNotIn("PREVENT_SLEEP", self.file())
        self.assertEqual(self.file()["ONIONPRESS_TOR_IMAGE"], "onionpress-tor:dev")

    def test_unchanged_value_writes_nothing(self):
        rc, out, err = self.run_cli("config", "set", "VM_MEMORY", "2")
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)
        self.assertIsNone(self.marker())
        self.assertEqual(self.effects, [])

    def test_restart_keys_are_named(self):
        rc, out, _ = self.run_cli("config", "set", "VM_CPU", "4")
        self.assertIn("Restart OnionPress", out)
        self.assertIn("VM_CPU", out)
        rc, out, _ = self.run_cli("config", "set", "PREVENT_SLEEP", "never")
        self.assertNotIn("Restart", out)
        rc, out, _ = self.run_cli("config", "set", "VM_DISK", "40")
        self.assertIn("first created", out)
        self.assertEqual(sorted(k for k in op_config.NEEDS_RESTART),
                         sorted({"ADDRESS_PREFIX", "VM_MEMORY", "VM_CPU",
                                 "CLOUDFLARE_TUNNEL_TOKEN", "ONIONHEAVEN_ADDRESS",
                                 "INSTALL_IA_PLUGIN", "INSTALL_CACHE_ENABLER",
                                 "ONIONHEAVEN_MAX_SERVICES", "ONIONHEAVEN_PROPAGATION_DELAY"}))

    def test_side_effects_run_for_the_changed_key(self):
        self.run_cli("config", "set", "LAUNCH_ON_LOGIN", "no")
        self.run_cli("config", "set", "START_ON_BOOT", "no")
        self.assertEqual(self.effects, [["LAUNCH_ON_LOGIN"], ["START_ON_BOOT"]])

    def test_change_is_logged_through_describe_change(self):
        self.run_cli("config", "set", "VM_CPU", "4")
        # The line was absent: the effective default, not the raw file value
        self.assertIn("VM_CPU: '2' (unset) → '4'", self.log())
        self.run_cli("config", "set", "VM_CPU", "6")
        self.assertIn("VM_CPU: '4' → '6'", self.log())

    def test_the_marker_accumulates_until_taken(self):
        self.run_cli("config", "set", "VM_CPU", "4")
        self.run_cli("config", "set", "PREVENT_SLEEP", "never")
        self.run_cli("config", "set", "VM_CPU", "8")
        self.assertEqual(self.marker(), ["VM_CPU", "PREVENT_SLEEP"])
        self.assertEqual(op_config.take_config_changes(self.dir), ["VM_CPU", "PREVENT_SLEEP"])
        self.assertIsNone(self.marker())
        self.assertEqual(op_config.take_config_changes(self.dir), [])

    def test_no_subcommand_prints_help(self):
        rc, out, err = self.run_cli("config")
        self.assertEqual(rc, 1)
        self.assertIn("{get,set,unset,list}", out + err)


class TestConfigUnsetGetList(_CliCase):

    def test_unset_removes_the_line_and_keeps_the_rest(self):
        rc, out, _ = self.run_cli("config", "unset", "VM_MEMORY")
        self.assertEqual(rc, 0)
        self.assertNotIn("VM_MEMORY", self.file())
        self.assertEqual(self.file()["ADDRESS_PREFIX"], "op2")
        with open(self.config) as f:
            self.assertIn("# OnionPress config\n", f.read())
        self.assertEqual(self.marker(), ["VM_MEMORY"])
        # Absent already: nothing to do, no marker.
        rc, out, _ = self.run_cli("config", "unset", "VM_MEMORY")
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)

    def test_get_prints_the_value_or_the_default(self):
        self.assertEqual(self.run_cli("config", "get", "VM_MEMORY")[1], "2\n")
        self.assertEqual(self.run_cli("config", "get", "VM_CPU")[1], "2\n")
        self.assertEqual(self.run_cli("config", "get", "INSTALL_CACHE_ENABLER")[1], "yes\n")
        self.assertEqual(self.run_cli("config", "get", "ONIONHEAVEN_ADDRESS")[1], HUB + "\n")
        rc, out, err = self.run_cli("config", "get", "ONIONNAME")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("not set", err)

    def test_list_shows_every_key_and_marks_defaults(self):
        rc, out, _ = self.run_cli("config", "list")
        self.assertEqual(rc, 0)
        lines = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        self.assertEqual(set(lines), set(op_config.CLI_CONFIG_KEYS))
        self.assertEqual(lines["VM_MEMORY"], "2")
        self.assertEqual(lines["VM_CPU"], "2  (default)")
        self.assertEqual(lines["ONIONNAME"], "  (unset)")
        self.assertIn("hand-edit only, in the file: ONIONPRESS_TOR_IMAGE", out)

    def test_list_json(self):
        rc, out, _ = self.run_cli("config", "list", "--json")
        doc = json.loads(out)
        self.assertEqual(set(doc), {"settings", "hand_edit_only"})
        self.assertEqual(set(doc["settings"]), set(op_config.CLI_CONFIG_KEYS))
        self.assertEqual(doc["settings"]["VM_MEMORY"], "2")
        self.assertEqual(doc["hand_edit_only"], ["ONIONPRESS_TOR_IMAGE"])


class TestSecretOnStdin(_CliCase):
    """CLOUDFLARE_TUNNEL_TOKEN: on stdin only, never on argv, never in
    what the command prints or logs."""

    def test_token_on_argv_is_refused(self):
        rc, out, err = self.run_cli("config", "set", "CLOUDFLARE_TUNNEL_TOKEN", TOKEN)
        self.assertEqual(rc, 2)
        self.assertIn("stdin", err)
        self.assertNotIn(TOKEN, out + err + self.log())
        self.assertNotIn("CLOUDFLARE_TUNNEL_TOKEN", self.file())

    def test_token_on_stdin_is_written_and_never_shown(self):
        rc, out, err = self.run_cli("config", "set", "CLOUDFLARE_TUNNEL_TOKEN", "-",
                                    stdin=TOKEN + "\n")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(self.file()["CLOUDFLARE_TUNNEL_TOKEN"], TOKEN)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN: set", out)
        self.assertIn("Restart OnionPress", out)
        self.assertNotIn(TOKEN, out + self.log())
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN: set", self.log())

        for args in (("get", "CLOUDFLARE_TUNNEL_TOKEN"), ("list",), ("list", "--json")):
            rc, out, err = self.run_cli("config", *args)
            self.assertNotIn(TOKEN, out + err, args)
            self.assertIn("<set>", out, args)
        doc = json.loads(self.run_cli("config", "list", "--json")[1])
        self.assertEqual(doc["settings"]["CLOUDFLARE_TUNNEL_TOKEN"], "<set>")

        # Replacing is logged as such; removing on argv is fine ("" is no secret).
        rc, out, _ = self.run_cli("config", "set", "CLOUDFLARE_TUNNEL_TOKEN", "-",
                                  stdin="A" * 40)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN: replaced", out)
        rc, out, _ = self.run_cli("config", "set", "CLOUDFLARE_TUNNEL_TOKEN", "")
        self.assertEqual(rc, 0)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN: removed", out)
        self.assertEqual(self.file()["CLOUDFLARE_TUNNEL_TOKEN"], "")
        self.assertEqual(self.run_cli("config", "get", "CLOUDFLARE_TUNNEL_TOKEN")[1],
                         "<empty>\n")

    def test_empty_stdin_is_a_removal(self):
        self.run_cli("config", "set", "CLOUDFLARE_TUNNEL_TOKEN", "-", stdin=TOKEN)
        rc, out, _ = self.run_cli("config", "set", "CLOUDFLARE_TUNNEL_TOKEN", "-", stdin="")
        self.assertEqual(rc, 0)
        self.assertEqual(self.file()["CLOUDFLARE_TUNNEL_TOKEN"], "")


class TestSideEffects(unittest.TestCase):
    """config.apply_side_effects: the login item on a Mac, the user units
    on Linux, nothing else and nothing on the other platform."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-cli-effects-")
        self.config = os.path.join(self.dir, "config")
        self.plist = os.path.join(self.dir, "LaunchAgents", "com.onionpress.launcher.plist")
        self.bundle = os.path.join(self.dir, "OnionPress.app")
        os.makedirs(os.path.join(self.bundle, "Contents", "MacOS"))
        self.lines = []
        self.runs = []

        def run(argv, **kwargs):
            self.runs.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.run = run

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def _apply(self, keys, os_name):
        return op_config.apply_side_effects(
            keys, self.config, os_name, self.lines.append, run=self.run,
            app_bundle=self.bundle, launchagent_path=self.plist)

    def test_mac_login_item_follows_launch_on_login(self):
        self._write("LAUNCH_ON_LOGIN=yes\n")
        out = self._apply(["LAUNCH_ON_LOGIN"], "macos")
        self.assertTrue(os.path.exists(self.plist))
        self.assertIn("Login item installed.", out)
        import plistlib
        with open(self.plist, "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist["ProgramArguments"], ["open", "-a", self.bundle])
        # This install's own label (its config's folder is not the account's
        # ~/.onionpress; issue #505).
        self.assertEqual(plist["Label"], login_item.launchagent_label(self.dir))
        self.assertNotEqual(plist["Label"], login_item.LAUNCHAGENT_LABEL)
        self.assertEqual(self.runs, [])

        self._write("LAUNCH_ON_LOGIN=no\n")
        out = self._apply(["LAUNCH_ON_LOGIN"], "macos")
        self.assertFalse(os.path.exists(self.plist))
        self.assertIn("Login item removed.", out)

    def test_mac_login_item_needs_a_bundle(self):
        self._write("LAUNCH_ON_LOGIN=yes\n")
        out = op_config.apply_side_effects(
            ["LAUNCH_ON_LOGIN"], self.config, "macos", self.lines.append,
            run=self.run, app_bundle="", launchagent_path=self.plist)
        self.assertFalse(os.path.exists(self.plist))
        self.assertTrue(out[0].startswith("WARNING"), out)

    def test_linux_units_follow_start_on_boot(self):
        self._write("START_ON_BOOT=no\n")
        out = self._apply(["START_ON_BOOT"], "linux")
        self.assertEqual(self.runs, [["systemctl", "--user", "disable",
                                      "onionpress.service", "onionpress-daemon.service"]])
        self.assertIn("Start on boot disabled.", out)
        self._write("START_ON_BOOT=yes\n")
        self._apply(["START_ON_BOOT"], "linux")
        self.assertEqual(self.runs[-1][2], "enable")
        self.assertFalse(os.path.exists(self.plist))

    def test_the_other_platform_does_nothing(self):
        self._write("START_ON_BOOT=no\nLAUNCH_ON_LOGIN=no\n")
        self.assertEqual(self._apply(["START_ON_BOOT"], "macos"), [])
        self.assertEqual(self._apply(["LAUNCH_ON_LOGIN"], "linux"), [])
        self.assertEqual(self.runs, [])

    def test_a_failed_systemctl_is_a_warning_line(self):
        self._write("START_ON_BOOT=yes\n")

        def failing(argv, **kwargs):
            raise OSError("no systemctl")
        out = op_config.apply_side_effects(
            ["START_ON_BOOT"], self.config, "linux", self.lines.append, run=failing)
        self.assertTrue(out[0].startswith("WARNING"), out)


class TestEveryConfigKeyIsReachable(unittest.TestCase):
    """Done when: every key in config.DEFAULTS and in config-template.txt
    is a `config set` key, except the hand-edit-only groups."""

    def test_defaults(self):
        for key in op_config.DEFAULTS:
            self.assertIn(key, op_config.CLI_CONFIG_KEYS, key)

    def test_template(self):
        template = _read("app/Resources/config-template.txt")
        keys = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", template, re.M))
        self.assertGreater(len(keys), 10)
        for key in keys:
            with self.subTest(key=key):
                self.assertTrue(key in op_config.CLI_CONFIG_KEYS
                                or op_config.is_hand_edit_only(key), key)
        hand_edit = {k for k in keys if op_config.is_hand_edit_only(k)}
        self.assertEqual(hand_edit, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS",
                                     "ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE"})

    def test_help_names_every_key(self):
        self.assertEqual(set(op_config.CONFIG_KEY_HELP), set(op_config.CLI_CONFIG_KEYS))

    def test_safe_keys_the_page_shows_are_settable(self):
        # Everything status.json shows a person can also change (the token
        # is not shown and is settable).
        for key in op_config.SAFE_CONFIG_KEYS:
            self.assertIn(key, op_config.CLI_CONFIG_KEYS, key)


class TestOnionHeavenCommand(_CliCase):
    """`onionpress onionheaven enable|disable|status [--json]` on the Mac
    path (the Linux launcher delegates to the same code)."""

    def _status(self):
        rc, out, err = self.run_cli("onionheaven", "status", "--json")
        self.assertEqual((rc, err), (0, ""))
        return json.loads(out)

    def test_enable_and_disable_write_the_choice(self):
        rc, out, _ = self.run_cli("onionheaven", "disable")
        self.assertEqual(rc, 0)
        self.assertEqual(self.file()["REGISTER_WITH_ONIONHEAVEN"], "no")
        self.assertIn("asks the hub to delete its copy", out)
        self.assertEqual(self.marker(), ["REGISTER_WITH_ONIONHEAVEN"])
        self.assertIn("OnionHeaven disabled by user", self.log())
        self.assertEqual(self._status()["enabled"], False)

        rc, out, _ = self.run_cli("onionheaven", "enable")
        self.assertEqual(rc, 0)
        self.assertEqual(self.file()["REGISTER_WITH_ONIONHEAVEN"], "yes")
        self.assertIn("register with the hub", out)
        self.assertEqual(self._status()["enabled"], True)
        self.assertEqual(self.file()["ADDRESS_PREFIX"], "op2")

    def test_status_text(self):
        rc, out, _ = self.run_cli("onionheaven")
        self.assertEqual(rc, 0)
        self.assertIn("OnionHeaven: enabled", out)  # unset reads as yes
        self.assertIn(f"Hub: {HUB}", out)
        self.run_cli("onionheaven", "disable")
        rc, out, _ = self.run_cli("onionheaven", "status")
        self.assertIn("OnionHeaven: disabled (enable with: onionpress onionheaven enable)", out)
        op_config.write_value(self.config, "REGISTER_WITH_ONIONHEAVEN", "maybe")
        rc, out, _ = self.run_cli("onionheaven", "status")
        self.assertIn("not yes or no", out)
        self.assertIn("onionpress onionheaven enable", out)

    def test_status_json_reports_the_record(self):
        doc = self._status()
        self.assertEqual(doc, {
            "enabled": True, "choice": "yes", "value": "", "hub": HUB,
            "registered": False, "content_address": None, "registered_with": None,
            "may_hold_key": []})
        with open(self.config, "a") as f:
            f.write(f"ONIONHEAVEN_ADDRESS={ONION}\n")
        with open(os.path.join(self.dir, onionheaven.REGISTRATION_RECORD), "w") as f:
            json.dump({"registered": True, "content_address": "b" * 56 + ".onion",
                       "onionheaven_address": HUB,
                       "left_on_hubs": [{"content_address": "c" * 56 + ".onion",
                                         "onionheaven_address": ONION}]}, f)
        doc = self._status()
        self.assertEqual(doc["hub"], ONION)
        self.assertEqual(doc["registered_with"], HUB)
        self.assertEqual(doc["may_hold_key"],
                         [["b" * 56 + ".onion", HUB], ["c" * 56 + ".onion", ONION]])
        rc, out, _ = self.run_cli("onionheaven", "status")
        self.assertIn(f"  {'c' * 56}.onion {ONION}", out)

    def test_a_refused_value_never_registers(self):
        # Only "yes" may ever register (tests/test_onionheaven_opt_in.py):
        # the subcommand writes exactly yes or no.
        rc, _, err = self.run_cli("onionheaven", "maybe")
        self.assertEqual(rc, 1)
        self.assertIn("Usage", err)
        self.assertNotIn("REGISTER_WITH_ONIONHEAVEN", self.file())


class TestCallers(unittest.TestCase):
    """The launchers delegate; the GUIs write config only through the
    command."""

    def test_launcher_cases_delegate(self):
        mac = _read("app/MacOS/onionpress")
        linux = _read("linux/onionpress")
        for src, py, cmds in ((mac, "bundled_python -m onionpress.cli", ("config", "onionheaven")),
                              (linux, "op_py", ("config",))):
            for cmd in cmds:
                pattern = (rf"\n        {cmd}\)\n(?:            #.*\n)*"
                           rf"            {re.escape(py)} --data-dir \"\$DATA_DIR\" {cmd} \"\$\{{@:2\}}\"\n"
                           rf"            ;;\n")
                self.assertRegex(src, pattern, cmd)
        self.assertRegex(linux, r'onionheaven_cmd\(\) \{\n    op_py --data-dir "\$DATA_DIR" onionheaven "\$@"\n\}')
        self.assertRegex(linux, r'onionheaven\)\n(?:            #.*\n)*            onionheaven_cmd "\$\{@:2\}"')
        self.assertNotIn("set_onionheaven_choice", linux)
        self.assertNotIn("onionheaven_explain", linux)

    def test_launcher_help_names_them(self):
        # One text for both platforms (cli.launcher_help, #529).
        from onionpress import cli
        for os_name in ("macos", "linux"):
            help_text = cli.launcher_help(os_name)
            self.assertIn("  config        ", help_text, os_name)
            self.assertIn("  onionheaven   ", help_text, os_name)
            self.assertIn("  onionname     ", help_text, os_name)
            self.assertIn("set KEY - with the", help_text, os_name)

    def test_guis_write_config_only_through_the_cli(self):
        for rel in ("src/onionpress/settings_ui.py", "linux/onionpress-tray"):
            src = _read(rel)
            self.assertNotRegex(src, r"\bwrite_value\(", rel)
            self.assertNotRegex(src, r"op_config\.write_config\(", rel)
        self.assertEqual(config_writers("src/menubar.py"), [])
        menubar = _read("src/menubar.py")
        self.assertNotIn("op_config.write_value(", menubar)
        self.assertNotIn("op_config.write_config(", menubar)
        self.assertIn('args = ["config", "set", key]', menubar)
        self.assertIn("ui_client.run_confirmed(\n            self.launcher, args, self._ask_cli_question", menubar)
        tray = _read("linux/onionpress-tray")
        self.assertIn('Launcher.run("config", "set", "SHARE_ANALYTICS_WITH_ONIONHOME"', tray)

    def test_settings_dialog_shares_the_restart_set(self):
        settings = _read("src/onionpress/settings_ui.py")
        self.assertIn("_NEEDS_RESTART = op_config.NEEDS_RESTART", settings)
        self.assertNotIn("restart_caffeinate", settings)
        self.assertNotIn("add_login_item", settings)

    def test_menubar_login_item_is_the_shared_module(self):
        menubar = _read("src/menubar.py")
        self.assertIn("login_item.install(", menubar)
        self.assertIn("login_item.remove(", menubar)
        self.assertNotIn("plistlib.dump", menubar)


# The census of config writers (issue #474). A write is a call of
# config's raw writers (write_value, remove_value, write_config), or an
# open for writing, os.open, copy, rename or replace whose target is the
# config file: a name or attribute called config_file/config_path, a name
# assigned os.path.join(..., "config") in the same function, or that join
# itself. config.py is the writer; the backup restore's two helpers put a
# restored backup's settings back (RESTORABLE_CONFIG, the restored
# address's prefix and login); nothing else writes.
CONFIG_WRITER_MODULE = "src/onionpress/config.py"
CONFIG_WRITES_ALLOWED = {
    ("src/onionpress/backup.py", "restore_backup_config"),
    ("src/onionpress/backup.py", "match_config_to_restored_address"),
}
_RAW_WRITERS = {"write_value", "remove_value", "write_config"}
_CONFIG_NAMES = {"config_file", "config_path"}


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        prefix = _call_name(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _joins_config(node):
    return (isinstance(node, ast.Call) and _call_name(node.func) == "os.path.join"
            and node.args and isinstance(node.args[-1], ast.Constant)
            and node.args[-1].value == "config")


def _write_mode(call):
    mode = call.args[1] if len(call.args) > 1 else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    return (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
            and any(c in mode.value for c in "wax+"))


def config_writers(rel):
    """[(function, line)] of config writes in *rel* (see above)."""
    tree = ast.parse(_read(rel))
    found = []
    # A bare write_value() is config's when imported from it (settings_ui's
    # write_config is the callback that runs the subcommand).
    imported = {a.asname or a.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and (node.module or "").rsplit(".", 1)[-1] == "config"
                for a in node.names}

    def scan(func_node, name):
        config_vars = set(_CONFIG_NAMES)
        for node in ast.walk(func_node):
            if (isinstance(node, ast.Assign) and _joins_config(node.value)):
                config_vars.update(t.id for t in node.targets if isinstance(t, ast.Name))

        def is_config(node):
            if isinstance(node, ast.Name):
                return node.id in config_vars
            if isinstance(node, ast.Attribute):
                return node.attr in _CONFIG_NAMES
            return _joins_config(node)

        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue
            callee = _call_name(node.func)
            short = callee.rsplit(".", 1)[-1]
            target = None
            if short in _RAW_WRITERS and (isinstance(node.func, ast.Attribute)
                                          or short in imported):
                found.append((name, node.lineno))
                continue
            if callee == "open" and node.args and _write_mode(node):
                target = node.args[0]
            elif callee == "os.open" and node.args:
                target = node.args[0]
            elif short in ("copy", "copy2", "copyfile", "move", "replace", "rename") \
                    and len(node.args) > 1:
                target = node.args[1]
            if target is not None and is_config(target):
                found.append((name, node.lineno))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Innermost function owns the call: scan only its own body.
            body = ast.Module(body=[n for n in node.body
                                    if not isinstance(n, (ast.FunctionDef,
                                                          ast.AsyncFunctionDef))],
                              type_ignores=[])
            scan(body, node.name)
    return sorted(set(found))


class TestConfigWriterCensus(unittest.TestCase):
    """Issue #474: `onionpress config` (config.py) is the one writer of
    ~/.onionpress/config; every other module in src/onionpress and the
    menubar goes through it (set_config_value, set_checked_value,
    apply_settings_updates, or the subcommand through the launcher)."""

    def _modules(self):
        rels = ["src/menubar.py"]
        pkg = os.path.join(SRC, "onionpress")
        for fn in sorted(os.listdir(pkg)):
            if fn.endswith(".py"):
                rels.append(f"src/onionpress/{fn}")
        return rels

    def test_no_module_but_config_writes_the_config(self):
        offenders = []
        for rel in self._modules():
            if rel == CONFIG_WRITER_MODULE:
                continue
            for func, line in config_writers(rel):
                if (rel, func) not in CONFIG_WRITES_ALLOWED:
                    offenders.append(f"{rel}:{line} in {func}()")
        self.assertEqual(offenders, [], "config written outside config.py "
                         "(use config.set_checked_value / set_config_value, or "
                         "`onionpress config set` from a UI)")

    def test_the_census_sees_a_write(self):
        # The census finds what it looks for: config.py's own writers and
        # the allowed restore helpers.
        own = {f for f, _ in config_writers(CONFIG_WRITER_MODULE)}
        self.assertIn("ensure_config", own)
        self.assertEqual({f for f, _ in config_writers("src/onionpress/backup.py")},
                         {f for _, f in CONFIG_WRITES_ALLOWED})

    def test_the_census_sees_each_kind_of_write(self):
        sample = textwrap.dedent("""
            import os, shutil
            from onionpress.config import write_value
            from onionpress import config as op_config

            def a(path):
                op_config.write_value(path, "K", "v")

            def b(path):
                write_value(path, "K", "v")

            def c(self, t):
                shutil.copy2(t, self.config_file)

            def d(data_dir):
                cfg = os.path.join(data_dir, "config")
                with open(cfg, "a") as f:
                    f.write("K=v")

            def e(config_path):
                os.replace(config_path + ".tmp", config_path)

            def reads(config_path, data_dir):
                with open(config_path) as f:
                    f.read()
                with open(os.path.join(data_dir, "config-overrides.json"), "w") as f:
                    pass
                op_config.set_config_value(config_path, "K", "v")
        """)
        tmp = tempfile.mkdtemp(prefix="op-census-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "sample.py")
        with open(path, "w") as f:
            f.write(sample)
        self.assertEqual([f for f, _ in config_writers(path)], ["a", "b", "c", "d", "e"])

    def test_the_allowed_writers_still_exist(self):
        for rel, func in CONFIG_WRITES_ALLOWED:
            self.assertRegex(_read(rel), rf"\ndef {func}\(", rel)


class TestAdminUsernameIsAReadUnlessAsked(unittest.TestCase):
    """backup.get_admin_username reads; a login found in WordPress is
    recorded as ONIONNAME only when the caller passes its log, through
    config.set_checked_value (issue #474)."""

    def setUp(self):
        from onionpress import backup
        self.backup = backup
        self.dir = tempfile.mkdtemp(prefix="op-admin-name-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = os.path.join(self.dir, "config")
        done = mock.Mock(returncode=0, stdout="owner\nother\n")
        p = mock.patch("onionpress.backup.subprocess.run", return_value=done)
        self.run = p.start()
        self.addCleanup(p.stop)

    def test_a_plain_call_writes_nothing(self):
        self.assertEqual(self.backup.find_admin_username(self.dir), ("owner", True))
        self.assertEqual(self.backup.get_admin_username(self.dir), "owner")
        self.assertFalse(os.path.exists(self.cfg))

    def test_with_a_log_the_login_is_recorded_once(self):
        logged = []
        self.assertEqual(self.backup.get_admin_username(self.dir, log=logged.append), "owner")
        self.assertEqual(op_config.read_value(self.cfg, "ONIONNAME"), "owner")
        self.assertEqual(op_config.take_config_changes(self.dir), ["ONIONNAME"])
        self.run.reset_mock()
        self.assertEqual(self.backup.find_admin_username(self.dir), ("owner", False))
        self.run.assert_not_called()

    def test_the_fallback_is_never_recorded(self):
        self.run.return_value = mock.Mock(returncode=1, stdout="")
        self.assertEqual(self.backup.get_admin_username(self.dir, log=lambda m: None), "admin")
        self.assertFalse(os.path.exists(self.cfg))


class TestMenubarWriteConfigValue(unittest.TestCase):
    """menubar.write_config_value runs the launcher's `config set`; the
    marker the CLI leaves is the daemon's to take (onionpress.daemon
    applies caffeinate, the analytics upload and status.json); a refusal
    is False."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-menubar-config-")
        src = _read("src/menubar.py")
        tree = ast.parse(src)
        from onionpress import ui_client
        ns = {"os": os, "subprocess": __import__("subprocess"), "op_config": op_config,
              "ui_client": ui_client}
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "write_config_value")
        exec(textwrap.dedent(ast.get_source_segment(src, node)), ns)
        self.write = ns["write_config_value"]
        self.app = mock.Mock()
        self.app.config_file = os.path.join(self.dir, "config")
        with open(self.app.config_file, "w") as f:
            f.write("VM_MEMORY=2\n")
        self.app.app_support = self.dir
        self.app.launcher_script = "/Applications/OnionPress.app/Contents/MacOS/onionpress"
        self.app.lines = []
        self.app.log = self.app.lines.append
        # The launcher client the menubar runs the command through (#526)
        self.app.launcher = ui_client.Launcher(self.app.launcher_script, self.app.log)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_that_sets(self, key, rc=0):
        """A subprocess.run that does what the CLI does to the files."""
        def run(argv, **kwargs):
            self.argv, self.stdin = list(argv), kwargs.get("input")
            if rc == 0:
                op_config.write_value(self.app.config_file, key, "X")
                op_config.note_config_changed(self.dir, [key])
            return mock.Mock(returncode=rc, stdout="", stderr="ERROR: refused\n")
        return run

    def test_runs_config_set_and_leaves_the_marker_to_the_daemon(self):
        with mock.patch("subprocess.run", self._run_that_sets("PREVENT_SLEEP")):
            ok = self.write(self.app, "PREVENT_SLEEP", "never")
        self.assertTrue(ok)
        self.assertEqual(self.argv, [self.app.launcher_script, "config", "set",
                                     "PREVENT_SLEEP", "never", "--json"])
        self.assertIsNone(self.stdin)
        # The menubar applies nothing itself: the daemon takes the marker.
        self.assertEqual(op_config.take_config_changes(self.dir), ["PREVENT_SLEEP"])
        self.assertNotIn("def apply_config_changes", _read("src/menubar.py"))

    def test_secret_goes_on_stdin(self):
        with mock.patch("subprocess.run", self._run_that_sets("CLOUDFLARE_TUNNEL_TOKEN")):
            ok = self.write(self.app, "CLOUDFLARE_TUNNEL_TOKEN", TOKEN)
        self.assertTrue(ok)
        self.assertEqual(self.argv[-3:], ["CLOUDFLARE_TUNNEL_TOKEN", "-", "--json"])
        self.assertEqual(self.stdin, TOKEN + "\n")
        self.assertNotIn(TOKEN, "\n".join(self.app.lines))

    def test_a_question_is_asked_then_answered_with_confirmed(self):
        """The CLI stops at "prefix-change" (exit 4, the JSON question);
        the menubar asks through _ask_cli_question and runs it again with
        --confirmed; Cancel runs nothing more (onionpress.confirm)."""
        import json
        question = {"confirm": "prefix-change", "current": "op2", "new": "abc",
                    "address": "a" * 56 + ".onion"}
        argvs = []

        def run(argv, **kwargs):
            argvs.append(list(argv))
            if "--confirmed" in argv:
                op_config.write_value(self.app.config_file, "ADDRESS_PREFIX", "abc")
                op_config.note_config_changed(self.dir, ["ADDRESS_PREFIX"])
                return mock.Mock(returncode=0, stdout='{"ok": true}\n', stderr="")
            return mock.Mock(returncode=3, stdout=json.dumps({"error": "needs a confirmation",
                                                                **question}) + "\n", stderr="")
        for answer in (True, False):
            with self.subTest(answer=answer):
                argvs.clear()
                self.app._ask_cli_question = mock.Mock(return_value=answer)
                with mock.patch("subprocess.run", run):
                    ok = self.write(self.app, "ADDRESS_PREFIX", "abc")
                self.assertIs(ok, answer)
                self.app._ask_cli_question.assert_called_once_with(
                    "prefix-change", {"error": "needs a confirmation", **question}, action="Change")
                if answer:
                    self.assertEqual(argvs[1][-2:], ["--confirmed", "prefix-change"])
                    self.assertEqual(op_config.read_value(self.app.config_file, "ADDRESS_PREFIX"), "abc")
                else:
                    self.assertEqual(len(argvs), 1)

    def test_a_refusal_is_false_and_logged(self):
        with mock.patch("subprocess.run", self._run_that_sets("VM_CPU", rc=2)):
            ok = self.write(self.app, "VM_CPU", "0")
        self.assertFalse(ok)
        self.assertIn("refused", "\n".join(self.app.lines))


class TestDaemonAppliesConfigChanges(unittest.TestCase):
    """onionpress.daemon.Daemon.apply_config_changes: what used to be the
    menubar's apply_config_changes (caffeinate, the upload, the tunnel
    flag, status.json)."""

    def setUp(self):
        from onionpress import daemon as op_daemon
        from onionpress.platform import resolve_paths
        self.dir = tempfile.mkdtemp(prefix="op-daemon-config-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = os.path.join(self.dir, "config")
        with open(self.config, "w") as f:
            f.write("VM_MEMORY=2\n")
        paths = resolve_paths(data_dir=self.dir, app_bundle="")
        self.d = op_daemon.Daemon(paths, version="t", launcher_script="/x/onionpress",
                                  docker=mock.Mock(), health_checker=mock.Mock())
        self.d.caffeine = mock.Mock()
        self.d.write_status_to_volume = mock.Mock()
        self.d.is_running = True

    def test_prevent_sleep_restarts_caffeinate_and_rewrites_status(self):
        self.d.apply_config_changes(["PREVENT_SLEEP"])
        self.d.caffeine.stop.assert_called_once_with()
        self.d.caffeine.start.assert_called_once_with()
        self.d.write_status_to_volume.assert_called_once_with()

    def test_sharing_turned_on_triggers_an_upload(self):
        with open(self.config, "a") as f:
            f.write("SHARE_ANALYTICS_WITH_ONIONHOME=yes\n")
        with mock.patch("onionpress.analytics_sharing.trigger_upload") as upload:
            self.d.apply_config_changes(["SHARE_ANALYTICS_WITH_ONIONHOME", "CLOUDFLARE_TUNNEL_TOKEN"])
        upload.assert_called_once_with()
        self.assertFalse(self.d.cloudflare_tunnel_enabled)
        self.d.caffeine.stop.assert_not_called()

    def test_nothing_for_no_keys(self):
        self.d.apply_config_changes([])
        self.d.write_status_to_volume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
