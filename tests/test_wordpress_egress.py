#!/usr/bin/env python3
"""WordPress and MariaDB reach nothing outside Docker (issue #144).

wordpress and db live on onionpress-wordpress-network, whose bridge has a
fixed name (onionpress-wp0). tor, onionheaven and cloudflared join it as
well as onionpress-network, their way out. On the Docker host the
ONIONPRESS-WP chain refuses everything that comes in from that bridge and
leaves it, or is for the host itself, except replies to connections made
from outside (the published port, LAN browsers, docker-proxy). Both
launchers and ContainerManager.start_core put the rules in place before
anything starts on the network; in the Colima VM a docker.service drop-in
puts them back before Docker restarts anything after a boot. Measured on
Docker 27.4 (2026-10-13): WordPress and MariaDB got "No route to host" for
the internet, the VM, its gateway (the Mac) and the published port's
hairpin, while db, both SOCKS ports, the 8083 API, Docker's resolver, the
published port (from the VM, the Mac and a simulated LAN client), tor ->
socat -> wordpress:80 and the healthcheck worked, and REMOTE_ADDR stayed
the gateway for the publisher and the client's own address for the LAN.

The same script shuts two more ways. The Tor side (onionpress-network,
bridge onionpress-tor0: tor, onionheaven, cloudflared, takeover workers)
reaches the internet but not the Docker host or any private, link-local,
CGNAT, loopback or multicast address (ONIONPRESS-TOR), DNS and replies
excepted; and WordPress's published port is refused on the host from
anything but loopback, so no container on any bridge reaches it through
docker-proxy as the gateway auto-login trusts. Measured on Docker 27.4
(2026-10-13, a throwaway Colima VM, system and rootless Docker): before,
a Tor-side container and another project's container reached a LAN host,
the VM's sshd, the Mac's loopback through 192.168.5.2 (and through Lima's
forward WordPress's port, as the gateway), and WordPress's port through
every host address as the gateway; after, all of that was refused while
the internet, DNS, wordpress:80, the publisher's loopback (still the
gateway), the VM's own address and a LAN client (its own address) worked.

Nothing here talks to Docker: the rules script runs against a fake
iptables that keeps its rules in a file, and the dispatch is tested with
stubs.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import wordpress_egress as egress  # noqa: E402
from onionpress.docker import DockerResult  # noqa: E402

DOCKER = os.path.join(ROOT, "app", "Resources", "docker")
COMPOSE = os.path.join(DOCKER, "docker-compose.yml")
CLOUDFLARE_COMPOSE = os.path.join(DOCKER, "docker-compose.cloudflare.yml")
MAC_LAUNCHER = os.path.join(ROOT, "app", "MacOS", "onionpress")
LINUX_LAUNCHER = os.path.join(ROOT, "linux", "onionpress")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function_body(text, name):
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def _services(text):
    """{service: block text} for a compose file, comment lines dropped."""
    services, current, in_services = {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        if not line.startswith(" "):
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = []
        elif current:
            services[current].append(line)
    return {name: "\n".join(lines) for name, lines in services.items()}


def _list_items(block, key):
    m = re.search(rf"^    {key}:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    if not m:
        return None
    return [re.sub(r"^\s*-\s*", "", line).strip().strip("'\"")
            for line in m.group(1).splitlines()]


def _network_block(text, key):
    """The top-level `networks:` entry for `key`, comment lines dropped."""
    top = text[text.index("\nnetworks:\n"):]
    lines = [l for l in top.splitlines() if not l.lstrip().startswith("#")]
    out, inside = [], False
    for line in lines:
        if re.match(rf"^  {re.escape(key)}:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^  \S", line):
            break
        if inside:
            out.append(line.strip())
    return out


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------

class TestComposeNetworks(unittest.TestCase):

    def setUp(self):
        self.text = _read(COMPOSE)
        self.services = _services(self.text)

    def test_wordpress_and_db_are_on_their_network_only(self):
        for name in ("wordpress", "db"):
            with self.subTest(service=name):
                self.assertEqual(_list_items(self.services[name], "networks"),
                                 ["wordpress-network"])
                self.assertNotIn("network_mode", self.services[name])

    def test_the_tor_side_is_on_both(self):
        """tor serves wordpress:80 (socat) and WordPress's SOCKS and 8083;
        onionheaven its bulk SOCKS. Both need onionpress-network to reach
        the internet."""
        for name in ("tor", "onionheaven"):
            with self.subTest(service=name):
                self.assertEqual(_list_items(self.services[name], "networks"),
                                 ["onionpress-network", "wordpress-network"])

    def test_cloudflared_is_on_both(self):
        cf = _services(_read(CLOUDFLARE_COMPOSE))["cloudflared"]
        self.assertEqual(_list_items(cf, "networks"),
                         ["onionpress-network", "wordpress-network"])

    def test_the_network_has_the_bridge_name_the_rules_match(self):
        block = _network_block(self.text, "wordpress-network")
        self.assertIn(f"name: {egress.NETWORK}", block)
        self.assertIn("driver: bridge", block)
        self.assertIn(f"com.docker.network.bridge.name: {egress.BRIDGE}", block)
        self.assertLessEqual(len(egress.BRIDGE), 15, "IFNAMSIZ")

    def test_the_cloudflare_file_declares_the_same_network(self):
        self.assertEqual(_network_block(_read(CLOUDFLARE_COMPOSE), "wordpress-network"),
                         _network_block(self.text, "wordpress-network"))
        self.assertEqual(_network_block(_read(CLOUDFLARE_COMPOSE), "onionpress-network"),
                         _network_block(self.text, "onionpress-network"))

    def test_not_internal_and_ipv4_only(self):
        """internal: true would publish no port and give WordPress no
        default route (no gateway for auto-login). A dual-stack network
        would win the default route of tor/onionheaven/cloudflared."""
        block = _network_block(self.text, "wordpress-network")
        self.assertNotIn("internal: true", block)
        self.assertIn("enable_ipv6: false", block)

    def test_the_tor_side_has_its_bridge_name_too(self):
        """Its rules name the bridge, so it has a fixed one; the cloudflare
        file declares it the same (test above)."""
        block = _network_block(self.text, "onionpress-network")
        self.assertIn(f"name: {egress.TOR_NETWORK}", block)
        self.assertIn("driver: bridge", block)
        self.assertIn(f"com.docker.network.bridge.name: {egress.TOR_BRIDGE}", block)
        self.assertLessEqual(len(egress.TOR_BRIDGE), 15, "IFNAMSIZ")
        self.assertNotEqual(egress.TOR_BRIDGE, egress.BRIDGE)
        self.assertNotIn("enable_ipv6: true", block)

    def test_its_name_sorts_after_onionpress_network(self):
        """A container on two plain IPv4 bridges takes its default route
        from the first network name in lexical order (Docker's endpoint
        ordering, measured on 27.4 in both connect orders). tor,
        onionheaven and cloudflared must keep theirs on onionpress-network,
        or Tor and Cloudflare lose the internet."""
        self.assertIn("name: onionpress-network",
                      _network_block(self.text, "onionpress-network"))
        self.assertLess("onionpress-network", egress.NETWORK)

    def test_takeover_workers_stay_off_it(self):
        """They face Tor and never need WordPress."""
        sources = {
            "src/onionpress/containers.py": _read(os.path.join(ROOT, "src", "onionpress", "containers.py")),
            "app/MacOS/onionpress": _read(MAC_LAUNCHER),
            "onionheaven-farm.py": _read(os.path.join(DOCKER, "tor", "onionheaven-farm.py")),
        }
        for name, text in sources.items():
            with self.subTest(source=name):
                self.assertNotIn(egress.NETWORK, text)
                self.assertIn('"--network", "onionpress-network"' if name.endswith(".py")
                              else "--network onionpress-network", text)
        # The heartbeat's side names no network at all any more.
        self.assertNotIn(egress.NETWORK,
                         _read(os.path.join(DOCKER, "tor", "onionheaven_common.py")))

    def test_only_these_services_join_it(self):
        on_it = {name for name, block in self.services.items()
                 if "wordpress-network" in (_list_items(block, "networks") or [])}
        self.assertEqual(on_it, {"wordpress", "db", "tor", "onionheaven"})


# ---------------------------------------------------------------------------
# The rules script, against a fake iptables
# ---------------------------------------------------------------------------

FAKE_IPTABLES = r'''
import json, os, shlex, sys

BUILTIN = ("INPUT", "FORWARD", "OUTPUT")
TARGETS = ("ACCEPT", "DROP", "RETURN", "LOG", "REJECT")
prog = sys.argv[1]
family = "6" if prog.startswith("ip6") else "4"
state_path = os.path.join(os.environ["FAKE_IPT_STATE"], "v" + family + ".json")
calls = os.path.join(os.environ["FAKE_IPT_STATE"], "calls")
with open(calls, "a") as f:
    f.write(prog + " " + " ".join(sys.argv[2:]) + "\n")
if os.environ.get("FAKE_IPT_FAIL") == family:
    sys.exit(4)

def load():
    try:
        with open(state_path) as f:
            return json.load(f)
    except OSError:
        return {c: [] for c in BUILTIN}

def save(st):
    with open(state_path, "w") as f:
        json.dump(st, f)

def fmt(arg):
    return '"%s"' % arg if " " in arg or arg == "" else arg

def jump_ok(st, spec):
    if "-j" in spec:
        t = spec[spec.index("-j") + 1]
        return t in TARGETS or t in st
    return True

def run(st, args):
    """One command; returns (rc, output lines)."""
    op, rest = args[0], args[1:]
    if op == "-N":
        if rest[0] in st:
            return 1, []
        st[rest[0]] = []
        return 0, []
    if op == "-S":
        names = rest[:1] or list(st)
        out = []
        for c in names:
            if c not in st:
                return 1, []
            out.append(("-P %s ACCEPT" if c in BUILTIN else "-N %s") % c)
        for c in names:
            out += ["-A %s %s" % (c, " ".join(fmt(a) for a in r)) for r in st[c]]
        return 0, out
    chain = rest[0]
    if chain not in st:
        return 1, []
    if op == "-F":
        st[chain] = []
        return 0, []
    if op == "-X":
        if chain in BUILTIN or st[chain] or any(
                r[r.index("-j") + 1] == chain for c in st for r in st[c] if "-j" in r):
            return 1, []
        del st[chain]
        return 0, []
    spec = rest[1:]
    if op == "-I" and spec and spec[0].isdigit():
        pos, spec = int(spec[0]) - 1, spec[1:]
    else:
        pos = 0
    if op == "-C":
        return (0 if spec in st[chain] else 1), []
    if op == "-D":
        if spec not in st[chain]:
            return 1, []
        st[chain].remove(spec)
        return 0, []
    if not jump_ok(st, spec):
        return 2, []
    if op == "-A":
        st[chain].append(spec)
    elif op == "-I":
        st[chain].insert(pos, spec)
    else:
        return 2, []
    return 0, []

args = sys.argv[2:]
if args[:1] == ["-w"]:
    args = args[2:] if len(args) > 1 and args[1].isdigit() else args[1:]
if prog.endswith("-restore"):
    args = [a for a in args if a != "--noflush"]
    st = load()
    for line in sys.stdin.read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line in ("*filter", "COMMIT"):
            continue
        if line.startswith(":"):
            name = line[1:].split()[0]
            if name not in BUILTIN:
                st[name] = []
            continue
        rc, _ = run(st, shlex.split(line))
        if rc:
            sys.exit(1)
    save(st)
    sys.exit(0)
st = load()
rc, out = run(st, args)
if out:
    print("\n".join(out))
if rc == 0 and args[0] not in ("-C", "-S"):
    save(st)
sys.exit(rc)
'''


class FakeHost:
    """A PATH with fake iptables/ip6tables (+ -restore) and a systemctl
    that records its calls; rules kept in JSON files."""

    def __init__(self, ipv6=True):
        self.dir = tempfile.mkdtemp(prefix="onionpress-egress-")
        self.bin = os.path.join(self.dir, "bin")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.bin)
        os.makedirs(self.state)
        fake = os.path.join(self.dir, "fake_iptables.py")
        with open(fake, "w") as f:
            f.write(FAKE_IPTABLES)
        names = ["iptables", "iptables-restore"]
        if ipv6:
            names += ["ip6tables", "ip6tables-restore"]
        for name in names:
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" {name} "$@"\n')
            os.chmod(path, 0o755)
        systemctl = os.path.join(self.bin, "systemctl")
        with open(systemctl, "w") as f:
            f.write(f'#!/bin/sh\necho "systemctl $*" >> "{self.state}/calls"\n')
        os.chmod(systemctl, 0o755)

    def env(self, **extra):
        return dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin",
                    FAKE_IPT_STATE=self.state, **extra)

    def run_script(self, *args, **extra):
        return subprocess.run(
            ["sh", "-c", egress.RULES_SCRIPT, "onionpress-wordpress-egress", *args],
            capture_output=True, text=True, env=self.env(**extra), timeout=60)

    def rules(self, family="4"):
        try:
            with open(os.path.join(self.state, f"v{family}.json")) as f:
                return json.load(f)
        except OSError:
            return {}

    def calls(self):
        try:
            with open(os.path.join(self.state, "calls")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


BR, CH = egress.BRIDGE, egress.CHAIN
TBR, TCH = egress.TOR_BRIDGE, egress.TOR_CHAIN
REFUSE = TCH + "-REFUSE"
PORT = "8080"
EXPECTED_CHAIN = [
    ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"],
    ["-m", "limit", "--limit", "6/min", "--limit-burst", "10", "-j", "LOG",
     "--log-prefix", egress.LOG_PREFIX, "--log-level", "4"],
    ["-j", "REJECT", "--reject-with", "icmp-admin-prohibited"],
]
LOCAL_NETS_V4 = ["10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                 "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4"]
LOCAL_NETS_V6 = ["fc00::/7", "fe80::/10", "ff00::/8"]


def _tor_chain(nets):
    return ([["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"],
             ["-p", "udp", "-m", "udp", "--dport", "53", "-j", "RETURN"],
             ["-p", "tcp", "-m", "tcp", "--dport", "53", "-j", "RETURN"],
             ["-m", "addrtype", "--dst-type", "LOCAL", "-j", REFUSE]]
            + [["-d", net, "-j", REFUSE] for net in nets])


EXPECTED_TOR_CHAIN = _tor_chain(LOCAL_NETS_V4)
EXPECTED_REFUSE = [
    ["-m", "limit", "--limit", "6/min", "--limit-burst", "10", "-j", "LOG",
     "--log-prefix", egress.TOR_LOG_PREFIX, "--log-level", "4"],
    ["-j", "REJECT", "--reject-with", "icmp-admin-prohibited"],
]
PORT_JUMP = ["!", "-i", "lo", "-p", "tcp", "-m", "tcp", "--dport", PORT, "-j", CH]
ALL_ARGS = ("apply", BR, CH, TBR, TCH, PORT)


class TestRulesScript(unittest.TestCase):

    def setUp(self):
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)

    def _save(self, st, family="4"):
        with open(os.path.join(self.host.state, f"v{family}.json"), "w") as f:
            json.dump(st, f)

    def test_is_valid_sh(self):
        subprocess.run(["sh", "-n"], input=egress.RULES_SCRIPT, text=True, check=True)

    def test_apply_on_a_host_without_docker_chains(self):
        """The VM's drop-in runs before dockerd: no DOCKER-USER yet."""
        r = self.host.run_script(*ALL_ARGS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(),
                         f"ok bridge={BR} tor={TBR} port={PORT} ipv6=applied")
        st = self.host.rules()
        self.assertEqual(st["FORWARD"][0], ["-j", "DOCKER-USER"])
        self.assertEqual(sorted(map(tuple, st["DOCKER-USER"])), sorted([
            ("-i", BR, "!", "-o", BR, "-j", CH),
            ("-i", TBR, "!", "-o", TBR, "-j", TCH)]))
        self.assertEqual(sorted(map(tuple, st["INPUT"])), sorted([
            ("-i", BR, "-j", CH), ("-i", TBR, "-j", TCH), tuple(PORT_JUMP)]))
        self.assertEqual(st[CH], EXPECTED_CHAIN)
        self.assertEqual(st[TCH], EXPECTED_TOR_CHAIN)
        self.assertEqual(st[REFUSE], EXPECTED_REFUSE)
        v6 = self.host.rules("6")
        self.assertEqual(v6[CH][-1], ["-j", "REJECT", "--reject-with", "icmp6-adm-prohibited"])
        self.assertEqual(v6[TCH], _tor_chain(LOCAL_NETS_V6))
        self.assertEqual(v6[REFUSE][-1], ["-j", "REJECT", "--reject-with", "icmp6-adm-prohibited"])
        self.assertIn(["-i", BR, "-j", CH], v6["INPUT"])
        self.assertIn(["-i", TBR, "-j", TCH], v6["INPUT"])
        self.assertIn(PORT_JUMP, v6["INPUT"])

    def test_the_defaults_are_the_compose_names(self):
        """The .deb's drop-in and older callers pass fewer arguments."""
        r = self.host.run_script("apply")
        self.assertEqual(r.stdout.strip(),
                         f"ok bridge={BR} tor={TBR} port=unchanged ipv6=applied")
        st = self.host.rules()
        self.assertIn(["-i", TBR, "-j", TCH], st["INPUT"])
        self.assertFalse([r for r in st["INPUT"] if "--dport" in r])

    def test_replies_first_refusal_last(self):
        """Replies to connections made from outside (browser through the
        published port, LAN visitors, docker-proxy) must pass; the refusal
        is the chain's last word."""
        self.host.run_script(*ALL_ARGS)
        chain = self.host.rules()[CH]
        self.assertIn("RELATED,ESTABLISHED", chain[0])
        self.assertEqual(chain[0][-1], "RETURN")
        self.assertEqual(chain[-1][:2], ["-j", "REJECT"])
        self.assertTrue(egress.LOG_PREFIX.startswith("CLEARNET-"),
                        "the menubar's dmesg poller collects CLEARNET lines")

    def test_the_tor_side_keeps_replies_dns_and_the_internet(self):
        """Replies (the published SOCKS port, docker-proxy), DNS (Docker's
        resolver asks a LAN or on-host server from the container's
        namespace) and every public address go on; the host itself and the
        local ranges go to the refusal, which logs and rejects."""
        self.host.run_script(*ALL_ARGS)
        st = self.host.rules()
        chain = st[TCH]
        self.assertIn("RELATED,ESTABLISHED", chain[0])
        dns = chain.index(["-p", "udp", "-m", "udp", "--dport", "53", "-j", "RETURN"])
        local = chain.index(["-m", "addrtype", "--dst-type", "LOCAL", "-j", REFUSE])
        self.assertLess(dns, local, "DNS to a resolver on the host itself (Pi-hole)")
        for rule in chain[local:]:
            self.assertEqual(rule[-2:], ["-j", REFUSE])
        self.assertEqual(st[REFUSE][-1][:2], ["-j", "REJECT"])
        self.assertTrue(egress.TOR_LOG_PREFIX.startswith("CLEARNET-"),
                        "the menubar's dmesg poller collects CLEARNET lines")
        self.assertLessEqual(len(egress.TOR_LOG_PREFIX), 29, "iptables' LOG prefix limit")
        # Nothing public: Tor relays and Cloudflare's edge are.
        nets = [r[1] for r in chain if r[0] == "-d"]
        self.assertEqual(nets, LOCAL_NETS_V4)
        self.assertNotIn("198.18.0.0/15", nets, "fake-IP proxies answer public names with it")
        self.assertNotIn("0.0.0.0/0", nets)

    def test_under_docker_its_rules_stay_ahead_of_dockers_return(self):
        self._save({"INPUT": [], "FORWARD": [["-j", "DOCKER-USER"]], "OUTPUT": [],
                    "DOCKER-USER": [["-j", "RETURN"]]})
        self.assertEqual(self.host.run_script(*ALL_ARGS).returncode, 0)
        st = self.host.rules()
        self.assertEqual(st["DOCKER-USER"][-1], ["-j", "RETURN"])
        self.assertEqual(len(st["DOCKER-USER"]), 3)
        self.assertEqual(st["FORWARD"], [["-j", "DOCKER-USER"]])

    def test_idempotent(self):
        self.host.run_script(*ALL_ARGS)
        first = self.host.rules()
        r = self.host.run_script(*ALL_ARGS)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.host.rules(), first)

    def test_an_older_chain_is_replaced_whole(self):
        self.host.run_script(*ALL_ARGS)
        for chain in (CH, TCH, REFUSE):
            with self.subTest(chain=chain):
                st = self.host.rules()
                good = st[chain]
                st[chain] = good[:-1] + [["-j", "ACCEPT"]]
                self._save(st)
                self.assertNotEqual(self.host.run_script("check", BR, CH, TBR, TCH).returncode, 0)
                st[chain] = good + [["-j", "ACCEPT"]]
                self._save(st)
                self.assertNotEqual(self.host.run_script("check", BR, CH, TBR, TCH).returncode, 0)
                self.assertEqual(self.host.run_script(*ALL_ARGS).returncode, 0)
                self.assertEqual(self.host.rules()[chain], good)

    def test_check(self):
        r = self.host.run_script("check", BR, CH, TBR, TCH, PORT)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), f"missing bridge={BR} tor={TBR} port={PORT}")
        self.host.run_script(*ALL_ARGS)
        r = self.host.run_script("check", BR, CH, TBR, TCH, PORT)
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (0, f"ok bridge={BR} tor={TBR} port={PORT}"))
        self.assertEqual(self.host.run_script("check").returncode, 0)
        self.assertEqual(self.host.run_script("check", BR, CH, TBR, TCH, "9999").returncode, 1)
        for jump in (["-i", BR, "-j", CH], ["-i", TBR, "-j", TCH], PORT_JUMP):
            with self.subTest(missing=jump):
                self.host.run_script(*ALL_ARGS)
                st = self.host.rules()
                st["INPUT"].remove(jump)
                self._save(st)
                self.assertEqual(self.host.run_script("check", BR, CH, TBR, TCH, PORT).returncode, 1)
        self.host.run_script(*ALL_ARGS)
        st = self.host.rules()
        st["DOCKER-USER"].remove(["-i", TBR, "!", "-o", TBR, "-j", TCH])
        self._save(st)
        self.assertEqual(self.host.run_script("check").returncode, 1)

    def test_remove(self):
        self.host.run_script(*ALL_ARGS)
        r = self.host.run_script("remove", BR, CH, TBR, TCH)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"removed bridge={BR} tor={TBR}")
        for family in ("4", "6"):
            st = self.host.rules(family)
            for chain in (CH, TCH, REFUSE):
                self.assertNotIn(chain, st)
            self.assertEqual(st["INPUT"], [])
            self.assertEqual(st["DOCKER-USER"], [])

    def test_removes_the_old_address_keyed_logging(self):
        """The macOS launcher's LOG rules named WordPress's and MariaDB's
        addresses on onionpress-network; after the move those addresses go
        to other containers (tor, takeover workers) and would log Tor's
        traffic as WordPress's."""
        old = [["-s", "172.18.0.4/32", "!", "-d", "172.18.0.0/16", "-i", "br-1234",
                "!", "-o", "br-1234", "-j", "LOG", "--log-prefix", "CLEARNET-wordpress: "],
               ["-s", "172.18.0.2/32", "!", "-d", "172.18.0.0/16", "-i", "br-1234",
                "!", "-o", "br-1234", "-j", "LOG", "--log-prefix", "CLEARNET-db: "]]
        mine = ["-i", "eth9", "-j", "ACCEPT"]
        self._save({"INPUT": [], "FORWARD": [["-j", "DOCKER-USER"]], "OUTPUT": [],
                    "DOCKER-USER": old + [mine, ["-j", "RETURN"]]})
        self.assertEqual(self.host.run_script(*ALL_ARGS).returncode, 0)
        self.assertEqual(self.host.rules()["DOCKER-USER"][-2:], [mine, ["-j", "RETURN"]])
        self.assertEqual(len(self.host.rules()["DOCKER-USER"]), 4)

    def test_without_ip6tables(self):
        host = FakeHost(ipv6=False)
        self.addCleanup(host.cleanup)
        r = host.run_script(*ALL_ARGS)
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (0, f"ok bridge={BR} tor={TBR} port={PORT} ipv6=absent"))

    def test_ipv6_failure_is_not_fatal_but_said(self):
        r = self.host.run_script(*ALL_ARGS, FAKE_IPT_FAIL="6")
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (0, f"ok bridge={BR} tor={TBR} port={PORT} ipv6=failed"))

    def test_ipv4_failure_is_an_error(self):
        r = self.host.run_script(*ALL_ARGS, FAKE_IPT_FAIL="4")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("error:"), r.stdout)

    def test_other_bridges_and_chains(self):
        r = self.host.run_script("apply", "eg-wp0", "EG-WP", "eg-tor0", "EG-TOR", "35080")
        self.assertEqual(r.returncode, 0, r.stdout)
        st = self.host.rules()
        self.assertEqual(sorted(map(tuple, st["INPUT"])), sorted([
            ("-i", "eg-wp0", "-j", "EG-WP"), ("-i", "eg-tor0", "-j", "EG-TOR"),
            ("!", "-i", "lo", "-p", "tcp", "-m", "tcp", "--dport", "35080", "-j", "EG-WP")]))
        self.assertIn("EG-TOR-REFUSE", st)

    def test_names_and_ports_are_checked(self):
        """The script evals its own rule lines when it checks them."""
        for args in (("apply", "wp0;reboot"), ("apply", BR, "CH AIN"),
                     ("apply", BR, CH, "eg tor0", TCH), ("apply", BR, CH, TBR, "$(id)"),
                     ("apply", BR, CH, TBR, TCH, "0"), ("apply", BR, CH, TBR, TCH, "08080"),
                     ("apply", BR, CH, TBR, TCH, "65536"), ("apply", BR, CH, TBR, TCH, "123456"),
                     ("apply", BR, CH, TBR, TCH, "80 -j ACCEPT")):
            with self.subTest(args=args):
                r = self.host.run_script(*args)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertTrue(r.stdout.startswith("error: bad "), r.stdout)
        self.assertEqual(self.host.calls(), [])
        self.assertEqual(self.host.run_script("apply", BR, CH, TBR, TCH, "65535").returncode, 0)

    def test_matches_bridges_never_a_docker_address(self):
        """Keyed by name, the rules hold for any subnet Docker picks and are
        in place before the bridges exist; nothing to redo when a container
        restarts with a new address. The only addresses in the script are
        the fixed local ranges the Tor side may not reach."""
        code = re.sub(r"(?m)^\s*#.*$", "", egress.RULES_SCRIPT.split("\nremove_address_logging()")[0])
        self.assertNotIn("--source", code)
        self.assertNotIn("--destination", code)
        self.assertNotRegex(code, r"\s-s\s")
        self.assertEqual(re.findall(r"\s-d\s+(\S+)", code), ["$net"])
        local_nets = code[code.index("local_nets() {"):code.index("\n}\n", code.index("local_nets() {"))]
        self.assertEqual(re.findall(r"\d+\.\d+\.\d+\.\d+", code),
                         re.findall(r"\d+\.\d+\.\d+\.\d+", local_nets))

    # -- WordPress's published port (the docker-proxy hairpin) --

    def test_the_published_port_is_refused_from_all_but_loopback(self):
        """Docker skips its DNAT for anything from one of its bridges, so a
        container's connection to <host address>:<port> reaches
        docker-proxy on INPUT and WordPress as the gateway. Loopback (the
        publisher's browser, Lima's forward) is never matched; LAN visitors
        are DNAT'd and never reach INPUT."""
        self.host.run_script(*ALL_ARGS)
        jumps = [r for r in self.host.rules()["INPUT"] if "--dport" in r]
        self.assertEqual(jumps, [PORT_JUMP])
        self.assertEqual(PORT_JUMP[:3], ["!", "-i", "lo"])
        self.assertEqual(PORT_JUMP[-2:], ["-j", CH], "replies pass, the rest is refused")

    def test_a_changed_port_leaves_no_rule_behind(self):
        """Another user's offset (18080), or this user's coming back."""
        self.host.run_script(*ALL_ARGS)
        self.host.run_script("apply", BR, CH, TBR, TCH, "18080")
        for family in ("4", "6"):
            jumps = [r for r in self.host.rules(family)["INPUT"] if "--dport" in r]
            self.assertEqual(jumps, [PORT_JUMP[:-3] + ["18080", "-j", CH]])

    def test_without_a_port_the_rule_is_left_as_it_is(self):
        """The .deb's boot-time drop-in does not know the port."""
        self.host.run_script(*ALL_ARGS)
        self.host.run_script("apply", BR, CH, TBR, TCH)
        self.assertIn(PORT_JUMP, self.host.rules()["INPUT"])

    def test_other_rules_on_the_port_and_the_chain_stay(self):
        mine = ["-p", "tcp", "-m", "tcp", "--dport", "8080", "-j", "ACCEPT"]
        self._save({"INPUT": [mine], "FORWARD": [], "OUTPUT": []})
        self.host.run_script(*ALL_ARGS)
        self.host.run_script("apply", BR, CH, TBR, TCH, "18080")
        st = self.host.rules()
        self.assertIn(mine, st["INPUT"])
        self.assertIn(["-i", BR, "-j", CH], st["INPUT"])
        self.host.run_script("remove", BR, CH, TBR, TCH)
        self.assertEqual(self.host.rules()["INPUT"], [mine])


