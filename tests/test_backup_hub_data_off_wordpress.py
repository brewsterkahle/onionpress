#!/usr/bin/env python3
"""An OnionHeaven hub's registrants' keys never pass through WordPress in a
backup.

On a hub, create_backup stages the registry and every registrant's onion
service key. The Settings page's backup is handed to the WordPress container
for download, and its password is the WordPress admin password, which the
page writes to the shared volume in plain text: code running in a hub's
WordPress had every registrant's key as soon as the owner made a backup
there. The Settings page's restore has the same shape: the upload and its
password both come through WordPress, and code running there can make the
file itself.

- The Settings page's backup (Mac handler, Linux branch) never holds the
  hub data (create_backup(include_hub_data=False), create_backup
  --without-hub-data), and on a hub or on OnionHome it is refused outright,
  before anything is copied: it would carry the node's own onion service
  key, with which code in WordPress could run a rival hub (or answer name
  lookups). The page shows why. Backups made from the app or the CLI keep
  the hub data (sealed to the hub's recovery key when one is set up:
  tests/test_hub_backup_sealed.py). OnionHome's name registry (no secret in
  it) stays in those.
- The Settings page's restore refuses a backup holding registrants' keys
  (plain or sealed) or OnionHome's name registry before anything is
  touched, and never writes either volume (a registry alone, as every
  Linux backup through 2.5.2 carries, is left out).

No docker: the container side is a stub script on PATH (the onionheaven
side is the real shell, run on a sandbox folder), a bash function, or
subprocess.run guarded; the Linux functions and branches run for real.
"""

import contextlib
import io
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

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, key_manager  # noqa: E402
from onionpress.cli import OnionPressCLI  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402
import test_backup_staging_links as bsl  # noqa: E402

PW = mv.PW
KEY = "ks_hs_id.ed25519_expanded_private"
# A registrant's key as the hub holds it (a real key pair, so that a
# restore's checks pass it): never in what WordPress gets.
REG_PRIV = b"\x09" * 64
REG_PUB = key_manager.derive_public_key(REG_PRIV)
A = key_manager.derive_onion_address(REG_PUB)
REGISTRANT_KEY = key_manager.build_openssh_key(REG_PRIV, REG_PUB)
DOCKER_HOST = "unix:///var/empty/no-docker-for-tests.sock"


def _branch(text, head, end):
    start = text.index(head) + len(head)
    return text[start:text.index(end, start)]


def _linux_create_backup():
    text = mv._read("linux/onionpress")
    start = text.index("\ncreate_backup() {")
    # The function embeds a python3 -c snippet with a bare "}" line.
    return text[start:text.index("\nrestore_backup() {", start)]


