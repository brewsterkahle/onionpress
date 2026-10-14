#!/usr/bin/env python3
"""Two things inside each OnionHeaven takeover worker.

The queue manager's command socket is root's only. The daemon is root and,
on `takeover <address>`, reads that address's key from the hub data volume
(every registrant's key is there) and hands it to Tor. Tor runs in the same
container as debian-tor, and the socket was /tmp/queue-manager.sock, mode
0666: whoever ran as debian-tor could have any registered address's key
loaded into that worker's Tor, and could hold the name in the
world-writable /tmp before the daemon bound it. It is now
/run/onionheaven-queue-manager.sock (only root writes /run), created 0600
(umask set around bind). Every client is root: `docker exec` into a worker
whose image runs as root, with no -u.

The post-takeover audit fetches the registry's healthcheck_address through
Tor, and an answer releases the takeover. The registry is writable by
onionpress-tor and every worker, so the value is fetched only if it is one
v3 onion address, whole; anything else is recorded as
invalid_healthcheck_address, never fetched, never a release.

No Docker: the socket is bound in a temp directory, the audit runs against
a temp registry with subprocess mocked.
"""

import importlib.util
import io
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
from contextlib import redirect_stdout
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


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


qm = _load("onionheaven_queue_manager_hardening", "onionheaven-queue-manager.py")
worker = _load("onionheaven_takeover_worker_hardening", "onionheaven-takeover-worker.py")

ADDR = "op2" + "a" * 53 + ".onion"
HC = "hc" + "b" * 54 + ".onion"
WORKER = "onionheaven-takeover-7"


# ---------------------------------------------------------------------------
# The queue manager's socket
# ---------------------------------------------------------------------------

class SocketPlaceTest(unittest.TestCase):

    def test_in_run_not_tmp(self):
        self.assertEqual(qm.SOCK_PATH, "/run/onionheaven-queue-manager.sock")
        src = _read("app/Resources/docker/tor/onionheaven-queue-manager.py")
        self.assertNotRegex(src, r'SOCK_PATH\s*=\s*"/tmp')
        self.assertNotIn("0o666", src)
        self.assertNotIn("0o777", src)

    def test_daemon_binds_through_bind_command_socket(self):
        src = _read("app/Resources/docker/tor/onionheaven-queue-manager.py")
        body = src[src.index("def run_daemon():"):src.index("def send_command(")]
        self.assertIn("server = bind_command_socket()", body)
        self.assertNotIn(".bind(", body)
        self.assertNotIn("chmod", body)


class BindCommandSocketTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-qm-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "qm.sock")

    def _bind(self):
        server = qm.bind_command_socket(self.path)
        self.addCleanup(server.close)
        return server

    def test_owner_only_from_the_start(self):
        seen = []
        real_umask = os.umask

        class Recording(socket.socket):
            def bind(self_, address):
                current = real_umask(0)
                real_umask(current)
                seen.append(current)
                return super().bind(address)

        before = real_umask(0o022)
        try:
            with mock.patch.object(qm.socket, "socket", Recording):
                self._bind()
            after = real_umask(before)
        finally:
            real_umask(before)
        self.assertEqual(seen, [0o177])  # bind() created it 0600
        self.assertEqual(after, 0o022)   # and the umask is put back
        st = os.lstat(self.path)
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_a_stale_name_is_replaced_never_followed(self):
        target = os.path.join(self.dir, "target")
        with open(target, "w") as f:
            f.write("keep\n")
        os.symlink(target, self.path)
        self._bind()
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.path).st_mode))
        with open(target) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_the_client_talks_to_it(self):
        server = self._bind()
        server.settimeout(5)

        def answer():
            conn, _ = server.accept()
            with conn:
                self.assertEqual(conn.recv(4096), b"status\n")
                conn.sendall(b'{"ok": true}\n')

        t = threading.Thread(target=answer)
        t.start()
        out = io.StringIO()
        with mock.patch.object(qm, "SOCK_PATH", self.path), redirect_stdout(out):
            qm.send_command("status")
        t.join(5)
        self.assertEqual(out.getvalue().strip(), '{"ok": true}')

    def test_no_socket_means_not_running(self):
        out = io.StringIO()
        with mock.patch.object(qm, "SOCK_PATH", os.path.join(self.dir, "absent.sock")), \
                redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            qm.send_command("status")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("queue manager not running", out.getvalue())


