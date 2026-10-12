#!/usr/bin/env python3
"""Which containers get the Docker socket.

The socket is root on the Docker host: the Colima VM on macOS, the user's
own daemon under rootless Docker on Linux. autoheal no longer mounts it: it
talks to autoheal-proxy, which passes only the two calls autoheal makes,
over a unix socket on a volume, with no network.

No PyYAML (CI installs nothing beyond CPython), so the compose files are
read with a small line-based splitter.
"""

import os
import re
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

COMPOSE = "app/Resources/docker/docker-compose.yml"
OTHER_COMPOSE_FILES = (
    "app/Resources/docker/docker-compose.cloudflare.yml",
    "linux/docker-compose.pi.yml",
)
PROXY_SOCKET = "/run/autoheal-proxy/docker.sock"


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


def _mounts_docker_socket(block):
    return re.search(r"^\s*-\s*\S+:/var/run/docker\.sock(:ro)?\s*$", block, re.M) is not None


def _list_items(block, key):
    """The `- item` lines under `key:` in a service block, unquoted."""
    m = re.search(rf"^    {key}:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        item = line.strip()[2:].strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            item = item[1:-1]
        items.append(item)
    return items


def _proxy_allowlist():
    """{METHOD: python regex} from autoheal-proxy's -allowXXX= flags.

    socket-proxy anchors every pattern with ^…$ and matches r.URL.Path (no
    query string). The patterns use only syntax that RE2 and Python agree on.
    """
    allow = {}
    for arg in _list_items(_services(_read(COMPOSE))["autoheal-proxy"], "command"):
        m = re.match(r"^-allow([A-Z]+)=(.*)$", arg)
        if m:
            allow.setdefault(m.group(1), []).append(re.compile("^" + m.group(2) + "$"))
    return allow


# ---------------------------------------------------------------------------
# Compose: who mounts the socket
# ---------------------------------------------------------------------------

class TestSocketMounts(unittest.TestCase):

    def test_autoheal_does_not_mount_the_socket(self):
        services = _services(_read(COMPOSE))
        self.assertIn("autoheal", services, "compose splitter found no autoheal service")
        self.assertFalse(
            _mounts_docker_socket(services["autoheal"]),
            "autoheal reaches Docker only through autoheal-proxy.",
        )

    def test_no_new_socket_mounts(self):
        services = _services(_read(COMPOSE))
        with_socket = {name for name, block in services.items()
                       if _mounts_docker_socket(block)}
        self.assertEqual(
            with_socket, {"tor", "onionheaven", "autoheal-proxy"},
            "The Docker socket is root on the Docker host; do not mount it "
            "into another service.",
        )

    def test_other_compose_files_mount_no_socket(self):
        for path in OTHER_COMPOSE_FILES:
            with self.subTest(path=path):
                self.assertNotIn("docker.sock", _read(path))


# ---------------------------------------------------------------------------
# Compose: autoheal through the proxy
# ---------------------------------------------------------------------------

class TestAutohealProxy(unittest.TestCase):

    def setUp(self):
        services = _services(_read(COMPOSE))
        self.autoheal = services["autoheal"]
        self.proxy = services["autoheal-proxy"]

    def test_autoheal_uses_the_proxy_socket(self):
        self.assertIn(f"DOCKER_SOCK={PROXY_SOCKET}", _list_items(self.autoheal, "environment"))
        self.assertIn("autoheal-proxy:/run/autoheal-proxy", _list_items(self.autoheal, "volumes"))
        self.assertIn("autoheal-proxy", _list_items(self.autoheal, "depends_on"))
        self.assertIn(f"-proxysocketendpoint={PROXY_SOCKET}",
                      _list_items(self.proxy, "command"))
        self.assertIn("autoheal-proxy:/run/autoheal-proxy", _list_items(self.proxy, "volumes"))

    def test_neither_container_has_a_network(self):
        """autoheal needs only the unix socket (no webhook is configured);
        the proxy serves that socket, not TCP. With no network, nothing on
        onionpress-network can reach the proxy."""
        for name, block in (("autoheal", self.autoheal), ("autoheal-proxy", self.proxy)):
            with self.subTest(service=name):
                self.assertRegex(block, r"(?m)^    network_mode: none\s*$")
                self.assertNotRegex(block, r"(?m)^    (networks|ports):")

    def test_proxy_serves_no_tcp(self):
        for arg in _list_items(self.proxy, "command"):
            with self.subTest(arg=arg):
                self.assertNotRegex(arg, r"^-(listenip|proxyport|allowfrom)=")

    def test_proxy_is_locked_down(self):
        self.assertRegex(self.proxy, r"(?m)^    read_only: true\s*$")
        self.assertEqual(_list_items(self.proxy, "cap_drop"), ["ALL"])
        self.assertIn("no-new-privileges:true", _list_items(self.proxy, "security_opt"))
        # The image's own user (65534) cannot open a socket owned by root in
        # the Colima VM or by the user under rootless Docker.
        self.assertRegex(self.proxy, r'(?m)^    user: "0:0"\s*$')

    def test_proxy_image_is_pinned_by_digest(self):
        m = re.search(r"(?m)^    image: (\S+)\s*$", self.proxy)
        self.assertIsNotNone(m)
        self.assertRegex(
            m.group(1),
            r"^ghcr\.io/wollomatic/socket-proxy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$",
            "The proxy holds the socket: pin it by multi-arch index digest "
            "(build/base-image-digest.sh), never a bare tag.",
        )

    def test_volume_is_named(self):
        self.assertRegex(_read(COMPOSE),
                         r"(?m)^  autoheal-proxy:\n    name: onionpress-autoheal-proxy$")


class TestProxyAllowlist(unittest.TestCase):
    """autoheal's script makes exactly two Docker API calls:
    GET /containers/json?filters=… and POST /containers/<id>/restart?t=….
    """

    def setUp(self):
        self.allow = _proxy_allowlist()

    def _allowed(self, method, path):
        return any(p.match(path) for p in self.allow.get(method, []))

    def test_only_get_and_post(self):
        self.assertEqual(set(self.allow), {"GET", "POST"})

    def test_autoheal_calls_pass(self):
        cid = "3f1c" * 16
        for method, path in (
            ("GET", "/containers/json"),
            ("GET", "/v1.43/containers/json"),
            ("POST", f"/containers/{cid}/restart"),
            ("POST", "/v1.47/containers/onionpress-wordpress/restart"),
        ):
            with self.subTest(method=method, path=path):
                self.assertTrue(self._allowed(method, path))

    def test_everything_else_is_refused(self):
        cid = "3f1c" * 16
        for method, path in (
            ("GET", f"/containers/{cid}/json"),       # inspect: env, secrets
            ("GET", f"/containers/{cid}/archive"),    # read files
            ("GET", f"/containers/{cid}/export"),
            ("GET", f"/containers/{cid}/logs"),
            ("GET", "/images/json"),
            ("GET", "/info"),
            ("POST", "/containers/create"),
            ("POST", f"/containers/{cid}/exec"),
            ("POST", f"/containers/{cid}/start"),
            ("POST", f"/containers/{cid}/kill"),
            ("POST", f"/containers/{cid}/update"),
            ("POST", "/exec/abc/start"),
            ("POST", "/images/create"),
            ("POST", "/containers/../restart"),
            ("POST", f"/containers/{cid}/restart/extra"),
            ("DELETE", f"/containers/{cid}"),
            ("PUT", f"/containers/{cid}/archive"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(self._allowed(method, path))


if __name__ == "__main__":
    unittest.main()
