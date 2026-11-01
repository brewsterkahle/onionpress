#!/usr/bin/env python3
"""The OnionHeaven hub data has a volume of its own, which WordPress never
mounts.

The hub's registry and the onion service key of every site registered with
it used to live in onionheaven/ on onionpress-persistent-data, mounted at
/var/lib/onionpress — and the wordpress container mounts that volume
read-write too. Now:

  * onionpress-onionheaven-data, mounted at /var/lib/onionheaven by
    onionpress-tor, onionheaven and the takeover workers only. Not under
    /var/lib/onionpress: a mount point inside a directory WordPress can
    write is not a boundary.
  * Every path in the image's code, the launchers and the host-side tools
    points there, and the host reaches it through the onionheaven or
    onionpress-tor container, never onionpress-wordpress.
  * The tor image's entrypoint moves the old directory over
    (onionheaven-data-migrate.py): verified copy first, old files removed
    only if unchanged, symlinks never followed, nothing moved into a
    directory that is not a volume.
  * The heartbeat retires takeover workers that still mount the old volume.

No PyYAML and no Docker: the compose file is read with a line-based
splitter, subprocess is mocked, and the migration runs on temporary
directories.
"""

import hashlib
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_TOR_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import onionheaven_common as oc  # noqa: E402

_farm_spec = importlib.util.spec_from_file_location(
    "onionheaven_farm", os.path.join(DOCKER_TOR_DIR, "onionheaven-farm.py"))
farm = importlib.util.module_from_spec(_farm_spec)
_farm_spec.loader.exec_module(farm)

COMPOSE = "app/Resources/docker/docker-compose.yml"
MIGRATE = os.path.join(DOCKER_TOR_DIR, "onionheaven-data-migrate.py")
VOLUME = "onionpress-onionheaven-data"
HUB_DIR = "/var/lib/onionheaven"
LEGACY_PATH = "/var/lib/onionpress/onionheaven"


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


