"""The OnionHeaven screen: the Mac dialog (onionheaven_ui) and the
WordPress page, both views of onionheaven.overview
(tests/test_onionheaven_overview.py holds the data side)."""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import onionheaven  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
HUB = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
OTHER_HUB = "heavenwyojfljooa7lqsaj2xuid5zzzzg6zdmen4khvdgajgxbenyjqw.onion"
SITE = "op2ujzdegxdncf32epf3dhodzdocis2jhtlgmxgedn73u55xtplpft7v.onion"
SITE2 = "op2x6hh7566tfjgvq6kbnxjbtfqxkwovompzom7wbbr6qmw4wxfogo6m.onion"
HC = "op2hc4seh2kvj72ceuvw75efr6edt4sywb5wkh7dnsipzz7fk4zri3r2.onion"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestDialogWords(unittest.TestCase):
    """The Mac dialog's pure helpers, importable here with AppKit and rumps
    stubbed (the module itself needs a Mac)."""

    MODULES = ("onionpress.onionheaven_ui", "onionpress.settings_ui", "onionpress.ui_helpers")

    @classmethod
    def setUpClass(cls):
        import inspect
        cls.saved = {name: sys.modules.get(name) for name in cls.MODULES + ("AppKit", "rumps")}
        appkit = sys.modules.get("AppKit")
        if appkit is None or not inspect.isclass(getattr(appkit, "NSObject", None)):
            # No AppKit here (or another test left a stub without the class
            # the module subclasses): a stub with what the import needs.
            appkit = types.ModuleType("AppKit")
            appkit.NSObject = type("NSObject", (), {})
            sys.modules["AppKit"] = appkit
        if "rumps" not in sys.modules:
            sys.modules["rumps"] = types.ModuleType("rumps")
        for name in cls.MODULES:
            sys.modules.pop(name, None)
        from onionpress import onionheaven_ui
        cls.ui = onionheaven_ui

    @classmethod
    def tearDownClass(cls):
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_ago(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 25, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self.ui.ago("2026-10-25T09:59:20Z", now), "40 s ago")
        self.assertEqual(self.ui.ago("2026-10-25T09:54:00+00:00", now), "6 min ago")
        self.assertEqual(self.ui.ago("2026-10-25T07:00:00+00:00", now), "3 h ago")
        self.assertEqual(self.ui.ago("2026-10-23T07:00:00+00:00", now), "2 d ago")
        self.assertEqual(self.ui.ago(None, now), "")
        self.assertEqual(self.ui.ago("garbage", now), "")

    def test_headlines(self):
        c = {"enabled": True, "registered": True, "last_heartbeat_at": None, "value": "yes"}
        self.assertEqual(self.ui.client_headline(c)[0], "This site is kept reachable by a hub")
        c = {"enabled": True, "registered": False, "value": "yes"}
        self.assertIn("Pending", self.ui.client_headline(c)[1])
        c = {"enabled": False, "registered": False, "value": "no"}
        self.assertIn("Disabled", self.ui.client_headline(c)[1])
        c = {"enabled": None, "registered": False, "value": "nope"}
        self.assertIn("'nope'", self.ui.client_headline(c)[1])

    def test_hub_badge(self):
        known = {"hub_known_as": "the Internet Archive's OnionHeaven", "hub_kind": "onionheaven",
                 "hub_is_default": True}
        self.assertEqual(self.ui.hub_badge(known),
                         ("Known host · the Internet Archive's OnionHeaven (default)", "known"))
        home = {"hub_known_as": "OnionHome", "hub_kind": "onionhome", "hub_is_default": False}
        self.assertIn("not an OnionHeaven hub", self.ui.hub_badge(home)[0])
        self.assertEqual(self.ui.hub_badge({"hub_known_as": None})[1], "unknown")

    def test_hub_headline(self):
        self.assertIn("not known", self.ui.hub_headline(None)[0])
        self.assertEqual(self.ui.hub_headline({"active": True, "registered_count": 1})[0],
                         "This OnionPress is an OnionHeaven hub for 1 site")
        self.assertEqual(self.ui.hub_headline({"active": True, "registered_count": 23})[0],
                         "This OnionPress is an OnionHeaven hub for 23 sites")
        self.assertIn("not a hub", self.ui.hub_headline({"active": False})[0])

    def test_dialog_writes_only_through_the_callback(self):
        src = _read("src/onionpress/onionheaven_ui.py")
        self.assertNotRegex(src, r"\bwrite_value\(")
        self.assertNotIn("op_config.write_config(", src)
        self.assertIn("confirm_and_write_changes(", src)
        self.assertNotIn("docker", src.lower().replace("no docker", ""))
        # Unset shows checked, as the Settings dialog showed it.
        self.assertIn('("REGISTER_WITH_ONIONHEAVEN", "yes")', src)
        self.assertIn('if old_values["REGISTER_WITH_ONIONHEAVEN"].strip() == "":', src)


