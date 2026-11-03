import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import hub_record  # noqa: E402

A = "a" * 56 + ".onion"
B = "b" * 56 + ".onion"
HUB = "c" * 56 + ".onion"


def _source(rel):
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, rel)) as f:
        return f.read()


class TestRecord(unittest.TestCase):
    def test_left_on_hubs_drops_malformed_entries(self):
        record = {"left_on_hubs": [
            {"content_address": A, "onionheaven_address": HUB},
            {"content_address": "x", "onionheaven_address": HUB},
            "junk"]}
        self.assertEqual(len(hub_record.left_on_hubs(record)), 1)
        self.assertEqual(hub_record.left_on_hubs({"left_on_hubs": 3}), [])

    def test_note_is_once_and_forget_reports(self):
        record = {}
        hub_record.note_left_on_hub(record, A, HUB, "superseded")
        hub_record.note_left_on_hub(record, A, HUB, "again")
        self.assertEqual(len(record["left_on_hubs"]), 1)
        self.assertTrue(hub_record.forget_left_on_hub(record, A, HUB))
        self.assertFalse(hub_record.forget_left_on_hub(record, A, HUB))

    def test_carry_over(self):
        previous = {"left_on_hubs": [1], "released": {"x": 1}}
        out = hub_record.carry_over({"registered": True}, previous)
        self.assertEqual(out["left_on_hubs"], [1])
        out = hub_record.carry_over({"left_on_hubs": None}, previous)
        self.assertNotIn("left_on_hubs", out)
        self.assertEqual(out["released"], {"x": 1})
        self.assertTrue(hub_record.missing_carried({"registered": True}))
        self.assertFalse(hub_record.missing_carried(
            {"left_on_hubs": None, "released": None}))

    def test_hold(self):
        record = {"released": {"content_address": A, "until": 100}}
        self.assertTrue(hub_record.address_on_hold(record, A, now=99))
        self.assertFalse(hub_record.address_on_hold(record, A, now=100))
        self.assertFalse(hub_record.address_on_hold(record, B, now=99))
        self.assertFalse(hub_record.address_on_hold({}, A, now=1))


class TestBothClientsUseIt(unittest.TestCase):
    """The record's rules live in hub_record only (issue #407; the Linux
    heartbeat client, its second reader, is gone)."""

    def test_no_second_copy(self):
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(__file__), "..", "linux",
                                                     "onionpress-heartbeat.py")))
        for rel in ("src/onionpress/onionheaven.py",):
            text = _source(rel)
            self.assertIn("hub_record", text, rel)
            self.assertNotIn('"left_on_hubs")\n    if not isinstance', text, rel)
            self.assertNotIn('"released")\n    if not isinstance', text, rel)


if __name__ == "__main__":
    unittest.main()
