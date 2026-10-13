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
            "onionheaven_common.py": _read(os.path.join(DOCKER, "tor", "onionheaven_common.py")),
        }
        for name, text in sources.items():
            with self.subTest(source=name):
                self.assertNotIn(egress.NETWORK, text)
                self.assertIn('"--network", "onionpress-network"' if name.endswith(".py")
                              else "--network onionpress-network", text)

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
EXPECTED_CHAIN = [
    ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"],
    ["-m", "limit", "--limit", "6/min", "--limit-burst", "10", "-j", "LOG",
     "--log-prefix", egress.LOG_PREFIX, "--log-level", "4"],
    ["-j", "REJECT", "--reject-with", "icmp-admin-prohibited"],
]


class TestRulesScript(unittest.TestCase):

    def setUp(self):
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)

    def test_is_valid_sh(self):
        subprocess.run(["sh", "-n"], input=egress.RULES_SCRIPT, text=True, check=True)

    def test_apply_on_a_host_without_docker_chains(self):
        """The VM's drop-in runs before dockerd: no DOCKER-USER yet."""
        r = self.host.run_script("apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"ok bridge={BR} ipv6=applied")
        st = self.host.rules()
        self.assertEqual(st["FORWARD"][0], ["-j", "DOCKER-USER"])
        self.assertEqual(st["DOCKER-USER"][0], ["-i", BR, "!", "-o", BR, "-j", CH])
        self.assertEqual(st["INPUT"][0], ["-i", BR, "-j", CH])
        self.assertEqual(st[CH], EXPECTED_CHAIN)
        v6 = self.host.rules("6")
        self.assertEqual(v6[CH][-1], ["-j", "REJECT", "--reject-with", "icmp6-adm-prohibited"])
        self.assertEqual(v6["INPUT"][0], ["-i", BR, "-j", CH])

    def test_replies_first_refusal_last(self):
        """Replies to connections made from outside (browser through the
        published port, LAN visitors, docker-proxy) must pass; the refusal
        is the chain's last word."""
        self.host.run_script("apply")
        chain = self.host.rules()[CH]
        self.assertIn("RELATED,ESTABLISHED", chain[0])
        self.assertEqual(chain[0][-1], "RETURN")
        self.assertEqual(chain[-1][:2], ["-j", "REJECT"])
        self.assertTrue(egress.LOG_PREFIX.startswith("CLEARNET-"),
                        "the menubar's dmesg poller collects CLEARNET lines")

    def test_under_docker_its_rules_stay_ahead_of_dockers_return(self):
        st = {"INPUT": [], "FORWARD": [["-j", "DOCKER-USER"]], "OUTPUT": [],
              "DOCKER-USER": [["-j", "RETURN"]]}
        with open(os.path.join(self.host.state, "v4.json"), "w") as f:
            json.dump(st, f)
        self.assertEqual(self.host.run_script("apply").returncode, 0)
        st = self.host.rules()
        self.assertEqual(st["DOCKER-USER"],
                         [["-i", BR, "!", "-o", BR, "-j", CH], ["-j", "RETURN"]])
        self.assertEqual(st["FORWARD"], [["-j", "DOCKER-USER"]])

    def test_idempotent(self):
        self.host.run_script("apply")
        first = self.host.rules()
        r = self.host.run_script("apply")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.host.rules(), first)

    def test_an_older_chain_is_replaced_whole(self):
        self.host.run_script("apply")
        st = self.host.rules()
        st[CH] = [["-j", "DROP"], ["-j", "ACCEPT"]]
        with open(os.path.join(self.host.state, "v4.json"), "w") as f:
            json.dump(st, f)
        self.assertNotEqual(self.host.run_script("check").returncode, 0)
        self.assertEqual(self.host.run_script("apply").returncode, 0)
        self.assertEqual(self.host.rules()[CH], EXPECTED_CHAIN)

    def test_check(self):
        r = self.host.run_script("check")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), f"missing bridge={BR}")
        self.host.run_script("apply")
        r = self.host.run_script("check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"ok bridge={BR}"))
        st = self.host.rules()
        st["INPUT"] = []
        with open(os.path.join(self.host.state, "v4.json"), "w") as f:
            json.dump(st, f)
        self.assertEqual(self.host.run_script("check").returncode, 1)

    def test_remove(self):
        self.host.run_script("apply")
        r = self.host.run_script("remove")
        self.assertEqual(r.returncode, 0)
        st = self.host.rules()
        self.assertNotIn(CH, st)
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
        st = {"INPUT": [], "FORWARD": [["-j", "DOCKER-USER"]], "OUTPUT": [],
              "DOCKER-USER": old + [mine, ["-j", "RETURN"]]}
        with open(os.path.join(self.host.state, "v4.json"), "w") as f:
            json.dump(st, f)
        self.assertEqual(self.host.run_script("apply").returncode, 0)
        self.assertEqual(self.host.rules()["DOCKER-USER"],
                         [["-i", BR, "!", "-o", BR, "-j", CH], mine, ["-j", "RETURN"]])

    def test_without_ip6tables(self):
        host = FakeHost(ipv6=False)
        self.addCleanup(host.cleanup)
        r = host.run_script("apply")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"ok bridge={BR} ipv6=absent"))

    def test_ipv6_failure_is_not_fatal_but_said(self):
        r = self.host.run_script("apply", FAKE_IPT_FAIL="6")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"ok bridge={BR} ipv6=failed"))

    def test_ipv4_failure_is_an_error(self):
        r = self.host.run_script("apply", FAKE_IPT_FAIL="4")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("error:"), r.stdout)

    def test_other_bridge_and_chain(self):
        r = self.host.run_script("apply", "eg-wp0", "EG-WP")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.host.rules()["INPUT"], [["-i", "eg-wp0", "-j", "EG-WP"]])

    def test_matches_the_bridge_never_an_address(self):
        """Keyed by name, the rules hold for any subnet Docker picks and are
        in place before the bridge exists; nothing to redo when a container
        restarts with a new address."""
        code = re.sub(r"(?m)^\s*#.*$", "", egress.RULES_SCRIPT.split("\nremove_address_logging()")[0])
        self.assertNotRegex(code, r"\s-[sd]\s+[\"$0-9]")
        self.assertNotIn("--source", code)
        self.assertNotIn("--destination", code)
        self.assertNotRegex(code, r"\d+\.\d+\.\d+\.\d+")


