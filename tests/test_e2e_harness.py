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

from harness import chaos, hardening, platforms, proc, safety, scenario  # noqa: E402
from harness.nodes.base import parse_curl_include, wp_port_from_status  # noqa: E402
from harness.nodes.linux_vm import LinuxVMNode, guest_template  # noqa: E402
from harness.nodes.mac import MacNode, pick_port_offset  # noqa: E402
from harness.report import NOT_APPLICABLE, Outcome, RunReport, merge  # noqa: E402


def _which(found):
    return lambda name: found.get(name)


class HostProbeTest(unittest.TestCase):
    def test_mac_with_lima_offers_both_platforms(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({"limactl": "/opt/lima/bin/limactl"}))
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(chosen["macos"].kind, "mac")
        self.assertEqual(chosen["linux"].kind, "linux-vm")
        self.assertEqual(not_tested, {})

    def test_mac_without_lima_falls_back_to_the_app_bundles_limactl(self):
        with tempfile.TemporaryDirectory() as app:
            bundled = os.path.join(app, "Contents", "Resources", "bin", "limactl")
            os.makedirs(os.path.dirname(bundled))
            open(bundled, "w").close()
            facts = platforms.probe_host(sys_platform="darwin", mac_ver="15.1", machine="arm64",
                                         which=_which({}), mac_app=app)
        self.assertEqual(facts.tools["limactl"], bundled)

    def test_a_bundle_without_limactl_is_passed_over(self):
        """A build still being made (`make dmg` beside a run) has no
        limactl yet: the next candidate's is used, so the run does not
        report "no limactl" while the installed app has one."""
        with tempfile.TemporaryDirectory() as root:
            half_built = os.path.join(root, "build", "OnionPress.app")
            installed = os.path.join(root, "installed", "OnionPress.app")
            os.makedirs(os.path.join(half_built, "Contents", "Resources"))
            bundled = os.path.join(installed, "Contents", "Resources", "bin", "limactl")
            os.makedirs(os.path.dirname(bundled))
            open(bundled, "w").close()
            facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                         which=_which({}), mac_app=[half_built, installed])
            self.assertEqual(facts.tools["limactl"], bundled)
            # The first bundle that has one wins.
            first = os.path.join(half_built, "Contents", "Resources", "bin", "limactl")
            os.makedirs(os.path.dirname(first))
            open(first, "w").close()
            facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                         which=_which({}), mac_app=[half_built, installed])
            self.assertEqual(facts.tools["limactl"], first)

    def test_app_candidates_order(self):
        self.assertEqual(platforms.app_candidates("/co"),
                         ["/co/OnionPress.app", platforms.INSTALLED_APP])
        self.assertEqual(platforms.app_candidates("/co", "/x/OnionPress.app"),
                         ["/x/OnionPress.app", "/co/OnionPress.app", platforms.INSTALLED_APP])

    def test_probe_and_session_ask_the_same_bundles(self):
        """The probe and the run once chose their bundle differently, so a
        probe said linux-vm while the run found no limactl."""
        for rel in ("e2e/probe.py", "e2e/harness/session.py"):
            with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("platforms.app_candidates(PROJECT_DIR", src, rel)
            self.assertNotIn("/Applications/OnionPress.app", src, rel)

    def test_mac_without_any_limactl_tests_only_macos(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({}))
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(list(chosen), ["macos"])
        self.assertIn("limactl", not_tested["linux"])

    def test_old_macos_is_not_tested(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="12.7", machine="x86_64",
                                     which=_which({}))
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertNotIn("macos", chosen)
        self.assertIn("older than 13.0", not_tested["macos"])

    UBUNTU = {"ID": "ubuntu", "ID_LIKE": "debian", "VERSION_ID": "24.04", "NAME": "Ubuntu",
              "PRETTY_NAME": "Ubuntu 24.04.5 LTS"}
    LIMA_HOST_TOOLS = {"limactl": "/usr/bin/limactl",
                       "qemu-system-x86_64": "/usr/bin/qemu-system-x86_64",
                       "qemu-system-aarch64": "/usr/bin/qemu-system-aarch64"}

    def test_linux_host_never_offers_macos(self):
        facts = platforms.probe_host(
            sys_platform="linux", machine="x86_64", os_release=self.UBUNTU,
            which=_which(self.LIMA_HOST_TOOLS), kvm_ok=True, firmware="/usr/share/OVMF/OVMF_CODE_4M.fd")
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(list(chosen), ["linux"])
        self.assertIn("not a Mac host", not_tested["macos"])

    def test_ubuntu_host_counts_as_debian_based(self):
        """#486: an Ubuntu (or Raspberry Pi OS, Mint) host offers linux-vm and
        is a Debian host for linux-user/linux-host — "not implemented yet",
        never "needs a Debian host"."""
        facts = platforms.probe_host(
            sys_platform="linux", machine="x86_64", os_release=self.UBUNTU,
            which=_which(self.LIMA_HOST_TOOLS), kvm_ok=True, firmware="/usr/share/OVMF/OVMF_CODE_4M.fd")
        self.assertTrue(facts.debian_based)
        self.assertEqual(facts.label, "Ubuntu 24.04 x86_64")   # what actually ran, not "Debian"
        offers = {o.kind: o for o in platforms.offers(facts)}
        self.assertTrue(offers["linux-vm"].available)
        for kind in ("linux-user", "linux-host"):
            self.assertFalse(offers[kind].available)
            self.assertIn("not implemented yet", offers[kind].reason)
            self.assertNotIn("needs a Debian", offers[kind].reason)
        pi = platforms.probe_host(sys_platform="linux", machine="aarch64", which=_which({}),
                                  os_release={"ID": "raspbian", "ID_LIKE": "debian", "VERSION_ID": "13"})
        self.assertTrue(pi.debian_based)
        mint = platforms.probe_host(sys_platform="linux", machine="x86_64", which=_which({}),
                                    os_release={"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "VERSION_ID": "22"})
        self.assertTrue(mint.debian_based)

    def test_non_debian_host_runs_only_vms(self):
        fedora = platforms.probe_host(
            sys_platform="linux", machine="x86_64",
            os_release={"ID": "fedora", "VERSION_ID": "42", "NAME": "Fedora Linux"},
            which=_which(self.LIMA_HOST_TOOLS), kvm_ok=True, firmware="/usr/share/edk2/ovmf/OVMF_CODE.fd")
        self.assertFalse(fedora.debian_based)
        offers = {o.kind: o for o in platforms.offers(fedora)}
        self.assertTrue(offers["linux-vm"].available)
        self.assertIn("needs a Debian-based host", offers["linux-user"].reason)
        self.assertIn("needs a Debian-based host", offers["linux-host"].reason)
        # A Mac is not a Debian-based host either.
        mac = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64", which=_which({}))
        self.assertFalse(mac.debian_based)

    def test_linux_vm_needs_uefi_firmware_too(self):
        facts = platforms.probe_host(
            sys_platform="linux", machine="x86_64", os_release=self.UBUNTU,
            which=_which(self.LIMA_HOST_TOOLS), kvm_ok=True, firmware=None)
        # firmware=None means "look on disk": force the lookup to find nothing.
        facts.tools["uefi-firmware"] = None
        self.assertEqual(platforms.linux_vm_missing(facts), ["UEFI firmware (ovmf / qemu-efi-aarch64)"])
        self.assertEqual(platforms.find_uefi_firmware("x86_64", exists=lambda p: p.endswith("OVMF_CODE.fd")),
                         "/usr/share/OVMF/OVMF_CODE.fd")
        self.assertIsNone(platforms.find_uefi_firmware("x86_64", exists=lambda p: False))
        self.assertIsNone(platforms.find_uefi_firmware("riscv64"))

    def test_legacy_platform_name_debian_still_selects_linux(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({"limactl": "/x/limactl"}))
        chosen, not_tested = platforms.choose(platforms.offers(facts), ["debian"])
        self.assertEqual(list(chosen), ["linux"])
        self.assertEqual(not_tested, {"macos": "not selected (--platforms)"})
        self.assertEqual(platforms.canonical_platform("linux"), "linux")
        self.assertEqual(platforms.canonical_platform("macos"), "macos")

    def test_every_linux_guest_is_a_lima_template(self):
        self.assertEqual(platforms.LINUX_GUESTS[0], platforms.DEFAULT_LINUX_GUEST)
        self.assertEqual(platforms.DEFAULT_LINUX_GUEST, "debian-13")
        self.assertIn("ubuntu-24.04", platforms.LINUX_GUESTS)
        for guest in platforms.LINUX_GUESTS:
            self.assertEqual(guest_template(guest), f"template:{guest}")
        with self.assertRaises(ValueError):
            guest_template("arch")
        self.assertEqual(LinuxVMNode.platform, "linux")

    def test_linux_without_kvm_says_what_is_missing(self):
        facts = platforms.probe_host(
            sys_platform="linux", machine="aarch64", os_release={"ID": "debian", "VERSION_ID": "13"},
            which=_which({"limactl": "/usr/bin/limactl"}), kvm_ok=False)
        self.assertEqual(facts.arch, "arm64")
        chosen, not_tested = platforms.choose(platforms.offers(facts))
        self.assertEqual(chosen, {})
        self.assertIn("/dev/kvm", not_tested["linux"])
        self.assertIn("qemu-system", not_tested["linux"])
        # A Debian host would offer users/the host itself, once built.
        self.assertIn("linux-user: linux-user nodes are not implemented yet", not_tested["linux"])

    def test_unselected_platform_is_listed_as_not_selected(self):
        facts = platforms.probe_host(sys_platform="darwin", mac_ver="27.0", machine="arm64",
                                     which=_which({"limactl": "/x/limactl"}))
        chosen, not_tested = platforms.choose(platforms.offers(facts), ["macos"])
        self.assertEqual(list(chosen), ["macos"])
        self.assertEqual(not_tested["linux"], "not selected (--platforms)")


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

    def test_a_mac_node_checks_its_own_derived_label(self):
        # Issue #505: a node's daemon runs under the label the product
        # derives from its data dir, so the live install's agent is not
        # the node's concern; the node's own label loaded by another
        # program still refuses it.
        sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))
        from onionpress import daemon_client
        root = tempfile.mkdtemp()
        data_dir = os.path.join(root, "mac-1", "h", ".onionpress")
        label = safety.node_agent_label(data_dir)
        self.assertEqual(label, daemon_client.agent_label(data_dir))
        self.assertTrue(label.startswith(safety.DAEMON_AGENT_LABEL + "."))
        node = MacNode("mac-1", None, root, root=root, real_home=root,
                       app_source=("app", "/nonexistent"), port_offset=31000)
        self.assertEqual(node.agent_label, label)
        theirs = (f"gui/501/{label} = {{\n\tstate = running\n"
                  "\tprogram = /Applications/OnionPress.app/Contents/MacOS/onionpress\n}")
        with self.assertRaisesRegex(safety.UnsafeTarget, re.escape(label)):
            safety.check_daemon_agent(theirs, 0, root, label)
        with self.assertRaises(safety.UnsafeTarget):
            safety.node_agent_label(daemon_client.default_data_dir())

    def test_daemon_label_is_the_products(self):
        with open(os.path.join(PROJECT_DIR, "src", "onionpress", "daemon_client.py"), encoding="utf-8") as f:
            m = re.search(r'^AGENT_LABEL = "([^"]+)"', f.read(), re.M)
        self.assertEqual(m.group(1), safety.DAEMON_AGENT_LABEL)

    def test_baseline_config_turns_off_everything_that_reaches_public_services(self):
        text = safety.baseline_config_text()
        self.assertIn("REGISTER_WITH_ONIONHEAVEN=no\n", text)
        self.assertIn("SHARE_ANALYTICS_WITH_ONIONHOME=no\n", text)
        self.assertIn("INSTALL_IA_PLUGIN=no\n", text)
        # No OnionHome: setup records the onionname unregistered and the
        # daemon never registers it (onionnames_registrar.configured_onionhome).
        self.assertIn("ONIONHOME_ADDRESS=\n", text)
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
        r.not_tested = {"linux": "no limactl"}
        r.applies = {"t0_stack::test_install": ["macos", "linux"],
                     "t3::test_linux_update": ["linux"]}
        r.add(Outcome("t0_stack::test_install", "macos", "passed", seconds=3))
        return r

    def test_untested_platform_gets_no_column_and_is_named(self):
        md = self._report().render_markdown()
        self.assertIn("| Test | macOS |", md)
        self.assertNotIn("Linux |", md.split("| Test |", 1)[1].splitlines()[0])
        self.assertIn("**Not tested:** Linux — no limactl", md)
        self.assertIn("PASS (every test that ran passed; untested platforms are listed above)", md)

    def test_a_test_that_does_not_apply_shows_a_dash(self):
        r = self._report()
        r.add(Outcome("t3::test_linux_update", "linux", "passed"))
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
        deb = RunReport(host="Ubuntu 24.04 x86_64", build=mac.build)
        deb.tested = {"linux": {"os": "Debian GNU/Linux 13", "kind": "linux-vm", "guest": "debian-13"}}
        deb.not_tested = {"macos": "not a Mac host"}
        deb.applies = dict(mac.applies)
        deb.add(Outcome("t0_stack::test_install", "linux", "failed", "apt"))
        mac2 = RunReport.from_json(mac.to_json())
        self.assertEqual(json.loads(mac2.to_json())["outcomes"], json.loads(mac.to_json())["outcomes"])
        merged = merge([mac2, deb])
        self.assertEqual(merged.columns(), ["macos", "linux"])
        self.assertEqual(merged.not_tested, {})
        self.assertEqual(merged.cell("t0_stack::test_install", "macos"), "pass")
        self.assertEqual(merged.cell("t0_stack::test_install", "linux"), "FAIL")
        self.assertIn("**Tested:** Linux — guest debian-13, kind linux-vm, os Debian GNU/Linux 13",
                      merged.render_markdown())

    def test_results_json_from_before_the_rename_still_merges(self):
        """A results.json written when the Linux platform was called "debian"
        (before #486) reads back as "linux" everywhere, so old and new runs
        merge into one matrix."""
        old = json.dumps({
            "build": {}, "host": "macOS 27.0 arm64", "tor": "public network", "tiers": ["t0"],
            "tested": {"debian": {"os": "Debian GNU/Linux 13", "kind": "linux-vm"}},
            "not_tested": {"macos": "no Mac build"}, "roles": {"node[debian]": "debian-1"},
            "applies": {"t0_stack::test_install": ["macos", "debian"]},
            "outcomes": [{"test": "t0_stack::test_install", "platform": "debian",
                          "outcome": "passed", "reason": "", "seconds": 2.0}],
            "timings": {}, "tripwire": [], "notes": [], "started": "", "finished": "",
            "verdict": "PASS"})
        r = RunReport.from_json(old)
        self.assertEqual(r.columns(), ["linux"])
        self.assertEqual(r.roles, {"node[linux]": "debian-1"})
        self.assertEqual(r.applies, {"t0_stack::test_install": ["macos", "linux"]})
        self.assertEqual(r.cell("t0_stack::test_install", "linux"), "pass")
        merged = merge([r, self._report()])
        self.assertEqual(merged.columns(), ["macos", "linux"])
        self.assertEqual(merged.not_tested, {})


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

    def test_the_launchers_published_port_is_read_from_its_status(self):
        # An Ubuntu guest with a system tor on 9050 made the launcher take
        # offset 10000 (2026-10-30); the harness must probe where it
        # published, not 8080.
        self.assertEqual(wp_port_from_status('{"wp_port": 18080, "server_active": true}'), 18080)
        self.assertEqual(wp_port_from_status('{"wp_port": "8080"}'), 8080)
        for text in ("", "not json", "{}", '{"wp_port": 8081}', '{"wp_port": 80}',
                     '{"wp_port": 78080}', '{"wp_port": null}', "[]"):
            with self.subTest(text):
                self.assertIsNone(wp_port_from_status(text))

    def test_a_linux_node_probes_the_port_its_status_names_or_8080(self):
        with tempfile.TemporaryDirectory() as root:
            node = LinuxVMNode("linux-1", None, root, root=root, limactl="/bin/false",
                               deb_source=("deb", "/x.deb"))
            node.read_file = lambda rel: ""
            self.assertEqual(node.wp_port, 8080, "no status yet: the default")
            node.read_file = lambda rel: '{"wp_port": 18080}' if rel == ".onionpress/status.json" else ""
            self.assertEqual(node.wp_port, 18080)



