#!/usr/bin/env python3
"""The OnionHeaven farm controller: the Docker socket stays out of onionheaven.

The heartbeat in the onionheaven container used to mount the Docker socket
to create and drive the takeover workers. The socket is root on the Docker
host (the Colima VM on macOS, the user's own account under rootless Docker
on Linux), and the heartbeat parses output from workers that face Tor, so a
compromised heartbeat could `docker run -v $HOME:/x`. A path-allowlisting
proxy (the one in front of autoheal) would not help: `POST
/containers/create` with any `Binds`, or `POST /containers/<id>/exec` with
any `Cmd`, is a path it would have to allow.

Now onionheaven-farm.py runs in its own container with the socket, no
network and every capability dropped, and knows six verbs with validated
arguments; the heartbeat reaches it over a unix socket on a volume only
the two containers mount (onionheaven_common.farm_request / farm_exec).
These tests pin down:

  * what the controller accepts (names, verbs, addresses; nothing else)
    and the fixed docker argv each verb runs — nothing in a request reaches
    docker except a validated name, verb or address;
  * the protocol, end to end over a real unix socket with docker faked;
  * that no other container mounts the raw socket, and that nothing in the
    tor image but the controller calls docker;
  * the compose, Dockerfile and entrypoint wiring.

No Docker: subprocess is faked everywhere. No PyYAML: the compose file is
read with the same line-based splitter as test_docker_socket_scope.py.
"""

