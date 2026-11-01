"""onionheaven.overview: one view of both OnionHeaven roles (which hub keeps
this site reachable, whom this install keeps reachable), what the
registration record keeps for it, and the plumbing that carries it: the
Mac daemon's state file and status.json, `onionpress onionheaven
overview`, the Linux status writer and heartbeat."""

import io
import json
import os
import re
import sys
import tempfile
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


class FakeApp:
    is_onionheaven = False
    healthcheck_address = HC

    def __init__(self, data_dir, config=None, onion_address=SITE):
        self.app_support = data_dir
        self.config_file = os.path.join(data_dir, "config")
        self.bin_dir = ""
        self.colima_home = data_dir
        self.docker_socket = "/nonexistent.sock"
        self.onion_address = onion_address
        self.version = "0.0.0"
        self.logs = []
        self._config = dict(config or {})

    def read_config_value(self, key, default=""):
        return self._config.get(key, default)

    def log(self, msg):
        self.logs.append(msg)


class TestRecordTimestamps(unittest.TestCase):
    """_mark_registered keeps when the registration began and when the
    last beat was accepted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = FakeApp(self.tmp.name)
        self._hub = onionheaven.ONIONHEAVEN_ADDRESS
        onionheaven.ONIONHEAVEN_ADDRESS = HUB

    def tearDown(self):
        onionheaven.ONIONHEAVEN_ADDRESS = self._hub
        self.tmp.cleanup()

    def _record(self):
        return onionheaven._load_registration_status(self.app)

    def test_first_beat_sets_both(self):
        onionheaven._mark_registered(self.app, SITE)
        rec = self._record()
        self.assertTrue(rec["registered"])
        self.assertTrue(rec["registered_at"])
        self.assertEqual(rec["registered_at"], rec["last_heartbeat_at"])

    def test_later_beats_keep_registered_at(self):
        onionheaven._mark_registered(self.app, SITE)
        rec = self._record()
        rec["registered_at"] = "2026-09-14T09:12:00+00:00"
        rec["last_heartbeat_at"] = "2026-09-14T09:12:00+00:00"
        onionheaven._save_registration_status(self.app, rec)
        onionheaven._mark_registered(self.app, SITE)  # the early-return path
        rec = self._record()
        self.assertEqual(rec["registered_at"], "2026-09-14T09:12:00+00:00")
        self.assertNotEqual(rec["last_heartbeat_at"], "2026-09-14T09:12:00+00:00")

    def test_a_new_hub_starts_a_new_registration(self):
        onionheaven._mark_registered(self.app, SITE)
        rec = self._record()
        rec["registered_at"] = "2026-09-14T09:12:00+00:00"
        onionheaven._save_registration_status(self.app, rec)
        onionheaven.ONIONHEAVEN_ADDRESS = OTHER_HUB
        onionheaven._mark_registered(self.app, SITE)
        rec = self._record()
        self.assertNotEqual(rec["registered_at"], "2026-09-14T09:12:00+00:00")
        self.assertEqual(rec["onionheaven_address"], OTHER_HUB)
        self.assertEqual([(e["content_address"], e["onionheaven_address"])
                          for e in rec["left_on_hubs"]], [(SITE, HUB)])


class TestKnownHosts(unittest.TestCase):

    def test_the_two_hosts(self):
        self.assertEqual(onionheaven.known_host(HUB)["kind"], "onionheaven")
        self.assertEqual(onionheaven.known_host(onionheaven.DEFAULT_ONIONHOME_ADDRESS)["kind"],
                         "onionhome")
        self.assertIsNone(onionheaven.known_host(OTHER_HUB))
        self.assertIsNone(onionheaven.known_host(""))
        self.assertIsNone(onionheaven.known_host(None))

    def test_copies(self):
        a = onionheaven.known_host(HUB)
        a["name"] = "x"
        self.assertNotEqual(onionheaven.known_host(HUB)["name"], "x")


class TestHubRegistryRows(unittest.TestCase):
    """The registry is data anything on the hub volume can write: every row
    is checked before the screen shows it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = FakeApp(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _docker(self, answers):
        def run(app, args, timeout=15, input=None):
            self.calls.append(args)
            for key, value in answers.items():
                if key in " ".join(args):
                    return value
            return False, ""
        return run

    def test_rows_are_checked(self):
        rows = "\t".join([SITE, "online", "2026-10-25T10:00:00Z", "2026-10-25T10:00:30Z", "2.5.7", "1"])
        hostile = "\t".join(["../../etc/passwd", "online", "x", "x", "x", "1"])
        odd = "\t".join([SITE2, "ÉVIL <b>", "x" * 100, "", "v\x01", "0"])
        short = "only\ttwo"
        with mock.patch.object(onionheaven, "_run_docker",
                               self._docker({"FROM registry": (True, "\n".join([rows, hostile, odd, short]))})):
            sites = onionheaven.hub_sites(self.app)
        self.assertEqual([s["content_address"] for s in sites], [SITE, SITE2])
        self.assertEqual(sites[0]["status"], "online")
        self.assertEqual(sites[0]["version"], "2.5.7")
        self.assertIs(sites[0]["wordpress_healthy"], True)
        self.assertEqual(sites[1]["status"], "unknown")
        self.assertEqual(sites[1]["last_healthy"], "")
        self.assertEqual(sites[1]["version"], "")
        self.assertIs(sites[1]["wordpress_healthy"], False)
        argv = self.calls[0]
        self.assertEqual(argv[:5], ["exec", "onionheaven", "sqlite3", "-separator", "\t"])
        self.assertEqual(argv[5], "/var/lib/onionheaven/registry.db")
        self.assertIn("unregistered_at IS NULL", argv[6])
        self.assertIn("LIMIT 200", argv[6])

    def test_no_answer_is_none_not_empty(self):
        with mock.patch.object(onionheaven, "_run_docker", self._docker({})):
            self.assertIsNone(onionheaven.hub_sites(self.app))
            self.assertIsNone(onionheaven.hub_counts(self.app))

    def test_counts(self):
        with mock.patch.object(onionheaven, "_run_docker",
                               self._docker({"COUNT(*)": (True, "23\t21\t2")})):
            self.assertEqual(onionheaven.hub_counts(self.app),
                             {"registered_count": 23, "online_count": 21, "taken_over_count": 2})
        with mock.patch.object(onionheaven, "_run_docker",
                               self._docker({"COUNT(*)": (True, "garbage")})):
            self.assertIsNone(onionheaven.hub_counts(self.app))

    def test_worker_names_are_checked(self):
        with mock.patch.object(onionheaven, "_run_docker",
                               self._docker({"ps": (True, "onionheaven-takeover-1\nonionheaven-takeover-x\nonionpress-wordpress\n")})):
            self.assertEqual(onionheaven.takeover_worker_count(self.app), 1)


class TestOverview(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._hub = onionheaven.ONIONHEAVEN_ADDRESS
        onionheaven.ONIONHEAVEN_ADDRESS = HUB

    def tearDown(self):
        onionheaven.ONIONHEAVEN_ADDRESS = self._hub
        self.tmp.cleanup()

    def _app(self, config=None, record=None, **kw):
        app = FakeApp(self.tmp.name, config, **kw)
        if record is not None:
            with open(os.path.join(self.tmp.name, onionheaven.REGISTRATION_RECORD), "w") as f:
                json.dump(record, f)
        return app

    def test_client_registered_with_the_default_hub(self):
        app = self._app(record={"registered": True, "onionheaven_address": HUB,
                                "content_address": SITE,
                                "registered_at": "2026-09-14T09:12:00+00:00",
                                "last_heartbeat_at": "2026-10-25T10:00:00+00:00"})
        c = onionheaven.client_overview(app)
        self.assertIs(c["enabled"], True)
        self.assertTrue(c["registered"])
        self.assertEqual(c["hub"], HUB)
        self.assertEqual(c["hub_known_as"], "the Internet Archive's OnionHeaven")
        self.assertTrue(c["hub_is_default"])
        self.assertEqual(c["content_address"], SITE)
        self.assertEqual(c["healthcheck_address"], HC)
        self.assertEqual(c["registered_at"], "2026-09-14T09:12:00+00:00")
        self.assertEqual(c["last_heartbeat_at"], "2026-10-25T10:00:00+00:00")
        self.assertEqual(c["left_on_hubs"], [])

    def test_client_custom_hub_with_a_copy_left_behind(self):
        onionheaven.ONIONHEAVEN_ADDRESS = OTHER_HUB
        app = self._app(config={"ONIONHEAVEN_ADDRESS": OTHER_HUB},
                        record={"registered": False, "onionheaven_address": OTHER_HUB,
                                "left_on_hubs": [
                                    {"content_address": SITE, "onionheaven_address": HUB,
                                     "since": "2026-10-24T20:41:00+00:00", "reason": "hub changed"},
                                    {"content_address": SITE2, "onionheaven_address": HUB,
                                     "since": "2026-10-01T00:00:00+00:00", "reason": "address changed"}]})
        c = onionheaven.client_overview(app)
        self.assertEqual(c["hub"], OTHER_HUB)
        self.assertIsNone(c["hub_known_as"])
        self.assertFalse(c["hub_is_default"])
        self.assertFalse(c["registered"])
        self.assertEqual(c["content_address"], SITE)  # the served address
        left = c["left_on_hubs"]
        self.assertEqual([e["key_here"] for e in left], [True, False])
        self.assertEqual(left[0]["hub_known_as"], "the Internet Archive's OnionHeaven")
        self.assertEqual(left[0]["reason"], "hub changed")
        self.assertEqual(c["retry_minutes"], 15)

    def test_client_disabled_and_garbage(self):
        self.assertIs(onionheaven.client_overview(
            self._app(config={"REGISTER_WITH_ONIONHEAVEN": "no"}))["enabled"], False)
        self.assertIsNone(onionheaven.client_overview(
            self._app(config={"REGISTER_WITH_ONIONHEAVEN": "nope"}))["enabled"])

    def test_own_address_falls_back_to_the_cache(self):
        with open(os.path.join(self.tmp.name, "onion_address"), "w") as f:
            f.write(SITE2 + "\n")
        app = self._app(onion_address=None)
        self.assertEqual(onionheaven.client_overview(app)["content_address"], SITE2)

    def test_not_a_hub_asks_no_docker(self):
        app = self._app()
        with mock.patch.object(onionheaven, "_run_docker",
                               side_effect=AssertionError("docker asked")) as run:
            h = onionheaven.hub_overview(app)
        self.assertFalse(h["active"])
        self.assertFalse(h["accepting_registrations"])
        self.assertEqual(h["sites"], [])
        self.assertEqual(h["settings"], {})
        run.assert_not_called()

    def _hub_docker(self, app, args, timeout=15, input=None):
        joined = " ".join(args)
        if "COUNT(*)" in joined:
            return True, "2\t1\t1"
        if "FROM registry" in joined:
            return True, "\n".join([
                "\t".join([SITE, "online", "2026-10-25T10:00:00Z", "", "2.5.7", "1"]),
                "\t".join([SITE2, "taken-over", "2026-10-25T04:00:00Z", "", "2.5.6", ""]),
            ])
        if args[0] == "ps":
            return True, "onionheaven-takeover-1\n"
        return False, ""

    def test_a_hub_by_config(self):
        app = self._app(config={"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes",
                                "ONIONHEAVEN_PROPAGATION_DELAY": "240"})
        with mock.patch.object(onionheaven, "_run_docker", self._hub_docker):
            h = onionheaven.hub_overview(app)
        self.assertTrue(h["active"])
        self.assertTrue(h["accepting_registrations"])
        self.assertFalse(h["is_canonical"])
        self.assertEqual((h["registered_count"], h["online_count"], h["taken_over_count"],
                          h["takeover_containers"]), (2, 1, 1, 1))
        self.assertEqual([s["status"] for s in h["sites"]], ["online", "taken-over"])
        self.assertEqual(h["settings"], {"ONIONHEAVEN_PROPAGATION_DELAY": "240",
                                         "ONIONHEAVEN_MAX_SERVICES": "50"})
        self.assertFalse(h["counts_unavailable"])
        self.assertFalse(h["sites_truncated"])

    def test_the_canonical_hub_by_address(self):
        app = self._app(onion_address=HUB)
        with mock.patch.object(onionheaven, "_run_docker", self._hub_docker):
            h = onionheaven.hub_overview(app, sites=False)
        self.assertTrue(h["active"])
        self.assertTrue(h["is_canonical"])
        self.assertEqual(h["known_as"], "the Internet Archive's OnionHeaven")
        self.assertEqual(h["sites"], [])

    def test_a_hub_whose_registry_does_not_answer(self):
        app = self._app(config={"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes"})
        with mock.patch.object(onionheaven, "_run_docker", return_value=(False, "")):
            h = onionheaven.hub_overview(app)
        self.assertTrue(h["counts_unavailable"])
        self.assertTrue(h["sites_unavailable"])
        self.assertEqual(h["registered_count"], 0)

    def test_overview_and_status_block(self):
        app = self._app(config={"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes"},
                        record={"registered": True, "onionheaven_address": HUB, "content_address": SITE})
        with mock.patch.object(onionheaven, "_run_docker", self._hub_docker):
            ov = onionheaven.overview(app)
        self.assertEqual(set(ov), {"client", "hub", "known_hosts", "generated_at"})
        self.assertEqual([k["kind"] for k in ov["known_hosts"]], ["onionheaven", "onionhome"])
        block = onionheaven.status_block(ov)
        # The flat keys onionpress-status.php has always read.
        for key in ("server_active", "client_registered", "client_enabled", "client_hub",
                    "registered_count", "online_count", "taken_over_count", "takeover_containers"):
            self.assertIn(key, block)
        self.assertTrue(block["server_active"])
        self.assertTrue(block["client_registered"])
        self.assertTrue(block["client_enabled"])
        self.assertEqual(block["client_hub"], HUB)
        self.assertEqual(block["registered_count"], 2)
        self.assertEqual(block["client"], ov["client"])
        self.assertEqual(block["hub"], ov["hub"])
        # The platform's own server_active wins when given (the Mac's is
        # the canonical address only).
        self.assertFalse(onionheaven.status_block(ov, server_active=False)["server_active"])
        json.dumps(block)  # serializable whole

    def test_overview_without_the_hub_half(self):
        app = self._app(config={"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes"})
        with mock.patch.object(onionheaven, "_run_docker",
                               side_effect=AssertionError("docker asked")):
            ov = onionheaven.overview(app, hub=False)
        self.assertIsNone(ov["hub"])
        block = onionheaven.status_block(ov)
        self.assertFalse(block["server_active"])
        self.assertEqual(block["registered_count"], 0)

    def test_cli_overview(self):
        app = self._app(record={"registered": True, "onionheaven_address": HUB,
                                "content_address": SITE,
                                "last_heartbeat_at": "2026-10-25T10:00:00+00:00"})
        out = io.StringIO()
        with mock.patch.object(onionheaven, "_run_docker",
                               side_effect=AssertionError("docker asked")):
            rc = onionheaven.run_onionheaven_command("overview", app, json_output=True, out=out)
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["client"]["hub"], HUB)
        self.assertFalse(data["hub"]["active"])
        out = io.StringIO()
        rc = onionheaven.run_onionheaven_command("overview", app, out=out)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Kept reachable by a hub: registered", text)
        self.assertIn("(the Internet Archive's OnionHeaven)", text)
        self.assertIn("Serving as an OnionHeaven hub: no", text)

    def test_describe_overview_names_an_unknown_hub_and_the_sites(self):
        app = self._app(config={"ONIONHEAVEN_ADDRESS": OTHER_HUB, "ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes"})
        onionheaven.ONIONHEAVEN_ADDRESS = OTHER_HUB
        with mock.patch.object(onionheaven, "_run_docker", self._hub_docker):
            text = onionheaven.describe_overview(onionheaven.overview(app))
        self.assertIn("(not a known host)", text)
        self.assertIn("Serving as an OnionHeaven hub: 2 registered, 1 online, 1 taken over", text)
        self.assertIn(f"  {SITE} online", text)


class TestWiring(unittest.TestCase):

    def test_daemon_publishes_the_overview(self):
        # The daemon's status pass is onionpress.status_writer's (#523).
        src = _read("src/onionpress/status_writer.py")
        self.assertIn("overview = onionheaven.overview(app, hub=bool(app.is_running))", src)
        self.assertIn("onionheaven.status_block(", src)
        self.assertIn('"onionheaven": app._onionheaven_overview,', src)
        self.assertNotIn("SELECT COUNT(*)", src)  # the inline query moved
        self.assertIn("        status_writer.write(self)\n", _read("src/onionpress/daemon.py"))

    def test_cli_knows_overview(self):
        self.assertIn("{enable,disable,status,overview}", _read("src/onionpress/cli.py"))

    def test_linux_status_carries_the_overview(self):
        # Linux's status is the same writer's, through `onionpress
        # write-status` (#523): no second query of the registry in bash.
        src = _read("linux/onionpress")
        self.assertIn('op_py --data-dir "$DATA_DIR" write-status', src)
        self.assertNotIn("sqlite3 /var/lib/onionheaven", src)
        self.assertNotIn("onionheaven overview --json", src)
        block = onionheaven.status_block(onionheaven.overview(
            FakeApp(tempfile.mkdtemp(prefix="op-ov-")), hub=False))
        self.assertIn("generated_at", block)

    def test_linux_heartbeat_keeps_the_timestamps(self):
        src = _read("linux/onionpress-heartbeat.py")
        body = src[src.index("def _record_registered("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn('"last_heartbeat_at": now,', body)
        self.assertIn('state["registered_at"] = now', body)



if __name__ == "__main__":
    unittest.main()
