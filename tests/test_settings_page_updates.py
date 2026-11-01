#!/usr/bin/env python3
"""The WordPress Settings page may change only its own config keys.

onionpress-settings.php asks for config changes by writing
config-updates.json into the onionpress-data volume, which anything running
in WordPress can write. Both hosts (the Mac's poll_config_updates and Linux's
apply_config_updates) used to merge every key and value from it into
~/.onionpress/config: image overrides, the OnionHeaven hub address, values
with newlines that add config lines. They now go through one allowlist,
config.SETTINGS_PAGE_KEYS, with a check per value, and never log the
Cloudflare Tunnel token: handle_action.apply_config, on both platforms
(`onionpress apply-config`, and every handle-action run).
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import config as op_config  # noqa: E402
from onionpress import wp_files  # noqa: E402
from onionpress.redact import scrub_for_upload  # noqa: E402
from test_config_one_reader import config_value_function  # noqa: E402

PHP = "app/Resources/plugins/onionpress-settings.php"

TOKEN = "eyJhIjoiMGYxZTJkM2M0YjVhNjk3ODhmN2U2ZDVjNGIzYTI5MTgiLCJ0IjoidHVubmVsIn0="

CONFIG = """\
# OnionPress config
ADDRESS_PREFIX=op2
SHARE_ANALYTICS_WITH_ONIONHOME=yes
START_ON_BOOT=no
REGISTER_WITH_ONIONHEAVEN=
ONIONHEAVEN_ADDRESS=oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion
"""

ATTACKER_ONION = "a" * 56 + ".onion"

# What code inside WordPress could ask for, beside two legitimate changes.
HOSTILE = {
    "SHARE_ANALYTICS_WITH_ONIONHOME": "no",
    "CLOUDFLARE_TUNNEL_TOKEN": TOKEN,
    "ONIONPRESS_TOR_IMAGE": "evil.example/tor:latest",
    "ONIONPRESS_WORDPRESS_IMAGE": "evil.example/wp:latest",
    "ONIONHEAVEN_ADDRESS": ATTACKER_ONION,
    "ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes",
    "ADDRESS_PREFIX": "evil",
    "VM_DISK": "1",
    "PREVENT_SLEEP": "never\nONIONPRESS_TOR_IMAGE=evil.example/tor",
}

# Keys the page must never be able to set.
FORBIDDEN = {
    "ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE",
    "ONIONHEAVEN_ADDRESS", "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "ADDRESS_PREFIX",
    "ONIONPRESS_BIND_HOST", "ONIONPRESS_PORT_OFFSET", "ONIONHOME_ADDRESS",
    "VM_DISK", "VM_CPU", "INSTALL_IA_PLUGIN", "REPUBLISH_ON_UNREACHABLE",
    "ONIONNAME", "ONIONNAME_REGISTERED", "TOR_IMPL",
}


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _php_fields():
    """{field: {"type", "readonly", "min", "max", "options", "config_keys"}}
    parsed from onionpress_settings_fields()."""
    php = _read(PHP)
    body = php[php.index("function onionpress_settings_fields()"):
               php.index("function onionpress_settings_page()")]
    starts = [(m.group(1), m.start())
              for m in re.finditer(r"^        '([A-Z_]+)' => array\(", body, re.M)]
    fields = {}
    for i, (key, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        chunk = body[start:end]
        field = {
            "type": re.search(r"'type'\s*=>\s*'(\w+)'", chunk).group(1),
            "readonly": bool(re.search(r"'readonly'\s*=>\s*true", chunk)),
            "options": None,
            "config_keys": {key},
        }
        for bound in ("min", "max"):
            m = re.search(rf"'{bound}'\s*=>\s*(\d+)", chunk)
            field[bound] = int(m.group(1)) if m else None
        m = re.search(r"'config_key'\s*=>.*'([A-Z_]+)'\s*:\s*'([A-Z_]+)'", chunk)
        if m:
            field["config_keys"] = set(m.groups())
        m = re.search(r"'options'\s*=>\s*array\(", chunk)
        if m:
            field["options"] = _option_keys(chunk, m.end())
        fields[key] = field
    return fields


def _option_keys(text, pos):
    """Keys of a PHP array( 'k' => 'label', ... ) literal opened before pos."""
    depth, i, region_start = 1, pos, pos
    while depth:
        ch = text[i]
        if ch == "'":
            i += 1
            while text[i] != "'":
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return re.findall(r"'((?:[^'\\]|\\.)*)'\s*=>", text[region_start:i - 1])


class TestAllowlist(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-settings-updates-")
        self.config = os.path.join(self.dir, "config")
        with open(self.config, "w") as f:
            f.write(CONFIG)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _config(self):
        with open(self.config) as f:
            return f.read()

    def test_forbidden_keys_are_not_allowlisted(self):
        self.assertEqual(FORBIDDEN & set(op_config.SETTINGS_PAGE_KEYS), set())

    # -- The CLI key set beside the page's (issue #365) --
    #
    # `onionpress config set` (config.CLI_CONFIG_KEYS) is the owner at a
    # terminal, so it reaches more keys than the page: every page key with
    # the same check object, plus the ones only the file could change. What
    # the page may never set splits into keys the CLI accepts (the owner's
    # settings) and keys nobody but a hand edit sets.

    CLI_ONLY = {
        "ADDRESS_PREFIX", "VM_DISK", "VM_CPU", "INSTALL_IA_PLUGIN",
        "REPUBLISH_ON_UNREACHABLE", "ONIONHEAVEN_ADDRESS", "ONIONHOME_ADDRESS",
        "ONIONNAME", "ONIONNAME_REGISTERED",
    }
    HAND_EDIT = {
        "ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE",
        "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "ONIONPRESS_BIND_HOST",
        "ONIONPRESS_PORT_OFFSET",
    }

    def test_cli_keys_include_the_page_keys_with_the_same_checks(self):
        for key, check in op_config.SETTINGS_PAGE_KEYS.items():
            self.assertIs(op_config.CLI_CONFIG_KEYS.get(key), check, key)

    def test_forbidden_keys_split_between_cli_and_hand_edit(self):
        self.assertEqual(FORBIDDEN, self.CLI_ONLY | self.HAND_EDIT | {"TOR_IMPL"})
        for key in self.CLI_ONLY:
            self.assertIn(key, op_config.CLI_CONFIG_KEYS, key)
            self.assertNotIn(key, op_config.SETTINGS_PAGE_KEYS, key)
        for key in self.HAND_EDIT | {"TOR_IMPL"}:
            self.assertNotIn(key, op_config.CLI_CONFIG_KEYS, key)
            self.assertTrue(op_config.check_config_value(key, "x"), key)
        for key in self.HAND_EDIT:
            self.assertTrue(op_config.is_hand_edit_only(key), key)

    def test_hostile_payload_through_the_cli_check(self):
        # The same payload at a terminal: the images and hub admission
        # are refused there too, a value with a newline is refused, and
        # the owner's own settings (the hub address, the prefix) pass
        # the format check (the page still never gets them).
        verdict = {key: op_config.check_config_value(key, value) == ""
                   for key, value in HOSTILE.items()}
        self.assertEqual(verdict, {
            "SHARE_ANALYTICS_WITH_ONIONHOME": True,
            "CLOUDFLARE_TUNNEL_TOKEN": True,   # stdin only, in run_config_command
            "ONIONPRESS_TOR_IMAGE": False,
            "ONIONPRESS_WORDPRESS_IMAGE": False,
            "ONIONHEAVEN_ADDRESS": True,
            "ONIONHEAVEN_ACCEPT_REGISTRATIONS": False,
            "ADDRESS_PREFIX": True,            # a 4-character base32 prefix
            "VM_DISK": False,                  # below the 10 GB floor
            "PREVENT_SLEEP": False,
        })
        for key in op_config.SETTINGS_PAGE_KEYS:
            self.assertNotRegex(key, r"IMAGE|PORT|BIND|PATH|DIR|PASSWORD|SECRET|ACCEPT",
                                f"{key} does not belong on the settings page")

    def test_only_the_page_keys_are_written(self):
        applied = op_config.apply_settings_updates(self.config, HOSTILE)
        config = op_config.read_config(self.config)
        self.assertEqual(config["SHARE_ANALYTICS_WITH_ONIONHOME"], "no")
        # Never set from the page unattended (#463): held for the host.
        self.assertNotIn("CLOUDFLARE_TUNNEL_TOKEN", config)
        self.assertEqual(applied.held, {"CLOUDFLARE_TUNNEL_TOKEN": TOKEN})
        self.assertEqual(config["ONIONHEAVEN_ADDRESS"],
                         "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion")
        self.assertEqual(config["ADDRESS_PREFIX"], "op2")
        for key in ("ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE",
                    "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "VM_DISK", "PREVENT_SLEEP"):
            self.assertNotIn(key, config)
        self.assertNotIn("evil", self._config())
        self.assertEqual(set(applied.rejected), set(HOSTILE) - {
            "SHARE_ANALYTICS_WITH_ONIONHOME", "CLOUDFLARE_TUNNEL_TOKEN"})
        self.assertEqual([k for k, _, _ in applied.changes], ["SHARE_ANALYTICS_WITH_ONIONHOME"])

    def test_a_tunnel_token_is_removed_but_never_set(self):
        """#463: a token puts the site on the clear web through a tunnel
        whose owner sees the host's IP; only removal is unattended."""
        applied = op_config.apply_settings_updates(
            self.config, {"CLOUDFLARE_TUNNEL_TOKEN": TOKEN})
        self.assertEqual((applied.accepted, applied.changes), ({}, []))
        self.assertEqual(applied.held, {"CLOUDFLARE_TUNNEL_TOKEN": TOKEN})
        op_config.write_value(self.config, "CLOUDFLARE_TUNNEL_TOKEN", TOKEN)
        replaced = TOKEN.replace("e", "f")
        applied = op_config.apply_settings_updates(
            self.config, {"CLOUDFLARE_TUNNEL_TOKEN": replaced})
        self.assertEqual(applied.held, {"CLOUDFLARE_TUNNEL_TOKEN": replaced})
        self.assertEqual(op_config.read_value(self.config, "CLOUDFLARE_TUNNEL_TOKEN"), TOKEN)
        applied = op_config.apply_settings_updates(self.config, {"CLOUDFLARE_TUNNEL_TOKEN": ""})
        self.assertEqual(applied.held, {})
        self.assertEqual(op_config.read_value(self.config, "CLOUDFLARE_TUNNEL_TOKEN"), "")

    def test_comments_and_other_keys_survive(self):
        op_config.apply_settings_updates(self.config, {"SHARE_ANALYTICS_WITH_ONIONHOME": "no"})
        self.assertEqual(self._config(), CONFIG.replace(
            "SHARE_ANALYTICS_WITH_ONIONHOME=yes", "SHARE_ANALYTICS_WITH_ONIONHOME=no"))

    def test_unchanged_values_are_accepted_but_not_rewritten(self):
        applied = op_config.apply_settings_updates(
            self.config, {"SHARE_ANALYTICS_WITH_ONIONHOME": "yes", "REGISTER_WITH_ONIONHEAVEN": ""})
        self.assertEqual(applied.changes, [])
        self.assertEqual(applied.accepted,
                         {"SHARE_ANALYTICS_WITH_ONIONHOME": "yes", "REGISTER_WITH_ONIONHEAVEN": ""})
        self.assertEqual(self._config(), CONFIG)

    def test_invalid_values_are_rejected(self):
        bad = [
            ("SHARE_ANALYTICS_WITH_ONIONHOME", "maybe"), ("SHARE_ANALYTICS_WITH_ONIONHOME", "yes "),
            ("SHARE_ANALYTICS_WITH_ONIONHOME", "yes\nVM_DISK=1"), ("SHARE_ANALYTICS_WITH_ONIONHOME", "yes\r"),
            ("SHARE_ANALYTICS_WITH_ONIONHOME", True), ("SHARE_ANALYTICS_WITH_ONIONHOME", None),
            ("REGISTER_WITH_ONIONHEAVEN", "true"), ("PREVENT_SLEEP", "always"),
            ("VM_MEMORY", "0"), ("VM_MEMORY", "65"), ("VM_MEMORY", "-1"),
            ("VM_MEMORY", "2.5"), ("VM_MEMORY", "\uff12"), ("VM_MEMORY", 2),
            ("VM_MEMORY", ""), ("ONIONHEAVEN_PROPAGATION_DELAY", "59"),
            ("ONIONHEAVEN_MAX_SERVICES", "501"), ("ONIONHEAVEN_MAX_SERVICES", "0"),
            ("CLOUDFLARE_TUNNEL_TOKEN", "short"),
            ("CLOUDFLARE_TUNNEL_TOKEN", TOKEN + "\nONIONPRESS_TOR_IMAGE=x"),
            ("CLOUDFLARE_TUNNEL_TOKEN", TOKEN + " x"),
            ("CLOUDFLARE_TUNNEL_TOKEN", "$(touch /tmp/x)" + TOKEN),
            ("CLOUDFLARE_TUNNEL_TOKEN", "x" * 2049),
        ]
        for key, value in bad:
            accepted, rejected = op_config.filter_settings_updates({key: value})
            self.assertEqual((accepted, rejected), ({}, [key]), f"{key}={value!r}")

    def test_valid_values_are_accepted(self):
        good = {
            "LAUNCH_ON_LOGIN": "yes", "START_ON_BOOT": "no",
            "PREVENT_SLEEP": "on-battery", "VM_MEMORY": "64",
            "REGISTER_WITH_ONIONHEAVEN": "", "SHARE_ANALYTICS_WITH_ONIONHOME": "yes",
            "ONIONHEAVEN_MAX_SERVICES": "1", "ONIONHEAVEN_PROPAGATION_DELAY": "3600",
        }
        self.assertEqual(op_config.filter_settings_updates(good), (good, []))
        for token in (TOKEN, TOKEN.rstrip("="), "A-b_c+d/" * 8, ""):
            self.assertEqual(
                op_config.filter_settings_updates({"CLOUDFLARE_TUNNEL_TOKEN": token}),
                ({"CLOUDFLARE_TUNNEL_TOKEN": token}, []))

    def test_not_an_object_writes_nothing(self):
        for payload in (["SHARE_ANALYTICS_WITH_ONIONHOME", "no"], "SHARE_ANALYTICS_WITH_ONIONHOME=no", 7, None):
            applied = op_config.apply_settings_updates(self.config, payload)
            self.assertEqual((applied.accepted, applied.changes), ({}, []))
            self.assertTrue(applied.rejected)
        self.assertEqual(self._config(), CONFIG)

    def test_rejected_names_are_log_safe_and_capped(self):
        payload = {"BAD\nKEY": "x", "x" * 500: "y"}
        payload.update({f"JUNK_{i}": "z" for i in range(50)})
        _, rejected = op_config.filter_settings_updates(payload)
        self.assertEqual(rejected[0], "BAD?KEY")
        self.assertEqual(rejected[1], "x" * 64)
        self.assertEqual(len(rejected), 21)
        self.assertEqual(rejected[-1], "(+32 more)")
        for name in rejected:
            self.assertNotRegex(name, r"[\x00-\x1f]")

    def test_secret_values_never_reach_a_log_line(self):
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN", op_config.SECRET_CONFIG_KEYS)
        self.assertTrue(op_config.SECRET_CONFIG_KEYS.isdisjoint(op_config.SAFE_CONFIG_KEYS))
        for old, new, word in (("", TOKEN, "set"), (TOKEN, TOKEN + "x", "replaced"),
                               (TOKEN, "", "removed")):
            line = op_config.describe_change("CLOUDFLARE_TUNNEL_TOKEN", old, new)
            self.assertEqual(line, f"CLOUDFLARE_TUNNEL_TOKEN: {word}")
        self.assertEqual(op_config.loggable_value("CLOUDFLARE_TUNNEL_TOKEN", TOKEN), "<set>")
        self.assertEqual(op_config.loggable_value("CLOUDFLARE_TUNNEL_TOKEN", ""), "<empty>")
        self.assertEqual(op_config.describe_change("VM_MEMORY", "1", "2"),
                         "VM_MEMORY: '1' → '2'")

    def test_mac_settings_dialog_masks_the_token(self):
        src = _read("src/onionpress/settings_ui.py")
        self.assertIn("op_config.loggable_value(k, new_values[k])", src)
        self.assertNotIn('f"{k}={new_values[k]}"', src)