def _container(name, service, *, cap_add=(), cap_drop=("ALL",),
               security_opt=("no-new-privileges:true",), privileged=False, mounts=()):
    return {"Name": "/" + name,
            "Config": {"Labels": {hardening.COMPOSE_SERVICE_LABEL: service}},
            "HostConfig": {"CapAdd": list(cap_add), "CapDrop": list(cap_drop),
                           "SecurityOpt": list(security_opt), "Privileged": privileged},
            "Mounts": list(mounts)}


class HardeningTest(unittest.TestCase):
    """T1 (a)'s readings of `docker inspect` (e2e/test_t1_a_hardening.py)."""

    def setUp(self):
        self.census, self.never = hardening.load_capability_census()

    def test_the_census_is_the_unit_suites(self):
        self.assertEqual(self.census["tor"], {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"})
        self.assertEqual(self.census["autoheal-proxy"], set())
        self.assertIn("cloudflared", self.census)
        self.assertIn("NET_RAW", self.never)

    def test_the_census_held(self):
        docs = [_container("onionpress-tor", "tor",
                           cap_add=["CAP_CHOWN", "CAP_DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
                           cap_drop=["CAP_ALL"], security_opt=["no-new-privileges=true"]),
                _container("onionpress-autoheal-proxy", "autoheal-proxy")]
        self.assertEqual(hardening.capability_findings(docs, self.census, self.never), [])

    def test_each_way_to_break_it_is_named(self):
        cases = {
            "not a compose service": _container("stray", "stray"),
            "privileged": _container("onionpress-db", "db", privileged=True),
            "not ALL": _container("onionpress-db", "db", cap_drop=["NET_RAW"]),
            "which nothing may": _container("onionpress-db", "db", cap_add=["NET_RAW"]),
            "beyond the census": _container("onionpress-db", "db", cap_add=["KILL"]),
            "no no-new-privileges": _container("onionpress-db", "db", security_opt=[]),
        }
        for words, doc in cases.items():
            with self.subTest(words):
                found = hardening.capability_findings([doc], self.census, self.never)
                self.assertTrue(any(words in f for f in found), found)

    def test_only_the_two_mount_the_socket_and_read_only(self):
        sock = {"Source": "/run/user/1000/docker.sock", "Destination": "/var/run/docker.sock",
                "RW": False}
        ok = [_container("onionpress-autoheal-proxy", "autoheal-proxy", mounts=[sock]),
              _container("onionheaven-farm", "onionheaven-farm", mounts=[sock]),
              _container("onionpress-autoheal", "autoheal",
                         mounts=[{"Source": "autoheal-proxy", "Destination": "/run/autoheal-proxy"}])]
        self.assertEqual(hardening.socket_findings(ok), [])
        bad = [_container("onionpress-wordpress", "wordpress", mounts=[sock]),
               _container("onionheaven-farm", "onionheaven-farm", mounts=[{**sock, "RW": True}])]
        found = hardening.socket_findings(bad)
        self.assertEqual(len(found), 2, found)

    def test_default_gateway_from_proc_net_route(self):
        route = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
                 "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n"
                 "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n")
        self.assertEqual(hardening.default_gateway(route), "172.18.0.1")
        self.assertEqual(hardening.default_gateway("Iface Destination Gateway\n"), "")
        self.assertEqual(hardening.default_gateway("h\neth0 00000000 zz0012AC\n"), "")


class _FakeNode:
    def __init__(self, **done):
        self.address = done.get("address", "")
        self.first_key_digest = done.get("digest", "")
        self.wp_admin_user = done.get("admin", "")
        self.steps = []

    def start(self):
        self.steps.append("start")

    def wait_for_address(self):
        return "a" * 56 + ".onion"

    def served_key_digest(self):
        return "digest"

    def wait_for_local_site(self):
        self.steps.append("local site")

    def setup_wordpress(self, user, password):
        self.steps.append(("setup", user))
        self.wp_admin_user = user


class _FakeVisitor:
    """tor_fetch answers in turn: an exception is raised (no answer), anything
    else is the response."""

    name = "visitor"

    def __init__(self, answers):
        self.answers = list(answers)
        self.fetched = []

    def tor_fetch(self, address, path, *, timeout):
        self.fetched.append((address, path))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _FakeSession:
    def __init__(self):
        self.verified = []

    def wait_installed(self, node):
        node.steps.append("installed")

    def verify_address(self, node, address):
        self.verified.append(address)
        return address


class ScenarioTest(unittest.TestCase):
    """ensure_ready: the site T0 builds, for a tier run without T0."""

    def test_a_fresh_node_is_brought_up(self):
        node, e2e = _FakeNode(), _FakeSession()
        scenario.ensure_ready(e2e, node)
        self.assertEqual(node.steps, ["installed", "start", "local site",
                                      ("setup", scenario.ADMIN_USER)])
        self.assertEqual(e2e.verified, [node.address])
        self.assertEqual(node.first_key_digest, "digest")

    def test_what_t0_did_is_not_done_again(self):
        node = _FakeNode(address="b" * 56 + ".onion", digest="t0", admin="op-e2e-admin")
        scenario.ensure_ready(_FakeSession(), node)
        self.assertEqual(node.steps, ["installed", "local site"])
        self.assertEqual(node.first_key_digest, "t0")

    def test_t0_uses_the_same_admin(self):
        with open(os.path.join(PROJECT_DIR, "e2e", "test_t0_stack.py"), encoding="utf-8") as f:
            self.assertIn("from harness.scenario import ADMIN_PASSWORD, ADMIN_USER", f.read())

    def _tor_page(self, answers, timeout=1):
        node = _FakeNode(address="c" * 56 + ".onion")
        node.name, node.timings, node.log, node.notes = "linux-1", None, None, []
        visitor = _FakeVisitor(answers)
        return node, visitor, lambda: scenario.tor_page(node, visitor, "/", timeout=timeout, every=0)

    def test_tor_page_retries_a_fetch_that_got_no_answer(self):
        node, visitor, fetch = self._tor_page([RuntimeError("curl: (28) timed out"), "answer"])
        self.assertEqual(fetch(), "answer")
        self.assertEqual(visitor.fetched, [(node.address, "/")] * 2)
        self.assertEqual(len(node.notes), 1)
        self.assertIn("1 failed attempts", node.notes[0])

    def test_tor_page_takes_the_first_answer_whatever_its_status(self):
        """Only a missing answer is retried; a 403 or 500 is the caller's
        to judge, so it is returned at once."""
        node, visitor, fetch = self._tor_page(["a 403", "never asked"])
        self.assertEqual(fetch(), "a 403")
        self.assertEqual(len(visitor.fetched), 1)
        self.assertEqual(node.notes, [])

    def test_tor_page_gives_up_with_the_last_error(self):
        _, _, fetch = self._tor_page([RuntimeError("curl: (28) timed out")] * 1000, timeout=0)
        with self.assertRaisesRegex(proc.WaitTimeout, "timed out"):
            fetch()

    def test_every_tier_file_is_found_by_run_sh(self):
        """run.sh takes e2e/test_<tier>_*.py."""
        for name in os.listdir(os.path.join(PROJECT_DIR, "e2e")):
            if name.startswith("test_") and name.endswith(".py"):
                with self.subTest(name):
                    self.assertRegex(name, r"^test_t\d_[a-z0-9_]+\.py$")


class _ChaosClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _ChaosNode:
    """A node whose daemon answers the way *behaviour* says: a function
    of (fault on, seconds since the last change) to a display state."""

    ADDRESS = "b" * 56 + ".onion"

    def __init__(self, clock, behaviour, *, republish_on_fault=False):
        self.clock = clock
        self.behaviour = behaviour
        self.republish_on_fault = republish_on_fault
        self.on = False
        self.route_returns = 0       # no-route: times the route comes back while on
        self.changed = 0.0
        self.calls = []
        self.log_text = "old line\nreachability: x — requested descriptor republish from tor-watchdog\n"
        self.log = proc.NodeLog(os.path.join(tempfile.mkdtemp(prefix="op-chaos-"), "node.log"))
        self.inspect = [{"Name": "/onionpress-tor", "Id": "abcdef0123456789",
                         "NetworkSettings": {"Networks": {
                             "onionpress-network": {"Aliases": ["onionpress-tor", "tor", "abcdef012345"]},
                             "onionpress-wp": {"Aliases": ["tor"]}}}}]

    def _result(self, argv, out="", rc=0):
        return proc.Result(argv, rc, out, "", 0.1)

    def _flip(self, on):
        if on == self.on:
            return
        self.on, self.changed = on, self.clock.now()
        if on and self.republish_on_fault:
            self.log_text += "reachability: 70% failing — requested descriptor republish\n"

    def docker(self, argv, **_):
        self.calls.append(("docker", argv))
        if argv[0] == "inspect":
            return self._result(argv, json.dumps(self.inspect))
        if argv[:1] == ["pause"] or argv[:2] == ["network", "disconnect"]:
            self._flip(True)
        elif argv[:1] == ["unpause"] or argv[:2] == ["network", "connect"]:
            self._flip(False)
        return self._result(argv)

    def vm_run(self, argv, **_):
        self.calls.append(("vm", argv))
        if argv[1:4] == ["ip", "route", "show"]:
            if self.on and not self.route_returns:
                return self._result(argv, "")
            self.route_returns = max(0, self.route_returns - 1) if self.on else self.route_returns
            return self._result(argv, "default via 192.168.5.2 dev eth0 proto dhcp src 192.168.5.15 metric 100\n")
        if argv[1:4] == ["ip", "-6", "route"] and argv[4] == "show":
            return self._result(argv, "")
        if "del" in argv or argv[1:3] == ["iptables", "-N"]:
            self._flip(True)
        elif "replace" in argv or argv[1:3] == ["iptables", "-X"]:
            self._flip(False)
        return self._result(argv)

    def exec(self, argv, **_):
        return self._result(argv, self.log_text)

    def read_file(self, path):
        assert path == chaos.STATE_FILE
        state = self.behaviour(self.on, self.clock.now() - self.changed)
        return json.dumps({"display_state": state, "has_internet": state != "offline",
                           "probe": {"code": "200"}, "onion_address": self.ADDRESS})


def _offline_while_on(on, since):
    if on:
        return "starting" if since < 20 else "offline"
    return "starting" if since < 60 else "available"


class ChaosTest(unittest.TestCase):
    """T1 (b)'s faults and verdicts (e2e/test_t1_b_chaos.py, issues #139
    and #266 item 3)."""

    def _run(self, name, behaviour, **node_kw):
        clock = _ChaosClock()
        node = _ChaosNode(clock, behaviour, **node_kw)
        outcome = chaos.run_fault(node, chaos.FAULTS[name], node.ADDRESS, clock=clock,
                                  recover_timeout=300)
        return node, outcome

    def test_attachments_keep_compose_aliases_only(self):
        doc = _ChaosNode(_ChaosClock(), _offline_while_on).inspect[0]
        attached = chaos.attachments(doc)
        self.assertEqual(attached, [("onionpress-network", ["tor"]), ("onionpress-wp", ["tor"])])
        self.assertEqual(chaos.connect_argvs("onionpress-tor", attached)[0],
                         ["network", "connect", "--alias", "tor", "onionpress-network", "onionpress-tor"])
        self.assertEqual(chaos.disconnect_argvs("onionpress-tor", attached)[1],
                         ["network", "disconnect", "onionpress-wp", "onionpress-tor"])

    def test_default_routes_are_put_back_as_they_were(self):
        v4 = chaos.default_routes("default via 192.168.5.2 dev eth0 proto dhcp metric 100\n"
                                  "192.168.5.0/24 dev eth0 proto kernel scope link\n")
        v6 = chaos.default_routes("default nhid 636719831 via fe80::2 dev eth0 proto ra metric 100 "
                                  "expires 1789sec pref medium\n")
        self.assertEqual(chaos.route_add_argvs(v4, v6), [
            ["ip", "route", "replace", "default", "via", "192.168.5.2", "dev", "eth0", "proto", "dhcp", "metric", "100"],
            ["ip", "-6", "route", "replace", "default", "via", "fe80::2", "dev", "eth0", "proto", "ra",
             "metric", "100", "pref", "medium"]])
        self.assertEqual(chaos.route_del_argvs(v4, [])[0][:3], ["ip", "route", "del"])

    def test_the_captive_portal_keeps_private_ranges_and_undoes_itself(self):
        on = chaos.captive_on_argvs()
        returns = [a[a.index("-d") + 1] for a in on if "RETURN" in a]
        self.assertIn("192.168.0.0/16", returns)     # Lima's network: ssh keeps working
        self.assertIn("172.16.0.0/12", returns)      # Docker's bridges
        rejects = [a for a in on if "REJECT" in a]
        self.assertEqual(len(rejects), 1)
        self.assertNotIn("--ctstate", rejects[0], "established guard connections must go too")
        jumps = {a[2] for a in on if a[1] == "-I"}
        self.assertEqual(jumps, {"OUTPUT", "FORWARD"})
        off = chaos.captive_off_argvs()
        self.assertEqual({a[2] for a in off if a[1] == "-D"}, jumps)
        self.assertEqual(off[-1], ["iptables", "-X", chaos.CAPTIVE_CHAIN])

    def test_samples_survive_a_half_written_state(self):
        self.assertEqual(chaos.sample_of("{", 3, "fault").state, "")
        self.assertEqual(chaos.sample_of("[]", 3, "fault").state, "")
        s = chaos.sample_of(json.dumps({"display_state": "offline", "has_internet": False,
                                        "probe": None}), 3, "fault")
        self.assertEqual((s.state, s.has_internet, s.probe), ("offline", False, ""))

    def test_no_route_that_reads_offline_and_recovers_passes(self):
        node, outcome = self._run("no-route", _offline_while_on)
        self.assertEqual(outcome.findings, [])
        self.assertIsNotNone(outcome.recovered_after)
        self.assertEqual(outcome.republish_lines, 0, "the log's earlier lines are not this fault's")
        self.assertFalse(node.on, "the fault is reverted")
        self.assertIn(["sudo", "ip", "route", "replace", "default", "via", "192.168.5.2", "dev", "eth0",
                       "proto", "dhcp", "src", "192.168.5.15", "metric", "100"],
                      [c[1] for c in node.calls if c[0] == "vm"])

    def test_a_default_route_that_comes_back_is_taken_away_again(self):
        """A DHCP renewal or router advertisement in the hold."""
        clock = _ChaosClock()
        node = _ChaosNode(clock, _offline_while_on)
        node.route_returns = 2
        outcome = chaos.run_fault(node, chaos.FAULTS["no-route"], node.ADDRESS, clock=clock)
        self.assertEqual(outcome.routes_came_back, 2)
        dels = [c[1] for c in node.calls if c[0] == "vm" and "del" in c[1]]
        self.assertEqual(len(dels), 3)
        self.assertIn("re-deleted 2", outcome.summary())

    def test_the_bug_139_was_is_named(self):
        """Route there, nothing past it: "starting" then "stuck" is what
        #139 reported."""
        _, outcome = self._run("captive-portal",
                               lambda on, since: ("stuck" if since > 300 else "starting") if on
                               else "available")
        joined = "\n".join(outcome.findings)
        self.assertIn("never read 'offline'", joined)
        self.assertIn("'stuck'", joined)

    def test_a_paused_tor_must_not_read_offline(self):
        _, outcome = self._run("tor-paused", _offline_while_on)
        self.assertTrue(any("sick stack" in f for f in outcome.findings), outcome.findings)
        _, outcome = self._run("tor-paused", lambda on, since: "starting" if on else "available")
        self.assertEqual(outcome.findings, [])

    def test_a_flap_reconnects_every_time_and_counts_new_republish_lines(self):
        node, outcome = self._run("network-flap", lambda on, since: "starting" if on else "available",
                                  republish_on_fault=True)
        ops = [c[1][:2] for c in node.calls if c[0] == "docker" and c[1][0] == "network"]
        self.assertEqual(ops.count(["network", "disconnect"]), 2 * 3)
        self.assertEqual(ops.count(["network", "connect"]), 2 * 3)
        self.assertEqual(outcome.republish_lines, 3)
        self.assertTrue(any("republish line" in f for f in outcome.findings))

    def test_a_node_that_never_comes_back_is_named(self):
        node, outcome = self._run("tor-paused", lambda on, since: "starting")
        self.assertIsNone(outcome.recovered_after)
        self.assertTrue(any("not 'available' again" in f for f in outcome.findings))
        self.assertFalse(node.on)

    def test_a_revert_runs_even_when_sampling_fails(self):
        clock = _ChaosClock()
        node = _ChaosNode(clock, _offline_while_on)

        def boom(path):
            raise RuntimeError("ssh went away")
        node.read_file = boom
        with self.assertRaisesRegex(RuntimeError, "ssh went away"):
            chaos.run_fault(node, chaos.FAULTS["captive-portal"], node.ADDRESS, clock=clock)
        self.assertFalse(node.on)
        self.assertIn(["sudo", "iptables", "-X", chaos.CAPTIVE_CHAIN], [c[1] for c in node.calls])

    def test_host_faults_are_linux_only(self):
        import ast
        path = os.path.join(PROJECT_DIR, "e2e", "test_t1_b_chaos.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        marks = {}
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef):
                marks[fn.name] = [ast.unparse(d) for d in fn.decorator_list]
        for name, fault in chaos.FAULTS.items():
            test = "test_" + name.replace("-", "_")
            self.assertIn(test, marks, f"{name} has no test in T1 (b)")
            self.assertEqual("pytest.mark.platform('linux')" in marks[test], fault.host, test)


if __name__ == "__main__":
    unittest.main()