class EveryClientIsRootTest(unittest.TestCase):
    """0600 is safe only while every client of the socket is root."""

    def test_docker_exec_calls_pass_no_user(self):
        """The farm controller (onionheaven-farm.py) runs every `docker exec`
        into a worker now: its verb table names the queue manager's
        commands, and op_exec adds nothing but the worker's name."""
        src = _read("app/Resources/docker/tor/onionheaven-farm.py")
        table = src[src.index("EXEC_VERBS = {"):src.index("\n}\n", src.index("EXEC_VERBS = {"))]
        self.assertIn('QUEUE_MANAGER = "/onionheaven-queue-manager.py"', src)
        qm_verbs = [line for line in table.splitlines() if "QUEUE_MANAGER," in line]
        self.assertEqual(len(qm_verbs), 5)
        for line in qm_verbs:
            self.assertNotRegex(line, r'"(-u|--user)"|"-u\w')
        op_exec = src[src.index("def op_exec("):src.index("\ndef ", src.index("def op_exec(") + 1)]
        self.assertIn('argv = ["exec", req["name"]] + inner', op_exec)
        self.assertNotRegex(op_exec, r"--user|\"-u\"")

    def test_workers_run_as_the_image_user_root(self):
        for rel, pattern in (
                ("app/Resources/docker/tor/onionheaven-farm.py",
                 r'\[\s*"run", "-d",.*?image,\s*\]'),
                ("src/onionpress/containers.py", r'\[\s*"run", "-d",.*?image,\s*\]'),
                ("app/MacOS/onionpress", r'docker run -d \\.*?ONIONHEAVEN_IMAGE')):
            spawns = re.findall(pattern, _read(rel), re.S)
            self.assertTrue(spawns, rel)
            for spawn in spawns:
                self.assertNotRegex(spawn, r'--user|"-u"|\s-u\s', rel)
        dockerfile = _read("app/Resources/docker/tor/Dockerfile")
        self.assertEqual(re.findall(r"^USER\s+(\S+)", dockerfile, re.M)[-1], "root")

    def test_the_daemon_is_started_as_root(self):
        entry = _read("app/Resources/docker/tor/entrypoint.sh")
        line = next(l for l in entry.splitlines()
                    if "/onionheaven-queue-manager.py daemon" in l)
        self.assertNotRegex(line, r"\b(su|runuser|setpriv|gosu)\b")

    def test_nothing_else_names_the_socket(self):
        for dirpath, dirnames, filenames in os.walk(DOCKER_TOR_DIR):
            # Sources only: a test run that writes bytecode leaves
            # __pycache__/onionheaven-queue-manager.*.pyc here, which
            # names the socket too.
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name == "onionheaven-queue-manager.py" or name.endswith((".pyc", ".pyo")):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8",
                          errors="replace") as f:
                    text = f.read()
                self.assertNotIn("queue-manager.sock", text, name)


# ---------------------------------------------------------------------------
# The audit's healthcheck address
# ---------------------------------------------------------------------------

HOSTILE_HEALTHCHECKS = [
    "",
    None,
    "example.com",
    "127.0.0.1:9051",
    "localhost",
    "user@" + HC,
    HC + ":80",
    HC + "/path",
    HC + "#",
    HC + "\n",
    HC.upper(),
    "{a,b}" + HC[5:],
    "[1-9]" + HC[5:],
    HC[:-6],
    "-K/etc/passwd",
]


class AuditInputTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-audit-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.logged = []
        self.released = []
        self.runs = []
        for target, name, value in (
                (oc, "ONIONHEAVEN_DATA_DIR", self.base),
                (oc, "DB_PATH", os.path.join(self.base, "registry.db")),
                (oc, "log", self.logged.append),
                (worker, "log", self.logged.append),
                (worker, "CONTAINER_NAME", WORKER),
                (worker, "_release_local", lambda ca: self.released.append(ca) or True),
                (worker.subprocess, "run", self._run)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = oc.db_connect()
        self.addCleanup(self.conn.close)
        oc.db_ensure_schema(self.conn)

    def _run(self, argv, **kwargs):
        self.runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="200", stderr="")

    def _seed(self, hc, ca=ADDR):
        self.conn.execute(
            "INSERT INTO registry (content_address, healthcheck_address, registered_at, "
            "status, takeover_container, audit_pending) "
            "VALUES (?, ?, '2026-01-01T00:00:00Z', 'taken-over', ?, '2026-01-01T00:00:00Z')",
            (ca, hc, WORKER))
        self.conn.commit()

    def _row(self, hc):
        return self.conn.execute(
            "SELECT * FROM registry WHERE healthcheck_address IS ?", (hc,)).fetchone()

    def test_a_non_address_is_never_fetched(self):
        for value in HOSTILE_HEALTHCHECKS:
            self.assertFalse(worker._check_healthcheck(value), repr(value))
        self.assertEqual(self.runs, [])
        self.assertEqual(len(self.logged), len(HOSTILE_HEALTHCHECKS))

    def test_an_onion_address_is(self):
        self.assertTrue(worker._check_healthcheck(HC))
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0][-1], f"http://{HC}/")
        self.assertEqual(self.runs[0][0], "curl")

    def test_a_planted_row_never_releases(self):
        for i, value in enumerate(v for v in HOSTILE_HEALTHCHECKS if v is not None):
            ca = "op2" + chr(ord("a") + i) * 53 + ".onion"
            self._seed(value, ca=ca)
            self.assertEqual(worker.process_audits(self.conn), 1)
            row = self._row(value)
            self.assertEqual(row["audit_result"], worker.AUDIT_INVALID_ADDRESS, repr(value))
            self.assertIsNone(row["audit_pending"])
            self.assertEqual(row["status"], "taken-over")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.released, [])
        # Nothing left to hold up the worker's other audits.
        self.assertEqual(worker.process_audits(self.conn), 0)

    def test_the_heartbeat_does_not_queue_it_again(self):
        self._seed("example.com")
        worker.process_audits(self.conn)
        row = dict(self._row("example.com"))
        self.assertTrue(row["audit_result"])          # heartbeat: no new audit
        self.assertNotEqual(row["audit_result"], "false_positive")  # nor a release

    def test_a_real_address_that_answers_is_released(self):
        self._seed(HC)
        self.assertEqual(worker.process_audits(self.conn), 1)
        self.assertEqual(self.released, [ADDR])
        self.assertEqual(self._row(HC)["audit_result"], "false_positive")


if __name__ == "__main__":
    unittest.main()