class TestSettingsPageMatchesAllowlist(unittest.TestCase):
    """What the page sends and what the host accepts must agree, or a field
    silently stops saving."""

    def setUp(self):
        self.fields = _php_fields()
        self.php = _read(PHP)

    def test_editable_fields_are_allowlisted(self):
        self.assertIn("START_ON_BOOT", self.fields)
        for key, field in self.fields.items():
            if field["readonly"]:
                continue
            for config_key in field["config_keys"]:
                self.assertIn(config_key, op_config.SETTINGS_PAGE_KEYS, key)

    def test_read_only_fields_are_not_allowlisted(self):
        read_only = {k for k, f in self.fields.items() if f["readonly"]}
        self.assertEqual(read_only, {"ADDRESS_PREFIX", "ONIONHEAVEN_ADDRESS"})
        self.assertTrue(read_only.isdisjoint(op_config.SETTINGS_PAGE_KEYS))

    def test_every_choice_passes_the_host_check(self):
        for key, field in self.fields.items():
            if not field["options"]:
                continue
            for config_key in field["config_keys"]:
                check = op_config.SETTINGS_PAGE_KEYS[config_key]
                for option in field["options"]:
                    self.assertTrue(check(option), f"{config_key}={option!r}")

    def test_number_ranges_match_the_host_check(self):
        numeric = {k: f for k, f in self.fields.items() if f["min"] is not None}
        self.assertEqual(set(numeric), {
            "VM_MEMORY", "ONIONHEAVEN_MAX_SERVICES", "ONIONHEAVEN_PROPAGATION_DELAY"})
        for key, field in numeric.items():
            check = op_config.SETTINGS_PAGE_KEYS[key]
            self.assertTrue(check(str(field["min"])) and check(str(field["max"])), key)
            self.assertFalse(check(str(field["min"] - 1)) or check(str(field["max"] + 1)), key)

    def test_token_field_is_write_only(self):
        self.assertEqual(self.fields["CLOUDFLARE_TUNNEL_TOKEN"]["type"], "secret")
        # The page checks the host's pattern, and never writes the token
        # into the config-current.json it can read back.
        self.assertIn(f"'#^{op_config.CLOUDFLARE_TOKEN_PATTERN}$#D'", self.php)
        self.assertIn("unset( $current['CLOUDFLARE_TUNNEL_TOKEN'] );", self.php)
        self.assertIn("op_CLOUDFLARE_TUNNEL_TOKEN_remove", self.php)
        render = self.php[self.php.index("$field['type'] === 'secret' ) : ?>"):]
        render = render[:render.index("<?php else : ?>")]
        self.assertIn('value=""', render)
        self.assertNotIn("$val", render)

    def test_writes_under_the_config_key(self):
        # START_ON_BOOT's field is LAUNCH_ON_LOGIN on a Mac; writing it under
        # the field name left Launch on Login unchanged there.
        self.assertIn("$updates[ $config_key ] = $val;", self.php)
        self.assertNotIn("$updates[ $key ] = $val;", self.php)

    def test_tor_impl_field_is_gone(self):
        self.assertNotIn("TOR_IMPL", self.fields)