def _volume_items(block):
    m = re.search(r"^    volumes:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    return [] if not m else [l.strip()[2:].strip() for l in m.group(1).splitlines()]


def _load_migrate():
    spec = importlib.util.spec_from_file_location("onionheaven_data_migrate", MIGRATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM registry").fetchone()[0]
    finally:
        conn.close()


_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"


def _address(i, fill="a"):
    """A distinct, well-formed v3 onion address for each i < 1024."""
    return _BASE32[i // 32] + _BASE32[i % 32] + fill * 54 + ".onion"


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

class TestCompose(unittest.TestCase):

    def setUp(self):
        self.text = _read(COMPOSE)
        self.services = _services(self.text)

    def test_volume_is_named(self):
        self.assertRegex(self.text, rf"(?m)^  onionheaven-data:\n    name: {VOLUME}$")

    def test_only_tor_and_onionheaven_mount_it(self):
        mounting = {name for name, block in self.services.items()
                    if any(item.split(":", 1)[0] == "onionheaven-data"
                           for item in _volume_items(block))}
        self.assertEqual(mounting, {"tor", "onionheaven"})
        for name in ("tor", "onionheaven"):
            with self.subTest(service=name):
                self.assertIn(f"onionheaven-data:{HUB_DIR}",
                              _volume_items(self.services[name]))

    def test_wordpress_never_sees_it(self):
        wp = self.services["wordpress"]
        self.assertNotIn("onionheaven-data", wp)
        self.assertNotIn(HUB_DIR, wp)
        self.assertNotIn(VOLUME, wp)

    def test_nothing_is_mounted_inside_the_shared_volume(self):
        """A mount point inside /var/lib/onionpress would sit in a directory
        that WordPress, which mounts that volume, can rename and replace."""
        for name, block in self.services.items():
            for item in _volume_items(block):
                target = item.split(":")[1] if ":" in item else item
                with self.subTest(service=name, mount=item):
                    self.assertFalse(target.rstrip("/").startswith("/var/lib/onionpress/"))

    def test_other_compose_files_leave_it_alone(self):
        for path in ("app/Resources/docker/docker-compose.cloudflare.yml",):
            with self.subTest(path=path):
                self.assertNotIn("onionheaven-data", _read(path))
                self.assertNotIn(HUB_DIR, _read(path))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CODE_DIRS = ("app", "src", "linux", "build", "tests")


def _code_files():
    for top in CODE_DIRS:
        for root, dirs, files in os.walk(os.path.join(PROJECT_ROOT, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
            for name in files:
                if name.endswith((".png", ".icns", ".jpg", ".gif", ".zip", ".pyc")):
                    continue
                yield os.path.join(root, name)


class TestPaths(unittest.TestCase):

    def test_common_constants(self):
        self.assertEqual(oc.ONIONHEAVEN_DATA_DIR, HUB_DIR)
        self.assertEqual(oc.HUB_DATA_VOLUME, VOLUME)
        self.assertEqual(oc.DB_PATH, f"{HUB_DIR}/registry.db")
        self.assertEqual(oc.KEYS_DIR, f"{HUB_DIR}/keys")

    def test_image_scripts_agree(self):
        qm = _read("app/Resources/docker/tor/onionheaven-queue-manager.py")
        self.assertRegex(qm, rf'(?m)^KEYS_DIR = "{re.escape(oc.KEYS_DIR)}"$')
        tm = _read("app/Resources/docker/tor/onionheaven-tor-manager.sh")
        self.assertIn(f'ONIONHEAVEN_KEYS_DIR="{oc.KEYS_DIR}"', tm)
        redirect = _read("app/Resources/docker/tor/onionheaven-redirect.sh")
        self.assertEqual(redirect.count(f"sqlite3 {oc.DB_PATH}"), 2)
        entrypoint = _read("app/Resources/docker/tor/entrypoint.sh")
        self.assertIn(f"mkdir -p {oc.KEYS_DIR}", entrypoint)
        self.assertIn(f'HEARTBEAT_LOG="{HUB_DIR}/heartbeat.log"', entrypoint)

    def test_host_constant_agrees(self):
        from onionpress import onionheaven
        self.assertEqual(onionheaven.ONIONHEAVEN_DATA_DIR, oc.ONIONHEAVEN_DATA_DIR)

    def test_the_old_path_is_gone(self):
        me = os.path.abspath(__file__)
        offenders = []
        for path in _code_files():
            if os.path.abspath(path) == me:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            if LEGACY_PATH in text:
                offenders.append(os.path.relpath(path, PROJECT_ROOT))
        self.assertEqual(offenders, [], "the hub data is at /var/lib/onionheaven now")

    def test_host_code_never_reaches_it_through_wordpress(self):
        """Each hub data path is preceded by the container it is reached
        through; that must be onionheaven or onionpress-tor."""
        containers = re.compile(r"onionpress-wordpress|onionpress-tor|TOR_CONTAINER"
                                r"|onionheaven(?=[\"':\s])")
        checked = 0
        for path in _code_files():
            rel = os.path.relpath(path, PROJECT_ROOT)
            if not rel.startswith(("src/", "linux/", "app/MacOS/", "tests/stress",
                                   "tests/onionheaven-stress-test.sh",
                                   "tests/test-onionheaven-local.sh")):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for m in re.finditer(re.escape(HUB_DIR), text):
                window = text[max(0, m.start() - 400):m.start()]
                found = containers.findall(window)
                if not found:
                    continue
                checked += 1
                with self.subTest(file=rel, at=m.start()):
                    self.assertNotEqual(found[-1], "onionpress-wordpress")
        # Ten since #504 (the Linux activation watcher's two reads went);
        # nine since #523 (the Linux launcher's own registry query went:
        # the status asks through onionheaven.overview).
        self.assertGreaterEqual(checked, 9)

    def test_wordpress_side_code_does_not_know_it(self):
        for top in ("app/Resources/plugins", "app/Resources/themes",
                    "app/Resources/docker/wordpress"):
            for root, _, files in os.walk(os.path.join(PROJECT_ROOT, top)):
                for name in files:
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8", errors="replace") as f:
                        with self.subTest(file=os.path.relpath(path, PROJECT_ROOT)):
                            self.assertNotIn(HUB_DIR, f.read())

    def test_backup_and_restore_use_the_onionheaven_container(self):
        from onionpress import backup as backup_module
        self.assertEqual(backup_module.ONIONHEAVEN_DATA_DIR, HUB_DIR)
        # The hub data's staging and restore moved into hub_backup (#484).
        backup = _read("src/onionpress/backup.py") + _read("src/onionpress/hub_backup.py")
        self.assertEqual(backup.count("['docker', 'exec', 'onionheaven', 'sh', '-c', HUB_DATA_TAR_SH]"), 2)
        self.assertIn("['docker', 'cp', '-', f'onionheaven:{ONIONHEAVEN_DATA_DIR}/keys/']", backup)
        self.assertIn("['docker', 'exec', '-i', 'onionheaven', 'sh', '-c',\n"
                      "                            REGISTRY_RESTORE_SH]", backup)
        self.assertNotRegex(backup, r"www-data[^\n]*onionheaven")
        # The Mac launcher's `restore` is onionpress.cli restore (backup.py).
        self.assertIn("restore_hub_data(onionheaven_dir, log_func)", backup)
        self.assertIn('restore-hub-data --src "$staging/onionheaven"', _read("linux/onionpress"))
        self.assertIn('stage-hub-backup --staging "$staging"', _read("linux/onionpress"))


# ---------------------------------------------------------------------------
# Image: entrypoint and Dockerfile
# ---------------------------------------------------------------------------

class TestImage(unittest.TestCase):

    def setUp(self):
        self.entrypoint = _read("app/Resources/docker/tor/entrypoint.sh")

    def test_dockerfile_ships_the_script_and_the_mount_point(self):
        dockerfile = _read("app/Resources/docker/tor/Dockerfile")
        self.assertIn("COPY onionheaven-data-migrate.py /onionheaven-data-migrate.py",
                      dockerfile)
        self.assertRegex(dockerfile, r"chmod \+x [^\n]*/onionheaven-data-migrate\.py")
        self.assertIn(f"RUN mkdir -m 700 {HUB_DIR}", dockerfile)

    def test_migration_fails_the_container(self):
        fn = self.entrypoint[self.entrypoint.index("migrate_onionheaven_data() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("python3 /onionheaven-data-migrate.py", fn)
        self.assertIn("exit 1", fn)

    def test_tor_migrates_before_the_api_server(self):
        main = self.entrypoint[self.entrypoint.index("# ==================== Onion service mode"):]
        self.assertLess(main.index("migrate_onionheaven_data"),
                        main.index("python3 /web-server.py"))

    def test_onionheaven_migrates_before_anything_reads_the_registry(self):
        start = self.entrypoint.index('if [ "${ONIONHEAVEN}" = "1" ]; then')
        branch = self.entrypoint[start:self.entrypoint.index("SOCKS-only mode", start)]
        call = branch.index("migrate_onionheaven_data")
        self.assertLess(call, branch.index("/onionheaven-redirect.sh &"))
        self.assertLess(call, branch.index("python3 /onionheaven-heartbeat.py"))

    def test_workers_do_not_migrate(self):
        """Workers mount only the new volume; there is nothing to move."""
        start = self.entrypoint.index('if [ "${TAKEOVER_WORKER}" = "1" ]; then')
        branch = self.entrypoint[start:self.entrypoint.index("\nfi\n", start)]
        self.assertNotIn("migrate_onionheaven_data", branch)


# ---------------------------------------------------------------------------
# Takeover workers
# ---------------------------------------------------------------------------

def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


def _mounts(*pairs):
    return json.dumps([{"Type": "volume", "Name": n, "Destination": d, "RW": True}
                       for n, d in pairs])


def _inspected(running, mounts):
    """The farm controller's reply to `inspect` for an existing worker."""
    return {"ok": True, "exists": True, "running": running, "mounts": mounts}


class TestWorkerMounts(unittest.TestCase):

    def _volume_args(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]

    def test_spawn_worker(self):
        """The farm controller's `docker run` for a worker, which it builds
        itself for the onionheaven container's spawn request."""
        self.assertEqual(farm.HUB_DATA_VOLUME, VOLUME)
        self.assertEqual(farm.ONIONHEAVEN_DATA_DIR, HUB_DIR)
        argv = farm.worker_run_argv("onionheaven-takeover-0", "img@sha256:" + "a" * 64)
        self.assertEqual(self._volume_args(argv), [f"{VOLUME}:{HUB_DIR}"])

    def test_dead_code_spawners_agree(self):
        """Not called today, but they must not bring the old mount back."""
        from onionpress.containers import ContainerManager
        from onionpress.docker import Docker
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = mock.Mock(ok=True, stderr="")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = mock.Mock(config_file=os.path.join(tmp, "config"), docker_dir=tmp)
        cm = ContainerManager(docker, paths, port_config=mock.Mock())
        cm.start_farm_worker(0, image="img")
        self.assertEqual(self._volume_args(docker.run.call_args.args[0]),
                         [f"{VOLUME}:{HUB_DIR}"])
        # The Mac launcher's unused _start_takeover_worker is gone.
        self.assertNotIn("TAKEOVER_WORKER=1", _read("app/MacOS/onionpress"))


class TestRetireOldWorkers(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        with mock.patch.object(oc, "log"):
            oc.db_ensure_schema(self.conn)
        self.conn.execute(
            "INSERT INTO takeover_containers (container_name, max_services,"
            " bootstrapped, assigned_count) VALUES ('onionheaven-takeover-0', 50, 1, 1)")
        self.conn.execute(
            "INSERT INTO registry (content_address, healthcheck_address,"
            " registered_at, status, takeover_container) VALUES"
            " ('a.onion', 'h.onion', 'now', 'taken-over', 'onionheaven-takeover-0')")
        self.conn.commit()
        patcher = mock.patch.object(oc, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, inspect, rm_ok=True, others=None):
        """Farm requests made for the registry's workers. inspect: the
        controller's reply for the registry's worker; others: {name: mounts
        JSON} of worker containers Docker lists that the registry does not."""
        others = others or {}

        def fake(op, **fields):
            name = fields.get("name")
            if op == "list":
                return {"ok": True, "names": ["onionheaven-takeover-0"] + list(others)}
            if op == "inspect" and name in others:
                return {"ok": True, "exists": True, "running": True, "mounts": others[name]}
            if op == "inspect":
                if isinstance(inspect, Exception):
                    raise inspect
                return inspect
            if op == "remove":
                return {"ok": True, "removed": rm_ok}
            raise AssertionError(f"unexpected {op} {fields}")
        with mock.patch.object(oc, "farm_request", side_effect=fake) as req, \
                mock.patch.object(oc.subprocess, "run",
                                  side_effect=AssertionError("no subprocess")):
            oc.cleanup_dead_workers(self.conn)
        self.all_calls = [(c.args[0], c.kwargs.get("name")) for c in req.call_args_list]
        return [(op, name) for op, name in self.all_calls
                if op != "list" and name not in others]

    def _worker_rows(self):
        return self.conn.execute("SELECT COUNT(*) FROM takeover_containers").fetchone()[0]

    def _assigned(self):
        return self.conn.execute("SELECT takeover_container FROM registry").fetchone()[0]

    def test_a_current_worker_stays(self):
        calls = self._run(_inspected(True, _mounts((VOLUME, HUB_DIR))))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._worker_rows(), 1)
        self.assertEqual(self._assigned(), "onionheaven-takeover-0")

    def test_an_old_worker_is_retired(self):
        calls = self._run(_inspected(True, _mounts(("onionpress-persistent-data",
                                                    "/var/lib/onionpress"))))
        self.assertIn(("remove", "onionheaven-takeover-0"), calls)
        self.assertEqual(self._worker_rows(), 0)
        self.assertIsNone(self._assigned())

    def test_doubt_keeps_the_worker(self):
        for mounts in ("", "not-json", "{}"):
            with self.subTest(mounts=mounts):
                calls = self._run(_inspected(True, mounts))
                self.assertEqual(len(calls), 1)
                self.assertEqual(self._worker_rows(), 1)

    def test_an_unreachable_controller_changes_nothing(self):
        for reply in (oc.FarmError("down"), {"ok": False, "error": "refused"},
                      {"ok": True}):
            with self.subTest(reply=reply):
                calls = self._run(reply)
                self.assertEqual(calls, [("inspect", "onionheaven-takeover-0")])
                self.assertEqual(self._worker_rows(), 1)
                self.assertEqual(self._assigned(), "onionheaven-takeover-0")

    def test_a_failed_removal_keeps_it_registered(self):
        self._run(_inspected(True, _mounts(("onionpress-persistent-data",
                                            "/var/lib/onionpress"))), rm_ok=False)
        self.assertEqual(self._worker_rows(), 1)
        self.assertEqual(self._assigned(), "onionheaven-takeover-0")

    def test_a_dead_worker_is_cleaned_up_as_before(self):
        calls = self._run(_inspected(False, _mounts((VOLUME, HUB_DIR))))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._worker_rows(), 0)
        self.assertIsNone(self._assigned())

    def test_a_vanished_worker_is_cleaned_up_as_before(self):
        calls = self._run({"ok": True, "exists": False})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._worker_rows(), 0)

    def test_old_workers_the_registry_does_not_list_are_retired_too(self):
        """takeover_containers is the snapshot the data move brought over;
        a worker an older heartbeat started later is in no row."""
        old = _mounts(("onionpress-persistent-data", "/var/lib/onionpress"))
        self._run(_inspected(True, _mounts((VOLUME, HUB_DIR))), others={
            "onionheaven-takeover-7": old,
            "onionheaven-poll-2": old,
            "onionheaven-takeover-9": _mounts((VOLUME, HUB_DIR)),
            "onionheaven-takeover-8": "not-json",
            "onionheaven-takeover-x": old,              # not a worker's name
        })
        removed = sorted(name for op, name in self.all_calls if op == "remove")
        self.assertEqual(removed, ["onionheaven-poll-2", "onionheaven-takeover-7"])
        self.assertEqual(self._worker_rows(), 1)
        self.assertEqual(self._assigned(), "onionheaven-takeover-0")


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------

class MigrationCase(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="op-hubdata-")
        self.addCleanup(self._cleanup)
        self.parent = os.path.join(self.base, "persistent")   # /var/lib/onionpress
        self.legacy = os.path.join(self.parent, "onionheaven")
        self.dest = os.path.join(self.base, "hub")            # /var/lib/onionheaven
        os.makedirs(self.parent)
        os.makedirs(self.dest)
        with open(os.path.join(self.parent, "status.json"), "w") as f:
            f.write("{}")

    def _cleanup(self):
        for root, dirs, files in os.walk(self.base):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o700)
                except OSError:
                    pass
            for n in files:
                try:
                    os.chmod(os.path.join(root, n), 0o600, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
        shutil.rmtree(self.base, ignore_errors=True)

    def run_script(self, *extra, mount_check=False):
        argv = [sys.executable, MIGRATE, "--legacy-parent", self.parent, "--dest", self.dest]
        if not mount_check:
            argv.append("--no-mount-check")
        result = subprocess.run(argv + list(extra), capture_output=True, text=True,
                                timeout=60)
        return result.returncode, result.stderr

    def write(self, rel, data, mode=0o600, root=None):
        path = os.path.join(root or self.legacy, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w" if isinstance(data, str) else "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return path

    def add_registrant(self, address="a" * 56 + ".onion", key="PEM-KEY"):
        kdir = os.path.join(self.legacy, "keys", address)
        self.write(os.path.join("keys", address, "ks_hs_id.ed25519_expanded_private"), key)
        self.write(os.path.join("keys", address, "hostname"), address + "\n")
        os.chmod(kdir, 0o700)
        return kdir

    def make_registry(self, rows=2, keep_open=False):
        os.makedirs(self.legacy, exist_ok=True)
        conn = sqlite3.connect(os.path.join(self.legacy, "registry.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE registry (content_address TEXT)")
        conn.executemany("INSERT INTO registry VALUES (?)",
                         [(f"r{i}",) for i in range(rows)])
        conn.commit()
        if keep_open:
            self.addCleanup(conn.close)
        else:
            conn.close()
        return conn

    def marker(self):
        with open(os.path.join(self.dest, ".migrated-from-persistent-data")) as f:
            return json.load(f)

    def tree(self, root):
        out = {}
        for r, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(r, name)
                st = os.lstat(path)
                rel = os.path.relpath(path, root)
                if os.path.isdir(path) and not os.path.islink(path):
                    out[rel] = ("dir", st.st_mode & 0o777)
                elif os.path.islink(path):
                    out[rel] = ("link", os.readlink(path))
                else:
                    out[rel] = ("file", st.st_mode & 0o777, _sha(path))
        return out


class TestMigration(MigrationCase):

    def test_fresh_install_marks_the_volume(self):
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.marker()["entries"], 0)
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o700)
        self.assertFalse(os.path.exists(self.legacy))

    def test_moves_everything_verified(self):
        self.make_registry(rows=3, keep_open=True)   # the rows are in -wal; -shm too
        kdir = self.add_registrant()
        self.write("heartbeat.log", "pass complete\n", 0o644)
        self.write("activate", "2026-10-12T00:00:00Z\n", 0o644)
        os.chmod(self.legacy, 0o755)
        before = self.tree(self.legacy)
        self.assertIn("registry.db-wal", before)
        self.assertIn("registry.db-shm", before)

        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("moved the OnionHeaven data to its own volume", err)

        after = self.tree(self.dest)
        for rel, entry in before.items():
            if rel.startswith("registry.db"):
                continue        # one database file now: below
            with self.subTest(rel=rel):
                # Same bytes; root's and private on the volume, whatever
                # the old modes were.
                want = ("dir", 0o700) if entry[0] == "dir" else ("file", 0o600, entry[2])
                self.assertEqual(after.get(rel), want)
        key_rel = os.path.relpath(kdir, self.legacy)
        self.assertEqual(after[key_rel], ("dir", 0o700))
        self.assertEqual(sorted(r for r in after if r.startswith("registry.db")),
                         ["registry.db"], "SQLite made one file of the database and "
                         "its log; the -shm index is rebuilt, not copied")
        self.assertEqual(after["registry.db"][1], 0o600)
        self.assertEqual(_rows(os.path.join(self.dest, "registry.db")), 3)

        self.assertFalse(os.path.lexists(self.legacy), "the old directory is removed")
        self.assertTrue(os.path.exists(os.path.join(self.parent, "status.json")))
        self.assertEqual(self.marker()["key_dirs"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.dest, ".migrating")))

    def test_two_containers_at_once_move_it_once(self):
        self.make_registry(rows=4)
        for i in range(20):
            self.add_registrant(_address(i))
        argv = [sys.executable, MIGRATE, "--legacy-parent", self.parent,
                "--dest", self.dest, "--no-mount-check"]
        procs = [subprocess.Popen(argv, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        results = []
        for p in procs:
            _, err = p.communicate(timeout=60)
            results.append((p.returncode, err))
        for rc, err in results:
            self.assertEqual(rc, 0, err)
        self.assertEqual(sum("moved the OnionHeaven data" in err for _, err in results), 1)
        names = os.listdir(self.dest)
        self.assertFalse([n for n in names if n.startswith((".pre-migration-",
                                                           "legacy-leftover-"))])
        self.assertEqual(len(os.listdir(os.path.join(self.dest, "keys"))), 20)
        self.assertEqual(_rows(os.path.join(self.dest, "registry.db")), 4)
        self.assertFalse(os.path.lexists(self.legacy))

    def test_second_run_changes_nothing(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        snapshot = self.tree(self.dest)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.dest), snapshot)

    def test_later_arrivals_are_kept_aside_and_not_used(self):
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        in_use = self.tree(self.dest)

        late = "b" * 56 + ".onion"
        self.add_registrant(late, key="LATE-KEY")
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("turned up at the old location", err)
        leftovers = [n for n in os.listdir(self.dest) if n.startswith("legacy-leftover-")]
        self.assertEqual(len(leftovers), 1)
        with open(os.path.join(self.dest, leftovers[0], "keys", late,
                               "ks_hs_id.ed25519_expanded_private")) as f:
            self.assertEqual(f.read(), "LATE-KEY")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "keys", late)))
        for rel, entry in in_use.items():
            self.assertEqual(self.tree(self.dest)[rel], entry)
        self.assertFalse(os.path.lexists(self.legacy))

    def test_what_was_on_the_volume_first_is_kept(self):
        self.make_registry(rows=5)
        self.add_registrant()
        self.write("registry.db", "restored-earlier", root=self.dest)
        self.write("empty.db", "", root=self.dest)
        os.makedirs(os.path.join(self.dest, "keys"))       # empty: dropped

        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        aside = [n for n in os.listdir(self.dest) if n.startswith(".pre-migration-")]
        self.assertEqual(len(aside), 1)
        with open(os.path.join(self.dest, aside[0], "registry.db")) as f:
            self.assertEqual(f.read(), "restored-earlier")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "empty.db")))
        self.assertEqual(_rows(os.path.join(self.dest, "registry.db")), 5)

    def test_old_entries_cannot_take_this_scripts_names(self):
        self.make_registry()
        self.add_registrant()
        self.write(".migrated-from-persistent-data", '{"forged": true}')
        self.write(".migrate.lock", "forged")
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.marker()["key_dirs"], 1)
        with open(os.path.join(self.dest, ".migrate.lock")) as f:
            self.assertEqual(f.read(), "")
        # No older version wrote these names: never copied, not even aside.
        self.assertFalse([n for n in os.listdir(self.dest)
                          if n.startswith((".pre-migration-", "legacy-leftover-"))])
        self.assertIn("not OnionHeaven data", err)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.lexists(self.legacy), "the next run removes them")
        self.assertEqual(self.marker()["key_dirs"], 1)

    def test_symlinks_are_never_followed(self):
        self.add_registrant()
        self.write("registry.db", "OLD")
        precious = self.write("precious", "HUB", root=self.dest)
        self.write(".migrated-from-persistent-data", "{}", root=self.dest)
        os.symlink(self.dest, os.path.join(self.legacy, "keys", "to-hub"))
        os.symlink(os.path.join(self.parent, "status.json"),
                   os.path.join(self.legacy, "status-link"))

        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        with open(precious) as f:
            self.assertEqual(f.read(), "HUB")
        self.assertTrue(os.path.exists(os.path.join(self.parent, "status.json")))
        leftover = [n for n in os.listdir(self.dest) if n.startswith("legacy-leftover-")][0]
        copied = self.tree(os.path.join(self.dest, leftover))
        self.assertFalse([r for r, e in copied.items() if e[0] == "link"])
        self.assertFalse(os.path.lexists(self.legacy))

    def test_a_symlink_in_place_of_the_old_directory(self):
        self.write("registry.db", "LIVE", root=self.dest)
        self.write(".migrated-from-persistent-data", "{}", root=self.dest)
        os.symlink(self.dest, self.legacy)
        before = self.tree(self.dest)

        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.lexists(self.legacy))
        after = self.tree(self.dest)
        after.pop(".migrate.lock")
        self.assertEqual(after, before)

    def test_nothing_moves_into_a_directory_that_is_not_a_volume(self):
        self.make_registry()
        self.add_registrant()
        before = self.tree(self.legacy)
        rc, err = self.run_script(mount_check=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("is not a volume", err)
        self.assertEqual(self.tree(self.legacy), before)
        self.assertEqual(os.listdir(self.dest), [])

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable files")
    def test_a_failed_copy_with_keys_at_stake_stops_the_container(self):
        self.make_registry()
        self.add_registrant()
        wal = self.write("registry.db-wal", "x")
        before = self.tree(self.legacy)
        os.chmod(wal, 0o000)
        rc, err = self.run_script()
        os.chmod(wal, 0o600)
        self.assertEqual(rc, 1, err)
        self.assertIn("not starting", err)
        self.assertEqual(self.tree(self.legacy), before)
        self.assertEqual(os.listdir(self.dest), [".migrate.lock"])

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable files")
    def test_later_arrivals_can_never_stop_the_container(self):
        """After the first move the old location holds nothing in use, and
        WordPress can write there."""
        self.make_registry()
        self.add_registrant()
        self.assertEqual(self.run_script()[0], 0)
        late = "c" * 56 + ".onion"
        self.add_registrant(late)
        stuck = os.path.join(self.legacy, "keys", late, "ks_hs_id.ed25519_expanded_private")
        os.chmod(stuck, 0o000)
        rc, err = self.run_script()
        os.chmod(stuck, 0o600)
        self.assertEqual(rc, 0, err)
        self.assertIn("did not copy", err)
        self.assertTrue(os.path.exists(stuck), "what could not be copied stays")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "keys", late)))

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable files")
    def test_a_failed_copy_without_keys_still_starts(self):
        os.chmod(self.write("heartbeat.log", "x"), 0o000)
        rc, err = self.run_script()
        self.assertEqual(rc, 0, err)
        self.assertIn("starting anyway", err)
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "heartbeat.log")))
        # The containers start on this volume, so it is the current one
        # from now on (tests/test_onionheaven_data_move_hardening.py).
        self.assertIn("move_failed", self.marker())