class TestVmInstaller(unittest.TestCase):
    """The Colima VM: script + docker.service drop-in, then the rules."""

    def setUp(self):
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)
        self.script = os.path.join(self.host.dir, "sbin", "onionpress-wordpress-egress")
        self.dropin = os.path.join(self.host.dir, "docker.service.d", "egress.conf")

    def run_installer(self, wp_port=8080):
        return subprocess.run(
            ["sh", "-c", egress.vm_installer(script=self.script, dropin=self.dropin,
                                             wp_port=wp_port)],
            capture_output=True, text=True, env=self.host.env(), timeout=60)

    def test_installs_script_and_drop_in_then_applies(self):
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"ok bridge={BR} tor={TBR} port=8080 ipv6=applied")
        self.assertTrue(os.stat(self.script).st_mode & stat.S_IXUSR)
        self.assertEqual(_read(self.script), "#!/bin/sh" + egress.RULES_SCRIPT)
        dropin = _read(self.dropin)
        self.assertEqual(dropin, egress.vm_dropin_text(script=self.script, wp_port=8080) + "\n")
        self.assertIn("[Service]\n", dropin)
        self.assertIn(f"ExecStartPre=-{self.script} apply {BR} {CH} {TBR} {TCH} 8080", dropin)
        st = self.host.rules()
        self.assertEqual(st[CH], EXPECTED_CHAIN)
        self.assertEqual(st[TCH], EXPECTED_TOR_CHAIN)
        self.assertIn(PORT_JUMP, st["INPUT"])
        self.assertEqual([c for c in self.host.calls() if c.startswith("systemctl")],
                         ["systemctl daemon-reload"])

    def test_a_new_port_rewrites_the_drop_in(self):
        """After a VM boot the drop-in applies the port the launcher last
        used; another offset must not leave the old one there."""
        self.run_installer()
        self.assertEqual(self.run_installer(wp_port=18080).returncode, 0)
        self.assertIn(f"apply {BR} {CH} {TBR} {TCH} 18080\n", _read(self.dropin))
        self.assertEqual([c for c in self.host.calls() if c.startswith("systemctl")],
                         ["systemctl daemon-reload"] * 2)
        jumps = [r for r in self.host.rules()["INPUT"] if "--dport" in r]
        self.assertEqual(jumps, [PORT_JUMP[:-3] + ["18080", "-j", CH]])

    def test_second_run_changes_nothing(self):
        self.run_installer()
        mtime = os.stat(self.script).st_mtime_ns
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(os.stat(self.script).st_mtime_ns, mtime)
        self.assertEqual([c for c in self.host.calls() if c.startswith("systemctl")],
                         ["systemctl daemon-reload"])

    def test_the_drop_in_never_stops_docker(self):
        """ExecStartPre=- : a failing script must not keep Docker (and the
        whole site) down; the launcher re-applies and says so."""
        self.assertIn(f"ExecStartPre=-{egress.VM_SCRIPT} apply ",
                      egress.vm_dropin_text())
        self.assertIn(f"ExecStartPre=-{egress.VM_SCRIPT} apply {BR} {CH} {TBR} {TCH} 8080",
                      egress.vm_dropin_text(wp_port=8080))
        self.assertTrue(egress.VM_DROPIN.startswith("/etc/systemd/system/docker.service.d/"))

    def test_is_valid_sh(self):
        subprocess.run(["sh", "-n"], input=egress.vm_installer(), text=True, check=True)
        subprocess.run(["sh", "-n"], input=egress.vm_installer(wp_port=18080), text=True,
                       check=True)


