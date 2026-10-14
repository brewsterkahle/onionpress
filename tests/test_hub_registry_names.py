#!/usr/bin/env python3
"""Names read from the OnionHeaven registry reach Docker only as workers'.

The heartbeat in the onionheaven container drives Docker (through the farm
controller, onionheaven-farm.py, since the socket left its container) and
takes container names from the registry: takeover_containers.container_name
and registry.takeover_container. The registry lives on the hub data volume,
which onionpress-tor (it faces Tor) and every takeover worker write as root.
A planted row naming onionpress-wordpress used to get `docker inspect` and,
since it does not mount the hub volume, `docker rm -f` from
cleanup_dead_workers; check_worker_bootstrap exec'd into whatever a row
named. Now:

  - every name that reaches a farm controller request (and so a docker
    call) must be a worker's, whole (onionheaven-(takeover|poll)-N), or it
    is logged and refused — before the controller, which refuses it too
    (tests/test_onionheaven_farm.py), is even asked;
  - drop_unnamed_worker_rows forgets such rows each heartbeat pass and
    unassigns the addresses assigned to them;
  - the onion addresses handed to a worker's queue manager must be v3
    addresses, whole (they go into control-port commands and key paths),
    and the queue manager refuses anything else itself;
  - key directories are removed only for an onion address (".." or an
    absolute path from the registry would remove another tree);
  - a census: every docker argv in the tor image's Python is listed below
    (since the farm controller, exactly one: the controller's own `docker`
    helper, whose arguments its validated verbs build), and each variable
    in it is checked in its function or reviewed here.

Nothing here talks to Docker: subprocess is mocked, and a test fails if a
refused name reaches it.
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)

import onionheaven_common as oc  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(DOCKER_TOR_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hb = _load("onionheaven_heartbeat_registry_names", "onionheaven-heartbeat.py")
qm = _load("onionheaven_queue_manager_registry_names", "onionheaven-queue-manager.py")

ADDR = "op2" + "a" * 53 + ".onion"
WORKER = "onionheaven-takeover-3"

# Names a row could carry. None of them is a worker's.
HOSTILE_NAMES = [
    "onionpress-wordpress",
    "onionpress-db",
    "onionpress-tor",
    "onionheaven",
    "--privileged",
    "-uroot",
    "onionheaven-takeover-",
    "onionheaven-takeover-x",
    "onionheaven-takeover-3\n",
    "onionheaven-takeover-3 onionpress-db",
    "onionheaven-takeover-3/../onionpress-db",
    "xonionheaven-takeover-3",
    "onionheaven-takeover-1234567890",
    "",
    None,
    7,
    b"onionheaven-takeover-3",
]

HOSTILE_ADDRESSES = [
    "",
    ".onion",
    "x.onion",
    "../../../var/lib/onionpress",
    "/var/lib/onionpress",
    ADDR + "\r\nSIGNAL HALT",
    ADDR + "\n",
    "--help",
    ADDR.upper(),
    "a" * 57 + ".onion",
    None,
]


def _no_subprocess(*args, **kwargs):
    raise AssertionError(f"no subprocess may run here: {args!r}")


def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


def _registry():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with mock.patch.object(oc, "log"):
        oc.db_ensure_schema(conn)
    return conn


def _add_worker(conn, name, bootstrapped=1, assigned=0):
    conn.execute("INSERT INTO takeover_containers (container_name, max_services, "
                 "bootstrapped, assigned_count) VALUES (?, 50, ?, ?)",
                 (name, bootstrapped, assigned))
    conn.commit()


def _add_row(conn, ca, hc, status="taken-over", container=None, version="2.5.3"):
    conn.execute("INSERT INTO registry (content_address, healthcheck_address, "
                 "registered_at, status, takeover_container, version) "
                 "VALUES (?, ?, 'now', ?, ?, ?)", (ca, hc, status, container, version))
    conn.commit()


class QuietLog(unittest.TestCase):

    def setUp(self):
        self.logged = []
        for target in (oc, hb):
            patcher = mock.patch.object(target, "log", side_effect=self.logged.append)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(oc, "addr_log", lambda a, m: self.logged.append(m))
        patcher.start()
        self.addCleanup(patcher.stop)

    def registry(self):
        conn = _registry()
        self.addCleanup(conn.close)
        return conn


class NameRuleTest(unittest.TestCase):

    def test_workers_names(self):
        for name in ("onionheaven-takeover-0", "onionheaven-takeover-17",
                     "onionheaven-poll-2", "onionheaven-takeover-999999999"):
            self.assertTrue(oc.is_worker_name(name), name)

    def test_anything_else(self):
        for name in HOSTILE_NAMES:
            self.assertFalse(oc.is_worker_name(name), repr(name))

    def test_addresses(self):
        self.assertTrue(oc.is_onion_address(ADDR))
        for address in HOSTILE_ADDRESSES:
            self.assertFalse(oc.is_onion_address(address), repr(address))


class DropUnnamedRowsTest(QuietLog):

    def test_planted_rows_go_and_their_addresses_are_unassigned(self):
        conn = self.registry()
        _add_worker(conn, WORKER, assigned=1)
        for i, name in enumerate(n for n in HOSTILE_NAMES if n is not None):
            _add_worker(conn, name)
            _add_row(conn, f"{i:02d}" + "b" * 54 + ".onion", "h" * 56 + ".onion",
                     container=name)
        _add_row(conn, ADDR, "c" * 56 + ".onion", container=WORKER)
        # A registry row may name a container takeover_containers does not.
        _add_row(conn, "d" * 56 + ".onion", "e" * 56 + ".onion",
                 container="onionpress-wordpress")

        with mock.patch.object(oc.subprocess, "run", side_effect=_no_subprocess):
            dropped = oc.drop_unnamed_worker_rows(conn)

        self.assertEqual(dropped, len([n for n in HOSTILE_NAMES if n is not None]))
        self.assertEqual([r[0] for r in conn.execute(
            "SELECT container_name FROM takeover_containers")], [WORKER])
        self.assertEqual([r[0] for r in conn.execute(
            "SELECT DISTINCT takeover_container FROM registry "
            "WHERE takeover_container IS NOT NULL")], [WORKER])
        # The unassigned rows stay taken-over: the heartbeat queues them on
        # a real worker.
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM registry WHERE status = 'taken-over' "
            "AND takeover_container IS NULL").fetchone()[0], dropped + 1)

    def test_a_null_name_is_dropped_too(self):
        conn = self.registry()
        conn.execute("INSERT INTO takeover_containers (container_name) VALUES (NULL)")
        conn.commit()
        self.assertEqual(oc.drop_unnamed_worker_rows(conn), 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM takeover_containers").fetchone()[0], 0)

    def test_the_log_line_is_one_line(self):
        conn = self.registry()
        _add_worker(conn, "onionpress-db\nOnionHeaven: all fine")
        oc.drop_unnamed_worker_rows(conn)
        self.assertTrue(self.logged)
        self.assertTrue(all("\n" not in line for line in self.logged))


class CleanupDeadWorkersTest(QuietLog):
    """The reported path: inspect + rm -f of a name a row planted."""

    def _run(self, conn):
        """Farm controller requests made, as (op, name)."""
        calls = []

        def fake(op, **fields):
            calls.append((op, fields.get("name")))
            if op == "list":
                return {"ok": True, "names": [WORKER]}
            if op == "inspect":
                # Nothing but the worker mounts the hub volume: a planted
                # name would be "running without it" and removed.
                return {"ok": True, "exists": True, "running": True, "mounts": "[]"}
            if op == "remove":
                return {"ok": True, "removed": True}
            raise AssertionError(f"unexpected {op} {fields}")
        with mock.patch.object(oc, "farm_request", side_effect=fake), \
                mock.patch.object(oc.subprocess, "run", side_effect=_no_subprocess):
            oc.cleanup_dead_workers(conn)
        return calls

    def test_no_planted_name_reaches_docker(self):
        conn = self.registry()
        _add_worker(conn, WORKER)
        for name in HOSTILE_NAMES:
            if name is not None:
                _add_worker(conn, name)
        calls = self._run(conn)
        targets = {name for op, name in calls if op in ("inspect", "remove")}
        self.assertEqual(targets, {WORKER})
        self.assertEqual([r[0] for r in conn.execute(
            "SELECT container_name FROM takeover_containers")], [])  # WORKER retired

    def test_the_worker_itself_is_still_handled(self):
        conn = self.registry()
        _add_worker(conn, WORKER)
        _add_worker(conn, "onionpress-wordpress")
        calls = self._run(conn)
        self.assertIn(("remove", WORKER), calls)
        self.assertNotIn(("remove", "onionpress-wordpress"), calls)


class GuardedCallsTest(QuietLog):
    """Each function that aims a farm controller request (and so a docker
    call) at a registry name refuses anything but a worker's name before
    asking. The controller refuses them too (tests/test_onionheaven_farm.py);
    this keeps the heartbeat from even asking, and logging why."""

    def _refuses(self, call):
        with mock.patch.object(oc.subprocess, "run", side_effect=_no_subprocess), \
                mock.patch.object(oc, "farm_request", side_effect=_no_subprocess), \
                mock.patch.object(oc, "farm_available", return_value=True):
            return call()

    def test_names(self):
        for name in HOSTILE_NAMES:
            with self.subTest(name=name):
                self.assertFalse(self._refuses(lambda: oc._check_worker_process(name)))
                self.assertFalse(self._refuses(lambda: oc._exec_takeover(name, ADDR)))
                self.assertFalse(self._refuses(lambda: oc._exec_release(name, ADDR)))
                self.assertIsNone(self._refuses(lambda: oc._has_onion_on_worker(name, ADDR)))
                self.assertIsNone(self._refuses(lambda: oc.get_queue_status(name)))
                self.assertFalse(self._refuses(lambda: oc._rm_worker(name)))
                self.assertIsNone(self._refuses(lambda: hb._get_tor_detached(name)))

    def test_addresses(self):
        for address in HOSTILE_ADDRESSES:
            with self.subTest(address=address):
                self.assertFalse(self._refuses(lambda: oc._exec_takeover(WORKER, address)))
                self.assertFalse(self._refuses(lambda: oc._exec_release(WORKER, address)))
                self.assertIsNone(self._refuses(
                    lambda: oc._has_onion_on_worker(WORKER, address)))

    def test_serving_status(self):
        conn = self.registry()
        _add_row(conn, ADDR, "c" * 56 + ".onion", container="onionpress-wordpress")
        self.assertEqual(self._refuses(lambda: oc.get_addr_serving_status(conn, ADDR)),
                         "activating")

    def test_remove_workers_counts_only_workers(self):
        removed = []

        def fake(op, **fields):
            self.assertEqual(op, "remove")
            removed.append(fields["name"])
            return {"ok": True, "removed": True}
        with mock.patch.object(oc, "farm_request", side_effect=fake):
            n = oc._remove_workers(["onionheaven-takeover-1", "onionpress-db",
                                    "onionheaven-poll-0", None])
        self.assertEqual(n, 2)
        self.assertEqual(sorted(removed), ["onionheaven-poll-0", "onionheaven-takeover-1"])

    def test_bootstrap_check_never_execs_into_a_planted_name(self):
        conn = self.registry()
        _add_worker(conn, "onionpress-tor", bootstrapped=0)
        _add_worker(conn, "onionpress-db", bootstrapped=1)
        with mock.patch.object(oc.subprocess, "run", side_effect=_no_subprocess), \
                mock.patch.object(oc, "farm_request", side_effect=_no_subprocess):
            oc.check_worker_bootstrap(conn)
        self.assertEqual(conn.execute(
            "SELECT bootstrapped FROM takeover_containers "
            "WHERE container_name = 'onionpress-tor'").fetchone()[0], 0)

    def test_tor_detached_keeps_only_addresses(self):
        out = "\n".join([ADDR, "garbage.onion", "X" * 56 + ".onion", "1" * 56 + ".onion", ""])
        with mock.patch.object(hb, "farm_exec", return_value=_done(out)):
            self.assertEqual(hb._get_tor_detached(WORKER), {ADDR})


class PickingWorkersTest(QuietLog):

    def test_pick_worker_passes_over_a_planted_name(self):
        conn = self.registry()
        _add_worker(conn, "onionpress-db", assigned=0)
        _add_worker(conn, WORKER, assigned=5)
        self.assertEqual(oc._pick_worker(conn), WORKER)

    def test_pick_worker_none_when_only_planted(self):
        conn = self.registry()
        _add_worker(conn, "onionpress-db")
        self.assertIsNone(oc._pick_worker(conn))

    def test_worker_index_ignores_planted_names(self):
        conn = self.registry()
        _add_worker(conn, "onionheaven-takeover-4")
        _add_worker(conn, "evil-99999999999999999999")
        _add_worker(conn, "onionheaven-takeover-1234567890")
        self.addCleanup(setattr, oc, "_next_worker_index", oc._next_worker_index)
        oc._init_worker_index(conn)
        self.assertEqual(oc._next_worker_index, 5)

    def test_takeover_never_prefers_a_planted_name(self):
        conn = self.registry()
        _add_worker(conn, "onionpress-db")
        _add_worker(conn, WORKER)
        _add_row(conn, ADDR, "c" * 56 + ".onion", status="online",
                 container="onionpress-db")
        queued = []
        with mock.patch.object(oc, "is_farm_mode", return_value=True), \
                mock.patch.object(oc, "_exec_takeover",
                                  side_effect=lambda w, a: queued.append(w) or True), \
                mock.patch.object(oc, "_ensure_capacity"):
            oc.takeover_function(conn, ADDR, "c" * 56 + ".onion", force=True)
        self.assertEqual(queued, [WORKER])


class KeyDirectoriesTest(QuietLog):
    """unregister_entry and cleanup_stress_tests remove KEYS_DIR/<address>:
    the address comes from the registry too."""

    def setUp(self):
        super().setUp()
        self.base = tempfile.mkdtemp(prefix="op-hub-keys-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.keys = os.path.join(self.base, "hub", "keys")
        self.victim = os.path.join(self.base, "persistent")
        os.makedirs(self.keys)
        os.makedirs(self.victim)
        with open(os.path.join(self.victim, "precious"), "w") as f:
            f.write("x")
        for name, value in (("KEYS_DIR", self.keys),
                            ("ONIONHEAVEN_DATA_DIR", os.path.dirname(self.keys))):
            patcher = mock.patch.object(oc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _planted(self):
        rel = os.path.relpath(self.victim, self.keys)  # ../../persistent
        return [rel, self.victim, ".."]

    def test_unregister_entry(self):
        conn = self.registry()
        for ca in self._planted():
            _add_row(conn, ca, "h" * 56 + ".onion", status="online")
            with mock.patch.object(oc.subprocess, "run", side_effect=_no_subprocess):
                oc.unregister_entry(conn, ca, "h" * 56 + ".onion", reason="test")
        self.assertTrue(os.path.exists(os.path.join(self.victim, "precious")))
        self.assertTrue(os.path.isdir(self.keys))

    def test_a_real_address_is_still_removed(self):
        conn = self.registry()
        _add_row(conn, ADDR, "h" * 56 + ".onion", status="online")
        os.makedirs(os.path.join(self.keys, ADDR))
        oc.unregister_entry(conn, ADDR, "h" * 56 + ".onion", reason="test")
        self.assertFalse(os.path.exists(os.path.join(self.keys, ADDR)))

    def test_cleanup_stress_tests(self):
        conn = self.registry()
        for i, ca in enumerate(self._planted()):
            _add_row(conn, ca, f"{i}" * 56 + ".onion", status="online",
                     version="stress-test-1")
        _add_worker(conn, "onionpress-wordpress", assigned=0)
        removed = []

        def fake(op, **fields):
            if op == "list":
                return {"ok": True, "names": ["onionpress-wordpress", "onionheaven-takeover-2",
                                              "onionheaven-takeover-x"]}
            if op == "remove":
                removed.append(fields["name"])
                return {"ok": True, "removed": True}
            raise AssertionError(f"unexpected {op} {fields}")
        with mock.patch.object(oc, "farm_request", side_effect=fake), \
                mock.patch.object(oc.subprocess, "run", side_effect=_no_subprocess):
            stats = oc.cleanup_stress_tests(conn)
        self.assertTrue(os.path.exists(os.path.join(self.victim, "precious")))
        self.assertEqual(stats["keys_deleted"], 0)
        self.assertEqual(removed, ["onionheaven-takeover-2"])
        self.assertEqual(stats["workers_removed"], 1)


class QueueManagerAddressesTest(unittest.TestCase):
    """The worker's own guard: an address reaches neither Tor's control
    port nor the key directory unless it is one."""

    def test_refused_without_touching_the_queue(self):
        manager = mock.Mock()
        for cmd in qm.ADDRESS_COMMANDS:
            for arg in ("x" * 56 + ".onion\r\nSIGNAL HALT", "../../etc/x.onion",
                        ".onion", ADDR + " " + ADDR):
                with self.subTest(cmd=cmd, arg=arg), \
                        mock.patch.object(qm, "log"):
                    result = qm.handle_command(manager, f"{cmd} {arg}")
                    self.assertEqual(result, {"error": "not an onion address"})
        self.assertEqual(manager.mock_calls, [])

    def test_addresses_still_work(self):
        manager = mock.Mock()
        manager.takeover.return_value = {"status": "queued", "address": ADDR}
        manager.addr_state.return_value = "active"
        manager.cmd.has_onion.return_value = True
        self.assertEqual(qm.handle_command(manager, f"takeover {ADDR}"),
                         {"status": "queued", "address": ADDR})
        qm.handle_command(manager, f"release {ADDR}")
        manager.release.assert_called_once_with(ADDR)
        self.assertEqual(qm.handle_command(manager, f"addr_state {ADDR}"),
                         {"address": ADDR, "serving_status": "active"})
        self.assertEqual(qm.handle_command(manager, f"has {ADDR}"),
                         {"address": ADDR, "has_onion": True})
        qm.handle_command(manager, "status")
        manager.status.assert_called_once_with()

    def test_the_daemon_dispatches_through_it(self):
        with open(os.path.join(DOCKER_TOR_DIR, "onionheaven-queue-manager.py")) as f:
            src = f.read()
        daemon = src[src.index("def run_daemon():"):src.index("def send_command(")]
        self.assertIn("handle_command(qm, data)", daemon)
        self.assertNotIn("qm.takeover(", daemon)


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------

# Every Python file in the tor image (onionpress-tor, onionheaven, workers).
IMAGE_PYTHON = sorted(f for f in os.listdir(DOCKER_TOR_DIR) if f.endswith(".py"))

# Functions that build a docker argv. A new one has to be looked at: where
# do its names and arguments come from? Since the farm controller took the
# socket, the heartbeat's code builds none: onionheaven_common asks the
# controller (farm_request / farm_exec), and the controller's `docker`
# helper is the one place an argv is handed to the CLI, with names, verbs
# and addresses its validate() has already checked.
DOCKER_CALLERS = {
    ("onionheaven-farm.py", "docker"),
}

# Calls that check a value before a docker call may use it.
GUARDS = {"worker_name_ok", "is_worker_name", "onion_address_ok", "is_onion_address"}

# Values in a docker argv that are not checked in their function, and why
# that is fine. Anything read from the registry or the network is not here.
REVIEWED = {}


def _docker_argvs():
    """(file, function, list node, function node) for each docker argv."""
    found = []
    for filename in IMAGE_PYTHON:
        with open(os.path.join(DOCKER_TOR_DIR, filename), encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename)

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child)
                    continue
                if (isinstance(child, (ast.List, ast.Tuple)) and child.elts
                        and isinstance(child.elts[0], ast.Constant)
                        and child.elts[0].value == "docker"):
                    found.append((filename, func.name if func else "<module>",
                                  child, func))
                visit(child, func)
        visit(tree, None)
    return found


def _guarded(func, name, before_line):
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and node.lineno < before_line and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id == name):
            callee = node.func
            called = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
            if called in GUARDS:
                return True
    return False


def _values(elt):
    """The non-constant parts of one argv element."""
    if isinstance(elt, ast.Constant):
        return []
    if isinstance(elt, ast.JoinedStr):
        return [v.value for v in elt.values if isinstance(v, ast.FormattedValue)]
    return [elt]


class DockerCallCensusTest(unittest.TestCase):

    def test_the_census_is_complete(self):
        callers = {(f, fn) for f, fn, _, _ in _docker_argvs()}
        self.assertEqual(callers, DOCKER_CALLERS,
                         "a docker call was added or removed in the tor image: "
                         "check where its arguments come from and update the census")

    def test_every_value_is_checked_or_reviewed(self):
        unchecked = []
        used_reviews = set()
        for filename, func_name, argv, func in _docker_argvs():
            for elt in argv.elts:
                for value in _values(elt):
                    if isinstance(value, ast.Name):
                        if value.id.isupper():
                            continue  # a module constant
                        if func is not None and _guarded(func, value.id, argv.lineno):
                            continue
                        key = (filename, func_name, value.id)
                    else:
                        key = (filename, func_name, ast.unparse(value))
                    if key in REVIEWED:
                        used_reviews.add(key)
                        continue
                    unchecked.append(key)
        self.assertEqual(unchecked, [],
                         "a docker argv takes a value that is neither checked "
                         "(worker_name_ok / onion_address_ok) nor reviewed")
        self.assertEqual(used_reviews, set(REVIEWED), "stale REVIEWED entries")

    def test_no_shell_script_in_the_image_calls_docker(self):
        for filename in sorted(os.listdir(DOCKER_TOR_DIR)):
            if not filename.endswith(".sh"):
                continue
            with open(os.path.join(DOCKER_TOR_DIR, filename), encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    code = line.split("#", 1)[0]
                    self.assertIsNone(
                        re.search(r"\bdocker\s+(exec|run|rm|cp|inspect|ps|kill|stop|"
                                  r"start|restart|update|commit)\b", code),
                        f"{filename}:{n}: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
