#!/usr/bin/env python3
"""A key import never destroys the site's identity before it has a new one.

The Linux Settings page's import-key-file and generate-vanity released the
OnionHeaven hubs, stopped OnionPress, removed the key volumes and
vanity-keys/ and only then decoded the key: garbage meant a first run and
a new address, the old identity gone for good. Every import now goes
through the launchers' replace_onion_key:

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

from onionpress import key_manager  # noqa: E402

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


# Stand-ins for what replace_onion_key calls outside itself, recording the
# order (into the same calls file as docker).
LINUX_PRELUDE = r'''
log() { echo "[log] $*" >> "$LOG_FILE"; }
python3() { "$PY" "$@"; }
stop_containers() { echo "stop_containers" >> "$CALLS"; }
start_heartbeat_client() { echo "start_heartbeat_client" >> "$CALLS"; }
onionheaven_release() {
    echo "onionheaven_release $* yes=${ONIONPRESS_YES:-}" >> "$CALLS"
    return "${RELEASE_RC:-0}"
}
'''
if sys.platform == "darwin":
    # The Linux launcher's GNU `sed -i`, on BSD sed.
    LINUX_PRELUDE += ('sed() { if [ "$1" = -i ]; then shift; command sed -i "" "$@"; '
                      'else command sed "$@"; fi; }\n')


def _linux_functions():
    text = _read(LINUX)
    return "\n".join(_shell_function(text, n) for n in (
        "is_onion_address", "vanity_key_dir", "docker_volume_state",
        "replace_onion_key", "set_address_prefix_for"))


class TestLinuxReplaceOnionKey(unittest.TestCase):

    def _replace(self, inst, key, **env):
        script = ("set -e\n" + LINUX_PRELUDE + _linux_functions() + "\n"
                  'rc=0; replace_onion_key "$KEY" || rc=$?\n'
                  'echo "rc=$rc"\n'
                  'echo "address=$REPLACE_KEY_ADDRESS"\n'
                  'echo "stopped=$REPLACE_KEY_STOPPED"\n'
                  'echo "error=$REPLACE_KEY_ERROR"\n'
                  'printf "kept=%s\\n" "$REPLACE_KEY_KEPT"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           timeout=120, env=inst.env(KEY=key, **env))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        return out, r

    def assert_untouched(self, inst, vanity_before):
        calls = inst.calls_list()
        self.assertFalse([c for c in calls if c.startswith("docker volume rm")], calls)
        self.assertNotIn("stop_containers", calls)
        self.assertFalse([c for c in calls if c.startswith("onionheaven_release")], calls)
        self.assertEqual(inst.vanity(), vanity_before)
        self.assertFalse(inst.pending())
        self.assertEqual(inst.staging_left(), [])
        self.assertIn("ADDRESS_PREFIX=op2\n", inst.config())

    def test_a_refused_key_changes_nothing(self):
        for name, key in (("not base32", "this is not a key!"),
                          ("13 bytes", b32(b"\x01" * 13)),
                          ("bit 255 set", b32(TOP_BIT)),
                          ("zero scalar", b32(bytes(32) + NEW[32:]))):
            for vanity in (None, OLD):
                with self.subTest(name, vanity=vanity is not None):
                    inst = _Install(self, vanity=vanity)
                    before = inst.vanity()
                    out, _ = self._replace(inst, key)
                    self.assertEqual(out["rc"], "1")
                    self.assertTrue(out["error"], out)
                    self.assertNotIn("Error:", out["error"])
                    self.assertEqual(inst.kept(), {})
                    self.assert_untouched(inst, before)
                    self.assertEqual(inst.calls_list(), [], "nothing was even asked of Docker")

    def test_a_valid_key_keeps_the_served_one_then_replaces_it(self):
        inst = _Install(self)
        out, r = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "0", r.stdout + r.stderr)
        self.assertEqual(out["address"], NEW_ADDR)
        self.assertEqual(out["stopped"], "yes")
        kept = inst.kept()
        self.assertEqual(len(kept), 1, kept)
        (name, files), = kept.items()
        self.assertTrue(name.startswith(OLD_ADDR + "-"), name)
        self.assertEqual(files["hs_ed25519_secret_key"], key_manager.CTOR_HEADER_SECRET + OLD)
        self.assertEqual(files["hostname"], (OLD_ADDR + "\n").encode())
        self.assertEqual(key_manager.parse_openssh_key(files["ks_hs_id.ed25519_expanded_private"])[0], OLD)
        kept_dir = os.path.join(inst.data, "replaced-keys", name)
        self.assertEqual(out["kept"], kept_dir)
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
        release = calls.index("onionheaven_release key-import yes=")
        self.assertTrue(reads and max(reads) < release, calls)
        self.assertLess(release, calls.index("stop_containers"))

    def test_a_vanity_key_is_kept_when_tor_does_not_answer(self):
        inst = _Install(self, served=None, vanity=OLD)
        out, r = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "0", r.stdout + r.stderr)
        self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])

    def test_both_kept_when_they_differ(self):
        other = expanded_key(b"pending import")
        inst = _Install(self, served=OLD, vanity=other)
        out, r = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "0", r.stdout + r.stderr)
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
                out, _ = self._replace(inst, b32(NEW))
                self.assertEqual(out["rc"], "1")
                self.assertIn("could not be read", out["error"])
                self.assert_untouched(inst, before)

    def test_a_copy_that_cannot_be_written_stops_it(self):
        inst = _Install(self)
        with open(os.path.join(inst.data, "replaced-keys"), "w") as f:
            f.write("not a folder")
        before = inst.vanity()
        out, _ = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "1")
        self.assertIn("could not be kept", out["error"])
        self.assert_untouched(inst, before)

    def test_an_unreadable_served_key_stops_it(self):
        inst = _Install(self)
        with open(inst.tor_fs + TOR_HS + "/hs_ed25519_secret_key", "wb") as f:
            f.write(b"garbage")
        before = inst.vanity()
        out, _ = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "1")
        self.assert_untouched(inst, before)

    def test_a_fresh_install_has_nothing_to_keep(self):
        inst = _Install(self, served=None, volumes=())
        out, r = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "0", r.stdout + r.stderr)
        self.assertEqual(out["kept"], "")
        self.assertEqual(inst.kept(), {})
        self.assertTrue(inst.pending())

    def test_the_same_key_is_nothing_to_do(self):
        inst = _Install(self, served=NEW)
        before = inst.vanity()
        out, _ = self._replace(inst, b32(NEW))
        self.assertEqual(out["rc"], "2")
        self.assertEqual(out["address"], NEW_ADDR)
        self.assertEqual(inst.kept(), {})
        self.assert_untouched(inst, before)

    def test_a_no_at_the_hub_question_stops_before_anything_is_replaced(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        out, _ = self._replace(inst, b32(NEW), RELEASE_RC="1")
        self.assertEqual(out["rc"], "1")
        self.assertIn("OnionHeaven", out["error"])
        calls = inst.calls_list()
        self.assertNotIn("stop_containers", calls)
        self.assertEqual(inst.vanity(), before)
        self.assertFalse(inst.pending())
        self.assertEqual(inst.staging_left(), [])
        self.assertEqual(len(inst.kept()), 1)


class TestLinuxSettingsPage(unittest.TestCase):
    """The handle-action import-key-file branch, run for real: the page's
    key in, import-result.json out."""

    PRELUDE = LINUX_PRELUDE + r'''
setup_db_passwords() { :; }
start_containers() { echo "start_containers" >> "$CALLS"; }
wait_for_services() { :; }
write_status() { :; }
wp_exec() {
    case "$1" in
        cat) cat "$PAGE/$(basename "$2")" 2>/dev/null ;;
        rm) rm -f "$PAGE/$(basename "$3")" ;;
    esac
}
wp_put() { cat > "$PAGE/$(basename "$1")"; }
'''

    def _run(self, inst, key_data):
        page = os.path.join(inst.tmp, "page")
        os.makedirs(page, exist_ok=True)
        with open(os.path.join(page, "import-key-data"), "w") as f:
            f.write(key_data)
        text = _read(LINUX)
        script = ("set -e\n" + self.PRELUDE + _linux_functions() + "\n"
                  + _shell_function(text, "write_key_result") + "\n"
                  + 'handle() {\n    local action=import-key-file\n    case "$action" in\n'
                  + _settings_branch(text, "import-key-file")
                  + "\n                    ;;\n    esac\n}\nhandle\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           timeout=120, env=inst.env(PAGE=page))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(page, "import-key-data")))
        with open(os.path.join(page, "import-result.json")) as f:
            return json.load(f)

    def test_garbage_leaves_the_identity_alone(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        result = self._run(inst, "garbage")
        self.assertIs(result["success"], False)
        self.assertIn("base32", result["error"])
        self.assertIn("imported_at", result)
        calls = inst.calls_list()
        self.assertFalse([c for c in calls if c.startswith("docker volume")], calls)
        self.assertNotIn("stop_containers", calls)
        self.assertNotIn("start_containers", calls)
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.kept(), {})
        self.assertFalse(inst.pending())

    def test_a_valid_key_keeps_the_old_one(self):
        inst = _Install(self)
        result = self._run(inst, b32(NEW))
        self.assertEqual((result["success"], result["address"]), (True, NEW_ADDR))
        self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])
        calls = inst.calls_list()
        self.assertIn("onionheaven_release key-import yes=true", calls)
        self.assertLess(calls.index("stop_containers"), calls.index("start_containers"))
        self.assertIn("start_heartbeat_client", calls)
        self.assertTrue(inst.pending())


class TestLinuxStructure(unittest.TestCase):

    def test_settings_page_paths_go_through_replace_onion_key(self):
        text = _read(LINUX)
        for label in ("generate-vanity", "import-key-file"):
            with self.subTest(label):
                branch = _settings_branch(text, label)
                self.assertIn("ONIONPRESS_YES=true replace_onion_key", branch)
                self.assertNotIn("docker volume rm", branch)
                self.assertNotIn("vanity-keys", branch)
                self.assertNotIn("onionheaven_release", branch)
                self.assertIn("write_key_result", branch)
                self.assertIn("start_heartbeat_client", branch)
        self.assertNotIn("import_key_noninteractive", text)

    def test_the_cli_asks_first(self):
        body = _shell_function(_read(LINUX), "import_key")
        self.assertLess(body.index("decode-key -"), body.index("read -r confirm"))
        self.assertLess(body.index("read -r confirm"), body.index("replace_onion_key"))
        self.assertNotIn("docker volume rm", body)
        self.assertNotIn("rm -rf", body)

    def test_no_volume_removal_before_the_start(self):
        """replace_onion_key leaves the key volumes to start_containers'
        pending-import path, which only counts a confirmed removal."""
        body = _shell_function(_read(LINUX), "replace_onion_key")
        self.assertNotIn("volume rm", body)
        order = ["import-key -", "staged-address", "keep-current",
                 "onionheaven_release key-import", "stop_containers",
                 'mv "$staging/vanity-keys" "$DATA_DIR/shared/vanity-keys"',
                 'touch "$DATA_DIR/.import-key-pending"', "set_address_prefix_for"]
        positions = [body.index(s) for s in order]
        self.assertEqual(positions, sorted(positions))


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