# ---------------------------------------------------------------------------
# Where the rules go (no Docker, no VM: stubs)
# ---------------------------------------------------------------------------

def _paths(tmp, bin_dir=None):
    from onionpress.platform import OnionPressPaths
    return OnionPressPaths(
        data_dir=tmp, documents_dir=tmp, config_file=os.path.join(tmp, "config"),
        secrets_file=os.path.join(tmp, "secrets"), log_file=os.path.join(tmp, "log"),
        launcher_log_file=os.path.join(tmp, "launcher.log"),
        pid_file=os.path.join(tmp, "pid"), shared_dir=tmp,
        docker_config_dir=tmp, bin_dir=bin_dir if bin_dir is not None else tmp,
        docker_dir=tmp, colima_home=os.path.join(tmp, "colima"),
        docker_socket=os.path.join(tmp, "docker.sock"), app_bundle="")


class TestDispatch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_macos_uses_only_the_apps_own_limactl(self):
        """Not one on PATH (Homebrew's, another Colima) and not LIMA_HOME
        from the environment: either could be someone else's VM."""
        docker = mock.Mock()
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.MACOS), \
             mock.patch.object(egress, "_run") as run:
            result = egress.ensure(_paths(self.tmp), docker)
        run.assert_not_called()
        docker.run.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("no bundled limactl", result.detail)

        limactl = os.path.join(self.tmp, "limactl")
        with open(limactl, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(limactl, 0o755)
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.MACOS), \
             mock.patch.object(egress, "_run", return_value=(0, "ok bridge=onionpress-wp0 ipv6=applied")) as run, \
             mock.patch.dict(os.environ, {"LIMA_HOME": "/elsewhere"}):
            result = egress.ensure(_paths(self.tmp), docker, wp_port=18080)
        self.assertTrue(result.ok)
        self.assertEqual(result.how, "colima-vm")
        argv, timeout, env = run.call_args[0]
        self.assertEqual(argv[:6], [limactl, "shell", "--workdir", "/", "colima", "--"])
        self.assertEqual(argv[6:9], ["sudo", "sh", "-c"])
        self.assertIn(egress.VM_DROPIN, argv[9])
        self.assertIn(f'exec "$script" apply {BR} {CH} {TBR} {TCH} 18080\n', argv[9])
        self.assertEqual(env["LIMA_HOME"], os.path.join(self.tmp, "colima", "_lima"))
        self.assertLessEqual(timeout, 60)

    def test_linux_rootless_enters_rootlesskits_namespaces(self):
        docker = mock.Mock()
        docker.run.return_value = DockerResult(0, '["name=seccomp,profile=builtin","name=rootless"]', "")
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX), \
             mock.patch.object(egress, "rootless_child_pid", return_value=4242), \
             mock.patch.object(egress, "_run", return_value=(0, "ok bridge=onionpress-wp0 ipv6=absent")) as run:
            result = egress.ensure(_paths(self.tmp), docker, runtime_dir="/run/user/1000",
                                   wp_port=8080)
        self.assertTrue(result.ok)
        self.assertEqual(result.how, "rootless")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:8], ["nsenter", "-U", "--preserve-credentials", "-n", "-m",
                                    "-t", "4242", "--"])
        self.assertEqual(argv[8:11], ["sh", "-c", egress.RULES_SCRIPT])
        self.assertEqual(argv[11:], ["onionpress-wordpress-egress", "apply", BR, CH, TBR, TCH,
                                     "8080"])
        # Only `docker info` reached Docker.
        self.assertEqual(docker.run.call_count, 1)

    def test_linux_rootless_without_a_child_pid_says_so(self):
        docker = mock.Mock()
        docker.run.return_value = DockerResult(0, '["name=rootless"]', "")
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX), \
             mock.patch.object(egress, "_run") as run:
            result = egress.ensure(_paths(self.tmp), docker, runtime_dir=self.tmp)
        run.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIn("dockerd-rootless", result.detail)

    def test_linux_system_docker_runs_the_helper(self):
        docker = mock.Mock()
        docker.run.side_effect = [
            DockerResult(0, '["name=apparmor","name=seccomp,profile=builtin"]', ""),
            DockerResult(0, "ok bridge=onionpress-wp0 ipv6=applied\n", ""),
        ]
        logs = []
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX), \
             mock.patch.object(egress, "_run") as run:
            result = egress.ensure(_paths(self.tmp), docker, logs.append, image="img@sha256:x",
                                   wp_port=8080)
        run.assert_not_called()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.how, "system-docker")
        argv = docker.run.call_args_list[1][0][0]
        self.assertEqual(argv, egress.helper_argv("img@sha256:x") + [TBR, TCH, "8080"])
        self.assertEqual(argv[-6:], ["apply", BR, CH, TBR, TCH, "8080"])
        self.assertTrue(logs and "egress block" in logs[0] and "WARNING" not in logs[0])

    def test_the_helper_joins_no_network(self):
        """Privileged, so it must take nothing from any container network,
        nor stay: --rm, --network none, the host's own iptables via PID 1."""
        argv = egress.helper_argv("img")
        self.assertEqual(argv[:2], ["run", "--rm"])
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv.count("--network"), 1)
        self.assertEqual(argv[argv.index("--pid") + 1], "host")
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "nsenter")
        image_at = argv.index("img")
        self.assertEqual(argv[image_at + 1:image_at + 6], ["-t", "1", "-m", "-n", "--"])
        self.assertNotIn("-v", argv)
        self.assertNotIn("--volume", argv)

    def test_system_docker_uses_the_pinned_tor_image(self):
        from onionpress.containers import ONIONHEAVEN_IMAGE_PIN
        docker = mock.Mock()
        docker.run.side_effect = [DockerResult(0, "[]", ""), DockerResult(0, "ok\n", "")]
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX), \
             mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("ONIONPRESS_TOR_IMAGE", None)
            egress.ensure(_paths(self.tmp), docker)
        self.assertIn(ONIONHEAVEN_IMAGE_PIN, docker.run.call_args_list[1][0][0])
        self.assertIn("@sha256:", ONIONHEAVEN_IMAGE_PIN)

    def test_a_failure_is_logged_never_raised(self):
        docker = mock.Mock()
        docker.run.side_effect = RuntimeError("boom")
        logs = []
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX):
            result = egress.ensure(_paths(self.tmp), docker, logs.append)
        self.assertFalse(result.ok)
        self.assertTrue(logs[0].startswith("WARNING: WordPress network egress block not in place"))

    def test_docker_not_answering(self):
        docker = mock.Mock()
        docker.run.return_value = DockerResult(1, "", "Cannot connect")
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX):
            result = egress.ensure(_paths(self.tmp), docker)
        self.assertFalse(result.ok)

    def test_script_args(self):
        self.assertEqual(egress.script_args(), ["apply", BR, CH, TBR, TCH])
        self.assertEqual(egress.script_args("check", 18080), ["check", BR, CH, TBR, TCH, "18080"])
        self.assertEqual(egress.script_args("apply", "8080")[-1], "8080")
        for bad in (0, 65536, -1, "80; reboot", "x"):
            with self.subTest(port=bad):
                with self.assertRaises(ValueError):
                    egress.script_args("apply", bad)

    def test_a_bad_port_is_an_error_not_a_guess(self):
        docker = mock.Mock()
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX):
            result = egress.ensure(_paths(self.tmp), docker, wp_port=70000)
        self.assertFalse(result.ok)
        docker.run.assert_not_called()

    def test_helper_output_must_say_ok(self):
        docker = mock.Mock()
        docker.run.return_value = DockerResult(0, "error: iptables rules not installed\n", "")
        self.assertFalse(egress.apply_system_docker(docker, "img").ok)

    def test_rootless_child_pid_must_be_this_users(self):
        os.makedirs(os.path.join(self.tmp, "dockerd-rootless"))
        with open(os.path.join(self.tmp, "dockerd-rootless", "child_pid"), "w") as f:
            f.write(f"{os.getpid()}\n")
        with mock.patch("os.stat") as st:
            st.return_value = mock.Mock(st_uid=os.getuid())
            self.assertEqual(egress.rootless_child_pid(self.tmp), os.getpid())
            st.return_value = mock.Mock(st_uid=os.getuid() + 1)
            self.assertIsNone(egress.rootless_child_pid(self.tmp))
        with open(os.path.join(self.tmp, "dockerd-rootless", "child_pid"), "w") as f:
            f.write("1\n")
        self.assertIsNone(egress.rootless_child_pid(self.tmp))


