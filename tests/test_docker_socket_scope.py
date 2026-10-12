#!/usr/bin/env python3
"""Which containers get the Docker socket, and what the others do without it.

The socket is root on the Docker host: the Colima VM on macOS, the user's
own daemon under rootless Docker on Linux. It used to be mounted into
onionpress-tor, onionheaven and autoheal. Now:

  * onionpress-tor (onion services + the OnionHeaven API, open to anyone on
    Tor) has no socket. Its OnionHeaven code records what needs Docker in
    the registry: an /offline as takeover_requested, which the heartbeat
    acts on; a release as release_pending, which the worker holding the
    address acts on.
  * autoheal talks to autoheal-proxy, which passes only the two calls
    autoheal makes, over a unix socket on a volume, with no network.
  * onionheaven (the heartbeat, which creates and drives the takeover
    workers) keeps it. docs/DOCKER-SOCKET.md has the plan for that one.

No PyYAML (CI installs nothing beyond CPython), so the compose files are
read with a small line-based splitter. No Docker either: every subprocess
call is patched to fail the test.
"""

import importlib.util
import os
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)

import onionheaven_common as oc  # noqa: E402

COMPOSE = "app/Resources/docker/docker-compose.yml"
OTHER_COMPOSE_FILES = (
    "app/Resources/docker/docker-compose.cloudflare.yml",
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

    def test_only_onionheaven_and_the_proxy_mount_the_socket(self):
        services = _services(_read(COMPOSE))
        self.assertIn("tor", services, "compose splitter found no tor service")
        with_socket = {name for name, block in services.items()
                       if _mounts_docker_socket(block)}
        self.assertEqual(
            with_socket, {"onionheaven", "autoheal-proxy"},
            "The Docker socket is root on the Docker host. Only the "
            "OnionHeaven heartbeat (onionheaven) and autoheal's filtering "
            "proxy may mount it — see docs/DOCKER-SOCKET.md before adding "
            "another.",
        )

    def test_tor_service_has_no_socket(self):
        tor = _services(_read(COMPOSE))["tor"]
        self.assertNotIn(
            "docker.sock", tor,
            "onionpress-tor serves Tor traffic and must not get the Docker "
            "socket; its OnionHeaven code hands Docker work to the "
            "onionheaven service through the registry.",
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


# ---------------------------------------------------------------------------
# onionheaven_common without the socket (web-server.py in onionpress-tor)
# ---------------------------------------------------------------------------

CA = "op2w5bhnczf4ryaxldryql7guydj2r52xhbj6o6kjscji5pruzwqyxad.onion"
HA = "vipwsi6bjeychxvdo3xjvojr2yvw7ggwagekemekmeccscryuhehp2id.onion"
HA_OTHER = "op2hecfkpduw2iou5aiul3tbg57tnkxqgwgq4uht4vyedanvbcv2wwqd.onion"
WORKER = "onionheaven-takeover-0"
WORKER_OTHER = "onionheaven-takeover-1"


def _ts(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


class _RegistryTest(unittest.TestCase):
    """In-memory registry; no Docker, no subprocess, no env-driven modes."""

    have_docker = False

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        oc.db_ensure_schema(self.conn)
        self.logs = []
        patches = [
            mock.patch.object(oc, "log", self.logs.append),
            mock.patch.object(oc, "docker_available", lambda: self.have_docker),
            mock.patch.object(
                oc.subprocess, "run",
                side_effect=AssertionError("no subprocess — these tests never touch Docker")),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("ONIONHEAVEN", None)
        os.environ.pop("TAKEOVER_WORKER", None)
        self.addCleanup(self.conn.close)

    def _add_worker(self, name=WORKER, assigned=0, bootstrapped=1):
        self.conn.execute(
            "INSERT INTO takeover_containers (container_name, bootstrapped, "
            "assigned_count, created_at) VALUES (?, ?, ?, ?)",
            (name, bootstrapped, assigned, _ts()))
        self.conn.commit()

    def _seed(self, ha=HA, status="online", last_healthy_ago=30, **cols):
        values = {"content_address": CA, "healthcheck_address": ha,
                  "registered_at": _ts(3600), "status": status,
                  "last_healthy": _ts(last_healthy_ago), **cols}
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.conn.execute(f"INSERT INTO registry ({names}) VALUES ({marks})",
                          tuple(values.values()))
        self.conn.commit()

    def _row(self, ha=HA):
        return dict(self.conn.execute(
            "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
            (CA, ha)).fetchone())

    def _assigned(self, name=WORKER):
        return self.conn.execute(
            "SELECT assigned_count FROM takeover_containers WHERE container_name = ?",
            (name,)).fetchone()[0]


class TestOfflineWithoutDocker(_RegistryTest):
    """/offline → takeover_function(force=True) in onionpress-tor."""

    def test_not_farm_mode_without_socket(self):
        self.assertFalse(oc.is_farm_mode())

    def test_records_a_request_and_takes_nothing_over(self):
        self._add_worker()
        self._seed()
        oc.takeover_function(self.conn, CA, HA, force=True)
        row = self._row()
        self.assertEqual(row["status"], "online")
        self.assertIsNone(row["last_taken_over"])
        self.assertIsNotNone(row["takeover_requested"])
        self.assertTrue(oc.takeover_requested_now(row))
        self.assertEqual(self._assigned(), 0)

    def test_no_request_for_a_row_already_taken_over(self):
        self._seed(status="taken-over", takeover_container=WORKER)
        oc.takeover_function(self.conn, CA, HA, force=True)
        self.assertIsNone(self._row()["takeover_requested"])

    def test_guards_still_apply(self):
        self._seed()
        self._seed(ha=HA_OTHER)  # a second, live row for the same address
        oc.takeover_function(self.conn, CA, HA, force=True)
        self.assertIsNone(self._row()["takeover_requested"])


class TestTakeoverRequestedNow(unittest.TestCase):

    def test_cases(self):
        t0, t1 = "2026-10-12T10:00:00Z", "2026-10-12T10:00:05Z"
        cases = (
            ({}, False),
            ({"takeover_requested": None, "last_healthy": t0}, False),
            ({"takeover_requested": t1, "last_healthy": t0}, True),
            ({"takeover_requested": t1, "last_healthy": None}, True),
            # /online after the /offline: the publisher is back
            ({"takeover_requested": t0, "last_healthy": t1}, False),
            # same second: never take over a site that may be up
            ({"takeover_requested": t1, "last_healthy": t1}, False),
        )
        for entry, expected in cases:
            with self.subTest(entry=entry):
                self.assertIs(oc.takeover_requested_now(entry), expected)


class TestHeartbeatActsOnRequest(_RegistryTest):
    """What the heartbeat's online branch does with a recorded /offline:
    takeover_function(force=True), then clear_takeover_request."""

    have_docker = True

    def setUp(self):
        super().setUp()
        self.exec_result = True
        self.exec_calls = []
        for p in (
            mock.patch.object(oc, "is_farm_mode", lambda: True),
            mock.patch.object(oc, "_ensure_capacity", lambda conn: None),
            mock.patch.object(oc, "_exec_takeover", self._fake_exec_takeover),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _fake_exec_takeover(self, worker, addr):
        self.exec_calls.append((worker, addr))
        return self.exec_result

    def _heartbeat_branch(self):
        entry = self._row()
        if entry["status"] == "online" and oc.takeover_requested_now(entry):
            oc.takeover_function(self.conn, CA, HA, force=True)
            oc.clear_takeover_request(self.conn, CA, HA, entry["takeover_requested"])
            return True
        return False

    def test_request_is_taken_over_and_cleared(self):
        self._add_worker()
        self._seed(last_healthy_ago=30, takeover_requested=_ts(5),
                   release_pending=_ts(20), takeover_container=WORKER)
        self.assertTrue(self._heartbeat_branch())
        row = self._row()
        self.assertEqual(row["status"], "taken-over")
        self.assertEqual(row["takeover_container"], WORKER)
        self.assertIsNone(row["takeover_requested"])
        # A release handed to this worker moments ago must not now DEL_ONION
        # the takeover just queued on it.
        self.assertIsNone(row["release_pending"])
        self.assertEqual(self._assigned(), 1)

    def test_one_attempt_per_request(self):
        """As when web-server.py ran the takeover itself: if the worker
        refuses, the staleness check takes over later."""
        self._add_worker()
        self.exec_result = False
        self._seed(last_healthy_ago=30, takeover_requested=_ts(5))
        self.assertTrue(self._heartbeat_branch())
        row = self._row()
        self.assertEqual(row["status"], "online")
        self.assertIsNone(row["takeover_requested"])

    def test_newer_request_survives_the_clear(self):
        self._seed(takeover_requested=_ts(0))
        oc.clear_takeover_request(self.conn, CA, HA, _ts(60))
        self.assertIsNotNone(self._row()["takeover_requested"])

    def test_publisher_back_since_request(self):
        self._add_worker()
        self._seed(last_healthy_ago=0, takeover_requested=_ts(10))
        self.assertFalse(self._heartbeat_branch())
        self.assertEqual(self._row()["status"], "online")
        self.assertEqual(self.exec_calls, [])


class TestReleaseWithoutDocker(_RegistryTest):
    """/online and /unregister → release_function in onionpress-tor."""

    def test_handed_to_the_worker(self):
        self._add_worker(assigned=1)
        self._add_worker(WORKER_OTHER)
        self._seed(status="taken-over", takeover_container=WORKER)
        oc.release_function(self.conn, CA, HA)
        row = self._row()
        self.assertEqual(row["status"], "online")
        self.assertIsNotNone(row["release_pending"])
        self.assertEqual(row["takeover_container"], WORKER)
        # Not counted down here; fix_assigned_count_drift settles it.
        self.assertEqual(self._assigned(), 1)

    def test_no_worker_no_handoff_no_sweep(self):
        self._add_worker()
        self._seed(status="taken-over")
        oc.release_function(self.conn, CA, HA)
        row = self._row()
        self.assertEqual(row["status"], "online")
        self.assertIsNone(row["release_pending"])

    def test_unregister_hands_off_too(self):
        self._add_worker(assigned=1)
        self._seed(status="taken-over", takeover_container=WORKER)
        oc.unregister_entry(self.conn, CA, HA, reason="explicit-unregister")
        row = self._row()
        self.assertEqual(row["status"], "unregistered")
        self.assertIsNotNone(row["release_pending"])


class TestReleaseWithDocker(_RegistryTest):
    """The onionheaven container keeps the synchronous path."""

    have_docker = True

    def test_exec_release_decrement_and_sweep(self):
        self._add_worker(assigned=1)
        self._add_worker(WORKER_OTHER)
        self._seed(status="taken-over", takeover_container=WORKER)
        calls = []
        with mock.patch.object(oc, "_exec_release",
                               lambda w, a: calls.append(w) or True):
            oc.release_function(self.conn, CA, HA)
        self.assertEqual(calls, [WORKER, WORKER_OTHER])
        self.assertIsNone(self._row()["release_pending"])
        self.assertEqual(self._assigned(), 0)


class TestWorkerPicksUpTheHandoff(_RegistryTest):
    """takeover-worker.py's process_releases is what runs the DEL_ONION."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "onionheaven_takeover_worker_under_test",
            os.path.join(DOCKER_TOR_DIR, "onionheaven-takeover-worker.py"))
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)
        self.released = []
        self.worker.CONTAINER_NAME = WORKER
        self.worker.log = self.logs.append
        self.worker._release_local = lambda addr: self.released.append(addr) or True

    def test_release_pending_is_executed_and_cleared(self):
        self._add_worker(assigned=1)
        self._seed(status="taken-over", takeover_container=WORKER)
        oc.release_function(self.conn, CA, HA)
        self.assertEqual(self.worker.process_releases(self.conn), 1)
        self.assertEqual(self.released, [CA])
        self.assertIsNone(self._row()["release_pending"])


class TestAssignedCountDrift(_RegistryTest):

    def _workers(self):
        return self.conn.execute(
            "SELECT container_name, assigned_count FROM takeover_containers").fetchall()

    def test_corrected_to_the_registry(self):
        self._add_worker(assigned=3)
        self._seed(status="taken-over", takeover_container=WORKER)
        oc.fix_assigned_count_drift(self.conn, self._workers())
        self.assertEqual(self._assigned(), 1)

    def test_concurrent_change_wins(self):
        self._add_worker(assigned=3)
        stale = self._workers()
        self.conn.execute("UPDATE takeover_containers SET assigned_count = 2")
        self.conn.commit()
        oc.fix_assigned_count_drift(self.conn, stale)
        self.assertEqual(self._assigned(), 2)


class TestStatusHelpersWithoutDocker(_RegistryTest):
    """/status and /status/<addr> are public; no failing `docker` per hit."""

    def test_queue_status(self):
        self.assertIsNone(oc.get_queue_status(WORKER))

    def test_serving_status(self):
        self._seed(status="taken-over", takeover_container=WORKER)
        self.assertEqual(oc.get_addr_serving_status(self.conn, CA), "activating")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class TestWiring(unittest.TestCase):

    def test_heartbeat_acts_on_requests_and_fixes_drift(self):
        text = _read("app/Resources/docker/tor/onionheaven-heartbeat.py")
        self.assertRegex(
            text,
            r'if entry\["status"\] == "online" and takeover_requested_now\(entry\):'
            r'(?:\n.*){1,8}?\n\s+takeover_function\(conn, ca, ha, force=True\)'
            r'\n\s+clear_takeover_request\(conn, ca, ha, entry\["takeover_requested"\]\)',
        )
        self.assertIn("fix_assigned_count_drift(conn, workers)", text)

    def test_linux_reset_runs_in_the_onionheaven_container(self):
        text = _read("linux/onionpress")
        self.assertNotIn("8083/reset-onionheaven", text,
                         "onionpress-tor answers 503 to /reset-onionheaven now.")
        self.assertRegex(text, r"reset_onionheaven_in_container\(\) \{\n"
                               r"\s+docker exec onionheaven python3 -c")
        self.assertEqual(text.count("reset_onionheaven_in_container"), 3,
                         "defined once, used by `reset` and `reset-onionheaven`")


if __name__ == "__main__":
    unittest.main()