class _Container:
    """A sandbox for the containers a backup reads: a hub data folder
    standing in for /var/lib/onionheaven, OnionHome's registry folder and
    wp-content, and a docker stub on PATH that serves them (onionheaven's
    scripts run for real against the folder)."""

    def __init__(self, test, tmp, *, hub=True, onionhome=False):
        self.tmp = tmp
        self.hub = os.path.join(tmp, "hub")
        os.makedirs(os.path.join(self.hub, "keys"))
        if hub:
            path = os.path.join(self.hub, "keys", A, KEY)
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as f:
                f.write(REGISTRANT_KEY)
            conn = sqlite3.connect(os.path.join(self.hub, "registry.db"))
            conn.execute("CREATE TABLE registry (content_address TEXT)")
            conn.execute("INSERT INTO registry VALUES (?)", (A,))
            conn.commit()
            conn.close()
        self.names = os.path.join(tmp, "onionnames")
        os.makedirs(self.names)
        if onionhome:
            conn = sqlite3.connect(os.path.join(self.names, "onionnames.db"))
            conn.execute("CREATE TABLE onionnames (name_lower TEXT, onionaddress TEXT)")
            conn.execute("INSERT INTO onionnames VALUES ('brewster', ?)", (A,))
            conn.commit()
            conn.close()
        self.wp = os.path.join(tmp, "wp-content")
        os.makedirs(os.path.join(self.wp, "themes"))
        with open(os.path.join(self.wp, "themes", "a.css"), "w") as f:
            f.write("css")
        pem = os.path.join(tmp, "site-key.pem")
        with open(pem, "wb") as f:
            f.write(key_manager.build_openssh_key(mv.PRIV, mv.PUB))
        self.calls = os.path.join(tmp, "calls")
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "docker"), "w") as f:
            f.write(f"""#!/bin/bash
echo "$*" >> {self.calls!r}
export COPYFILE_DISABLE=1       # macOS tar: no ._ files
if [ "$1 $2 $3 $4" = "exec onionheaven sh -c" ]; then
    exec sh -c "${{5//\\/var\\/lib\\/onionheaven/{self.hub}}}"
fi
case "$*" in
  "exec onionpress-tor test -f /var/lib/onionnames/onionnames.db")
      test -f {self.names!r}/onionnames.db ;;
  "cp onionpress-tor:/var/lib/onionnames/"*)
      cp {self.names!r}/"${{2##*/}}" "$3" ;;
  *ks_hs_id.ed25519_expanded_private*) cat {pem!r} ;;
  *"wp config get DB_PASSWORD"*) echo pw ;;
  *"wp config get"*) echo wordpress ;;
  *mariadb-dump*) echo "CREATE TABLE wp_posts;" ;;
  "exec -u www-data onionpress-wordpress tar -cf - "*) tar -cf - -C {self.wp!r} . ;;
  *) exit 1 ;;
esac
""")
        os.chmod(os.path.join(self.bin, "docker"), 0o755)
        patcher = mock.patch.dict(os.environ, {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "DOCKER_HOST": DOCKER_HOST})
        patcher.start()
        test.addCleanup(patcher.stop)

    def calls_made(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def hub_reads(self):
        """The calls that read hub data (the tar of the registry and keys);
        the hub_data_state question reads nothing out."""
        return [c for c in self.calls_made()
                if c.startswith("exec onionheaven") and "tar -cf -" in c]


def _assert_no_hub_data(test, members):
    test.assertFalse([n for n in members if n.startswith("onionheaven")], sorted(members))
    test.assertFalse([n for n, data in members.items() if REGISTRANT_KEY in data])


# ---------------------------------------------------------------------------
# Creating a backup
# ---------------------------------------------------------------------------

class TestCreateBackup(unittest.TestCase):
    """backup.create_backup, the Mac's (and `onionpress.cli backup`'s)."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp(prefix="op-hub-off-wp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.logs = []

    def _backup(self, include_hub_data, **container):
        self.container = _Container(self, self.tmp, **container)
        out = os.path.join(self.tmp, "backup.zip")
        meta = backup.create_backup(mv.ADDR, "admin", PW, out, "2.5.3",
                                    self.logs.append, data_dir=self.data_dir,
                                    include_hub_data=include_hub_data)
        return meta, bsl._inner_names_and_bytes(out, PW)

    def _refused(self, want, **container):
        out = os.path.join(self.tmp, "backup.zip")
        with self.assertRaises(backup.SettingsPageBackupRefused) as cm:
            self._backup(include_hub_data=False, **container)
        self.assertEqual(str(cm.exception), want)
        self.assertIsInstance(cm.exception, ValueError)
        self.assertFalse(os.path.exists(out))
        # Refused before anything but the node's own key was looked at:
        # no hub data read, no database dump, no wp-content.
        calls = self.container.calls_made()
        self.assertEqual(self.container.hub_reads(), [])
        self.assertFalse([c for c in calls if "mariadb-dump" in c or "tar -cf -" in c], calls)

    def test_a_hub_is_refused_a_backup_for_wordpress(self):
        """Even without the hub data it would carry the hub's own key, and
        with it a rival hub could collect registrants' keys."""
        self._refused(backup.HUB_BACKUP_REFUSED_MSG)
        self.assertTrue([m for m in self.logs if "refused (the Settings page's backup)" in m])

    def test_onionhome_is_refused_a_backup_for_wordpress(self):
        self._refused(backup.ONIONHOME_BACKUP_REFUSED_MSG, hub=False, onionhome=True)

    def test_a_hub_backup_for_the_owners_disk_keeps_it(self):
        """No recovery key set up: in the clear under the password, as
        before, and the operator hears about it."""
        meta, members = self._backup(include_hub_data=True)
        self.assertEqual(members[f"onionheaven/keys/{A}/{KEY}"], REGISTRANT_KEY)
        self.assertIn("onionheaven/registry.db", members)
        self.assertIs(meta["is_onionheaven"], True)
        self.assertEqual(meta["onionheaven_data"], "unsealed")
        self.assertNotIn("excludes_onionheaven_data", meta)
        self.assertIn(backup.HUB_DATA_UNSEALED_WARNING, self.logs)

    def test_a_node_that_is_no_hub_has_nothing_to_say(self):
        meta, members = self._backup(include_hub_data=False, hub=False)
        _assert_no_hub_data(self, members)
        self.assertNotIn("excludes_onionheaven_data", meta)
        self.assertNotIn("onionheaven_data", meta)
        self.assertFalse([m for m in self.logs if "refused" in m])

    def test_no_answer_from_the_container_leaves_it_out_as_before(self):
        with mock.patch.object(backup, "hub_data_state", return_value=None):
            meta, members = self._backup(include_hub_data=False)
        _assert_no_hub_data(self, members)
        self.assertNotIn("excludes_onionheaven_data", meta)
        self.assertTrue([m for m in self.logs if "did not say whether" in m])

    def test_onionhomes_name_registry_stays_in(self):
        """Not secret (names and addresses /api/name/lookup serves), and an
        app restore of a backup without it would bring OnionHome up with an
        empty registry, every name free to claim."""
        meta, members = self._backup(include_hub_data=True, hub=False, onionhome=True)
        self.assertIn("onionhome/onionnames.db", members)
        self.assertIs(meta["is_onionhome"], True)

    def test_the_cli_backup_keeps_it(self):
        """`onionpress backup` (Mac launcher, scrub) writes to the owner's
        disk."""
        cli = object.__new__(OnionPressCLI)
        cli.paths = mock.Mock(data_dir=self.data_dir)
        cli.containers = mock.Mock(get_onion_address=mock.Mock(return_value=mv.ADDR))
        cli.log = self.logs.append
        import contextlib
        import io
        with mock.patch("onionpress.backup.verify_wp_admin", return_value=(True, None)), \
                mock.patch("onionpress.backup.create_backup") as create, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_backup(PW, os.path.join(self.tmp, "b.zip"), "admin"), 0)
        self.assertIs(create.call_args.kwargs["include_hub_data"], True)


