#!/usr/bin/env python3
"""A key import never destroys the site's identity before it has a new one.

The Linux Settings page's import-key-file and generate-vanity released the
OnionHeaven hubs, stopped OnionPress, removed the key volumes and
vanity-keys/ and only then decoded the key: garbage meant a first run and
a new address, the old identity gone for good. Both are gone from the
Linux page now (it refuses the import: tests/test_settings_page_onion_key.py),
and every import goes through the launchers' replace_onion_key:

  1. the key is checked (key_manager.check_expanded_secret_key) and staged,
     and the staged files are read back as its address;
  2. a copy of every key the install may be using goes to
     ~/.onionpress/replaced-keys/<address>-<time>/ (keep-current); no copy,
     no import;
  3. only then the hubs are released and OnionPress is stopped;
  4. the staged folder becomes vanity-keys/ and .import-key-pending is set
     (the next start removes the old key volumes, confirmed);
  5. ADDRESS_PREFIX follows.

The launchers' functions run for real here against a stub `docker` (an
executable first on PATH, so key_manager.py's own `docker exec` reads hit
it too). Nothing talks to a Docker daemon.
"""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import confirm, key_import, key_manager  # noqa: E402

SCRIPTS_DIR = os.path.join(PROJECT_DIR, "src", "onionpress")
LINUX = "linux/onionpress"
MAC = "app/MacOS/onionpress"
NO_DOCKER = "unix:///var/empty/no-docker-for-tests.sock"


def _read(rel):
    with open(os.path.join(PROJECT_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _shell_function(text, name):
    m = re.search(r"^" + name + r"\(\)\s*\{.*?^\}", text, re.DOTALL | re.MULTILINE)
    if m is None:
        raise AssertionError(f"{name}() not found")
    return m.group(0)


def _settings_branch(text, label):
    """The handle-action case's `<label>)` branch, up to its ;;."""
    start = text.index(f"\n                {label})\n")
    return text[start:text.index("\n                    ;;\n", start)]


def expanded_key(seed):
    h = bytearray(hashlib.sha512(seed).digest())
    h[0] &= 248
    h[31] = (h[31] & 127) | 64
    return bytes(h)


def address_of(priv):
    return key_manager.derive_onion_address(key_manager.derive_public_key(priv))


OLD = expanded_key(b"the site's key")
NEW = expanded_key(b"the imported key")
OLD_ADDR = address_of(OLD)
NEW_ADDR = address_of(NEW)
TOP_BIT = NEW[:31] + bytes([NEW[31] | 0x80]) + NEW[32:]


def b32(key):
    return base64.b32encode(key).decode("ascii")


# The stub docker: records every call, serves the tor container's files from
# $TOR_FS, lists the volumes in $VOLUMES (one per line; VOLUME_LS_FAILS=yes
# makes the listing fail, as with Docker down) and removes them on rm.
DOCKER_STUB = r'''#!/bin/bash
printf 'docker %s\n' "$*" >> "$CALLS"
case "$1" in
    info) exit 0 ;;
    exec)
        shift
        while [ "${1#-}" != "$1" ]; do
            case "$1" in -u|-e) shift 2 ;; *) shift ;; esac
        done
        container="$1"; shift
        if [ "$container" = onionpress-tor ] && [ "$1" = cat ] \
                && [ -n "${TOR_FS:-}" ] && [ -f "$TOR_FS$2" ]; then
            cat "$TOR_FS$2"; exit 0
        fi
        exit 1 ;;
    volume)
        case "$2" in
            ls) [ "${VOLUME_LS_FAILS:-}" = yes ] && exit 1
                cat "$VOLUMES" 2>/dev/null; exit 0 ;;
            rm) shift 2
                for v in "$@"; do
                    grep -vx -- "$v" "$VOLUMES" > "$VOLUMES.t" || true
                    mv "$VOLUMES.t" "$VOLUMES"
                done
                exit 0 ;;
        esac ;;
esac
exit 1
'''

TOR_HS = "/var/lib/tor/hidden_service/wordpress"


def _tree(path):
    """{relative path: bytes} of every file under *path*."""
    out = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, path)] = f.read()
    return out