class TestStartCore(unittest.TestCase):

    def _manager(self, docker, **kw):
        from onionpress.config import PortConfig
        from onionpress.containers import ContainerManager
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return ContainerManager(docker, _paths(tmp), PortConfig(10000, 18080, 19050, 19077),
                                **kw)

    def test_the_rules_go_in_before_compose_up(self):
        events = []
        docker = mock.Mock()
        docker.run.side_effect = lambda args, **kw: events.append(("run", args[:2])) \
            or DockerResult(0, "", "")
        docker.compose.side_effect = lambda args, **kw: events.append(("compose", args)) \
            or DockerResult(0, "", "")
        cm = self._manager(docker, egress=lambda: events.append(("egress",)))
        self.assertTrue(cm.start_core())
        self.assertEqual(events[0], ("egress",))
        compose_at = [e[0] for e in events].index("compose")
        self.assertIn(("run", ["network", "inspect"]), events[:compose_at],
                      "the Tor network's bridge is checked before compose up")

    def test_default_is_ensure_with_this_users_port(self):
        docker = mock.Mock()
        cm = self._manager(docker)
        with mock.patch.object(egress, "ensure") as ensure:
            cm._egress()
        ensure.assert_called_once_with(cm.paths, docker, cm._log, wp_port=18080)