class TestLinuxCreateBackup(unittest.TestCase):
    """linux/onionpress create_backup, run for real with docker stubbed and
    the shared Python helpers it calls."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
docker() {
    case "$1 $2" in
        "exec onionpress-tor") echo "$ADDR" ;;
        *) return 1 ;;
    esac
}
db_client() { echo "-- dump"; }
verify_wp_admin_password() { cat >/dev/null; }
setup_db_passwords() { :; }
python3() { "$PY" "$@"; }
op_py() {
    PYTHONPATH="$SRC" "$PY" -c 'import sys
from onionpress.cli import OnionPressCLI as C
cli = object.__new__(C)
cli.log = lambda m: print(m, file=sys.stderr)
args = sys.argv[1:]
data_dir = None
if args[:1] == ["--data-dir"]:
    data_dir, args = args[1], args[2:]
cmd, a = args[0], args[1:]
if cmd == "stage-wp-content":
    sys.exit(C.cmd_stage_wp_content(a[1]))
if cmd == "stage-hub-backup":
    sys.exit(C.cmd_stage_hub_backup(a[1], data_dir))
if cmd == "settings-page-backup-check":
    sys.exit(C.cmd_settings_page_backup_check(a[1], data_dir))
if cmd == "prune-image-derivatives":
    sys.exit(cli.cmd_prune_image_derivatives(a[1]))
if cmd == "pack-backup":
    sys.exit(cli.cmd_pack_backup(a[1], a[3], sys.stdin.read()))
sys.exit(2)' "$@"
}
'''

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp(prefix="op-hub-off-wp-linux-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, *args, recovery_key=False, **container):
        c = _Container(self, self.tmp, **container)
        self.container = c
        out = os.path.join(self.tmp, "backup.zip")
        home = os.path.join(self.tmp, "home")
        if recovery_key:
            from onionpress import hub_backup
            self.private_text, self.key_id = hub_backup.create_recovery_key(
                os.path.join(home, ".onionpress"))
        tmpdir = os.path.join(self.tmp, "t")
        os.makedirs(tmpdir, exist_ok=True)
        env = {
            "PATH": os.environ["PATH"], "HOME": home, "TMPDIR": tmpdir,
            "DATA_DIR": os.path.join(home, ".onionpress"),
            "LOG_FILE": os.path.join(self.tmp, "log"),
            "SRC": mv.SRC, "PY": sys.executable, "PW": PW, "ADDR": mv.ADDR,
            "WORDPRESS_DB_PASSWORD": "x", "ONIONPRESS_VERSION": "2.5.3",
            "DOCKER_HOST": DOCKER_HOST,
        }
        is_onion = mv._bash_function(mv._read("linux/onionpress"), "is_onion_address")
        call = " ".join(["create_backup", '"$PW"', repr(out)] + list(args))
        r = subprocess.run(
            ["bash", "-c", self.PRELUDE + is_onion + _linux_create_backup()
             + f'\n{call}\necho "rc=$? refusal=$BACKUP_REFUSAL"'],
            capture_output=True, text=True, timeout=120, env=env)
        try:
            with open(env["LOG_FILE"]) as f:
                log = f.read()
        except FileNotFoundError:
            log = ""
        self.stderr = r.stderr
        return r.stdout, log, out

    def test_without_hub_data_a_hub_is_refused(self):
        stdout, log, out = self._run("--without-hub-data")
        self.assertIn(f"rc=3 refusal={backup.HUB_BACKUP_REFUSED_MSG}\n", stdout, log)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self.container.hub_reads(), [])
        self.assertIn("refused (the Settings page's backup)", log)

    def test_without_hub_data_onionhome_is_refused(self):
        stdout, log, out = self._run("--without-hub-data", hub=False, onionhome=True)
        self.assertIn(f"rc=3 refusal={backup.ONIONHOME_BACKUP_REFUSED_MSG}\n", stdout, log)
        self.assertFalse(os.path.exists(out))

    def test_the_owners_backup_keeps_it(self):
        """No recovery key: in the clear under the password, with a
        warning on the terminal."""
        stdout, log, out = self._run()
        self.assertIn("rc=0 refusal=\n", stdout, log)
        members = bsl._inner_names_and_bytes(out, PW)
        self.assertEqual(members[f"onionheaven/keys/{A}/{KEY}"], REGISTRANT_KEY)
        meta = json.loads(members["metadata.json"])
        self.assertIs(meta["has_onionheaven"], True)
        self.assertEqual(meta["onionheaven_data"], "unsealed")
        self.assertNotIn("excludes_onionheaven_data", meta)
        self.assertIn("hub-recovery-key create", self.stderr)
        self.assertIn(backup.HUB_DATA_UNSEALED_WARNING, log)

    def test_the_owners_backup_seals_it_to_the_recovery_key(self):
        from onionpress import hub_backup
        stdout, log, out = self._run(recovery_key=True)
        self.assertIn("rc=0 refusal=\n", stdout, log)
        members = bsl._inner_names_and_bytes(out, PW)
        _assert_no_hub_data(self, {n: d for n, d in members.items()
                                   if n != hub_backup.SEALED_MEMBER})
        self.assertNotIn(REGISTRANT_KEY, members[hub_backup.SEALED_MEMBER])
        meta = json.loads(members["metadata.json"])
        self.assertIs(meta["has_onionheaven"], True)
        self.assertEqual(meta["onionheaven_data"], "sealed")
        self.assertEqual(meta["onionheaven_recovery_key_id"], self.key_id)
        self.assertEqual(meta["onionheaven_sealed_format"], hub_backup.SEALED_FORMAT)
        hub = hub_backup.unpack(hub_backup.unseal(
            members[hub_backup.SEALED_MEMBER],
            hub_backup.parse_private_key(self.private_text)), lambda m: None)
        self.assertEqual(hub.keys, {A: REGISTRANT_KEY})
        self.assertNotIn("hub-recovery-key create", self.stderr)

    def test_a_node_that_is_no_hub_has_nothing_to_say(self):
        stdout, log, out = self._run("--without-hub-data", hub=False)
        self.assertIn("rc=0 refusal=\n", stdout, log)
        meta = json.loads(bsl._inner_names_and_bytes(out, PW)["metadata.json"])
        self.assertNotIn("excludes_onionheaven_data", meta)
        self.assertNotIn("onionheaven_data", meta)

    def test_an_unknown_option_is_refused(self):
        stdout, _, out = self._run("--with-hub-data")
        self.assertIn("rc=1", stdout)
        self.assertFalse(os.path.exists(out))


# ---------------------------------------------------------------------------
# Who leaves it out: every path that hands a backup to WordPress
# ---------------------------------------------------------------------------

class TestEveryWordPressDeliveredBackupLeavesItOut(unittest.TestCase):
    """A census of the shipped sources: the hand-off to the WordPress
    container exists only in the Settings page's create-backup branches,
    and those (and only those) leave the hub data out; every other backup
    says it keeps it."""

    HANDOFF = re.compile(r"""umask 077; cat > "\$1"'""")
    SOURCES = ["src/menubar.py", "linux/onionpress", "app/MacOS/onionpress",
               "linux/onionpress-tray", "linux/onionpress-service.py"]

    def _python_sources(self):
        root = os.path.join(PROJECT_ROOT, "src")
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT)
                    yield rel, mv._read(rel)

    def test_the_handoff_to_wordpress_is_only_the_settings_pages_backup(self):
        found = []
        for rel in self.SOURCES + [r for r, _ in self._python_sources()]:
            text = mv._read(rel)
            for m in self.HANDOFF.finditer(text):
                found.append(rel)
        self.assertEqual(sorted(set(found)), ["linux/onionpress", "src/menubar.py"])
        mac = _branch(mv._read("src/menubar.py"), 'elif action == "create-backup":',
                      'elif action == "restore-backup":')
        self.assertEqual(len(self.HANDOFF.findall(mac)), 1)
        self.assertEqual(len(self.HANDOFF.findall(mv._read("src/menubar.py"))), 1)
        linux = _branch(mv._read("linux/onionpress"), "                create-backup)\n",
                        "\n                    ;;\n")
        self.assertEqual(len(self.HANDOFF.findall(linux)), 1)
        self.assertEqual(len(self.HANDOFF.findall(mv._read("linux/onionpress"))), 1)

    def test_the_settings_pages_backups_leave_the_hub_data_out(self):
        mac = _branch(mv._read("src/menubar.py"), 'elif action == "create-backup":',
                      'elif action == "restore-backup":')
        create = mac.index("backup_manager.create_backup(")
        call = mac[create:mac.index(")", mac.index("self.log", create))]
        self.assertIn("include_hub_data=False", call)
        self.assertLess(create, self.HANDOFF.search(mac).start())
        linux = _branch(mv._read("linux/onionpress"), "                create-backup)\n",
                        "\n                    ;;\n")
        self.assertRegex(linux, r'create_backup "\$bp_password" "\$bp_path" '
                                r'--without-hub-data\b')
        self.assertLess(linux.index("--without-hub-data"), self.HANDOFF.search(linux).start())

    def test_every_python_create_backup_call_says_which_it_is(self):
        calls = []
        for rel, text in self._python_sources():
            for m in re.finditer(r"\bcreate_backup\(", text):
                if text[max(0, m.start() - 4):m.start()] == "def ":
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                if "#" in text[line_start:m.start()]:
                    continue        # a comment naming it
                # The call's arguments, up to its closing parenthesis.
                depth, i = 0, m.end() - 1
                while True:
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    if depth == 0:
                        break
                    i += 1
                calls.append((rel, text[m.start():i + 1]))
        self.assertEqual(len(calls), 3, calls)
        for rel, call in calls:
            with self.subTest(rel, call=call):
                self.assertRegex(call, r"include_hub_data=(True|False)\b")
        falses = [(rel, c) for rel, c in calls if "include_hub_data=False" in c]
        self.assertEqual(len(falses), 1)
        self.assertEqual(falses[0][0], "src/menubar.py")

    def test_every_linux_create_backup_call_says_which_it_is(self):
        text = mv._read("linux/onionpress")
        calls = [line.strip() for line in text.splitlines()
                 if re.search(r'\bcreate_backup "', line)
                 and not line.lstrip().startswith("#")]
        self.assertEqual(len(calls), 3, calls)
        self.assertEqual(len([c for c in calls if "--without-hub-data" in c]), 1)

    def test_the_tray_and_scrub_go_through_the_launchers_backup(self):
        """They write to ~/OnionPress/backups, never to WordPress."""
        self.assertIn('Launcher.run("backup"', mv._read("linux/onionpress-tray"))
        self.assertIn('[LAUNCHER_BIN, "backup", "--output"', mv._read("src/onionpress/scrub.py"))