class TestWiring(unittest.TestCase):

    def test_menubar_opens_the_screen_from_the_state(self):
        src = _read("src/menubar.py")
        self.assertIn('rumps.MenuItem("OnionHeaven...", callback=self.open_onionheaven)', src)
        body = src[src.index("def open_onionheaven("):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("show_onionheaven_dialog(", body)
        self.assertIn("state=self.state if self._state_fresh else None", body)
        self.assertIn("callbacks={'write_config': self.write_config_value}", body)

    def test_settings_dialog_no_longer_holds_the_hub_rows(self):
        src = _read("src/onionpress/settings_ui.py")
        self.assertNotIn('"OnionHeaven Hub (advanced):"', src)
        self.assertNotIn('("ONIONHEAVEN_ADDRESS",', src)
        self.assertIn("def confirm_and_write_changes(", src)
        self.assertIn("def validate_hub_address(", src)

    def test_wordpress_page(self):
        php = _read("app/Resources/plugins/onionpress-settings.php")
        page = php[php.index("function onionpress_onionheaven_page()"):]
        page = page[:page.index("\nfunction onionpress_settings_site_page()")]
        # The owner's, before the host's files are read.
        gate = page.index("onionpress_settings_owner_allowed()")
        self.assertIn("wp_die( 'Unauthorized' );", page[gate:gate + 120])
        self.assertLess(gate, page.index("/var/lib/onionpress/status.json"))
        self.assertLess(gate, page.index("onionpress_settings_fields()"))
        # Its form is the settings form: the save handler applies the keys.
        self.assertIn("wp_nonce_field( 'onionpress_settings_save', 'onionpress_settings_nonce' )", page)
        self.assertIn('name="op_REGISTER_WITH_ONIONHEAVEN"', page)
        for key in ("ONIONHEAVEN_PROPAGATION_DELAY", "ONIONHEAVEN_MAX_SERVICES"):
            self.assertIn(f"'{key}'", page)
        self.assertNotIn('name="op_ONIONHEAVEN_ADDRESS"', page)  # shown, never changed here
        # Every address shown passed the v3 check.
        self.assertGreaterEqual(page.count("onionpress_oh_is_v3("), 7)
        # The submenu is the owner's.
        menu = php[php.index("add_submenu_page("):]
        self.assertIn("'onionpress-onionheaven'", menu[:400])
        self.assertEqual(php.count("add_submenu_page("), 1)
        before = php[php.index("add_submenu_page(") - 80:php.index("add_submenu_page(")]
        self.assertIn("if ( onionpress_settings_owner_allowed() ) {", before)

    def test_wordpress_fields_moved_off_the_main_page(self):
        php = _read("app/Resources/plugins/onionpress-settings.php")
        body = php[php.index("function onionpress_settings_fields()"):php.index("function onionpress_settings_page()")]
        for key in ("REGISTER_WITH_ONIONHEAVEN", "ONIONHEAVEN_ADDRESS", "ONIONHEAVEN_MAX_SERVICES",
                    "ONIONHEAVEN_PROPAGATION_DELAY"):
            chunk = body[body.index(f"'{key}' => array("):]
            chunk = chunk[:chunk.index("),\n")]
            self.assertIn("'page'        => 'onionheaven'", chunk, key)
        self.assertEqual(body.count("'page'        => 'onionheaven'"), 4)
        main = php[php.index("function onionpress_settings_page()"):php.index("function onionpress_onionheaven_page()")]
        self.assertIn("if ( ! empty( $field['page'] ) ) {\n                    continue;", main)
        self.assertIn("admin.php?page=onionpress-onionheaven", main)



if __name__ == "__main__":
    unittest.main()