class _Install:
    """A scratch home: ~/.onionpress with a config, maybe a vanity key, the
    tor container's files and the Docker volumes, and a stub docker."""

    def __init__(self, case, served=OLD, vanity=None, volumes=("onionpress-onion-keys",
                 "onionpress-tor-state"), volume_ls_fails=False):
        self.case = case
        self.tmp = tempfile.mkdtemp(prefix="op-key-replace-")
        case.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "home", ".onionpress")
        os.makedirs(os.path.join(self.data, "shared", "vanity-keys"))
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=op2\n")
        if vanity is not None:
            key_manager.import_key_b32(b32(vanity), os.path.join(self.data, "shared", "vanity-keys"))
        self.tor_fs = os.path.join(self.tmp, "torfs")
        if served is not None:
            hs = self.tor_fs + TOR_HS
            os.makedirs(hs)
            with open(os.path.join(hs, "hs_ed25519_secret_key"), "wb") as f:
                f.write(key_manager.CTOR_HEADER_SECRET + served)
            with open(os.path.join(hs, "hostname"), "w") as f:
                f.write(address_of(served) + "\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as f:
            f.write(DOCKER_STUB)
        os.chmod(stub, 0o755)
        self.volumes = os.path.join(self.tmp, "volumes")
        with open(self.volumes, "w") as f:
            f.write("".join(v + "\n" for v in volumes))
        self.calls = os.path.join(self.tmp, "calls")
        open(self.calls, "w").close()
        self.log = os.path.join(self.tmp, "log")
        self.volume_ls_fails = volume_ls_fails

    def env(self, **extra):
        return {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "HOME": os.path.join(self.tmp, "home"), "DATA_DIR": self.data,
            "LOG_FILE": self.log, "SCRIPTS_DIR": SCRIPTS_DIR, "PY": sys.executable,
            "CALLS": self.calls, "TOR_FS": self.tor_fs, "VOLUMES": self.volumes,
            "VOLUME_LS_FAILS": "yes" if self.volume_ls_fails else "",
            "DOCKER_HOST": NO_DOCKER, "TMPDIR": self.tmp, **extra}

    def calls_list(self):
        with open(self.calls) as f:
            return f.read().splitlines()

    def vanity(self):
        return _tree(os.path.join(self.data, "shared", "vanity-keys"))

    def kept(self):
        root = os.path.join(self.data, "replaced-keys")
        if not os.path.isdir(root):
            return {}
        return {name: _tree(os.path.join(root, name)) for name in os.listdir(root)}

    def staging_left(self):
        return [n for n in os.listdir(self.data) if n.startswith(".key-import-")]

    def config(self):
        with open(os.path.join(self.data, "config")) as f:
            return f.read()

    def pending(self):
        return os.path.exists(os.path.join(self.data, ".import-key-pending"))


class _FakeDocker:
    """key_import's `docker`: the tor container's files from the install's
    TOR_FS (what the stub docker serves key_manager's own reads from)."""

    class _Result:
        def __init__(self, ok, stdout=""):
            self.ok, self.stdout, self.output = ok, stdout, stdout

    def __init__(self, inst):
        self.inst = inst

    def exec(self, container, command, timeout=30, check=False, quiet=False, user=None):
        path = self.inst.tor_fs + command[1]
        if container == "onionpress-tor" and command[0] == "cat" and os.path.isfile(path):
            with open(path) as f:
                return self._Result(True, f.read())
        return self._Result(False)


class _FakeContainers:
    """What replace_key asks of the ContainerManager: volume_state (read
    from the install's volume list; the stub's failing `volume ls` is state
    2) and stop."""

    def __init__(self, inst):
        self.inst = inst

    def volume_state(self, name):
        if self.inst.volume_ls_fails:
            return 2
        with open(self.inst.volumes) as f:
            return 0 if name in f.read().split() else 1

    def stop(self):
        with open(self.inst.calls, "a") as f:
            f.write("stop_containers\n")
        return True


class ReplaceKeyBase(unittest.TestCase):

    def _replace(self, inst, key, release_why=None, **kw):
        """key_import.replace_key against *inst*; *release_why* is what the
        hubs may still hold (a list of {"why": ...} items)."""
        def release(reason):
            with open(inst.calls, "a") as f:
                f.write(f"onionheaven_release {reason}\n")
            return list(release_why or [])
        logged = []
        with mock.patch.dict(os.environ, inst.env()):
            result = key_import.replace_key(
                key, data_dir=inst.data, docker=_FakeDocker(inst),
                containers=_FakeContainers(inst), log=logged.append,
                release=release, hub_held_ok=kw.pop("hub_held_ok", lambda remaining: not remaining),
                linux=False, **kw)
        return result

    def assert_untouched(self, inst, vanity_before):
        calls = inst.calls_list()
        self.assertFalse([c for c in calls if c.startswith("docker volume rm")], calls)
        self.assertNotIn("stop_containers", calls)
        self.assertFalse([c for c in calls if c.startswith("onionheaven_release")], calls)
        self.assertEqual(inst.vanity(), vanity_before)
        self.assertFalse(inst.pending())
        self.assertEqual(inst.staging_left(), [])
        self.assertIn("ADDRESS_PREFIX=op2\n", inst.config())


class TestReplaceKey(ReplaceKeyBase):
    """replace_onion_key's tests, from when it was bash twice: the same
    cases against key_import.replace_key."""

    def test_a_refused_key_changes_nothing(self):
        for name, key in (("not base32", "this is not a key!"),
                          ("13 bytes", b32(b"\x01" * 13)),
                          ("bit 255 set", b32(TOP_BIT)),
                          ("zero scalar", b32(bytes(32) + NEW[32:]))):
            for vanity in (None, OLD):
                with self.subTest(name, vanity=vanity is not None):
                    inst = _Install(self, vanity=vanity)
                    before = inst.vanity()
                    r = self._replace(inst, key)
                    self.assertEqual(r.status, "failed")
                    self.assertTrue(r.error, r)
                    self.assertNotIn("Error:", r.error)
                    self.assertFalse(r.stopped)
                    self.assertEqual(inst.kept(), {})
                    self.assert_untouched(inst, before)
                    self.assertEqual(inst.calls_list(), [], "nothing was even asked of Docker")

    def test_a_valid_key_keeps_the_served_one_then_replaces_it(self):
        inst = _Install(self)
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "replaced", r)
        self.assertEqual(r.address, NEW_ADDR)
        self.assertTrue(r.stopped)
        kept = inst.kept()
        self.assertEqual(len(kept), 1, kept)
        (name, files), = kept.items()
        self.assertTrue(name.startswith(OLD_ADDR + "-"), name)
        self.assertEqual(files["hs_ed25519_secret_key"], key_manager.CTOR_HEADER_SECRET + OLD)
        self.assertEqual(files["hostname"], (OLD_ADDR + "\n").encode())
        self.assertEqual(key_manager.parse_openssh_key(files["ks_hs_id.ed25519_expanded_private"])[0], OLD)
        kept_dir = os.path.join(inst.data, "replaced-keys", name)
        self.assertEqual(r.kept, [kept_dir])
        for path, mode in ((os.path.join(inst.data, "replaced-keys"), 0o700), (kept_dir, 0o700),
                           (os.path.join(kept_dir, "hs_ed25519_secret_key"), 0o600)):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode, path)
        # The new key is in place, the rest is left to the next start.
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])
        self.assertEqual(key_manager.staged_key_address(
            os.path.join(inst.data, "shared", "vanity-keys", NEW_ADDR)), NEW_ADDR)
        self.assertTrue(inst.pending())
        self.assertEqual(inst.staging_left(), [])
        self.assertIn(f"ADDRESS_PREFIX={NEW_ADDR[:3]}\n", inst.config())
        calls = inst.calls_list()
        self.assertFalse([c for c in calls if c.startswith("docker volume rm")], calls)
        # Kept before the hubs are asked, asked before anything stops.
        reads = [i for i, c in enumerate(calls) if "hs_ed25519_secret_key" in c]
        release = calls.index("onionheaven_release key-import")
        self.assertTrue(reads and max(reads) < release, calls)
        self.assertLess(release, calls.index("stop_containers"))

    def test_a_vanity_key_is_kept_when_tor_does_not_answer(self):
        inst = _Install(self, served=None, vanity=OLD)
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "replaced", r)
        self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])

    def test_both_kept_when_they_differ(self):
        other = expanded_key(b"pending import")
        inst = _Install(self, served=OLD, vanity=other)
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "replaced", r)
        self.assertEqual(sorted(n.rsplit("-", 1)[0] for n in inst.kept()),
                         sorted([OLD_ADDR, address_of(other)]))

    def test_no_copy_no_import(self):
        cases = {
            # A key volume, but neither tor nor vanity-keys to read it from.
            "key volume": dict(served=None),
            # Docker cannot say which volumes there are.
            "docker down": dict(served=None, volume_ls_fails=True),
            # C Tor's state alone holds a key.
            "tor-state": dict(served=None, volumes=("onionpress-tor-state",)),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                inst = _Install(self, **kw)
                before = inst.vanity()
                r = self._replace(inst, b32(NEW))
                self.assertEqual(r.status, "failed")
                self.assertIn("could not be read", r.error)
                self.assert_untouched(inst, before)

    def test_a_copy_that_cannot_be_written_stops_it(self):
        inst = _Install(self)
        with open(os.path.join(inst.data, "replaced-keys"), "w") as f:
            f.write("not a folder")
        before = inst.vanity()
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "failed")
        self.assertIn("could not be kept", r.error)
        self.assert_untouched(inst, before)

    def test_an_unreadable_served_key_stops_it(self):
        inst = _Install(self)
        with open(inst.tor_fs + TOR_HS + "/hs_ed25519_secret_key", "wb") as f:
            f.write(b"garbage")
        before = inst.vanity()
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "failed")
        self.assert_untouched(inst, before)

    def test_a_fresh_install_has_nothing_to_keep(self):
        inst = _Install(self, served=None, volumes=())
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "replaced", r)
        self.assertEqual(r.kept, [])
        self.assertEqual(inst.kept(), {})
        self.assertTrue(inst.pending())

    def test_the_same_key_is_nothing_to_do(self):
        inst = _Install(self, served=NEW)
        before = inst.vanity()
        r = self._replace(inst, b32(NEW))
        self.assertEqual(r.status, "unchanged")
        self.assertEqual(r.address, NEW_ADDR)
        self.assertEqual(inst.kept(), {})
        self.assert_untouched(inst, before)

    def test_a_no_at_the_hub_question_stops_before_anything_is_replaced(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        r = self._replace(inst, b32(NEW), release_why=[{"why": "unanswered"}])
        self.assertEqual(r.status, "failed")
        self.assertIn("OnionHeaven", r.error)
        self.assertFalse(r.stopped)
        calls = inst.calls_list()
        self.assertNotIn("stop_containers", calls)
        self.assertEqual(inst.vanity(), before)
        self.assertFalse(inst.pending())
        self.assertEqual(inst.staging_left(), [])
        self.assertEqual(len(inst.kept()), 1)

    def test_a_replaced_identity_ends_a_pending_volume_migration(self):
        inst = _Install(self)
        marker = os.path.join(inst.data, ".key-volume-migration-pending")
        open(marker, "w").close()
        self.assertEqual(self._replace(inst, b32(NEW)).status, "replaced")
        self.assertFalse(os.path.exists(marker))

    def test_the_heartbeat_client_is_stopped_before_the_hubs_and_back_on_a_no(self):
        for answer, restarted in (([], False), ([{"why": "unanswered"}], True)):
            with self.subTest(restarted=restarted):
                inst = _Install(self, vanity=OLD)
                events = []
                with mock.patch.object(key_import.heartbeat_unit, "stop",
                                       lambda log: events.append("stop")), \
                        mock.patch.object(key_import.heartbeat_unit, "start",
                                          lambda log: events.append("start")), \
                        mock.patch.dict(os.environ, inst.env()):
                    key_import.replace_key(
                        b32(NEW), data_dir=inst.data, docker=_FakeDocker(inst),
                        containers=_FakeContainers(inst), log=lambda m: None,
                        release=lambda reason: events.append("release") or answer,
                        hub_held_ok=lambda remaining: not remaining, linux=True)
                self.assertEqual(events, ["stop", "release"] + (["start"] if restarted else []))


class TestPrefixRule(unittest.TestCase):
    """One prefix rule (the launchers had three): the configured length,
    3 when there is none, held to 2-5."""

    def test_the_length_follows_the_configured_prefix(self):
        for current, length in (("", 3), ("a", 3), ("ab", 2), ("abcd", 4), ("abcde", 5),
                                ("abcdefgh", 5)):
            with self.subTest(current=current):
                self.assertEqual(key_import.prefix_for_address(NEW_ADDR, current),
                                 NEW_ADDR[:length])

    def test_follow_address_prefix_writes_the_config(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "config")
        with open(path, "w") as f:
            f.write("FOO=bar\nADDRESS_PREFIX=abcd\n")
        key_import.follow_address_prefix(path, NEW_ADDR, lambda m: None)
        with open(path) as f:
            self.assertEqual(f.read(), f"FOO=bar\nADDRESS_PREFIX={NEW_ADDR[:4]}\n")
        os.remove(path)
        key_import.follow_address_prefix(path, NEW_ADDR, lambda m: None)
        with open(path) as f:
            self.assertEqual(f.read(), f"ADDRESS_PREFIX={NEW_ADDR[:3]}\n")


class _Args:
    def __init__(self, key=None, file=None, check=False, json=False, confirmed=()):
        self.key, self.file, self.check, self.json = key, file, check, json
        self.confirmed = list(confirmed)


class TestRun(ReplaceKeyBase):
    """`onionpress import-key` (key_import.run): the check/confirm
    protocol around replace_key. The Mac's menubar hands it the page's key
    on stdin ("-"), so a question cannot be asked of a terminal there."""

    def _run(self, inst, args, *, interactive=False, answers="", environ=None,
             release_why=None, linux=False):
        out, err = io.StringIO(), io.StringIO()

        def release(reason):
            with open(inst.calls, "a") as f:
                f.write(f"onionheaven_release {reason}\n")
            return list(release_why or [])

        paths = mock.Mock(data_dir=inst.data)
        env = dict(environ or {})
        with mock.patch.dict(os.environ, inst.env()):
            rc = key_import.run(
                args, paths=paths, docker=_FakeDocker(inst),
                containers=_FakeContainers(inst), log=lambda m: None,
                release=release, linux=linux, interactive=interactive,
                environ=env, out=out, err=err, stdin=io.StringIO(answers))
        return rc, out.getvalue(), err.getvalue()

    def test_without_a_confirmation_nothing_is_replaced(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        rc, out, err = self._run(inst, _Args(b32(NEW)))
        self.assertEqual(rc, confirm.EXIT_REFUSED)
        self.assertIn("Cancelled.", out)
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.kept(), {})
        self.assertNotIn("stop_containers", inst.calls_list())
        self.assertFalse(inst.pending())

    def test_confirmed_it_keeps_the_old_key_and_replaces_it(self):
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args(b32(NEW), confirmed=["address-change"]))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Imported key for: " + NEW_ADDR, out)
        self.assertIn("Launch OnionPress to use this address.", out)
        self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])
        self.assertTrue(inst.pending())
        self.assertIn(f"ADDRESS_PREFIX={NEW_ADDR[:3]}\n", inst.config())
        calls = inst.calls_list()
        self.assertLess(calls.index("onionheaven_release key-import"),
                        calls.index("stop_containers"))

    def test_the_old_variable_and_the_env_stand_in_for_the_flag(self):
        for env in ({"ONIONPRESS_IMPORT_KEY_CONFIRMED": "yes"},
                    {"ONIONPRESS_CONFIRMED": "reset,address-change"}):
            with self.subTest(env=env):
                inst = _Install(self, vanity=OLD)
                rc, out, err = self._run(inst, _Args(b32(NEW)), environ=env)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])

    def test_only_yes_confirms_the_old_variable(self):
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args(b32(NEW)),
                                 environ={"ONIONPRESS_IMPORT_KEY_CONFIRMED": "1"})
        self.assertEqual(rc, confirm.EXIT_REFUSED)
        self.assertEqual(inst.kept(), {})

    def test_a_terminal_is_asked_and_only_y_confirms(self):
        for answer, replaced in (("y\n", True), ("Y\n", True), ("n\n", False), ("", False),
                                 ("yes\n", False)):
            with self.subTest(answer=answer):
                inst = _Install(self, vanity=OLD)
                rc, out, err = self._run(inst, _Args(b32(NEW)), interactive=True,
                                         answers=answer, linux=True)
                self.assertIn("Continue? [y/N] ", out)
                self.assertEqual(rc, 0 if replaced else 1, out + err)
                self.assertEqual(inst.pending(), replaced)
                if not replaced:
                    self.assertIn("Cancelled.", out)
                    self.assertEqual(inst.kept(), {})

    def test_a_refused_key_is_refused_before_the_question(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        rc, out, err = self._run(inst, _Args(b32(TOP_BIT), confirmed=["address-change"]))
        self.assertEqual(rc, 1)
        self.assertIn("top bit", err)
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.calls_list(), [])

    def test_check_json_asks_without_changing_anything(self):
        """The check/confirm protocol (onionpress.confirm): --check --json
        answers the question, exit 4, and replaces nothing."""
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        rc, out, err = self._run(inst, _Args(b32(NEW), check=True, json=True))
        self.assertEqual(rc, confirm.EXIT_CONFIRM, out + err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                         {"confirm": "address-change", "current": OLD_ADDR, "new": NEW_ADDR})
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.kept(), {})
        self.assertEqual(inst.calls_list(), [])
        # The same key: nothing to ask
        rc, out, err = self._run(inst, _Args(b32(OLD), check=True, json=True))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                         {"ok": True, "address": OLD_ADDR, "unchanged": True})
        # No key volume, nothing to replace: nothing to ask
        inst = _Install(self, served=None, volumes=())
        rc, out, err = self._run(inst, _Args(b32(NEW), check=True, json=True))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                         {"ok": True, "address": NEW_ADDR})

    def test_a_bad_check_answers_an_error(self):
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args("not a key!", check=True, json=True))
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.strip().splitlines()[-1]))

    def test_the_key_comes_from_stdin_or_a_file(self):
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args("-", confirmed=["address-change"]),
                                 answers=b32(NEW) + "\n")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])
        inst = _Install(self, vanity=OLD)
        keyfile = os.path.join(inst.tmp, "hs_ed25519_secret_key")
        with open(keyfile, "wb") as f:
            f.write(key_manager.CTOR_HEADER_SECRET + NEW)
        rc, out, err = self._run(inst, _Args(file=keyfile, confirmed=["address-change"]))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])
        rc, out, err = self._run(inst, _Args(file=os.path.join(inst.tmp, "missing")))
        self.assertEqual(rc, 1)
        rc, out, err = self._run(inst, _Args())
        self.assertEqual(rc, 0)
        self.assertIn("Usage: onionpress import-key", out)

    def test_a_hub_that_keeps_the_key_asks_a_terminal_and_refuses_a_script(self):
        why = [{"why": "unanswered"}]
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args(b32(NEW), confirmed=["address-change"]),
                                 release_why=why)
        self.assertEqual(rc, 1)
        self.assertIn("Cancelled: an OnionHeaven hub", err)
        self.assertFalse(inst.pending())
        self.assertNotIn("stop_containers", inst.calls_list())
        # --confirmed hub-holds-key, ONIONPRESS_YES and a "y" go on
        for kw in (dict(args=_Args(b32(NEW), confirmed=["address-change", "hub-holds-key"])),
                   dict(args=_Args(b32(NEW), confirmed=["address-change"]),
                        environ={"ONIONPRESS_YES": "true"}),
                   dict(args=_Args(b32(NEW), confirmed=["address-change"]),
                        interactive=True, answers="y\n")):
            with self.subTest(kw=sorted(kw)):
                inst = _Install(self, vanity=OLD)
                rc, out, err = self._run(inst, release_why=why, **kw)
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(inst.pending())
        # nothing a user could take back (a hub that cannot be reached any more): on
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args(b32(NEW), confirmed=["address-change"]),
                                 release_why=[{"why": "key-gone"}])
        self.assertEqual(rc, 0, out + err)

    def test_a_failure_after_the_stop_says_so(self):
        inst = _Install(self, vanity=OLD)
        with mock.patch.object(key_import.os, "rename", side_effect=OSError("no")):
            rc, out, err = self._run(inst, _Args(b32(NEW), confirmed=["address-change"]))
        self.assertEqual(rc, 1)
        self.assertIn("OnionPress was stopped; launch it again.", err)
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [OLD_ADDR])

    def test_linux_words_its_ending(self):
        inst = _Install(self, vanity=OLD)
        rc, out, err = self._run(inst, _Args(b32(NEW), confirmed=["address-change"]), linux=True)
        self.assertEqual(rc, 0)
        self.assertIn("Run 'onionpress restart'", out)