class TestLinuxSettingsPageBackupBranch(unittest.TestCase):
    """linux/onionpress's create-backup settings action, run for real with
    create_backup stubbed: it asks for the hub data to be left out and
    passes the note on."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
setup_db_passwords() { :; }
create_backup() {
    echo "$*" > "$ARGS"
    BACKUP_REFUSAL=""
    if [ "$HUB" = 1 ]; then
        BACKUP_REFUSAL="$REFUSAL"
        return 3
    fi
    printf 'backup bytes' > "$2"
    return 0
}
docker() {
    case "$*" in
        "exec -u www-data onionpress-wordpress cat /var/lib/onionpress/backup-password") echo pw ;;
        "exec -i -u www-data onionpress-wordpress sh -c "*)
            shift 7; sh -c "$1" sh "$SANDBOX$3" ${4:+"$4"} ;;
        "exec onionpress-tor "*) echo "$ADDR" ;;
    esac
    return 0
}
'''

    def _run(self, hub):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        sandbox = os.path.join(tmp, "container")
        os.makedirs(os.path.join(sandbox, "var", "lib", "onionpress"))
        text = mv._read("linux/onionpress")
        branch = _branch(text, "                create-backup)\n", "\n                    ;;\n")
        helpers = "".join(
            f"{name}() {{\n" + _branch(text, f"\n{name}() {{\n", "\n}\n") + "\n}\n"
            for name in ("wp_put", "wp_exec", "is_onion_address"))
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(tmp, "home"),
            "TMPDIR": tmp, "LOG_FILE": os.path.join(tmp, "log"),
            "ARGS": os.path.join(tmp, "args"), "SANDBOX": sandbox,
            "ADDR": mv.ADDR, "HUB": "1" if hub else "0", "DOCKER_HOST": DOCKER_HOST,
            "REFUSAL": backup.HUB_BACKUP_REFUSED_MSG,
        }
        r = subprocess.run(["bash", "-c", self.PRELUDE + helpers
                            + "run_branch() {\n" + branch + "\n}\nrun_branch\n"],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(env["ARGS"]) as f:
            args = f.read().split()
        self.handed_over = [n for n in os.listdir(os.path.join(sandbox, "var", "lib", "onionpress"))
                            if n.endswith(".zip")]
        with open(os.path.join(sandbox, "var", "lib", "onionpress", "backup-result.json")) as f:
            return args, json.load(f)

    def test_a_hub_hears_why_it_was_refused(self):
        args, result = self._run(hub=True)
        self.assertEqual(args[2:], ["--without-hub-data", "--without-host-secrets"])
        self.assertEqual(result["success"], False)
        self.assertEqual(result["error"], backup.HUB_BACKUP_REFUSED_MSG)
        self.assertEqual(self.handed_over, [])

    def test_any_other_node_gets_its_backup(self):
        args, result = self._run(hub=False)
        self.assertEqual(args[2:], ["--without-hub-data", "--without-host-secrets"])
        self.assertIs(result["success"], True)
        self.assertNotIn("note", result)
        self.assertEqual(len(self.handed_over), 1)


class TestMacSettingsPageBackupBranch(unittest.TestCase):
    """src/menubar.py's create-backup action (AppKit, so checked as
    source): a refusal reaches the page as the result's error, and nothing
    is handed to WordPress."""

    def test_the_refusal_is_the_pages_error(self):
        body = _branch(mv._read("src/menubar.py"), 'elif action == "create-backup":',
                       'elif action == "restore-backup":')
        create = body.index("backup_manager.create_backup(")
        # The hand-off comes after create_backup, which raises on a hub;
        # the except clause writes str(e) as the page's error.
        self.assertLess(create, body.index('umask 077; cat > "$1"'))
        self.assertIn('"success": False, "error": str(e)', body[create:])
        self.assertNotIn("note", body)

    def test_the_messages_are_plain_text(self):
        """They go into the page, and Linux's into JSON."""
        for text in (backup.HUB_BACKUP_REFUSED_MSG, backup.ONIONHOME_BACKUP_REFUSED_MSG,
                     backup.HUB_DATA_RESTORE_REFUSED_MSG,
                     backup.ONIONHOME_RESTORE_REFUSED_MSG):
            self.assertRegex(text, r"\A[A-Za-z0-9 .,:;'()-]+\Z")