class TestMigrationInternals(MigrationCase):

    def setUp(self):
        super().setUp()
        self.mod = _load_migrate()

    def test_a_file_that_changed_after_the_copy_stays(self):
        address = "x" * 56 + ".onion"
        path = self.write(f"keys/{address}/hostname", "NEW")
        parent_fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, parent_fd)
        fd = os.open(self.legacy, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        tree = self.mod._scan(fd)
        stale = (3, hashlib.sha256(b"OLD").hexdigest())
        kept = self.mod._remove_old(parent_fd, fd, tree,
                                    {f"keys/{address}/hostname": stale}, {}, False, [])
        self.assertIn(f"keys/{address}/hostname", kept)
        with open(path) as f:
            self.assertEqual(f.read(), "NEW")

    def test_a_key_rewritten_during_its_copy_is_copied_again(self):
        self.make_registry()
        kdir = self.add_registrant(key="OLD-KEY")
        key = os.path.join(kdir, "ks_hs_id.ed25519_expanded_private")
        real = self.mod._copy_out
        calls = []

        def copy_then_rewrite(src, target, limit):
            got = real(src, target, limit)
            if target.endswith("ks_hs_id.ed25519_expanded_private"):
                calls.append(1)
                if len(calls) == 1:
                    with open(key, "w") as f:
                        f.write("NEW-KEY")
            return got

        with mock.patch.object(self.mod, "_copy_out", side_effect=copy_then_rewrite), \
                mock.patch.object(self.mod, "log"):
            self.mod.migrate(self.parent, self.dest, attempts=1, delay=0)
        self.assertEqual(len(calls), 2, "a copy the file no longer matches is not used")
        with open(os.path.join(self.dest, os.path.relpath(key, self.legacy))) as f:
            self.assertEqual(f.read(), "NEW-KEY")
        self.assertEqual(self.marker()["key_dirs"], 1)
        self.assertFalse(os.path.lexists(self.legacy))

    def test_mountinfo(self):
        info = os.path.join(self.base, "mountinfo")
        target = os.path.realpath(self.dest)
        with open(info, "w") as f:
            f.write("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n")
            f.write("90 22 8:1 /volumes/x/_data "
                    + target.replace(" ", "\\040") + " rw - ext4 /dev/sda1 rw\n")
        self.assertTrue(self.mod.is_mount_point(self.dest, mountinfo=info))
        self.assertFalse(self.mod.is_mount_point(self.parent, mountinfo=info))


if __name__ == "__main__":
    unittest.main()