class TestLauncherDelegation(unittest.TestCase):
    """Both launchers' import_key is one line: the CLI's import-key. The
    settings page never replaces the key on Linux."""

    def test_both_launchers_delegate(self):
        for rel, runner in ((MAC, "bundled_python -m onionpress.cli"), (LINUX, "op_py")):
            with self.subTest(rel):
                text = _read(rel)
                body = _shell_function(text, "import_key")
                self.assertIn('import-key "$@"', body)
                self.assertIn(runner, body)
                self.assertLess(len(body.splitlines()), 6)
                for gone in ("replace_onion_key", "set_address_prefix_for", "REPLACE_KEY_"):
                    self.assertNotIn(gone, text)
        # The Mac ran a bundled copy of key_manager.py for it; the Linux
        # restore still does (the backup's ctor-to-arti, #406).
        self.assertNotIn("key_manager.py", _read(MAC))

    def test_the_settings_page_never_replaces_the_key(self):
        text = _read(LINUX)
        # The page's requests are the shared handler's (issue #366): the
        # Linux launcher has no import-key-file branch, and the handler
        # refuses the import on Linux before any launcher runs.
        self.assertNotIn("\n                import-key-file)\n", text)
        self.assertNotIn("\n                generate-vanity)\n", text)
        handler = _read("src/onionpress/handle_action.py")
        branch = handler[handler.index("def _import_key_file("):handler.index("def _import_key(")]
        self.assertIn("IMPORT_REFUSED_LINUX_MSG", branch)
        self.assertNotIn("replace_onion_key", handler)
        self.assertNotIn("volume", branch)

    def test_the_order_of_replace_key(self):
        """No volume removal before the start (the pending-import path
        counts only a confirmed removal), and the steps in their order."""
        src = _read("src/onionpress/key_import.py")
        body = src[src.index("def replace_key("):src.index("def _log_file(")]
        self.assertNotIn("volume rm", body)
        self.assertNotIn('"rm"', body)
        order = ["import_key_b32", "staged_key_address", "keep_current_keys",
                 "release(", "containers.stop()", "os.rename(staged_keys, vanity)",
                 "IMPORT_KEY_PENDING", "follow_address_prefix"]
        positions = [body.index(s) for s in order]
        self.assertEqual(positions, sorted(positions))

    def test_the_cli_asks_before_replacing(self):
        src = _read("src/onionpress/key_import.py")
        body = src[src.index("def run("):]
        self.assertLess(body.index("decoded_address(key_b32)"),
                        body.index('"address-change" in confirmed'))
        self.assertLess(body.index('"address-change" in confirmed'),
                        body.index("replace_key("))



