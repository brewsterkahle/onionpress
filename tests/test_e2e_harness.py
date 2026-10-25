#!/usr/bin/env python3
"""The e2e harness's own logic, checked without a VM, Docker or pytest:
the host probe, the safety guard, the tripwire and the platform-aware
report (docs/E2E-TESTING.md, e2e/harness/)."""

import json
import os
import re
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "e2e"))

from harness import platforms, safety  # noqa: E402
from harness.nodes.base import parse_curl_include  # noqa: E402
from harness.nodes.mac import pick_port_offset  # noqa: E402
from harness.report import NOT_APPLICABLE, Outcome, RunReport, merge  # noqa: E402


def _which(found):
    return lambda name: found.get(name)


class HostProbeTest(unittest.TestCase):
    def test_mac_with_lima_offers_both_platforms(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({"limactl": "/opt/lima/bin/limactl"}))
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(chosen["macos"].kind, "mac")
        self.assertEqual(chosen["debian"].kind, "linux-vm")
        self.assertEqual(not_tested, {})

    def test_mac_without_lima_falls_back_to_the_app_bundles_limactl(self):
        with tempfile.TemporaryDirectory() as app:
            bundled = os.path.join(app, "Contents", "Resources", "bin", "limactl")
            os.makedirs(os.path.dirname(bundled))
            open(bundled, "w").close()
            facts = platforms.probe_host(sys_platform="darwin", mac_ver="15.1", machine="arm64",
                                         which=_which({}), mac_app=app)
        self.assertEqual(facts.tools["limactl"], bundled)

    def test_mac_without_any_limactl_tests_only_macos(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({}))
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(list(chosen), ["macos"])
        self.assertIn("limactl", not_tested["debian"])

    def test_old_macos_is_not_tested(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="12.7", machine="x86_64",
                                     which=_which({}))
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertNotIn("macos", chosen)
        self.assertIn("older than 13.0", not_tested["macos"])

    def test_linux_host_never_offers_macos(self):
        facts = platforms.probe_host(
            sys_platform="linux", machine="x86_64", os_release={"ID": "ubuntu", "VERSION_ID": "24.04"},
            which=_which({"limactl": "/usr/bin/limactl", "qemu-system-x86_64": "/usr/bin/qemu-system-x86_64"}),
            kvm_ok=True)
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(list(chosen), ["debian"])
        self.assertIn("not a Mac host", not_tested["macos"])

    def test_linux_without_kvm_says_what_is_missing(self):
        facts = platforms.probe_host(
            sys_platform="linux", machine="aarch64", os_release={"ID": "debian", "VERSION_ID": "13"},
            which=_which({"limactl": "/usr/bin/limactl"}), kvm_ok=False)
        self.assertEqual(facts.arch, "arm64")
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(chosen, {})
        self.assertIn("/dev/kvm", not_tested["debian"])
        self.assertIn("qemu-system", not_tested["debian"])
        # A Debian host would offer users/the host itself, once built.
        self.assertIn("linux-user: linux-user nodes are not implemented yet", not_tested["debian"])

    def test_unselected_platform_is_listed_as_not_selected(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({"limactl": "/x/limactl"}))
        chosen, not_tested = platforms.choose(platforms.offers(facts), ["macos"])
        self.assertEqual(list(chosen), ["macos"])
        self.assertEqual(not_tested["debian"], "not selected (--platforms)")