class FakeTorNetworkDocker:
    """`docker` for migrate_tor_network: an onionpress-network with these
    options and containers; rm -f takes a container off it."""

    def __init__(self, options, names, rm_ok=True):
        self.options, self.names, self.rm_ok = options, list(names), rm_ok
        self.calls = []

    def run(self, args, **kw):
        self.calls.append(list(args))
        if args[:2] == ["network", "inspect"]:
            if self.options is None:
                return DockerResult(1, "", "Error: No such network")
            if args[3] == "{{json .Options}}":
                return DockerResult(0, self.options + "\n", "")
            return DockerResult(0, " ".join(self.names) + " \n", "")
        if args[:2] == ["rm", "-f"]:
            if args[2] in self.names:
                self.names.remove(args[2])
            return DockerResult(0, args[2] + "\n", "")
        if args[:3] == ["network", "disconnect", "-f"]:
            self.names.remove(args[4])
            return DockerResult(0, "", "")
        if args[:2] == ["network", "rm"]:
            return DockerResult(0 if self.rm_ok and not self.names else 1, "",
                                "" if self.rm_ok else "has active endpoints")
        return DockerResult(0, "", "")


class TestTorNetworkMove(unittest.TestCase):
    """onionpress-network made before its bridge had a name (Docker's
    br-<id>, which the Tor side's rules miss) is recreated once."""

    OURS = ["onionpress-tor", "onionheaven-takeover-3", "onionheaven-poll-0",
            "onionpress-cloudflared", "onionpress-wordpress", "onionpress-db"]

    def _manager(self, docker):
        from onionpress.config import PortConfig
        from onionpress.containers import ContainerManager
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        logs = []
        cm = ContainerManager(docker, _paths(tmp), PortConfig(0, 8080, 9050, 9077),
                              log_func=logs.append, egress=lambda: None)
        return cm, logs

    def test_an_old_network_is_emptied_and_removed(self):
        docker = FakeTorNetworkDocker("{}", ["onionheaven", "someone-else"] + self.OURS)
        cm, logs = self._manager(docker)
        self.assertTrue(cm.migrate_tor_network())
        removed = [c[2] for c in docker.calls if c[:2] == ["rm", "-f"]]
        self.assertEqual(removed[0], "onionheaven", "it starts workers on the network")
        self.assertEqual(sorted(removed[1:]), sorted(self.OURS))
        self.assertIn(["network", "disconnect", "-f", egress.TOR_NETWORK, "someone-else"],
                      docker.calls)
        self.assertNotIn("someone-else", removed)
        self.assertEqual(docker.calls[-1], ["network", "rm", egress.TOR_NETWORK])
        self.assertTrue(any("someone-else" in l and "disconnecting" in l for l in logs))

    # Names that only look like OnionPress's: never removed, only
    # disconnected (the workers are onionheaven-poll-<n> and
    # onionheaven-takeover-<n>, nothing else).
    LOOK_ALIKES = ["onionheaven-takeover-3x", "onionheaven-poll-1-mine",
                   "onionheaven-takeover-", "onionpress-tor-dev", "my-onionheaven"]

    def test_names_that_only_look_like_ours_are_never_removed(self):
        docker = FakeTorNetworkDocker("{}", self.LOOK_ALIKES + self.OURS)
        cm, _ = self._manager(docker)
        self.assertTrue(cm.migrate_tor_network())
        removed = [c[2] for c in docker.calls if c[:2] == ["rm", "-f"]]
        self.assertEqual(sorted(removed), sorted(self.OURS))
        disconnected = [c[4] for c in docker.calls if c[:3] == ["network", "disconnect", "-f"]]
        self.assertEqual(sorted(disconnected), sorted(self.LOOK_ALIKES))

    def test_a_network_on_its_bridge_is_left_alone(self):
        docker = FakeTorNetworkDocker(
            json.dumps({"com.docker.network.bridge.name": egress.TOR_BRIDGE}), self.OURS)
        cm, _ = self._manager(docker)
        self.assertFalse(cm.migrate_tor_network())
        self.assertEqual(len(docker.calls), 1)

    def test_no_network_or_no_answer_changes_nothing(self):
        for options in (None, "", "not json", "[]"):
            with self.subTest(options=options):
                docker = FakeTorNetworkDocker(options, self.OURS)
                cm, _ = self._manager(docker)
                self.assertFalse(cm.migrate_tor_network())
                self.assertEqual(len(docker.calls), 1)

    def test_a_network_that_will_not_go_is_a_warning(self):
        docker = FakeTorNetworkDocker('{"com.docker.network.bridge.name":"br-x"}', [],
                                      rm_ok=False)
        cm, logs = self._manager(docker)
        self.assertFalse(cm.migrate_tor_network())
        self.assertTrue(any(l.startswith("WARNING: could not recreate onionpress-network")
                            for l in logs))

    def test_start_core_moves_it_after_retiring_and_before_compose(self):
        docker = FakeTorNetworkDocker("{}", ["onionpress-tor"])
        docker.compose = mock.Mock(side_effect=lambda args, **kw: docker.calls.append(
            ["compose"] + list(args)) or DockerResult(0, "", ""))
        cm, _ = self._manager(docker)
        self.assertTrue(cm.start_core())
        flat = [" ".join(c) for c in docker.calls]
        self.assertLess(flat.index(f"network rm {egress.TOR_NETWORK}"),
                        [i for i, c in enumerate(flat) if c.startswith("compose up")][0])
        self.assertLess([i for i, c in enumerate(flat) if c.startswith("ps -a")][0],
                        flat.index(f"network rm {egress.TOR_NETWORK}"))