class TestLinuxSettingsPage(unittest.TestCase):
    """The page's import-key-file on Linux, through the shared handler
    (onionpress.handle_action, issue #366): the page's key in,
    import-result.json out. Anything running in WordPress can ask for it
    and no one at a Linux host is asked, so it is refused whatever the
    key: no launcher runs, nothing is stopped, kept, released or
    replaced."""

    def _run(self, inst, key_data):
        import test_handle_action as tha
        from onionpress import handle_action as ha
        from onionpress.platform import resolve_paths
        docker = tha.FakeDocker(os.path.join(inst.tmp, "sandbox"))
        docker.put("import-key-data", key_data)
        docker.put("requested-action", "import-key-file\n")
        launcher = os.path.join(inst.tmp, "launcher")
        with open(launcher, "w") as f:
            f.write('#!/bin/bash\necho "launcher $*" >> "$CALLS"\n')
        os.chmod(launcher, 0o755)
        host = ha.Host(launcher=launcher, paths=resolve_paths(data_dir=inst.data, app_bundle=""),
                       docker=docker, log=lambda m: None, os_name="linux")
        with mock.patch.dict(os.environ, {"CALLS": inst.calls}):
            result = ha.handle(host)
        self.assertEqual(result, {"handled": "import-key-file"})
        self.assertIsNone(docker.get("import-key-data"))
        return docker.result("import-result.json")

    def _assert_refused(self, inst, key_data):
        before = inst.vanity()
        result = self._run(inst, key_data)
        self.assertIs(result["success"], False)
        self.assertIn("onionpress import-key", result["error"])
        self.assertIn("imported_at", result)
        self.assertEqual(inst.calls_list(), [])
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.kept(), {})
        self.assertFalse(inst.pending())

    def test_garbage_leaves_the_identity_alone(self):
        self._assert_refused(_Install(self, vanity=OLD), "garbage")

    def test_a_valid_key_is_refused_too(self):
        self._assert_refused(_Install(self), b32(NEW))