class TestMacApplyConfig(unittest.TestCase):
    """The Mac's `onionpress apply-config` (onionpress.handle_action.apply_config,
    issue #366; the menubar's poll_config_updates until then) against a
    stubbed WordPress volume: the allowlist, the marker for the running
    menubar, the side effects; a truncated file is left for the next run."""

    def setUp(self):
        import test_handle_action as tha
        from onionpress import handle_action as ha
        from onionpress.platform import resolve_paths
        self.dir = tempfile.mkdtemp(prefix="op-apply-config-mac-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = os.path.join(self.dir, "config")
        with open(self.config, "w") as f:
            f.write(CONFIG.replace("START_ON_BOOT=no", "LAUNCH_ON_LOGIN=yes"))
        self.docker = tha.FakeDocker(os.path.join(self.dir, "sandbox"))
        self.lines = []
        self.host = ha.Host(launcher="/nonexistent/onionpress",
                            paths=resolve_paths(data_dir=self.dir, app_bundle=""),
                            docker=self.docker, log=self.lines.append, os_name="macos")
        self.effects = []
        p = mock.patch.object(op_config, "apply_side_effects",
                              side_effect=lambda keys, *a, **k: self.effects.append(sorted(keys)) or [])
        p.start()
        self.addCleanup(p.stop)
        self.ha = ha

    def _apply(self, payload):
        self.docker.put("config-updates.json", payload)
        with open(os.devnull, "w") as devnull:
            return self.ha.apply_config(self.host, out=devnull)

    def test_hostile_payload(self):
        payload = dict(HOSTILE, LAUNCH_ON_LOGIN="no")
        self.assertEqual(sorted(self._apply(json.dumps(payload))),
                         ["LAUNCH_ON_LOGIN", "SHARE_ANALYTICS_WITH_ONIONHOME"])
        config = op_config.read_config(self.config)
        # Held for the person at the Mac (#463; test_handle_action.TestTunnelToken).
        self.assertNotIn("CLOUDFLARE_TUNNEL_TOKEN", config)
        self.assertEqual(config["LAUNCH_ON_LOGIN"], "no")
        self.assertNotIn("ONIONPRESS_TOR_IMAGE", config)
        self.assertNotIn("PREVENT_SLEEP", config)
        log = "\n".join(self.lines)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("evil", log)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN waits for the person at this Mac", log)
        self.assertIn("ONIONPRESS_TOR_IMAGE", log)
        self.assertIn("ONIONHEAVEN_ADDRESS", log)
        accepted = ["LAUNCH_ON_LOGIN", "SHARE_ANALYTICS_WITH_ONIONHOME"]
        self.assertEqual(self.effects, [accepted])
        # What the running menubar owes (caffeinate, the tunnel flag,
        # status.json) waits in the marker it takes on its next tick.
        self.assertEqual(sorted(op_config.take_config_changes(self.dir)), accepted)
        self.assertIsNone(self.docker.get("config-updates.json"))
        # Read and removed as www-data, whose folder it is: root's cat or
        # rm there could be led by a link it planted.
        for call in self.docker.calls:
            self.assertEqual(call[:3], ["exec", "-u", "www-data"], call)

    def test_side_effects_follow_accepted_values(self):
        self._apply(json.dumps({
            "PREVENT_SLEEP": "never", "LAUNCH_ON_LOGIN": "yes",
            "SHARE_ANALYTICS_WITH_ONIONHOME": "yes"}))
        self.assertEqual(self.effects, [["LAUNCH_ON_LOGIN", "PREVENT_SLEEP",
                                         "SHARE_ANALYTICS_WITH_ONIONHOME"]])
        self.assertEqual(sorted(op_config.take_config_changes(self.dir)),
                         ["LAUNCH_ON_LOGIN", "PREVENT_SLEEP", "SHARE_ANALYTICS_WITH_ONIONHOME"])

    def test_truncated_json_is_left_for_the_next_run(self):
        self.assertIsNone(self._apply('{"SHARE_ANALYTICS_WITH_ONIONHOME": "n'))
        self.assertEqual(self.docker.get("config-updates.json"), '{"SHARE_ANALYTICS_WITH_ONIONHOME": "n')
        self.assertEqual(self.effects, [])

    def test_the_menubar_runs_it_through_the_launcher(self):
        src = _read("src/menubar.py")
        self.assertNotIn("def poll_config_updates", src)
        self.assertNotIn("apply_settings_updates(", src)
        mac = _read("app/MacOS/onionpress")
        self.assertIn('bundled_python -m onionpress.cli --data-dir "$DATA_DIR" apply-config', mac)
        handler = _read("src/onionpress/handle_action.py")
        self.assertIn('host.run_launcher(["apply-config"]', handler)
        # and every handle-action run applies it itself, on both platforms
        # (tests/test_handle_action.py)
        self.assertIn("    applied = apply_config(host)\n", handler)
        self.assertNotIn('if host.os_name == "macos" else []', handler)


class TestLinuxApplyConfig(unittest.TestCase):
    """Linux's `onionpress apply-config` is the same handle_action.apply_config
    (until 2026-10-31 the launcher's bash apply_config_updates, run from its
    status loop every 30 s, with its own systemctl copy of the START_ON_BOOT
    side effect): the allowlist, the tunnel token refused outright (#463;
    on a Mac it waits for the person at the Mac), START_ON_BOOT re-applied
    through config.apply_side_effects (whose Linux branch runs the user
    units: tests/test_cli_config.py) even when unchanged."""

    def setUp(self):
        import test_handle_action as tha
        from onionpress import handle_action as ha
        from onionpress.platform import resolve_paths
        self.dir = tempfile.mkdtemp(prefix="op-apply-config-linux-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = os.path.join(self.dir, "config")
        with open(self.config, "w") as f:
            f.write(CONFIG)
        self.docker = tha.FakeDocker(os.path.join(self.dir, "sandbox"))
        self.lines = []
        self.host = ha.Host(launcher="/nonexistent/onionpress",
                            paths=resolve_paths(data_dir=self.dir, app_bundle=""),
                            docker=self.docker, log=self.lines.append, os_name="linux")
        self.effects = []
        p = mock.patch.object(op_config, "apply_side_effects",
                              side_effect=lambda keys, *a, **k: self.effects.append(sorted(keys)) or [])
        p.start()
        self.addCleanup(p.stop)
        self.ha = ha

    def _apply(self, payload):
        self.docker.put("config-updates.json", payload)
        with open(os.devnull, "w") as devnull:
            return self.ha.apply_config(self.host, out=devnull)

    def test_hostile_payload(self):
        accepted = self._apply(json.dumps(dict(HOSTILE, START_ON_BOOT="yes")))
        self.assertEqual(sorted(accepted), ["SHARE_ANALYTICS_WITH_ONIONHOME", "START_ON_BOOT"])
        config = op_config.read_config(self.config)
        self.assertEqual(config["SHARE_ANALYTICS_WITH_ONIONHOME"], "no")
        self.assertEqual(config["START_ON_BOOT"], "yes")
        self.assertNotIn("CLOUDFLARE_TUNNEL_TOKEN", config)  # refused on Linux (#463)
        self.assertEqual(config["ONIONHEAVEN_ADDRESS"],
                         "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion")
        for key in ("ONIONPRESS_TOR_IMAGE", "ONIONPRESS_WORDPRESS_IMAGE",
                    "ONIONHEAVEN_ACCEPT_REGISTRATIONS", "VM_DISK", "PREVENT_SLEEP"):
            self.assertNotIn(key, config)
        with open(self.config) as f:
            raw = f.read()
        self.assertTrue(raw.startswith("# OnionPress config\n"), "comments dropped")
        self.assertNotIn("evil", raw)
        log = "\n".join(self.lines)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("evil", log)
        self.assertIn("onionpress config set CLOUDFLARE_TUNNEL_TOKEN -", log)
        self.assertRegex(log, r"ignored.*ONIONPRESS_TOR_IMAGE")
        self.assertRegex(log, r"ignored.*ONIONHEAVEN_ADDRESS")
        self.assertEqual(self.effects, [["SHARE_ANALYTICS_WITH_ONIONHOME", "START_ON_BOOT"]])
        self.assertIsNone(self.docker.get("config-updates.json"))
        # Read and removed as www-data, whose folder it is.
        for call in self.docker.calls:
            self.assertEqual(call[:3], ["exec", "-u", "www-data"], call)

    def test_start_on_boot_is_reapplied_when_unchanged(self):
        self.assertEqual(self._apply(json.dumps({"START_ON_BOOT": "no"})), ["START_ON_BOOT"])
        self.assertEqual(self.effects, [["START_ON_BOOT"]])

    def test_truncated_json_is_left_for_the_next_run(self):
        self.assertIsNone(self._apply('{"SHARE_ANALYTICS_WITH_ONIONHOME": "n'))
        self.assertEqual(self.docker.get("config-updates.json"), '{"SHARE_ANALYTICS_WITH_ONIONHOME": "n')
        with open(self.config) as f:
            self.assertEqual(f.read(), CONFIG)
        self.assertEqual(self.effects, [])

    def test_the_launcher_delegates(self):
        linux = _read("linux/onionpress")
        self.assertRegex(linux, r'\n        apply-config\)\n(?:            #.*\n)*'
                                r'            op_py --data-dir "\$DATA_DIR" apply-config\n')
        self.assertNotIn("apply_config_updates", linux)
        self.assertNotIn("systemctl --user enable onionpress.service", linux)


class TestOldLogLinesAreScrubbed(unittest.TestCase):
    """Logs already on disk carry the token as older versions logged it;
    they are scrubbed on their way to OnionHome."""

    def test_scrub_for_upload(self):
        raw = (
            f"[t] Settings page: applying 2 config update(s)\n"
            f"[t]   CLOUDFLARE_TUNNEL_TOKEN: '' → '{TOKEN}'\n"
            f"[t]   CLOUDFLARE_TUNNEL_TOKEN: '{TOKEN}' → ''\n"
            f"[t] Settings updated: VM_MEMORY=2, CLOUDFLARE_TUNNEL_TOKEN={TOKEN}, X=y\n"
            f"[t]   CLOUDFLARE_TUNNEL_TOKEN: replaced\n"
        ).encode()
        with tempfile.TemporaryDirectory() as app_support:
            out = scrub_for_upload(raw, app_support, "onionpress.log").decode()
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN[:20], out)
        self.assertEqual(out.count("CLOUDFLARE_TUNNEL_TOKEN: <redacted>"), 2)
        self.assertIn("VM_MEMORY=2, CLOUDFLARE_TUNNEL_TOKEN=<redacted>, X=y", out)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN: replaced", out)


class TestMacLauncherKeepsTokenPadding(unittest.TestCase):
    """The Mac launcher read the token with `cut -d= -f2`, which keeps only
    the text up to the next "=": a base64 token's trailing padding was cut
    off (#323). It now reads everything after the first "=", the first
    line only, through config_value, as config.read_value does (#404)."""

    def test_token_read_whole(self):
        src = _read("app/MacOS/onionpress")
        m = re.search(r"^# Read Cloudflare Tunnel token.*?^export CLOUDFLARE_TUNNEL_TOKEN$",
                      src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m)
        with tempfile.TemporaryDirectory() as tmp:
            for token in (TOKEN, TOKEN + "=", "abc"):
                with self.subTest(token=token):
                    with open(os.path.join(tmp, "config"), "w") as f:
                        f.write(f"VM_CPU=2\nCLOUDFLARE_TUNNEL_TOKEN={token}\n"
                                "CLOUDFLARE_TUNNEL_TOKEN=second\n")
                    out = subprocess.run(
                        ["bash", "-c", config_value_function() + m.group(0)
                         + '\nprintf "%s" "$CLOUDFLARE_TUNNEL_TOKEN"'],
                        capture_output=True, text=True, timeout=30,
                        env={**os.environ, "DATA_DIR": tmp})
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertEqual(out.stdout, token)
                    self.assertEqual(op_config.read_value(
                        os.path.join(tmp, "config"), "CLOUDFLARE_TUNNEL_TOKEN"), token)


if __name__ == "__main__":
    unittest.main()
