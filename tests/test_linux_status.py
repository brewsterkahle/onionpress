import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import linux_status  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TestBuild(unittest.TestCase):
    def test_every_field_lands(self):
        f = linux_status.parse_fields([
            "state=running", "version=2.6.0", "onion_address=x.onion",
            "uptime_seconds=12", "bootstrap_pct=100",
            'containers={"onionpress-tor": "running"}',
            "wayback_queue_count=3", "updated_at=now",
            "oh_server_active=true", "oh_registered_count=4",
            "oh_online_count=3", "oh_taken_over_count=1",
            "oh_takeover_containers=2", "oh_client_registered=true",
            "oh_client_hub=h.onion", "oh_client_enabled=yes",
            "load_avg=[0.1, 0.2, 0.3]", "host_uptime_seconds=99",
            "wp_installed=true", "onboarded=true", "mkp224o_available=true",
            "wp_admin_password_path=/p", "vanity_in_progress=true",
            "serving_from_wayback=true", "wp_port=8081",
            'onionheaven_overview={"client": {"a": 1}, "secret": 2}',
        ])
        s = linux_status.build(f)
        self.assertEqual(s["platform"], "linux")
        self.assertEqual(s["uptime_seconds"], 12)
        self.assertEqual(s["containers"], {"onionpress-tor": "running"})
        self.assertEqual(s["load_avg"], [0.1, 0.2, 0.3])
        oh = s["onionheaven"]
        self.assertTrue(oh["server_active"])
        self.assertTrue(oh["client_enabled"])
        self.assertEqual(oh["takeover_containers"], 2)
        self.assertEqual(oh["client"], {"a": 1})
        self.assertNotIn("secret", oh)
        self.assertEqual(s["wp_admin_password_path"], "/p")
        self.assertEqual(s["wp_port"], 8081)

    def test_defaults_and_unreadable_values(self):
        s = linux_status.build(linux_status.parse_fields(
            ["uptime_seconds=abc", "containers=not json",
             "onionheaven_overview=[1]", "wp_admin_password_path="]))
        self.assertEqual(s["uptime_seconds"], 0)
        self.assertEqual(s["containers"], {})
        self.assertIsNone(s["wp_admin_password_path"])
        self.assertFalse(s["onionheaven"]["client_enabled"])
        self.assertEqual(s["wp_port"], 8080)
        json.dumps(s)

    def test_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            linux_status.parse_fields(["nope=1"])
        with self.assertRaises(ValueError):
            linux_status.parse_fields(["state"])


class TestLauncherUsesIt(unittest.TestCase):
    def test_write_status_passes_named_fields_only(self):
        with open(os.path.join(ROOT, "linux", "onionpress")) as f:
            text = f.read()
        self.assertNotIn("sys.argv[26]", text)
        used = set(re.findall(r'--field "([a-z0-9_]+)=', text))
        self.assertEqual(used, set(linux_status.FIELDS))


if __name__ == "__main__":
    unittest.main()