def _menubar_method(name):
    """A method of the menubar's app class, compiled on its own (the module
    needs AppKit and rumps)."""
    import ast
    src = _read("src/menubar.py")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in src/menubar.py")


class TestMenubarAsksThePersonAtTheMac(unittest.TestCase):
    """The menubar's half of the Settings page's key import (issue #366):
    `onionpress handle-action` stops at {"confirm": "address-change"}
    (tests/test_handle_action.py), and the menubar asks in a native dialog
    naming both addresses, Cancel the default; only Replace runs it again
    with --confirmed address-change, Cancel with --cancel."""

    QUESTION = {"handled": "import-key-file", "confirm": "address-change",
                "current": OLD_ADDR, "new": NEW_ADDR}

    def _app(self, answer):
        import textwrap
        import types
        from onionpress import ui_client
        ns = {"ui_client": ui_client, "rumps": mock.Mock()}
        for name in ("_ask_cli_question", "_ask_settings_page_question"):
            exec(textwrap.dedent(_menubar_method(name)), ns)
        app = types.SimpleNamespace(log=mock.Mock(), show_native_alert=mock.Mock(return_value=answer))
        for name in ("_ask_cli_question", "_ask_settings_page_question"):
            setattr(app, name, types.MethodType(ns[name], app))
        return app

    def test_replace_answers_yes(self):
        app = self._app(answer=1)
        self.assertIs(app._ask_settings_page_question(self.QUESTION), True)
        message = app.show_native_alert.call_args.kwargs["message"]
        self.assertIn(OLD_ADDR, message)
        self.assertIn(NEW_ADDR, message)
        self.assertIn("replace this site's onion service key", message)
        kwargs = app.show_native_alert.call_args.kwargs
        self.assertEqual(kwargs["buttons"][kwargs["default_button"]], "Cancel")
        self.assertEqual(kwargs["cancel_button"], 0)
        self.assertEqual(kwargs["buttons"], ["Cancel", "Replace"])

    def test_cancel_or_no_answer_is_no(self):
        for answer in (0, None):
            with self.subTest(answer=answer):
                app = self._app(answer=answer)
                self.assertIs(app._ask_settings_page_question(self.QUESTION), False)

    def test_an_unknown_question_is_no(self):
        app = self._app(answer=1)
        self.assertIs(app._ask_settings_page_question({"confirm": "format-disk"}), False)
        app.show_native_alert.assert_not_called()

    def test_the_poll_runs_the_answer_back(self):
        # The daemon's tick runs handle-action and puts the question it
        # stops at in its state (onionpress.daemon); the menubar asks and
        # runs the answer back itself.
        daemon = _read("src/onionpress/daemon.py")
        body = daemon[daemon.index("def _settings_page_run(self):"):daemon.index("def _handle_action(self, args):")]
        self.assertIn('self._pending_question = question', body)
        self.assertNotIn("--confirmed", body)
        src = _read("src/menubar.py")
        body = src[src.index("def _answer_settings_page_question(self, question):"):]
        body = body[:body.index("\n    def ")]
        self.assertIn('["--confirmed", question["confirm"]] if answered_yes else ["--cancel"]', body)
        self.assertIn('self.launcher.run_json("handle-action", "--json", *args)', body)
        # The dialog gives up after 5 minutes: no answer is Cancel.
        self.assertIn("max_wait = 300", _menubar_method("show_native_alert"))

    def test_a_refused_key_never_reaches_the_dialog(self):
        """The handler checks the key through `import-key - --check` before
        it keeps the question for the menubar."""
        handler = _read("src/onionpress/handle_action.py")
        body = handler[handler.index("def _import_key(host, key, confirmed):"):handler.index("# -- backup --")]
        check = body.index('["import-key", "-", "--check", "--json"]')
        self.assertLess(check, body.index("_pending_save("))
        self.assertLess(body.index("elif r.returncode != 0:"), body.index('["import-key", "-", "--confirmed", "address-change"]'))
        self.assertNotIn("import-key", _read("src/menubar.py"))