class TestSettingsPageBackupRefusal(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-sp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(backup, "onionhome_registry_present", return_value=False)
        self.onionhome = p.start()
        self.addCleanup(p.stop)

    def _config(self, text):
        with open(os.path.join(self.tmp, "config"), "w") as f:
            f.write(text)

    def test_who_is_refused(self):
        refuse = backup.settings_page_backup_refusal
        hub, home = backup.HUB_BACKUP_REFUSED_MSG, backup.ONIONHOME_BACKUP_REFUSED_MSG
        self.assertIsNone(refuse(mv.ADDR, "none", data_dir=self.tmp))
        self.assertIsNone(refuse(mv.ADDR, None, data_dir=self.tmp))
        self.assertEqual(refuse(backup.ONIONHEAVEN_HUB_ADDRESS, "none", data_dir=self.tmp), hub)
        self.assertEqual(refuse(mv.ADDR, "hub", data_dir=self.tmp), hub)
        self.assertEqual(refuse(backup.ONIONHOME_ADDRESS, "none", data_dir=self.tmp), home)
        for value, want in (("yes", hub), (" YES ", hub), ("no", None), ("yess", None),
                            ("", None)):
            with self.subTest(value=value):
                self._config(f"ONIONHEAVEN_ACCEPT_REGISTRATIONS={value}\n")
                self.assertEqual(refuse(mv.ADDR, "none", data_dir=self.tmp), want)
        self._config("")
        self.onionhome.return_value = True
        self.assertEqual(refuse(mv.ADDR, "none", data_dir=self.tmp), home)

    def test_the_cli_helper_says_why(self):
        for kw, rc, want in (({"hub_state": "hub"}, 3, backup.HUB_BACKUP_REFUSED_MSG),
                             ({"hub_state": "none"}, 0, "")):
            out = io.StringIO()
            with mock.patch.object(backup, "hub_data_state", return_value=kw["hub_state"]), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(OnionPressCLI.cmd_settings_page_backup_check(
                    mv.ADDR, self.tmp), rc)
            self.assertEqual(out.getvalue().strip(), want)

    def test_the_addresses_are_the_tor_images(self):
        tor_dir = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
        sys.path.insert(0, tor_dir)
        try:
            import onionheaven_common as oc
        finally:
            sys.path.remove(tor_dir)
        self.assertEqual(backup.ONIONHEAVEN_HUB_ADDRESS, oc.ONIONHEAVEN_HUB_ADDRESS)
        web = mv._read("app/Resources/docker/tor/web-server.py")
        self.assertIn(f'_ONIONHOME_ADDRESS = "{backup.ONIONHOME_ADDRESS}"', web)
        from onionpress import onionnames_registrar
        self.assertEqual(backup.ONIONHOME_ADDRESS, onionnames_registrar.DEFAULT_ONIONHOME_ADDRESS)

    def test_not_on_server_active(self):
        """status.json's onionheaven.server_active is true on every Linux
        node: no refusal may rest on it."""
        for rel in ("src/onionpress/backup.py", "linux/onionpress"):
            text = mv._read(rel)
            start = text.index("settings_page_backup_refusal" if rel.endswith(".py")
                               else "settings-page-backup-check")
            self.assertNotIn("server_active", text[start:start + 3000].replace(
                "status.json's onionheaven.server_active", "").replace(
                "status.json's server_active", ""))


# ---------------------------------------------------------------------------
# Restoring from the Settings page
# ---------------------------------------------------------------------------

def _staging(root, name, *, keys=False, empty_keys=False, registry=False,
             onionhome=False, sealed=False, metadata=None):
    staging = mv._make_staging(root, metadata or mv._metadata(), name=name)
    os.makedirs(os.path.join(staging, "database"))
    with open(os.path.join(staging, "database", "wordpress.sql"), "w") as f:
        f.write("SELECT 1;\n")
    if keys or empty_keys or registry:
        os.makedirs(os.path.join(staging, "onionheaven", "keys"))
    if keys:
        os.makedirs(os.path.join(staging, "onionheaven", "keys", A))
        with open(os.path.join(staging, "onionheaven", "keys", A, KEY), "wb") as f:
            f.write(REGISTRANT_KEY)
    if registry:
        with open(os.path.join(staging, "onionheaven", "registry.db"), "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)
    if onionhome:
        os.makedirs(os.path.join(staging, "onionhome"))
        with open(os.path.join(staging, "onionhome", "onionnames.db"), "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)
    if sealed:
        # Hub data sealed to a recovery key (hub_backup): opaque here.
        with open(os.path.join(staging, "onionheaven.sealed"), "wb") as f:
            f.write(os.urandom(200))
    return staging


class TestSettingsPageRestoreRefusal(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_what_is_refused(self):
        cases = {
            "keys": ({"keys": True, "registry": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
            "keys, no registry": ({"keys": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
            "onionhome": ({"onionhome": True}, backup.ONIONHOME_RESTORE_REFUSED_MSG),
            # Sealed to the hub's recovery key: never opened through
            # WordPress, and refused like a hub's plain keys.
            "sealed": ({"sealed": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
            # Every Linux backup through 2.5.2: the empty registry and keys/
            # every node has. Left out of the restore, not refused.
            "registry alone": ({"registry": True, "empty_keys": True}, None),
            "nothing": ({}, None),
        }
        for label, (kw, want) in cases.items():
            with self.subTest(label):
                staging = _staging(self.tmp, label, **kw)
                self.assertEqual(backup.settings_page_restore_refusal(staging), want)

    def test_the_cli_helper_says_why(self):
        import contextlib
        import io
        for kw, rc, want in (({"keys": True}, 3, backup.HUB_DATA_RESTORE_REFUSED_MSG),
                             ({"onionhome": True}, 3, backup.ONIONHOME_RESTORE_REFUSED_MSG),
                             ({"registry": True}, 0, "")):
            staging = _staging(self.tmp, f"cli-{rc}-{sorted(kw)}", **kw)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(OnionPressCLI.cmd_settings_page_restore_check(staging), rc)
            self.assertEqual(out.getvalue().strip(), want)


class _KeyWritten(Exception):
    pass


class TestRestoreFromBackupThroughWordPress(unittest.TestCase):
    """restore_from_backup(through_wordpress=True), the Mac Settings page's
    restore: refused before anything is touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)
        self.data_dir = os.path.join(self.tmp, "home", ".onionpress")
        os.makedirs(self.data_dir)
        with open(os.path.join(self.data_dir, "onion_address"), "w") as f:
            f.write(mv.OLD_ADDR + "\n")
        self.logs = []
        p = mock.patch("onionpress.backup.subprocess.run", mv._NoDocker(self))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(key_manager, "write_private_key", side_effect=_KeyWritten)
        p.start()
        self.addCleanup(p.stop)

    def _backup(self, name, **kw):
        staging = _staging(self.tmp, name + ".staging", **kw)
        out = os.path.join(self.tmp, name)
        backup.pack_backup(staging, out, PW, lambda m: None)
        shutil.rmtree(staging)
        return out

    def _assert_untouched(self):
        self.assertEqual(os.listdir(self.data_dir), ["onion_address"])
        with open(os.path.join(self.data_dir, "onion_address")) as f:
            self.assertEqual(f.read(), mv.OLD_ADDR + "\n")

    def test_a_hubs_backup_is_refused_untouched(self):
        for kw, want in (({"keys": True, "registry": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
                         ({"sealed": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
                         ({"onionhome": True}, backup.ONIONHOME_RESTORE_REFUSED_MSG)):
            with self.subTest(want, **kw):
                path = self._backup(f"b-{len(self.logs)}-{sorted(kw)[0]}.zip", **kw)
                with self.assertRaises(backup.SettingsPageRestoreRefused) as cm:
                    backup.restore_from_backup(path, PW, self.logs.append,
                                               data_dir=self.data_dir,
                                               through_wordpress=True)
                self.assertEqual(str(cm.exception), want)
                self.assertIsInstance(cm.exception, ValueError)
                self._assert_untouched()
                # The staging folder is gone with what it decrypted.
                self.assertFalse([n for n in os.listdir(tempfile.gettempdir())
                                  if n.startswith("onionpress-restore-")
                                  and os.path.exists(os.path.join(
                                      tempfile.gettempdir(), n, "onionheaven"))])

    def test_the_apps_restore_goes_ahead(self):
        path = self._backup("hub.zip", keys=True, registry=True, onionhome=True)
        with self.assertRaises(_KeyWritten):
            backup.restore_from_backup(path, PW, self.logs.append, data_dir=self.data_dir)

    def test_a_backup_without_hub_data_goes_ahead(self):
        path = self._backup("site.zip", registry=True, empty_keys=True)
        with self.assertRaises(_KeyWritten):
            backup.restore_from_backup(path, PW, self.logs.append,
                                       data_dir=self.data_dir, through_wordpress=True)

    def test_the_settings_page_asks_for_it(self):
        src = mv._read("src/menubar.py")
        body = src[src.index('elif action == "restore-backup":'):]
        body = body[:body.index("\n            else:")]
        self.assertIn("restore_from_backup(local_zip, password, self.log,\n", body)
        self.assertRegex(body, r"restore_from_backup\(local_zip, password, self\.log,\s+"
                               r"through_wordpress=True[,)]")
        # The only caller in what ships.
        callers = [rel for rel in ("src/menubar.py", "src/onionpress/cli.py",
                                   "src/onionpress/setup_window.py")
                   if "restore_from_backup(" in mv._read(rel)]
        self.assertEqual(callers, ["src/menubar.py"])
        self.assertEqual(mv._read("src/menubar.py").count("restore_from_backup("), 1)


class TestSettingsPageRestoreNeverWritesTheVolumes(unittest.TestCase):
    """restore_container_artifacts(hub_volumes=False): what passed the
    refusal (a registry alone) is left out, never written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _restore(self, staging, metadata=None, **kw):
        calls, logs = [], []

        def run(argv, **kwargs):
            calls.append(list(argv))
            out = "restored dropped=0\n" if argv[-1] == backup.REGISTRY_RESTORE_SH else (
                "/tmp/tmp.Ab12Cd34\n" if "mktemp" in argv[-2:] else
                "wordpress\n" if "get" in argv else "")
            return subprocess.CompletedProcess(
                argv, 0, stdout=out if kwargs.get("text") else out.encode(),
                stderr="" if kwargs.get("text") else b"")

        with mock.patch.object(backup.subprocess, "run", side_effect=run):
            backup.restore_container_artifacts(staging, metadata or {}, logs.append, **kw)
        touched = [c for c in calls if any("onionheaven" in a or "onionnames" in a
                                           for a in c)]
        return touched, logs

    def test_left_out_and_logged(self):
        staging = _staging(self.tmp, "s", keys=True, registry=True, onionhome=True)
        touched, logs = self._restore(staging, hub_volumes=False)
        self.assertEqual(touched, [])
        self.assertTrue([m for m in logs if "OnionHeaven data left out" in m], logs)
        self.assertTrue([m for m in logs if "OnionHome name registry left out" in m], logs)

    def test_the_apps_restore_writes_them(self):
        staging = _staging(self.tmp, "s", keys=True, registry=True, onionhome=True)
        touched, _ = self._restore(staging)
        self.assertTrue([c for c in touched if "onionheaven:/var/lib/onionheaven/keys/" in c])
        # The registry goes to onionpress-tor for the image's checks to
        # rebuild (backup.restore_name_registry).
        self.assertTrue([c for c in touched if "onionpress-tor:/tmp/tmp.Ab12Cd34/"
                         "backup/onionhome/onionnames.db" in c])

    def test_a_hubs_settings_page_backup_says_what_it_lacks(self):
        staging = _staging(self.tmp, "s")
        _, logs = self._restore(staging, {"excludes_onionheaven_data": True})
        self.assertTrue([m for m in logs if "holds neither the hub's registry" in m], logs)
        _, logs = self._restore(staging, {})
        self.assertFalse([m for m in logs if "holds neither the hub's registry" in m], logs)


class TestLinuxRestoreBackup(unittest.TestCase):
    """linux/onionpress restore_backup, run for real against backup files
    (the harness of test_backup_metadata_validation)."""

    PRELUDE = r'''
log() { echo "[log] $1" | tee -a "$LOG_FILE"; }
docker() { echo "docker $*" >> "$CALLS"; return 0; }
db_client() { echo "db_client $*" >> "$CALLS"; return 0; }
setup_db_passwords() { :; }
python3() { "$PY" "$@"; }
op_py() {
    case "$1" in
        extract-backup)
            PYTHONPATH="$SRC" "$PY" -c 'import sys
from onionpress import cli
sys.exit(cli.OnionPressCLI.cmd_extract_backup(
    None, sys.argv[2], sys.argv[4], sys.stdin.read().rstrip("\n")))' "${@:2}" ;;
        put-wp-tree|restore-hub-data) echo "op_py $*" >> "$CALLS" ;;
        *) PYTHONPATH="$SRC" "$PY" -m onionpress.cli "$@" ;;
    esac
}
'''

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)

    def _backup(self, name, **kw):
        staging = _staging(self.tmp, name + ".staging", **kw)
        out = os.path.join(self.tmp, name)
        backup.pack_backup(staging, out, PW, lambda m: None)
        shutil.rmtree(staging)
        return out

    def _run(self, path, *args):
        home = os.path.join(self.tmp, "home")
        shutil.rmtree(home, True)
        data = os.path.join(home, ".onionpress")
        os.makedirs(data)
        with open(os.path.join(data, "config"), "w") as f:
            f.write("ONIONNAME=me\n")
        calls = os.path.join(self.tmp, "calls")
        if os.path.exists(calls):
            os.remove(calls)
        tmpdir = os.path.join(self.tmp, "t")
        os.makedirs(tmpdir, exist_ok=True)
        env = {
            "PATH": os.environ["PATH"], "HOME": home, "TMPDIR": tmpdir,
            "DATA_DIR": data, "LOG_FILE": os.path.join(self.tmp, "log"),
            "CALLS": calls, "SRC": mv.SRC, "PY": sys.executable, "PW": PW,
            "DOCKER_HOST": DOCKER_HOST,
        }
        text = mv._read("linux/onionpress")
        funcs = "".join(mv._bash_function(text, name)
                        for name in ("is_onion_address", "restore_backup"))
        call = " ".join(["restore_backup", '"$PW"', repr(path)] + list(args))
        r = subprocess.run(["bash", "-c", self.PRELUDE + funcs + call + '\necho "rc=$?"'],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertIn("rc=", r.stdout, r.stderr)
        out, rc = r.stdout.rsplit("rc=", 1)
        try:
            with open(calls) as f:
                made = f.read()
        except FileNotFoundError:
            made = ""
        return int(rc.strip()), out, data, made

    def test_a_hubs_backup_is_refused_untouched(self):
        for kw, want in (({"keys": True, "registry": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
                         ({"sealed": True}, backup.HUB_DATA_RESTORE_REFUSED_MSG),
                         ({"onionhome": True}, backup.ONIONHOME_RESTORE_REFUSED_MSG)):
            with self.subTest(want, **kw):
                path = self._backup(f"b{len(kw)}-{sorted(kw)[0]}.zip", **kw)
                rc, out, data, made = self._run(path, "--from-settings-page")
                self.assertEqual(rc, 3, out)
                # The Settings page branch writes the last line as the error.
                self.assertEqual(out.rstrip("\n").rsplit("\n", 1)[-1], want)
                self.assertEqual(made, "")
                self.assertEqual(os.listdir(data), ["config"])

    def test_a_registry_alone_is_left_out(self):
        path = self._backup("old-linux.zip", registry=True, empty_keys=True)
        rc, out, data, made = self._run(path, "--from-settings-page")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("restore-hub-data", made)
        self.assertNotIn("inspect onionheaven", made)
        self.assertIn("OnionHeaven data left out", out)
        self.assertEqual(out.rstrip("\n").rsplit("\n", 1)[-1], mv.ADDR)

    def test_the_cli_restore_path_is_unchanged(self):
        path = self._backup("hub.zip", keys=True, registry=True)
        rc, out, _, made = self._run(path)
        self.assertEqual(rc, 0, out)
        self.assertIn("op_py restore-hub-data --src", made)

    def test_without_the_settings_page_sealed_hub_data_is_named_not_restored(self):
        path = self._backup("sealed.zip", sealed=True)
        rc, out, _, made = self._run(path)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("restore-hub-data", made)
        self.assertIn("sealed to the hub's recovery key and were not restored", out)

    def test_an_unknown_option_is_refused(self):
        path = self._backup("x.zip")
        rc, _, data, made = self._run(path, "--from-elsewhere")
        self.assertEqual(rc, 1)
        self.assertEqual(made, "")


class TestLinuxSettingsPageRestoreBranch(unittest.TestCase):
    """linux/onionpress's restore-backup settings action: it asks for the
    refusal and shows the reason."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
setup_db_passwords() { :; }
stop_containers() { echo stop >> "$CALLS"; }
start_containers() { echo start >> "$CALLS"; }
wait_for_services() { :; }
write_status() { :; }
write_restore_result() { echo "$1|$2" >> "$RESULT"; }
restore_backup() {
    echo "restore_backup $3" >> "$CALLS"
    echo "[log] Restore: extracting backup archive..."
    echo "$REFUSAL"
    return 3
}
docker() {
    case "$1" in
        cp) printf 'uploaded zip' > "$3" ;;
        exec) [ "$2 $3 $5" = "-u www-data cat" ] && echo pw ;;
    esac
    return 0
}
'''

    def test_the_refusal_reaches_the_page(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = mv._read("linux/onionpress")
        branch = _branch(text, "                restore-backup)\n", "\n                    ;;\n")
        helpers = mv._bash_function(text, "is_onion_address") + mv._bash_function(text, "wp_exec")
        tmpdir = os.path.join(tmp, "t")
        os.makedirs(tmpdir)
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(tmp, "home"),
            "TMPDIR": tmpdir, "LOG_FILE": os.path.join(tmp, "log"),
            "CALLS": os.path.join(tmp, "calls"), "RESULT": os.path.join(tmp, "result"),
            "REFUSAL": backup.HUB_DATA_RESTORE_REFUSED_MSG, "DOCKER_HOST": DOCKER_HOST,
        }
        r = subprocess.run(["bash", "-c", self.PRELUDE + helpers
                            + "run_branch() {\n" + branch + "\n}\nrun_branch\n"],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(env["CALLS"]) as f:
            self.assertEqual(f.read(), "restore_backup --from-settings-page\n")
        with open(env["RESULT"]) as f:
            self.assertEqual(f.read(), f"false|{backup.HUB_DATA_RESTORE_REFUSED_MSG}\n")
        self.assertEqual(os.listdir(tmpdir), [])


# ---------------------------------------------------------------------------
# The Settings page tells the owner
# ---------------------------------------------------------------------------

class TestSettingsPageSaysSo(unittest.TestCase):

    def setUp(self):
        self.php = mv._read("app/Resources/plugins/onionpress-settings.php")
        self.backup = _branch(self.php, "<h2>Backup</h2>", "<!-- Restore -->")
        self.restore = _branch(self.php, "<h2>Restore</h2>", "</form>")

    def test_the_backup_section(self):
        # On every node: status.json's server_active is true on every Linux
        # node, so the page does not try to tell.
        self.assertNotIn("$oh_server_active", self.backup)
        self.assertIn("On an OnionHeaven hub or on OnionHome, a backup is refused "
                      "here", self.backup)
        self.assertIn("<code>onionpress backup</code>", self.backup)
        self.assertIn("esc_html( $backup_result['error'] )", self.backup)

    def test_the_restore_section(self):
        self.assertIn("is refused here", self.restore)
        self.assertIn("<code>onionpress restore</code>", self.restore)

    def test_the_live_notice_shows_the_note_as_text(self):
        poll = self.php[self.php.index("onionpress_poll_action&op_action="):]
        poll = poll[:poll.index("}, 3000);")]
        self.assertIn("note.textContent = result.note;", poll)
        self.assertNotIn("innerHTML", poll)


if __name__ == "__main__":
    unittest.main()