import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
DOCKER_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker")
DOCKER_TOR_DIR = os.path.join(DOCKER_DIR, "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import onionheaven_common as oc  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "onionheaven_farm", os.path.join(DOCKER_TOR_DIR, "onionheaven-farm.py"))
farm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(farm)

COMPOSE = "app/Resources/docker/docker-compose.yml"
CLOUDFLARE_COMPOSE = "app/Resources/docker/docker-compose.cloudflare.yml"
FARM_VOLUME = "onionpress-onionheaven-farm"
FARM_MOUNT = "onionheaven-farm:/run/onionheaven-farm"

WORKER = "onionheaven-takeover-3"
POLL_WORKER = "onionheaven-poll-2"
ADDR = "op2ijk3cvd7kswainvwlg7uqxuoghaxzns6quht2csz3cdp5sgr2lnqd.onion"
IMAGE = "ghcr.io/example/onionpress-tor:latest@sha256:" + "c" * 64


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


def _mounts_docker_socket(block):
    return re.search(r"^\s*-\s*\S+:/var/run/docker\.sock(:ro)?\s*$", block, re.M) is not None


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class _FakeDocker:
    """Stands in for farm.docker: records every argv, answers by verb."""

    def __init__(self, names=(), inspect=None, run_rc=0):
        self.calls = []
        self.names = list(names)
        self.inspect = inspect if inspect is not None else _done("true []")
        self.run_rc = run_rc

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        verb = argv[0]
        if verb == "ps":
            return _done("\n".join(self.names) + "\n")
        if verb == "inspect":
            return self.inspect
        if verb == "run":
            return _done("cid\n", self.run_rc, "boom" if self.run_rc else "")
        if verb == "exec":
            return _done(json.dumps({"ran": argv[1:]}) + "\n")
        return _done()

    def verbs(self):
        return [argv[0] for argv, _ in self.calls]


class _FarmTest(unittest.TestCase):

    def setUp(self):
        self.logs = []
        self.docker = _FakeDocker()
        patches = [
            mock.patch.object(farm, "log", self.logs.append),
            mock.patch.object(farm, "docker", self.docker),
            mock.patch.object(farm.subprocess, "run",
                              side_effect=AssertionError("docker is faked here")),
            mock.patch.dict(os.environ, {"ONIONHEAVEN_IMAGE": IMAGE}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        farm._own_image = None
        self.addCleanup(setattr, farm, "_own_image", None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation(_FarmTest):

    def assertRefused(self, req, *words):
        reply = farm.handle(req)
        self.assertFalse(reply["ok"], reply)
        for word in words:
            self.assertIn(word, reply["error"])
        self.assertEqual(self.docker.calls, [], "a refused request reached docker")
        self.assertTrue(any("refused" in line for line in self.logs), self.logs)

    def test_only_objects_with_a_known_op(self):
        for req in ([], "spawn", 3, None, {}, {"op": "run"}, {"op": 1},
                    {"op": "create"}, {"op": "spawn "}, {"op": "SPAWN"}):
            with self.subTest(req=req):
                self.assertRefused(req)
                self.docker.calls.clear()
                self.logs.clear()

    def test_no_unexpected_fields(self):
        self.assertRefused({"op": "spawn", "name": WORKER, "image": "x"}, "unexpected field")
        self.docker.calls.clear()
        self.assertRefused({"op": "list", "all": True}, "unexpected field")
        self.docker.calls.clear()
        self.assertRefused({"op": "exec", "name": WORKER, "verb": "status",
                            "cmd": "sh"}, "unexpected field")

    def test_required_fields_and_types(self):
        self.assertRefused({"op": "spawn"}, "name is required")
        self.docker.calls.clear()
        self.assertRefused({"op": "spawn", "name": 7}, "not a string")
        self.docker.calls.clear()
        self.assertRefused({"op": "exec", "name": WORKER}, "verb is required")
        self.docker.calls.clear()
        self.assertRefused({"op": "exec", "name": WORKER, "verb": ["status"]}, "not a string")

    def test_names_are_workers_only(self):
        bad = ["onionpress-tor", "onionheaven", "onionheaven-farm", "wordpress",
               "onionheaven-takeover-", "onionheaven-takeover-x",
               "onionheaven-takeover-1234567890", " onionheaven-takeover-1",
               "onionheaven-takeover-1\n", "onionheaven-takeover-1;rm",
               "../onionheaven-takeover-1", "onionheaven-takeover-1 --privileged", ""]
        for name in bad:
            for op in ("spawn", "remove", "inspect"):
                with self.subTest(op=op, name=name):
                    self.assertRefused({"op": op, "name": name}, "bad name")
                    self.docker.calls.clear()
                    self.logs.clear()
            with self.subTest(op="exec", name=name):
                self.assertRefused({"op": "exec", "name": name, "verb": "status"}, "bad name")
                self.docker.calls.clear()
                self.logs.clear()

    def test_poll_workers_may_be_removed_and_inspected_but_not_spawned_or_entered(self):
        self.assertRefused({"op": "spawn", "name": POLL_WORKER}, "bad name")
        self.docker.calls.clear()
        self.logs.clear()
        self.assertRefused({"op": "exec", "name": POLL_WORKER, "verb": "status"}, "bad name")
        self.docker.calls.clear()
        self.assertTrue(farm.handle({"op": "remove", "name": POLL_WORKER})["ok"])
        self.assertTrue(farm.handle({"op": "inspect", "name": POLL_WORKER})["ok"])

    def test_verbs_are_the_table(self):
        for verb in ("sh", "python3", "takeover ", "Status", "", "exec", "ps aux"):
            with self.subTest(verb=verb):
                self.assertRefused({"op": "exec", "name": WORKER, "verb": verb}, "unknown verb")
                self.docker.calls.clear()
                self.logs.clear()

    def test_addresses_are_v3_onions(self):
        bad = [ADDR.upper(), ADDR[:-6], ADDR + "/", "a" * 56 + ".onion\n",
               ADDR + " --privileged", "../" + ADDR, "", "0" * 56 + ".onion"]
        for address in bad:
            with self.subTest(address=address):
                self.assertRefused({"op": "exec", "name": WORKER, "verb": "takeover",
                                    "address": address}, "bad address")
                self.docker.calls.clear()
                self.logs.clear()

    def test_address_required_or_forbidden_per_verb(self):
        for verb, (_, takes_address, _) in farm.EXEC_VERBS.items():
            with self.subTest(verb=verb):
                if takes_address:
                    self.assertRefused({"op": "exec", "name": WORKER, "verb": verb},
                                       "needs an address")
                else:
                    self.assertRefused({"op": "exec", "name": WORKER, "verb": verb,
                                        "address": ADDR}, "takes no address")
                self.docker.calls.clear()
                self.logs.clear()


# ---------------------------------------------------------------------------
# What each op runs
# ---------------------------------------------------------------------------

class TestOps(_FarmTest):

    def test_spawn_runs_the_fixed_argv(self):
        reply = farm.handle({"op": "spawn", "name": WORKER})
        self.assertEqual(reply, {"ok": True, "name": WORKER})
        self.assertEqual(self.docker.verbs(), ["ps", "rm", "run"])
        rm, run = self.docker.calls[1][0], self.docker.calls[2][0]
        self.assertEqual(rm, ["rm", "-f", WORKER])
        self.assertEqual(run, farm.worker_run_argv(WORKER, IMAGE, os.environ.get("TZ", "UTC")))
        self.assertEqual(run[-1], IMAGE)
        self.assertIn(("--cap-drop", "ALL"), set(zip(run, run[1:])))
        self.assertEqual(sorted(v for f, v in zip(run, run[1:]) if f == "--cap-add"),
                         ["DAC_OVERRIDE", "SETGID", "SETUID"])
        self.assertNotIn("--privileged", run)
        self.assertEqual([run[i + 1] for i, a in enumerate(run) if a == "-v"],
                         [f"{farm.HUB_DATA_VOLUME}:{farm.ONIONHEAVEN_DATA_DIR}"])
        self.assertIn("--network", run)
        self.assertEqual(run[run.index("--network") + 1], "onionpress-network")

    def test_spawn_shape_takes_nothing_from_the_environment_but_tz_and_cap(self):
        """A compromised heartbeat cannot reach the controller's
        environment, but a check that the argv has no hole for it."""
        argv = farm.worker_run_argv(WORKER, IMAGE, "Europe/Amsterdam")
        text = " ".join(argv)
        self.assertIn("TZ=Europe/Amsterdam", text)
        self.assertIn(f"MAX_TAKEOVER_SERVICES={farm.MAX_SERVICES_PER_WORKER}", text)
        self.assertEqual(argv.count("-v"), 1)
        self.assertNotIn("--mount", argv)
        self.assertNotIn("--pid", argv)
        self.assertNotIn("--userns", argv)
        self.assertNotIn("--device", argv)
        self.assertNotIn("--privileged", argv)

    def test_spawn_without_an_image(self):
        os.environ.pop("ONIONHEAVEN_IMAGE")
        self.docker.inspect = _done("", 1)
        reply = farm.handle({"op": "spawn", "name": WORKER})
        self.assertFalse(reply["ok"])
        self.assertNotIn("run", self.docker.verbs())

    def test_spawn_is_capped(self):
        self.docker.names = [f"onionheaven-takeover-{i}" for i in range(farm.MAX_WORKERS)]
        reply = farm.handle({"op": "spawn", "name": f"onionheaven-takeover-{farm.MAX_WORKERS}"})
        self.assertFalse(reply["ok"])
        self.assertIn("limit", reply["error"])
        self.assertNotIn("run", self.docker.verbs())
        # Replacing a worker that already exists is not another worker.
        self.docker.calls.clear()
        reply = farm.handle({"op": "spawn", "name": "onionheaven-takeover-0"})
        self.assertTrue(reply["ok"], reply)
        self.assertIn("run", self.docker.verbs())

    def test_spawn_reports_docker_failure(self):
        self.docker.run_rc = 125
        reply = farm.handle({"op": "spawn", "name": WORKER})
        self.assertFalse(reply["ok"])
        self.assertIn("docker run failed", reply["error"])

    def test_remove(self):
        reply = farm.handle({"op": "remove", "name": WORKER})
        self.assertEqual(reply["ok"], True)
        self.assertTrue(reply["removed"])
        self.assertEqual(self.docker.calls[0][0], ["rm", "-f", WORKER])

    def test_list_filters_to_workers(self):
        self.docker.names = ["onionpress-tor", "onionheaven-takeover-1", "onionheaven",
                             "onionheaven-poll-0", "onionheaven-takeover-x", "wordpress"]
        reply = farm.handle({"op": "list"})
        self.assertEqual(reply, {"ok": True,
                                 "names": ["onionheaven-poll-0", "onionheaven-takeover-1"]})
        self.assertEqual(self.docker.calls[0][0], ["ps", "-a", "--format", "{{.Names}}"])

    def test_inspect(self):
        mounts = json.dumps([{"Name": farm.HUB_DATA_VOLUME,
                              "Destination": farm.ONIONHEAVEN_DATA_DIR}])
        self.docker.inspect = _done("true " + mounts + "\n")
        reply = farm.handle({"op": "inspect", "name": WORKER})
        self.assertEqual(reply, {"ok": True, "exists": True, "running": True,
                                 "mounts": mounts})
        self.assertEqual(self.docker.calls[0][0][:3], ["inspect", "--format",
                                                       "{{.State.Running}} {{json .Mounts}}"])
        self.assertEqual(self.docker.calls[0][0][-1], WORKER)
        self.docker.inspect = _done("false " + mounts + "\n")
        self.assertFalse(farm.handle({"op": "inspect", "name": WORKER})["running"])
        self.docker.inspect = _done("", 1)
        self.assertEqual(farm.handle({"op": "inspect", "name": WORKER}),
                         {"ok": True, "exists": False})

    def test_exec_verbs_run_their_argv_and_nothing_else(self):
        for verb, (inner, takes_address, timeout) in farm.EXEC_VERBS.items():
            with self.subTest(verb=verb):
                self.docker.calls.clear()
                req = {"op": "exec", "name": WORKER, "verb": verb}
                if takes_address:
                    req["address"] = ADDR
                reply = farm.handle(req)
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["returncode"], 0)
                self.assertEqual(len(self.docker.calls), 1)
                argv, used_timeout = self.docker.calls[0]
                self.assertEqual(argv, ["exec", WORKER] + inner + ([ADDR] if takes_address else []))
                self.assertEqual(used_timeout, timeout)
                self.assertEqual(json.loads(reply["stdout"])["ran"], argv[1:])

    def test_queue_manager_verbs_name_its_commands(self):
        qm = _read("app/Resources/docker/tor/onionheaven-queue-manager.py")
        for verb in ("takeover", "release", "has", "addr_state", "status"):
            inner = farm.EXEC_VERBS[verb][0]
            self.assertEqual(inner, ["python3", "/onionheaven-queue-manager.py", verb])
            self.assertIn(f'"{verb}"', qm, f"queue manager has no {verb} command")
        self.assertIn("nionheaven-takeover-worker", farm.EXEC_VERBS["alive"][0][-1])
        self.assertIn("onions/detached", farm.EXEC_VERBS["onions-detached"][0][-1])
        self.assertIn("status/bootstrap-phase", farm.EXEC_VERBS["bootstrap-phase"][0][-1])

    def test_exec_output_is_capped(self):
        big = _done("x" * (farm.MAX_OUTPUT + 100), 0, "y" * (farm.MAX_OUTPUT + 100))
        with mock.patch.object(farm, "docker", lambda argv, timeout: big):
            reply = farm.handle({"op": "exec", "name": WORKER, "verb": "status"})
        self.assertEqual(len(reply["stdout"]), farm.MAX_OUTPUT)
        self.assertEqual(len(reply["stderr"]), farm.MAX_OUTPUT)

    def test_exec_timeout_is_an_error_not_output(self):
        timed_out = subprocess.CompletedProcess([], None, "", "timeout")
        with mock.patch.object(farm, "docker", lambda argv, timeout: timed_out):
            reply = farm.handle({"op": "exec", "name": WORKER, "verb": "status"})
        self.assertFalse(reply["ok"])

    def test_tor_address(self):
        hub = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
        with mock.patch.object(farm, "docker", lambda argv, timeout: _done(hub + "\n")):
            reply = farm.handle({"op": "tor-address"})
        self.assertEqual(reply, {"ok": True, "address": hub})
        for out in (_done("not an address\n"), _done(hub, 1), _done(hub + "\nextra\n")):
            with mock.patch.object(farm, "docker", lambda argv, timeout: out):
                self.assertIsNone(farm.handle({"op": "tor-address"})["address"])
        reply = farm.handle({"op": "tor-address"})  # the fake returns JSON
        self.assertEqual(self.docker.calls[-1][0],
                         ["exec", farm.TOR_CONTAINER, "cat", farm.OWN_HOSTNAME_PATH])

    def test_every_op_is_logged_without_output(self):
        farm.handle({"op": "exec", "name": WORKER, "verb": "status"})
        self.assertTrue(any(line.startswith("exec onionheaven-takeover-3 status -> ok")
                            for line in self.logs), self.logs)
        self.assertFalse(any("ran" in line for line in self.logs), self.logs)

    def test_a_crashing_handler_does_not_kill_the_server(self):
        def boom(req):
            raise RuntimeError("x")
        with mock.patch.dict(farm.HANDLERS, {"list": boom}):
            a, b = socket.socketpair()
            try:
                threading.Thread(target=farm.serve_connection,
                                 args=(b, threading.BoundedSemaphore(1))).start()
                a.sendall(b'{"op": "list"}\n')
                reply = json.loads(a.recv(4096))
            finally:
                a.close()
        self.assertEqual(reply, {"ok": False, "error": "internal error"})


# ---------------------------------------------------------------------------
# The protocol, end to end
# ---------------------------------------------------------------------------

class TestProtocol(_FarmTest):
    """A real unix socket; onionheaven_common's client against farm's server."""

    def setUp(self):
        super().setUp()
        base = tempfile.gettempdir()
        if len(base) > 60:  # unix socket paths are limited to ~104 bytes
            base = "/tmp"
        self.tmp = tempfile.mkdtemp(prefix="ohfarm", dir=base)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sock_dir = os.path.join(self.tmp, "run")
        sock_path = os.path.join(sock_dir, "farm.sock")
        for p in (mock.patch.object(farm, "SOCKET_DIR", sock_dir),
                  mock.patch.object(farm, "SOCKET_PATH", sock_path),
                  mock.patch.object(oc, "FARM_SOCKET", sock_path)):
            p.start()
            self.addCleanup(p.stop)
        self.server = farm.listen()
        self.server.settimeout(0.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _serve(self):
        slots = threading.BoundedSemaphore(farm.MAX_CONCURRENT)
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=farm.serve_connection, args=(conn, slots),
                             daemon=True).start()

    def _shutdown(self):
        self.stop.set()
        self.thread.join(2)
        self.server.close()

    def _raw(self, data, eof=False):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(farm.SOCKET_PATH)
            s.sendall(data)
            if eof:
                s.shutdown(socket.SHUT_WR)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        return out

    def test_socket_is_private_and_seen_by_the_client(self):
        mode = stat.S_IMODE(os.stat(farm.SOCKET_PATH).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(oc.farm_available())

    def test_round_trips(self):
        self.docker.names = ["onionheaven-takeover-0", "wordpress"]
        self.assertEqual(oc.farm_request("list"),
                         {"ok": True, "names": ["onionheaven-takeover-0"]})
        self.assertEqual(oc.farm_list(), ["onionheaven-takeover-0"])
        result = oc.farm_exec(WORKER, "takeover", ADDR)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["ran"],
                         [WORKER, "python3", "/onionheaven-queue-manager.py", "takeover", ADDR])
        self.assertEqual(oc.farm_request("spawn", name=WORKER), {"ok": True, "name": WORKER})
        self.assertTrue(oc.farm_remove(WORKER))
        self.assertEqual(oc.farm_inspect(WORKER),
                         {"exists": True, "running": True, "mounts": "[]"})
        self.assertEqual(oc.read_tor_container_address(), None)  # fake prints JSON

    def test_refusals_reach_the_client_as_errors(self):
        with self.assertRaises(oc.FarmError):
            oc.farm_exec("onionpress-tor", "status")
        with self.assertRaises(oc.FarmError):
            oc.farm_exec(WORKER, "sh")
        reply = oc.farm_request("spawn", name="wordpress")
        self.assertFalse(reply["ok"])
        self.assertIsNone(oc.farm_inspect("wordpress"))
        self.assertFalse(oc.farm_remove("wordpress"))
        self.assertEqual(self.docker.calls, [])

    def test_unreadable_requests(self):
        for data in (b"not json\n", b"[1, 2]\n", b'"spawn"\n', b"\xff\xfe\n",
                     b"{" + b" " * farm.MAX_REQUEST + b"}\n"):
            with self.subTest(data=data[:20]):
                reply = json.loads(self._raw(data))
                self.assertFalse(reply["ok"])
        self.assertEqual(self.docker.calls, [])
        # Without a newline the request still ends at EOF.
        self.assertEqual(json.loads(self._raw(b'{"op": "list"}', eof=True)),
                         {"ok": True, "names": []})

    def test_a_silent_client_does_not_hold_a_slot_forever(self):
        with mock.patch.object(farm, "REQUEST_TIMEOUT", 0.2):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(farm.SOCKET_PATH)
                s.sendall(b'{"op": "li')
                reply = s.recv(4096)
        self.assertFalse(json.loads(reply)["ok"])

    def test_concurrent_requests(self):
        results = []

        def one():
            results.append(oc.farm_request("list"))
        threads = [threading.Thread(target=one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [{"ok": True, "names": []}] * 20)

    def test_no_controller(self):
        self._shutdown()
        os.unlink(farm.SOCKET_PATH)
        self.assertFalse(oc.farm_available())
        with self.assertRaises(oc.FarmError):
            oc.farm_request("list")
        self.assertIsNone(oc.farm_list())
        self.assertIsNone(oc.farm_inspect(WORKER))
        self.assertFalse(oc.farm_remove(WORKER))
        self.assertIsNone(oc.read_tor_container_address())


class TestClient(unittest.TestCase):

    def test_malformed_replies_are_errors(self):
        for reply in ("[]", "3", '{"names": []}', '{"ok": "yes"}', "", "{"):
            with self.subTest(reply=reply):
                fake = mock.Mock()
                fake.__enter__ = lambda s: s
                fake.__exit__ = lambda s, *a: None
                fake.recv = mock.Mock(side_effect=[reply.encode(), b""])
                with mock.patch.object(oc.socket, "socket", return_value=fake):
                    with self.assertRaises(oc.FarmError):
                        oc.farm_request("list")

    def test_exec_translates_a_reply(self):
        with mock.patch.object(oc, "farm_request", return_value={
                "ok": True, "returncode": 3, "stdout": "out", "stderr": "err"}) as req:
            result = oc.farm_exec(WORKER, "has", ADDR)
        req.assert_called_once_with("exec", name=WORKER, verb="has", address=ADDR)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "out", "err"))
        with mock.patch.object(oc, "farm_request", return_value={"ok": True}) as req:
            result = oc.farm_exec(WORKER, "status")
        req.assert_called_once_with("exec", name=WORKER, verb="status")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (None, "", ""))


# ---------------------------------------------------------------------------
# Who has the socket, and what calls docker
# ---------------------------------------------------------------------------

class TestSocketCensus(unittest.TestCase):

    def test_only_proxies_mount_the_raw_socket(self):
        """A service that mounts the Docker socket must be a proxy with a
        fixed job: no network, every capability dropped, read-only mount."""
        services = _services(_read(COMPOSE))
        with_socket = {name for name, block in services.items()
                       if _mounts_docker_socket(block)}
        self.assertEqual(with_socket, {"autoheal-proxy", "onionheaven-farm"})
        for name in with_socket:
            block = services[name]
            with self.subTest(service=name):
                self.assertRegex(block, r"(?m)^    network_mode: none\s*$")
                self.assertNotRegex(block, r"(?m)^    (networks|ports):")
                self.assertEqual(_list_items(block, "cap_drop"), ["ALL"])
                self.assertIn("no-new-privileges:true", _list_items(block, "security_opt"))
                self.assertTrue(any(v.endswith("/var/run/docker.sock:ro")
                                    for v in _list_items(block, "volumes")), block)
        for name, block in services.items():
            if name not in with_socket:
                with self.subTest(service=name):
                    self.assertFalse(_mounts_docker_socket(block))
                    self.assertNotIn("/var/run/docker.sock", block)
        self.assertNotIn("docker.sock", _read(CLOUDFLARE_COMPOSE))

    def test_only_the_controller_calls_docker(self):
        """Everything shipped in the tor image, apart from the controller,
        gets by without a docker CLI call."""
        callers = set()
        for name in sorted(os.listdir(DOCKER_TOR_DIR)):
            path = os.path.join(DOCKER_TOR_DIR, name)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                code = "\n".join(line for line in f.read().splitlines()
                                 if not line.lstrip().startswith("#"))
            if name.endswith(".py"):
                calls = re.search(r"""\[\s*["']docker["']""", code)
            else:
                calls = re.search(r"(?m)^\s*(?:\S+=\S*\s+)*docker\s", code)
            if calls:
                callers.add(name)
        self.assertEqual(callers, {"onionheaven-farm.py"})

    def test_heartbeat_side_has_no_docker_left(self):
        oc_text = _read("app/Resources/docker/tor/onionheaven_common.py")
        for gone in ("_patch_worker_code", "_WORKER_CODE_FILES", "docker_available",
                     "DOCKER_SOCKET", "takeover_image", '"docker"'):
            self.assertNotIn(gone, oc_text, gone)
        self.assertNotIn("subprocess", _read("app/Resources/docker/tor/onionheaven-heartbeat.py"))
        self.assertIn("farm_available", _read("app/Resources/docker/tor/web-server.py"))
        self.assertNotIn("docker_available", _read("app/Resources/docker/tor/web-server.py"))

    def test_reset_restarts_the_heartbeat_locally(self):
        """reset_onionheaven runs inside the onionheaven container; the
        heartbeat is a local process, not something to `docker exec` into."""
        text = _read("app/Resources/docker/tor/onionheaven_common.py")
        fn = text[text.index("def reset_onionheaven("):]
        self.assertIn('["pkill", "-f", "onionheaven-heartbeat"]', fn)
        self.assertIn('subprocess.Popen(["python3", "/onionheaven-heartbeat.py"]', fn)
        code = "\n".join(l for l in fn.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn('"docker"', code)
        self.assertNotIn("docker exec", code)


# ---------------------------------------------------------------------------
# Wiring: constants, compose, Dockerfile, entrypoint
# ---------------------------------------------------------------------------

class TestWiring(unittest.TestCase):

    def test_constants_agree(self):
        self.assertEqual(farm.SOCKET_PATH, oc.FARM_SOCKET)
        self.assertEqual(farm.HUB_DATA_VOLUME, oc.HUB_DATA_VOLUME)
        self.assertEqual(farm.ONIONHEAVEN_DATA_DIR, oc.ONIONHEAVEN_DATA_DIR)
        self.assertEqual(farm.OWN_HOSTNAME_PATH, oc.OWN_HOSTNAME_PATH)
        self.assertEqual(farm.FARM_WORKER_RE.pattern, oc._FARM_WORKER_RE.pattern)
        for addr in (ADDR, ADDR.upper(), "x.onion", ADDR + "x"):
            self.assertEqual(bool(farm.ONION_ADDRESS_RE.fullmatch(addr)),
                             bool(oc._ONION_ADDRESS_RE.fullmatch(addr)), addr)
        self.assertEqual(farm.MAX_SERVICES_PER_WORKER, oc.MAX_SERVICES_PER_WORKER)
        self.assertGreater(oc.FARM_TIMEOUT, max(t for _, _, t in farm.EXEC_VERBS.values()))
        self.assertGreater(oc.FARM_TIMEOUT, 30)  # spawn's docker run
        self.assertGreater(oc.FARM_MAX_REPLY, 2 * farm.MAX_OUTPUT)

    def test_compose_farm_service(self):
        services = _services(_read(COMPOSE))
        block = services["onionheaven-farm"]
        env = _list_items(block, "environment")
        self.assertIn("FARM_CONTROLLER=1", env)
        self.assertIn("NO_ONION_SERVICE=1", env)
        self.assertRegex(block, r"(?m)^    container_name: onionheaven-farm\s*$")
        volumes = _list_items(block, "volumes")
        self.assertEqual([v for v in volumes if "docker.sock" not in v], [FARM_MOUNT])
        self.assertRegex(block, r"(?m)^    restart: unless-stopped\s*$")
        self.assertRegex(block, r"(?m)^    read_only: true\s*$")
        self.assertEqual(_list_items(block, "tmpfs"), ["/tmp"])
        self.assertNotRegex(block, r"(?m)^    user:")
        # Same image as the heartbeat's container, by the same expression.
        farm_image = re.search(r"(?m)^    image: (\S+)$", block).group(1)
        hb_image = re.search(r"(?m)^    image: (\S+)$", services["onionheaven"]).group(1)
        self.assertEqual(farm_image, hb_image)
        self.assertIn("ONIONPRESS_TOR_IMAGE", farm_image)

    def test_compose_farm_volume(self):
        text = _read(COMPOSE)
        self.assertRegex(text, rf"(?m)^  onionheaven-farm:\n    name: {FARM_VOLUME}$")
        services = _services(text)
        mounting = {name for name, block in services.items()
                    if any(v.split(":", 1)[0] == "onionheaven-farm"
                           for v in _list_items(block, "volumes"))}
        self.assertEqual(mounting, {"onionheaven-farm", "onionheaven"})
        self.assertNotIn("onionheaven-farm", _read(CLOUDFLARE_COMPOSE))

    def test_dockerfile_ships_the_controller(self):
        dockerfile = _read("app/Resources/docker/tor/Dockerfile")
        self.assertIn("COPY onionheaven-farm.py /onionheaven-farm.py", dockerfile)
        self.assertRegex(dockerfile, r"chmod \+x [^\n]*/onionheaven-farm\.py")
        self.assertIn("COPY --from=docker-cli /usr/local/bin/docker", dockerfile)

    def test_entrypoint_runs_only_the_controller(self):
        """The branch comes before anything else runs: no migration, no
        Tor, no key handling in the controller's container."""
        text = _read("app/Resources/docker/tor/entrypoint.sh")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        branch = re.search(r'if \[ "\$\{FARM_CONTROLLER\}" = "1" \]; then\n'
                           r'    exec python3 /onionheaven-farm\.py\nfi\n', code)
        self.assertIsNotNone(branch)
        before = code[:branch.start()]
        self.assertNotRegex(before, r"(?m)^(if |migrate_\w+|write_client_torrc|python3 .*|su .*|socat .*)$")
        self.assertLess(branch.start(), code.index('if [ "${TAKEOVER_WORKER}" = "1" ]'))

    def test_launchers_leave_the_controller_alone(self):
        """retire_old_onionheaven_containers removes Tor-side containers
        that lack the hub data volume; the controller has none on purpose."""
        # Both launchers' start runs ContainerManager's step (start_core, #442.6).
        for launcher in ("app/MacOS/onionpress", "linux/onionpress"):
            self.assertIn("start-core", _read(launcher))
        from onionpress import containers
        self.assertIsNone(containers._HUB_CONTAINER_RE.fullmatch("onionheaven-farm"))
        self.assertIsNotNone(containers._HUB_CONTAINER_RE.fullmatch("onionheaven"))


if __name__ == "__main__":
    unittest.main()