class TestKeepCurrentKeys(unittest.TestCase):
    """key_manager's half, without the launchers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-keep-keys-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "replaced-keys")

    def test_keep_replaced_key(self):
        pub = key_manager.derive_public_key(OLD)
        when = time.mktime((2026, 10, 13, 12, 0, 0, 0, 0, 0))
        first = key_manager.keep_replaced_key(self.root, OLD, pub, when)
        second = key_manager.keep_replaced_key(self.root, OLD, pub, when)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.basename(second).startswith(os.path.basename(first)))
        self.assertEqual(key_manager.staged_key_address(first), OLD_ADDR)
        self.assertEqual(sorted(os.listdir(first)), [
            "hostname", "hs_ed25519_public_key", "hs_ed25519_secret_key",
            "ks_hs_id.ed25519_expanded_private"])
        for name in os.listdir(first):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(first, name)).st_mode), 0o600)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            key_manager.keep_replaced_key(self.root, TOP_BIT, key_manager.derive_public_key(TOP_BIT))
        os.symlink(self.tmp, self.root)
        with self.assertRaises(ValueError):
            key_manager.keep_replaced_key(self.root, OLD, key_manager.derive_public_key(OLD))

    def test_a_failed_write_leaves_no_folder(self):
        with mock.patch.object(key_manager, "_write_new_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                key_manager.keep_replaced_key(self.root, OLD, key_manager.derive_public_key(OLD))
        self.assertEqual(os.listdir(self.root), [])

    def test_keep_current_keys(self):
        vanity = os.path.join(self.tmp, "vanity")
        key_manager.import_key_b32(b32(NEW), vanity)
        served = {key_manager.CTOR_SECRET_PATH: key_manager.CTOR_HEADER_SECRET + OLD}

        def cat(path):
            return (True, served[path]) if path in served else (False, b"No such file")

        with mock.patch.object(key_manager, "_docker_cat", side_effect=cat):
            kept = key_manager.keep_current_keys(self.root, [os.path.join(vanity, NEW_ADDR)])
            self.assertEqual(sorted(key_manager.staged_key_address(k) for k in kept),
                             sorted([OLD_ADDR, NEW_ADDR]))
            # The delivered PEM of the same key is one key, kept once.
            served[key_manager.DELIVERED_KEY_PATH] = key_manager.build_openssh_key(
                OLD, key_manager.derive_public_key(OLD))
            self.assertEqual(len(key_manager.keep_current_keys(self.root)), 1)
        with mock.patch.object(key_manager, "_docker_cat", return_value=(False, b"")):
            self.assertEqual(key_manager.keep_current_keys(self.root), [])


class TestPageText(unittest.TestCase):

    def test_the_settings_page_no_longer_says_forever(self):
        php = _read("app/Resources/plugins/onionpress-settings.php")
        start = php.index("<h2>Import Onion Service Key</h2>")
        section = php[start:php.index("</form>", start)]
        self.assertNotIn("Not reversible", section)
        self.assertIn("replaced-keys", section)


if __name__ == "__main__":
    unittest.main()