# ---------------------------------------------------------------------------
# The launchers
# ---------------------------------------------------------------------------

class TestLaunchers(unittest.TestCase):

    def test_both_shut_the_network_before_the_first_compose_up(self):
        for path in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(launcher=os.path.relpath(path, ROOT)):
                body = _function_body(_read(path), "start_containers")
                code = "\n".join(l for l in body.splitlines()
                                 if not l.lstrip().startswith("#"))
                self.assertIn("\n    block_wordpress_egress\n", code)
                self.assertLess(code.index("block_wordpress_egress"),
                                code.index("docker compose up"))

    def test_the_helpers_call_the_shared_cli_and_never_stop_the_start(self):
        for path, runner in ((MAC_LAUNCHER, "bundled_python -m onionpress.cli"),
                             (LINUX_LAUNCHER, "op_py")):
            with self.subTest(launcher=os.path.relpath(path, ROOT)):
                body = _function_body(_read(path), "block_wordpress_egress")
                self.assertIn(f'{runner} --data-dir "$DATA_DIR" wordpress-egress', body)
                self.assertTrue(body.rstrip().endswith("return 0"),
                                "both launchers run under set -e")
                self.assertIn("WARNING: WordPress network egress block not in place", body)

    def test_mac_passes_its_own_limactl(self):
        body = _function_body(_read(MAC_LAUNCHER), "block_wordpress_egress")
        self.assertIn('--bin-dir "$BIN_DIR"', body)

    def test_both_pass_this_users_port(self):
        """The published-port rule needs the port, offset included; both
        launchers export ONIONPRESS_WP_PORT before anything runs."""
        for path in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(launcher=os.path.relpath(path, ROOT)):
                text = _read(path)
                body = _function_body(text, "block_wordpress_egress")
                self.assertIn('--wp-port "$ONIONPRESS_WP_PORT"', body)
                self.assertRegex(text, r"(?m)^export ONIONPRESS_WP_PORT=\$\(\(8080 \+ ONIONPRESS_PORT_OFFSET\)\)$")
                self.assertLess(text.index("export ONIONPRESS_WP_PORT="),
                                text.index("\nblock_wordpress_egress() {"))

    def test_the_cli_reads_the_port(self):
        from onionpress import cli
        calls = []
        with mock.patch.object(egress, "ensure",
                               side_effect=lambda *a, **kw: calls.append(kw) or egress.Result(True, "x", "ok")), \
             mock.patch.dict(os.environ, {"ONIONPRESS_WP_PORT": "28080"}), \
             mock.patch("builtins.print"):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            self.assertEqual(cli.main(["--data-dir", tmp, "wordpress-egress"]), 0)
            self.assertEqual(cli.main(["--data-dir", tmp, "wordpress-egress", "--wp-port", "8080"]), 0)
        self.assertEqual([c["wp_port"] for c in calls], [28080, 8080])

    # -- onionpress-network onto its fixed bridge --

    def test_both_move_the_tor_network_before_the_first_compose_up(self):
        for path in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(launcher=os.path.relpath(path, ROOT)):
                body = _function_body(_read(path), "start_containers")
                code = "\n".join(l for l in body.splitlines()
                                 if not l.lstrip().startswith("#"))
                self.assertIn("\n    migrate_tor_network\n", code)
                self.assertLess(code.index("\n    retire_old_onionheaven_containers\n"),
                                code.index("\n    migrate_tor_network\n"))
                self.assertLess(code.index("\n    migrate_tor_network\n"),
                                code.index("docker compose up"))

    def test_the_launchers_carry_the_same_move(self):
        bodies = [_function_body(_read(p), "migrate_tor_network")
                  for p in (MAC_LAUNCHER, LINUX_LAUNCHER)]
        self.assertEqual(bodies[0], bodies[1])
        self.assertIn(f'"com.docker.network.bridge.name":"{egress.TOR_BRIDGE}"', bodies[0])
        self.assertIn(egress.TOR_NETWORK, bodies[0])
        self.assertTrue(bodies[0].rstrip().endswith("return 0"), "set -e")

    MOVE_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