class TestVmInstaller(unittest.TestCase):
    """The Colima VM: script + docker.service drop-in, then the rules."""

    def setUp(self):
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)
        self.script = os.path.join(self.host.dir, "sbin", "onionpress-wordpress-egress")
        self.dropin = os.path.join(self.host.dir, "docker.service.d", "egress.conf")

    def run_installer(self):
        return subprocess.run(
            ["sh", "-c", egress.vm_installer(script=self.script, dropin=self.dropin)],
            capture_output=True, text=True, env=self.host.env(), timeout=60)

    def test_installs_script_and_drop_in_then_applies(self):
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"ok bridge={BR} ipv6=applied")
        self.assertTrue(os.stat(self.script).st_mode & stat.S_IXUSR)
        self.assertEqual(_read(self.script), "#!/bin/sh" + egress.RULES_SCRIPT)
        dropin = _read(self.dropin)
        self.assertEqual(dropin, egress.vm_dropin_text(script=self.script) + "\n")
        self.assertIn("[Service]\n", dropin)
        self.assertIn(f"ExecStartPre=-{self.script} apply {BR} {CH}", dropin)
        self.assertEqual(self.host.rules()[CH], EXPECTED_CHAIN)
        self.assertEqual([c for c in self.host.calls() if c.startswith("systemctl")],
                         ["systemctl daemon-reload"])

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
        self.assertTrue(egress.VM_DROPIN.startswith("/etc/systemd/system/docker.service.d/"))

    def test_is_valid_sh(self):
        subprocess.run(["sh", "-n"], input=egress.vm_installer(), text=True, check=True)


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
            result = egress.ensure(_paths(self.tmp), docker)
        self.assertTrue(result.ok)
        self.assertEqual(result.how, "colima-vm")
        argv, timeout, env = run.call_args[0]
        self.assertEqual(argv[:6], [limactl, "shell", "--workdir", "/", "colima", "--"])
        self.assertEqual(argv[6:9], ["sudo", "sh", "-c"])
        self.assertIn(egress.VM_DROPIN, argv[9])
        self.assertEqual(env["LIMA_HOME"], os.path.join(self.tmp, "colima", "_lima"))
        self.assertLessEqual(timeout, 60)

    def test_linux_rootless_enters_rootlesskits_namespaces(self):
        docker = mock.Mock()
        docker.run.return_value = DockerResult(0, '["name=seccomp,profile=builtin","name=rootless"]', "")
        with mock.patch.object(egress, "detect_os", return_value=egress.OS.LINUX), \
             mock.patch.object(egress, "rootless_child_pid", return_value=4242), \
             mock.patch.object(egress, "_run", return_value=(0, "ok bridge=onionpress-wp0 ipv6=absent")) as run:
            result = egress.ensure(_paths(self.tmp), docker, runtime_dir="/run/user/1000")
        self.assertTrue(result.ok)
        self.assertEqual(result.how, "rootless")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:8], ["nsenter", "-U", "--preserve-credentials", "-n", "-m",
                                    "-t", "4242", "--"])
        self.assertEqual(argv[8:11], ["sh", "-c", egress.RULES_SCRIPT])
        self.assertEqual(argv[-3:], ["apply", BR, CH])
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
            result = egress.ensure(_paths(self.tmp), docker, logs.append, image="img@sha256:x")
        run.assert_not_called()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.how, "system-docker")
        argv = docker.run.call_args_list[1][0][0]
        self.assertEqual(argv, egress.helper_argv("img@sha256:x"))
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

    def test_the_rules_go_in_before_compose_up(self):
        from onionpress.config import PortConfig
        from onionpress.containers import ContainerManager
        events = []
        docker = mock.Mock()
        docker.run.return_value = DockerResult(0, "", "")
        docker.compose.side_effect = lambda args, **kw: events.append(("compose", args)) \
            or DockerResult(0, "", "")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cm = ContainerManager(docker, _paths(tmp), PortConfig(0, 8080, 9050, 9077),
                              egress=lambda: events.append(("egress",)))
        self.assertTrue(cm.start_core())
        self.assertEqual(events[0], ("egress",))
        self.assertEqual(events[1][0], "compose")

    def test_default_is_ensure(self):
        from onionpress.config import PortConfig
        from onionpress.containers import ContainerManager
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        docker = mock.Mock()
        cm = ContainerManager(docker, _paths(tmp), PortConfig(0, 8080, 9050, 9077))
        with mock.patch.object(egress, "ensure") as ensure:
            cm._egress()
        ensure.assert_called_once_with(cm.paths, docker, cm._log)


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
        m = re.search(r"\n      (awk '.*?') \\\n", build)
        self.assertIsNotNone(m)
        out = subprocess.run(
            ["sh", "-c", m.group(1) + ' "$1"', "sh",
             os.path.join(ROOT, "src", "onionpress", "wordpress_egress.py")],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual("#!/bin/sh\n" + out, "#!/bin/sh" + egress.RULES_SCRIPT)
        self.assertIn('> "$dest/onionpress-wordpress-egress"', build)
        self.assertIn('chmod 0755 "$dest/onionpress-wordpress-egress"', build)

        dropin = _read(os.path.join(ROOT, "linux", "onionpress-wordpress-egress.conf"))
        self.assertIn("[Service]\n", dropin)
        self.assertIn("\nExecStartPre=-/opt/onionpress/onionpress-wordpress-egress "
                      f"apply {egress.BRIDGE} {egress.CHAIN}\n", dropin)
        self.assertIn('"$DEB_ROOT/usr/lib/systemd/system/docker.service.d/'
                      'onionpress-wordpress-egress.conf"', build)
        postinst = build[build.index("DEBIAN/postinst"):build.index("\nPOSTINST\n")]
        self.assertIn("\nsystemctl daemon-reload 2>/dev/null || true\n", postinst)

    def test_py2app_bundles_the_module(self):
        self.assertIn("'onionpress.wordpress_egress'", _read(os.path.join(ROOT, "setup.py")))


if __name__ == "__main__":
    unittest.main()
