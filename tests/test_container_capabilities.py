#!/usr/bin/env python3
"""Every container drops NET_RAW and runs with no-new-privileges.

Auto-login's "local" scope and /__op_proxy.php admit loopback and the
onionpress-network gateway by source address. NET_RAW, in Docker's default
capability set, would let root in any container on that network ARP-spoof
the gateway or forge its packets, so no compose service and no takeover
worker keeps it. no-new-privileges keeps a compromised unprivileged process
(debian-tor, www-data, mysql) from getting back to root through a setuid
binary; every privilege drop in these images is root calling setuid()
itself (su run as root, gosu, Apache), which it allows.

autoheal, autoheal-proxy and cloudflared drop every capability: none of
them uses one (docker-compose.yml says why for each).

No PyYAML and no Docker: the compose files are read with a line-based
splitter (as in test_docker_socket_scope.py) and subprocess is mocked.
"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker")
sys.path.insert(0, os.path.join(DOCKER_DIR, "tor"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import onionheaven_common as oc  # noqa: E402

COMPOSE = "app/Resources/docker/docker-compose.yml"
CLOUDFLARE_COMPOSE = "app/Resources/docker/docker-compose.cloudflare.yml"
COMPOSE_FILES = (COMPOSE, CLOUDFLARE_COMPOSE)

# Services that use no capability at all.
NO_CAPABILITIES = {
    COMPOSE: {"autoheal", "autoheal-proxy"},
    CLOUDFLARE_COMPOSE: {"cloudflared"},
}

NNP = "no-new-privileges:true"


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


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
    """The `- item` lines under `key:` in a service block, unquoted; None
    when the key is absent. Flow lists (`key: [A, B]`) count as absent, so
    a test fails rather than misreads one."""
    m = re.search(rf"^    {key}:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    if not m:
        return None
    items = []
    for line in m.group(1).splitlines():
        item = line.strip()[2:].strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            item = item[1:-1]
        items.append(item)
    return items


def _pairs(argv):
    """(flag, value) for every adjacent pair of a docker run argv."""
    return set(zip(argv, argv[1:]))


# ---------------------------------------------------------------------------
# Compose services
# ---------------------------------------------------------------------------

class TestComposeServices(unittest.TestCase):

    def setUp(self):
        self.files = {path: _services(_read(path)) for path in COMPOSE_FILES}

    def test_splitter_sees_every_service(self):
        """The checks below loop over what the splitter finds; make sure it
        finds them all, so a service it misses cannot slip through."""
        self.assertLessEqual(
            {"tor", "wordpress", "db", "autoheal", "autoheal-proxy", "onionheaven"},
            set(self.files[COMPOSE]))
        self.assertIn("cloudflared", self.files[CLOUDFLARE_COMPOSE])
        for path, services in self.files.items():
            with self.subTest(path=path):
                names = re.findall(r"(?m)^    container_name:", _read(path))
                self.assertEqual(len(names), len(services))

    def test_every_service_drops_net_raw(self):
        for path, services in self.files.items():
            for name, block in services.items():
                with self.subTest(path=path, service=name):
                    drops = _list_items(block, "cap_drop") or []
                    self.assertTrue(
                        "NET_RAW" in drops or "ALL" in drops,
                        f"{name}: add `cap_drop: [NET_RAW]` (block style). With "
                        "NET_RAW, root in the container can forge packets "
                        "from the onionpress-network gateway, which "
                        "auto-login and /__op_proxy.php admit.")

    def test_every_service_has_no_new_privileges(self):
        for path, services in self.files.items():
            for name, block in services.items():
                with self.subTest(path=path, service=name):
                    opts = _list_items(block, "security_opt") or []
                    self.assertIn(NNP, opts)
                    self.assertFalse(any("no-new-privileges:false" in o for o in opts))

    def test_nothing_adds_capabilities_back(self):
        for path, services in self.files.items():
            for name, block in services.items():
                with self.subTest(path=path, service=name):
                    adds = _list_items(block, "cap_add") or []
                    self.assertFalse({"ALL", "NET_RAW", "NET_ADMIN", "SYS_ADMIN"} & set(adds),
                                     adds)
                    self.assertNotRegex(block, r"(?m)^    cap_add:\s*\[")
                    self.assertNotRegex(block, r"(?m)^    privileged:\s*(true|yes)\b")

    def test_unprivileged_services_drop_everything(self):
        for path, names in NO_CAPABILITIES.items():
            for name in names:
                with self.subTest(path=path, service=name):
                    block = self.files[path][name]
                    self.assertEqual(_list_items(block, "cap_drop"), ["ALL"])
                    self.assertIsNone(_list_items(block, "cap_add"))

    def test_autoheal_relies_on_owning_the_proxy_socket(self):
        """autoheal holds no DAC_OVERRIDE: it can connect to the proxy's
        socket (0600 by default) only because both run as uid 0, the
        socket's owner. Keep both at uid 0 and the owner's write bit set."""
        proxy = self.files[COMPOSE]["autoheal-proxy"]
        self.assertRegex(proxy, r'(?m)^    user: "0:0"\s*$')
        self.assertNotRegex(self.files[COMPOSE]["autoheal"], r"(?m)^    user:")
        for arg in _list_items(proxy, "command"):
            m = re.match(r"^-proxysocketendpointfilemode=([0-7]+)$", arg)
            if "proxysocketendpointfilemode" in arg:
                self.assertIsNotNone(m, arg)
                self.assertTrue(int(m.group(1), 8) & 0o200, arg)

    def test_healthchecks_use_no_raw_sockets(self):
        for path, services in self.files.items():
            for name, block in services.items():
                m = re.search(r"(?m)^      test:\s*(.*)$", block)
                if m:
                    with self.subTest(path=path, service=name):
                        self.assertNotRegex(m.group(1), r"\b(ping6?|arping)\b")


# ---------------------------------------------------------------------------
# Takeover workers (docker run, outside compose)
# ---------------------------------------------------------------------------

def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


class TestTakeoverWorkers(unittest.TestCase):
    """The onionheaven container's heartbeat starts the takeover workers with
    `docker run`. They serve taken-over onion addresses from
    onionpress-network, so they get the compose services' hardening."""

    def assertHardened(self, argv):
        pairs = _pairs(argv)
        self.assertIn(("--cap-drop", "NET_RAW"), pairs)
        self.assertIn(("--security-opt", NNP), pairs)
        self.assertNotIn("--cap-add", argv)
        self.assertNotIn("--privileged", argv)
        # Flags, not arguments to the image's entrypoint.
        self.assertLess(argv.index("--cap-drop"), len(argv) - 1)
        self.assertLess(argv.index("--security-opt"), len(argv) - 1)

    def test_spawn_worker(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE takeover_containers (container_name TEXT PRIMARY KEY,"
                     " max_services INTEGER, bootstrapped INTEGER,"
                     " assigned_count INTEGER, created_at TEXT)")
        self.addCleanup(conn.close)
        self.addCleanup(setattr, oc, "_next_worker_index", oc._next_worker_index)
        image = "img@sha256:" + "a" * 64
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_IMAGE": image}), \
                mock.patch.object(oc, "log"), \
                mock.patch.object(oc.subprocess, "run", return_value=_done("cid\n")) as run:
            oc._spawn_worker(conn)
        runs = [c.args[0] for c in run.call_args_list if c.args[0][1] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-1], image)
        self.assertHardened(runs[0][:-1])

    def test_dead_code_spawners_agree(self):
        """Not called today, but they must not start a worker without it."""
        from onionpress.containers import ContainerManager
        from onionpress.docker import Docker
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = mock.Mock(ok=True, stderr="")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = mock.Mock(config_file=os.path.join(tmp, "config"), docker_dir=tmp)
        cm = ContainerManager(docker, paths, port_config=mock.Mock())
        cm.start_farm_worker(0, image="img")
        argv = docker.run.call_args.args[0]
        self.assertEqual(argv[-1], "img")
        self.assertHardened(argv[:-1])

        mac = _read("app/MacOS/onionpress")
        fn = mac[mac.index("_start_takeover_worker() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertRegex(fn, r"(?m)^        --cap-drop NET_RAW \\$")
        self.assertRegex(fn, r"(?m)^        --security-opt no-new-privileges:true \\$")
        self.assertNotIn("--cap-add", fn)
        self.assertNotIn("--privileged", fn)


# ---------------------------------------------------------------------------
# What the images do, as far as these settings are concerned
# ---------------------------------------------------------------------------

def _image_files():
    for sub in ("tor", "wordpress"):
        root = os.path.join(DOCKER_DIR, sub)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "wordlists"]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        yield os.path.relpath(path, PROJECT_ROOT), f.read()
                except UnicodeDecodeError:
                    continue


class TestImages(unittest.TestCase):

    def test_nothing_uses_raw_sockets(self):
        """Nothing in our images opens a raw or packet socket, so NET_RAW
        costs nothing. Something that needs one has to say so here first."""
        raw = re.compile(
            r"\bSOCK_RAW\b|\bAF_PACKET\b|\bIPPROTO_ICMP\b|\barping\b|\btcpdump\b"
            r"|\bsetcap\b|\biputils\b|\bping6?\s+-[cnqW]"
            r"|\b(?:IP[46]?|IP)-(?:SENDTO|RECV|RECVFROM|DATAGRAM):|\bINTERFACE:")
        seen = 0
        for rel, text in _image_files():
            seen += 1
            with self.subTest(path=rel):
                self.assertIsNone(raw.search(text))
        self.assertGreater(seen, 10)

    def test_privilege_drops_run_as_root(self):
        """no-new-privileges blocks setuid binaries, not root calling
        setuid(). The tor image's processes start as root (the Dockerfile
        ends on USER root) and drop to debian-tor with su; su is run only
        by root-run code (the entrypoint and the queue manager it starts),
        and nothing calls sudo. A non-root process calling su or sudo
        would fail under no-new-privileges."""
        tor_dockerfile = _read("app/Resources/docker/tor/Dockerfile")
        users = re.findall(r"(?m)^USER\s+(\S+)", tor_dockerfile)
        self.assertEqual(users[-1], "root")
        self.assertNotRegex(_read("app/Resources/docker/wordpress/Dockerfile"),
                            r"(?m)^USER\s")

        su_callers = set()
        for rel, text in _image_files():
            code = "\n".join(l for l in text.splitlines()
                             if not l.lstrip().startswith("#"))
            with self.subTest(path=rel):
                self.assertNotRegex(code, r"\bsudo\b")
            if re.search(r"""(^|[\s;&|(])su\s+-|["']su["']\s*,""", code, re.M):
                su_callers.add(os.path.basename(rel))
        self.assertEqual(su_callers, {"entrypoint.sh", "onionheaven-queue-manager.py"})


if __name__ == "__main__":
    unittest.main()
