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


class TestLinuxStructure(unittest.TestCase):

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
        # replace_onion_key's one caller: the CLI's import, which asks.
        callers = [line.strip() for line in text.splitlines()
                   if re.search(r"\breplace_onion_key\b", line)
                   and not line.lstrip().startswith("#")
                   and not line.startswith("replace_onion_key()")]
        self.assertEqual(len(callers), 1, callers)
        self.assertIn("replace_onion_key", _shell_function(text, "import_key"))
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


class TestMacImportKey(unittest.TestCase):
    """app/MacOS/onionpress import_key, run for real: the menubar hands it
    the page's key on stdin ("import-key -"), so its own question reads the
    end of the input. It replaces a key only with the menubar's explicit
    ONIONPRESS_IMPORT_KEY_CONFIRMED=yes, and then in replace_onion_key's
    order."""

    PRELUDE = r'''
log() { echo "[log] $*" >> "$LOG_FILE"; }
bundled_python() { "$PY" "$@"; }
stop_containers() { echo "stop_containers" >> "$CALLS"; }
onionheaven_release() {
    echo "onionheaven_release $* yes=${ONIONPRESS_YES:-}" >> "$CALLS"
    return "${RELEASE_RC:-0}"
}
'''
    if sys.platform != "darwin":
        # The Mac launcher's BSD `sed -i ''`, on GNU sed.
        PRELUDE += ('sed() { if [ "$1" = -i ] && [ -z "$2" ]; then shift 2; '
                    'command sed -i "$@"; else command sed "$@"; fi; }\n')

    def _import(self, inst, key, **env):
        text = _read(MAC)
        funcs = "\n".join(_shell_function(text, n) for n in (
            "is_onion_address", "vanity_key_dir", "docker_volume_state",
            "replace_onion_key", "set_address_prefix_for", "import_key"))
        extra = env.pop("ONIONPRESS_IMPORT_KEY_ARGS", "")
        first = env.pop("ONIONPRESS_IMPORT_KEY_FLAGS_FIRST", "")
        script = "set -e\n" + self.PRELUDE + funcs + f"\nimport_key {first} - {extra}\n"
        return subprocess.run(["bash", "-c", script], input=key + "\n",
                              capture_output=True, text=True, timeout=120,
                              env=inst.env(**env))

    def test_without_the_menubars_confirmation_nothing_is_replaced(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        r = self._import(inst, b32(NEW))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)    # refused, as every unconfirmed run
        self.assertIn("Cancelled.", r.stdout)
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.kept(), {})
        self.assertNotIn("stop_containers", inst.calls_list())
        self.assertFalse(inst.pending())

    def test_confirmed_it_keeps_the_old_key_and_replaces_it(self):
        inst = _Install(self, vanity=OLD)
        r = self._import(inst, b32(NEW), ONIONPRESS_IMPORT_KEY_CONFIRMED="yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Confirmed at this Mac.", r.stdout)
        self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])
        self.assertEqual(os.listdir(os.path.join(inst.data, "shared", "vanity-keys")), [NEW_ADDR])
        self.assertTrue(inst.pending())
        self.assertIn(f"ADDRESS_PREFIX={NEW_ADDR[:3]}\n", inst.config())
        calls = inst.calls_list()
        self.assertFalse([c for c in calls if c.startswith("docker volume rm")], calls)
        self.assertLess(calls.index("onionheaven_release key-import yes="),
                        calls.index("stop_containers"))

    def test_only_yes_confirms(self):
        inst = _Install(self, vanity=OLD)
        r = self._import(inst, b32(NEW), ONIONPRESS_IMPORT_KEY_CONFIRMED="1")
        self.assertIn("Cancelled.", r.stdout)
        self.assertEqual(inst.kept(), {})

    def test_a_refused_key_is_refused_before_the_question(self):
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        r = self._import(inst, b32(TOP_BIT), ONIONPRESS_IMPORT_KEY_CONFIRMED="yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("top bit", r.stderr)
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.calls_list(), [])

    def test_check_json_asks_without_changing_anything(self):
        """The check/confirm protocol (onionpress.confirm): --check --json
        answers the question, exit 4, and replaces nothing."""
        inst = _Install(self, vanity=OLD)
        before = inst.vanity()
        r = self._import(inst, b32(NEW), ONIONPRESS_IMPORT_KEY_ARGS="--check --json")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                         {"confirm": "address-change", "current": OLD_ADDR, "new": NEW_ADDR})
        self.assertEqual(inst.vanity(), before)
        self.assertEqual(inst.kept(), {})
        # Only the read that decides whether there is a key to replace
        self.assertEqual([c for c in inst.calls_list() if not c.startswith("docker volume ls")], [])
        # The flags may come before the key
        r = self._import(inst, b32(NEW), ONIONPRESS_IMPORT_KEY_FLAGS_FIRST="--check --json")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["confirm"], "address-change")
        # The same key: nothing to ask
        r = self._import(inst, b32(OLD), ONIONPRESS_IMPORT_KEY_ARGS="--check --json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                         {"ok": True, "address": OLD_ADDR, "unchanged": True})

    def test_confirmed_flag_and_env_stand_in_for_the_old_variable(self):
        for env in ({"ONIONPRESS_IMPORT_KEY_ARGS": "--confirmed address-change --json"},
                    {"ONIONPRESS_CONFIRMED": "reset,address-change"}):
            with self.subTest(env=env):
                inst = _Install(self, vanity=OLD)
                r = self._import(inst, b32(NEW), **env)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("Confirmed at this Mac.", r.stdout)
                self.assertEqual([n.rsplit("-", 1)[0] for n in inst.kept()], [OLD_ADDR])
                if "ONIONPRESS_IMPORT_KEY_ARGS" in env:
                    self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                                     {"ok": True, "address": NEW_ADDR})

    def test_the_two_launchers_replace_alike(self):
        linux = _shell_function(_read(LINUX), "replace_onion_key")
        mac = _shell_function(_read(MAC), "replace_onion_key")
        self.assertEqual(mac.replace("bundled_python ", "python3 "), linux)
        body = _shell_function(_read(MAC), "import_key")
        self.assertLess(body.index("decode-key -"),
                        body.index('if [ "${ONIONPRESS_IMPORT_KEY_CONFIRMED:-}" = "yes" ]'))
        self.assertLess(body.index("read -r confirm"), body.index("replace_onion_key"))
        self.assertNotIn("docker volume rm", body)
        self.assertNotIn("rm -rf", body)


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
        ns = {}
        for name in ("_ask_to_replace_address", "_ask_cli_question", "_ask_settings_page_question"):
            exec(textwrap.dedent(_menubar_method(name)), ns)
        app = types.SimpleNamespace(log=mock.Mock(), show_native_alert=mock.Mock(return_value=answer))
        for name in ("_ask_to_replace_address", "_ask_cli_question", "_ask_settings_page_question"):
            setattr(app, name, types.MethodType(ns[name], app))
        return app

    def test_replace_answers_yes(self):
        app = self._app(answer=1)
        self.assertIs(app._ask_settings_page_question(self.QUESTION), True)
        title, message = app.show_native_alert.call_args.args[:2]
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
        src = _read("src/menubar.py")
        body = src[src.index("def _settings_page_run(self):"):src.index("def _handle_action(self, args):")]
        self.assertIn('["--confirmed", result["confirm"]] if answered_yes else ["--cancel"]', body)
        self.assertIn('[self.launcher_script, "handle-action", "--json", *args]', src)
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