case "$1 $2" in
    "network inspect")
        [ -f "$STUB_DIR/options" ] || exit 1
        case "$4" in
            "{{json .Options}}") cat "$STUB_DIR/options" ;;
            *) n=$(cat "$STUB_DIR/inspected" 2>/dev/null || echo 0)
               echo $((n + 1)) > "$STUB_DIR/inspected"
               cat "$STUB_DIR/names.$n" 2>/dev/null || cat "$STUB_DIR/names" ;;
        esac ;;
    "network rm") exit "${STUB_RM_RC:-0}" ;;
esac
exit 0
"""

    def _move(self, launcher, options, names, later_names=None, rm_rc=0):
        tmp = tempfile.mkdtemp(prefix="op-tor-net-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "bin"))
        stub = os.path.join(tmp, "bin", "docker")
        with open(stub, "w") as f:
            f.write(self.MOVE_STUB)
        os.chmod(stub, 0o755)
        if options is not None:
            with open(os.path.join(tmp, "options"), "w") as f:
                f.write(options + "\n")
        with open(os.path.join(tmp, "names.0"), "w") as f:
            f.write(" ".join(names) + " \n")
        with open(os.path.join(tmp, "names"), "w") as f:
            f.write(" ".join(later_names if later_names is not None else names) + " \n")
        open(os.path.join(tmp, "calls"), "w").close()
        script = ("set -e\n"
                  f'LOG_FILE="{tmp}/log"\n'
                  'log() { printf "%s\\n" "$*" >> "$LOG_FILE.lines"; }\n'
                  + _function_body(_read(launcher), "migrate_tor_network") + "\n}\n"
                  "migrate_tor_network\n")
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             timeout=60, env={"PATH": os.path.join(tmp, "bin") + ":/usr/bin:/bin",
                                              "STUB_DIR": tmp, "STUB_RM_RC": str(rm_rc)})
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(os.path.join(tmp, "calls")) as f:
            calls = f.read().splitlines()
        try:
            with open(os.path.join(tmp, "log.lines")) as f:
                lines = f.read()
        except OSError:
            lines = ""
        return calls, lines

    def test_the_move_empties_and_removes_an_old_network(self):
        ours = ["onionpress-tor", "onionheaven-takeover-3", "onionheaven-poll-0",
                "onionpress-cloudflared", "onionpress-wordpress", "onionpress-db"]
        for launcher in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(launcher=os.path.relpath(launcher, ROOT)):
                calls, lines = self._move(launcher, "{}", ["onionheaven", "other"] + ours,
                                          later_names=["other"] + ours)
                removed = [c.split()[-1] for c in calls if c.startswith("rm -f ")]
                self.assertEqual(removed[0], "onionheaven")
                self.assertEqual(sorted(removed[1:]), sorted(ours))
                self.assertIn(f"network disconnect -f {egress.TOR_NETWORK} other", calls)
                self.assertEqual(calls[-1], f"network rm {egress.TOR_NETWORK}")
                self.assertIn("other is on onionpress-network but is not OnionPress's", lines)

    def test_the_move_never_removes_names_that_only_look_like_ours(self):
        look_alikes = TestTorNetworkMove.LOOK_ALIKES
        for launcher in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(launcher=os.path.relpath(launcher, ROOT)):
                calls, lines = self._move(launcher, "{}", look_alikes + ["onionpress-tor"])
                removed = [c.split()[-1] for c in calls if c.startswith("rm -f ")]
                self.assertEqual(removed, ["onionpress-tor"])
                for name in look_alikes:
                    self.assertIn(f"network disconnect -f {egress.TOR_NETWORK} {name}", calls)
                    self.assertIn(f"{name} is on onionpress-network but is not OnionPress's",
                                  lines)

    def test_the_move_leaves_a_network_on_its_bridge_alone(self):
        options = json.dumps({"com.docker.network.bridge.name": egress.TOR_BRIDGE},
                             separators=(",", ":"))
        for launcher in (MAC_LAUNCHER, LINUX_LAUNCHER):
            for opts in (options, None, ""):
                with self.subTest(launcher=os.path.relpath(launcher, ROOT), options=opts):
                    calls, _ = self._move(launcher, opts, ["onionpress-tor"])
                    self.assertEqual(len(calls), 1, calls)

    def test_a_network_that_will_not_go_is_a_warning(self):
        for launcher in (MAC_LAUNCHER, LINUX_LAUNCHER):
            with self.subTest(launcher=os.path.relpath(launcher, ROOT)):
                _, lines = self._move(launcher, '{"com.docker.network.bridge.name":"br-x"}',
                                      [], rm_rc=1)
                self.assertIn("WARNING: could not recreate onionpress-network", lines)

    # -- Rootless Docker's port driver --

    def test_linux_notes_when_lan_visitors_look_local(self):
        """RootlessKit's builtin driver (Docker's default) hands every
        published-port client to WordPress from the gateway; the launcher
        says so when WordPress is on every interface. Measured on Docker
        27.4 / RootlessKit 2.3.1: 127.0.0.1 and a LAN client both arrived
        as the gateway; with the slirp4netns driver, as 10.0.2.2 and as
        themselves."""
        fn = _function_body(_read(LINUX_LAUNCHER), "note_if_lan_looks_local") + "\n}\n"
        tmp = tempfile.mkdtemp(prefix="op-rootless-note-")
        self.addCleanup(shutil.rmtree, tmp, True)
        proc = os.path.join(tmp, "proc")
        runtime = os.path.join(tmp, "run")
        os.makedirs(os.path.join(proc, "4242"))
        os.makedirs(os.path.join(runtime, "dockerd-rootless"))
        with open(os.path.join(runtime, "dockerd-rootless", "child_pid"), "w") as f:
            f.write("4242\n")
        self.assertIn('"/proc/$pid/cmdline"', fn)
        fn = fn.replace('"/proc/$pid/cmdline"', f'"{proc}/$pid/cmdline"')

        def note(bind, driver):
            with open(os.path.join(proc, "4242", "cmdline"), "wb") as f:
                f.write(b"/proc/self/exe\0--net=slirp4netns\0"
                        + (f"--port-driver={driver}\0".encode() if driver else b"")
                        + b"--copy-up=/etc\0")
            script = ("set -e\nlog() { echo \"$*\"; }\n"
                      f"DATA_DIR={tmp}\nONIONPRESS_WP_PORT=8080\nONIONPRESS_BIND_HOST={bind}\n"
                      + fn + "note_if_lan_looks_local\n")
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                 timeout=30, env={"PATH": "/usr/bin:/bin",
                                                  "XDG_RUNTIME_DIR": runtime})
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout
        self.assertIn("NOTE: rootless Docker's builtin port driver", note("0.0.0.0", "builtin"))
        self.assertIn("NOTE: rootless Docker's builtin port driver", note("0.0.0.0", None))
        self.assertIn("ONIONPRESS_BIND_HOST=127.0.0.1", note("192.168.1.5", "builtin"))
        for bind, driver in (("127.0.0.1", "builtin"), ("0.0.0.0", "slirp4netns"),
                             ("0.0.0.0", "implicit")):
            with self.subTest(bind=bind, driver=driver):
                self.assertEqual(note(bind, driver), "")
        os.remove(os.path.join(runtime, "dockerd-rootless", "child_pid"))
        self.assertEqual(note("0.0.0.0", "builtin"), "", "system Docker: DNAT keeps addresses")

    def test_linux_says_it_on_every_start(self):
        body = _function_body(_read(LINUX_LAUNCHER), "start_containers")
        self.assertIn("\n    note_if_lan_looks_local\n", body)

    def test_mac_no_longer_installs_address_keyed_logging(self):
        text = _read(MAC_LAUNCHER)
        self.assertNotIn("install_clearnet_logging", text)
        self.assertNotIn('"CLEARNET-wordpress: "', text)

    def test_menubar_still_collects_clearnet_lines(self):
        menubar = _read(os.path.join(ROOT, "src", "menubar.py"))
        self.assertIn('if "CLEARNET" in l', menubar)

    def test_linux_reapplies_when_rootless_docker_restarts(self):
        text = _read(LINUX_LAUNCHER)
        loop = _function_body(text, "_status_poll_loop")
        self.assertIn("now=$(rootless_netns)", loop)
        self.assertIn("block_wordpress_egress", loop)
        netns = _function_body(text, "rootless_netns")
        self.assertIn("dockerd-rootless/child_pid", netns)
        self.assertIn('readlink "/proc/$pid/ns/net"', netns)

    def test_the_deb_ships_the_script_and_a_docker_drop_in(self):
        """System Docker restarts the containers at boot before the user's
        service runs; the drop-in puts the rules there first. It runs as
        root before dockerd, so it is plain sh cut from RULES_SCRIPT."""
        build = _read(os.path.join(ROOT, "build", "build-linux.sh"))
        # The .deb's tree is linux/install-tree.sh's list, which cuts the
        # script (an "egress" entry).
        tree = _read(os.path.join(ROOT, "linux", "install-tree.sh"))
        m = re.search(r"\n +(awk '.*?') \\\n", tree)
        self.assertIsNotNone(m)
        out = subprocess.run(
            ["sh", "-c", m.group(1) + ' "$1"', "sh",
             os.path.join(ROOT, "src", "onionpress", "wordpress_egress.py")],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual("#!/bin/sh\n" + out, "#!/bin/sh" + egress.RULES_SCRIPT)
        self.assertRegex(tree, r"\negress +src/onionpress/wordpress_egress.py +"
                               r"onionpress-wordpress-egress +0755\n")
        self.assertIn('linux/install-tree.sh" stage', build)

        dropin = _read(os.path.join(ROOT, "linux", "onionpress-wordpress-egress.conf"))
        self.assertIn("[Service]\n", dropin)
        self.assertIn("\nExecStartPre=-/opt/onionpress/onionpress-wordpress-egress "
                      f"apply {egress.BRIDGE} {egress.CHAIN} {egress.TOR_BRIDGE} "
                      f"{egress.TOR_CHAIN}\n", dropin)
        self.assertIn('"$DEB_ROOT/usr/lib/systemd/system/docker.service.d/'
                      'onionpress-wordpress-egress.conf"', build)
        postinst = build[build.index("DEBIAN/postinst"):build.index("\nPOSTINST\n")]
        self.assertIn("\nsystemctl daemon-reload 2>/dev/null || true\n", postinst)

    def test_py2app_bundles_the_module(self):
        self.assertIn("'onionpress.wordpress_egress'", _read(os.path.join(ROOT, "setup.py")))


if __name__ == "__main__":
    unittest.main()