class SafetyTest(unittest.TestCase):
    def test_scratch_root_must_stay_out_of_the_real_home(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(safety.UnsafeTarget):
                safety.check_scratch_root(os.path.join(home, "x"), home)
            with self.assertRaises(safety.UnsafeTarget):
                safety.check_scratch_root(os.path.dirname(home), home)
        with self.assertRaises(safety.UnsafeTarget):
            safety.check_scratch_root("relative/root", "/Users/someone")
        with self.assertRaises(safety.UnsafeTarget):
            safety.check_scratch_root("/tmp/" + "x" * 60, "/Users/someone")
        with self.assertRaises(safety.UnsafeTarget):
            safety.check_scratch_root("/tmp/with space", "/Users/someone")
        self.assertTrue(safety.check_scratch_root("/tmp/ope2e", "/Users/someone"))

    def test_a_node_answering_with_the_live_address_stops_the_run(self):
        live = "a" * 56 + ".onion"
        with self.assertRaises(safety.UnsafeTarget):
            safety.check_not_live(live, live)
        safety.check_not_live("b" * 56 + ".onion", live)
        safety.check_not_live(live, "")

    def test_live_address_is_read_only_when_well_formed(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".onionpress"))
            path = os.path.join(home, ".onionpress", "onion_address")
            with open(path, "w") as f:
                f.write("c" * 56 + ".onion\n")
            self.assertEqual(safety.live_onion_address(home), "c" * 56 + ".onion")
            with open(path, "w") as f:
                f.write("Generating...\n")
            self.assertEqual(safety.live_onion_address(home), "")

    def test_daemon_agent_of_another_install_refuses_a_mac_node(self):
        root = tempfile.mkdtemp()
        theirs = ("gui/501/org.onionpress.daemon = {\n\tstate = running\n"
                  "\tprogram = /Applications/OnionPress.app/Contents/MacOS/onionpress\n}")
        with self.assertRaises(safety.UnsafeTarget):
            safety.check_daemon_agent(theirs, 0, root)
        ours = theirs.replace("/Applications", os.path.join(root, "mac-1"))
        safety.check_daemon_agent(ours, 0, root)
        safety.check_daemon_agent("Could not find service", 113, root)
        args_only = ("gui/501/org.onionpress.daemon = {\n\targuments = {\n"
                     "\t\t/Applications/OnionPress.app/Contents/MacOS/onionpress\n\t\tdaemon\n\t}\n}")
        with self.assertRaises(safety.UnsafeTarget):
            safety.check_daemon_agent(args_only, 0, root)

    def test_daemon_label_is_the_products(self):
        with open(os.path.join(PROJECT_DIR, "src", "onionpress", "daemon.py"), encoding="utf-8") as f:
            m = re.search(r'^AGENT_LABEL = "([^"]+)"', f.read(), re.M)
        self.assertEqual(m.group(1), safety.DAEMON_AGENT_LABEL)

    def test_baseline_config_turns_off_everything_that_reaches_public_services(self):
        text = safety.baseline_config_text()
        self.assertIn("REGISTER_WITH_ONIONHEAVEN=no\n", text)
        self.assertIn("SHARE_ANALYTICS_WITH_ONIONHOME=no\n", text)
        self.assertIn("INSTALL_IA_PLUGIN=no\n", text)
        # Every key is one the product reads (config template).
        with open(os.path.join(PROJECT_DIR, "app", "Resources", "config-template.txt"),
                  encoding="utf-8") as f:
            template = f.read()
        for key in safety.BASELINE_CONFIG:
            self.assertRegex(template, rf"(?m)^#?\s*{key}=", key)

    def test_public_service_addresses_come_from_the_source(self):
        addrs = safety.public_service_addresses()
        self.assertEqual(len(addrs), 2)
        for addr in addrs.values():
            self.assertRegex(addr, r"^[a-z2-7]{56}\.onion$")

    def test_tripwire_finds_public_addresses_in_logs(self):
        hub = safety.public_service_addresses()["the canonical OnionHeaven hub"]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "logs"))
            with open(os.path.join(d, "logs", "a.log"), "w") as f:
                f.write("quiet line\n" + f"heartbeat to {hub[:20]}… failed\n")
            with open(os.path.join(d, "b.log"), "w") as f:
                f.write("nothing here\n")
            hits = safety.tripwire_scan([d])
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0][0].endswith("a.log"))
        self.assertEqual(hits[0][1], 2)


class ReportTest(unittest.TestCase):
    def _report(self):
        r = RunReport(host="macOS 27.0 arm64", build={"mac": "onionpress.dmg sha256 ab…"})
        r.tested = {"macos": {"os": "macOS 27.0", "arch": "arm64", "kind": "mac"}}
        r.not_tested = {"debian": "no limactl"}
        r.applies = {"t0_stack::test_install": ["macos", "debian"],
                     "t3::test_linux_update": ["debian"]}
        r.add(Outcome("t0_stack::test_install", "macos", "passed", seconds=3))
        return r

    def test_untested_platform_gets_no_column_and_is_named(self):
        md = self._report().render_markdown()
        self.assertIn("| Test | macOS |", md)
        self.assertNotIn("Debian 13 |", md.split("| Test |", 1)[1].splitlines()[0])
        self.assertIn("**Not tested:** Debian 13 — no limactl", md)
        self.assertIn("PASS (every test that ran passed; untested platforms are listed above)", md)

    def test_a_test_that_does_not_apply_shows_a_dash(self):
        r = self._report()
        r.add(Outcome("t3::test_linux_update", "debian", "passed"))
        self.assertEqual(r.cell("t3::test_linux_update", "macos"), NOT_APPLICABLE)

    def test_failure_and_tripwire_fail_the_verdict(self):
        r = self._report()
        r.add(Outcome("t0_stack::test_first_start", "macos", "failed", "boom"))
        self.assertTrue(r.verdict.startswith("FAIL (1 failed)"))
        r = self._report()
        r.tripwire.append(["mac-1", "x.log", 3, "OnionHome"])
        self.assertTrue(r.verdict.startswith("FAIL (tripwire"))

    def test_teardown_error_replaces_a_pass_without_a_second_row(self):
        r = self._report()
        r.add(Outcome("t0_stack::test_install", "macos", "error", "teardown"))
        self.assertEqual([o.outcome for o in r.outcomes], ["error"])

    def test_nothing_tested_is_never_a_pass(self):
        r = RunReport(host="x")
        self.assertTrue(r.verdict.startswith("NOTHING TESTED"))

    def test_json_round_trip_and_merge_of_two_single_os_runs(self):
        mac = self._report()
        deb = RunReport(host="Debian 13 x86_64", build=mac.build)
        deb.tested = {"debian": {"os": "Debian GNU/Linux 13", "kind": "linux-vm"}}
        deb.not_tested = {"macos": "not a Mac host"}
        deb.applies = dict(mac.applies)
        deb.add(Outcome("t0_stack::test_install", "debian", "failed", "apt"))
        mac2 = RunReport.from_json(mac.to_json())
        self.assertEqual(json.loads(mac2.to_json())["outcomes"], json.loads(mac.to_json())["outcomes"])
        merged = merge([mac2, deb])
        self.assertEqual(merged.columns(), ["macos", "debian"])
        self.assertEqual(merged.not_tested, {})
        self.assertEqual(merged.cell("t0_stack::test_install", "macos"), "pass")
        self.assertEqual(merged.cell("t0_stack::test_install", "debian"), "FAIL")


class NodeHelpersTest(unittest.TestCase):
    def test_parse_curl_include_takes_the_last_response(self):
        text = ("HTTP/1.1 100 Continue\r\n\r\n"
                "HTTP/1.1 302 Found\r\nLocation: /wp-admin/\r\n"
                "Set-Cookie: a=1\r\nSet-Cookie: wordpress_logged_in_x=y\r\n\r\nbody")
        resp = parse_curl_include(text)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.header("Location"), "/wp-admin/")
        self.assertEqual(len(resp.cookies()), 2)
        self.assertEqual(resp.body, "body")

    def test_port_offset_skips_busy_ports(self):
        busy = {8080 + 31000}
        self.assertEqual(pick_port_offset(is_free=lambda p: p not in busy), 32000)


if __name__ == "__main__":
    unittest.main()
