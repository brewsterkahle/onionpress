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
  lookups). It fails closed: a node whose containers do not answer the
  checks, or that has no address to check, is refused too. The page shows
  why. Backups made from the app or the CLI keep
  the hub data (sealed to the hub's recovery key when one is set up:
  tests/test_hub_backup_sealed.py). OnionHome's name registry (no secret in
  it) stays in those.
- The Settings page's restore refuses a backup holding registrants' keys
  (plain or sealed) or OnionHome's name registry before anything is
  touched, and never writes either volume (a registry alone, as every
  Linux backup through 2.5.2 carries, is left out).
- The checks never read an error as "no": the hub check
  (HUB_DATA_STATE_SH) answers nothing for a registry sqlite3 cannot read
  and waits out a writer's lock; create_backup's OnionHome step asks as the
  refusal does. The owner's backup is then made without that data and
  says so (the log, metadata.json, the note with the finished backup), and
  Linux refuses a Settings page backup whose check failed in the Mac's
  words.

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
    scripts and onionpress-tor's registry check run for real against the
    folders). The containers named in *unanswered* answer those scripts
    as a stopped container does: an error from the daemon, exit 1.
    keys=False keeps a hub's keys/ empty (registry rows only);
    sqlite3_fails puts a sqlite3 on PATH that fails as on a registry
    locked past its busy timeout."""

    def __init__(self, test, tmp, *, hub=True, keys=True, onionhome=False, unanswered=(),
                 sqlite3_fails=False):
        self.tmp = tmp
        self.hub = os.path.join(tmp, "hub")
        os.makedirs(os.path.join(self.hub, "keys"))
        if hub:
            if keys:
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
if [ "$1 $3 $4" = "exec sh -c" ]; then
    case " {" ".join(unanswered)} " in
      *" $2 "*) echo "Error response from daemon: container $2 is not running" >&2
                exit 1 ;;
    esac
fi
if [ "$1 $2 $3 $4" = "exec onionheaven sh -c" ]; then
    exec sh -c "${{5//\\/var\\/lib\\/onionheaven/{self.hub}}}"
fi
if [ "$1 $2 $3 $4 $6" = "exec onionpress-tor sh -c sh" ]; then
    exec sh -c "$5" sh "${{7//\\/var\\/lib\\/onionnames/{self.names}}}"
fi
case "$*" in
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
        if sqlite3_fails:
            with open(os.path.join(self.bin, "sqlite3"), "w") as f:
                f.write('#!/bin/sh\necho "Error: database is locked" >&2\nexit 5\n')
            os.chmod(os.path.join(self.bin, "sqlite3"), 0o755)
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
        # Both checks answered: nothing unchecked.
        self.assertNotIn("onionheaven_data_unchecked", meta)
        self.assertNotIn("onionhome_registry_unchecked", meta)
        self.assertEqual(backup.unchecked_notes(meta), [])
        self.assertFalse([m for m in self.logs if "did not say whether" in m])

    def test_no_answer_from_onionheaven_is_refused_a_backup_for_wordpress(self):
        """Fails closed: a hub whose onionheaven container does not answer
        (stopped, restarting, Docker busy) cannot say it is no hub."""
        self._refused(backup.UNVERIFIED_BACKUP_REFUSED_MSG, unanswered=("onionheaven",))
        self.assertTrue([m for m in self.logs if "refused (the Settings page's backup)" in m])

    def test_no_answer_about_onionhomes_registry_is_refused_a_backup_for_wordpress(self):
        """Fails closed: OnionHome is known by its registry, and a tor
        container that does not answer the check (a failed `docker exec`)
        cannot say there is none."""
        self._refused(backup.UNVERIFIED_BACKUP_REFUSED_MSG, hub=False, onionhome=True,
                      unanswered=("onionpress-tor",))
        self.assertIn("exec onionpress-tor sh -c", "\n".join(self.container.calls_made()))

    def test_no_answer_from_onionheaven_the_owners_backup_goes_ahead_without_it(self):
        """The owner's backup is not handed to WordPress: it is made, says
        the hub data is not in it (the log, metadata.json, the note with
        the finished backup), and holds none."""
        meta, members = self._backup(include_hub_data=True, unanswered=("onionheaven",))
        _assert_no_hub_data(self, members)
        self.assertNotIn("onionheaven_data", meta)
        self.assertTrue([m for m in self.logs if "did not say whether" in m])
        self.assertIn(backup.HUB_DATA_UNCHECKED_WARNING, self.logs)
        self.assertIs(meta["onionheaven_data_unchecked"], True)
        self.assertIs(json.loads(members["metadata.json"])["onionheaven_data_unchecked"], True)
        self.assertNotIn("onionhome_registry_unchecked", meta)
        self.assertEqual(backup.unchecked_notes(meta), [backup.HUB_DATA_UNCHECKED_NOTE])

    def test_a_hubs_registry_alone_makes_it_a_hub(self):
        """Registry rows and no keys yet: refused as a hub's."""
        self._refused(backup.HUB_BACKUP_REFUSED_MSG, keys=False)

    def test_a_registry_sqlite3_cannot_read_is_no_answer(self):
        """keys/ empty and the registry's rows unreadable (locked past the
        busy timeout, damaged, no sqlite3): the check printed "none" here,
        and the Settings page's backup took the hub for any other node. Now
        it is refused, fail closed."""
        self._refused(backup.UNVERIFIED_BACKUP_REFUSED_MSG, keys=False, sqlite3_fails=True)
        self.assertTrue([m for m in self.logs if "refused (the Settings page's backup)" in m])

    def test_a_registry_sqlite3_cannot_read_the_owners_backup_says_so(self):
        meta, members = self._backup(include_hub_data=True, keys=False, sqlite3_fails=True)
        _assert_no_hub_data(self, members)
        self.assertIs(meta["is_onionheaven"], False)
        self.assertIs(meta["onionheaven_data_unchecked"], True)
        self.assertIn(backup.HUB_DATA_UNCHECKED_WARNING, self.logs)

    def test_no_answer_about_onionhomes_registry_the_owners_backup_goes_ahead_without_it(self):
        """Step 4b asks as the refusal does (onionhome_registry_present).
        Its `docker exec onionpress-tor test -f` read a failed exec as "not
        OnionHome": the registry stayed out of OnionHome's backup and
        nothing said so. Now the backup is made, without it, and says so."""
        meta, members = self._backup(include_hub_data=True, hub=False, onionhome=True,
                                     unanswered=("onionpress-tor",))
        self.assertFalse([n for n in members if n.startswith("onionhome")], sorted(members))
        self.assertIs(meta["is_onionhome"], False)
        self.assertIs(meta["onionhome_registry_unchecked"], True)
        self.assertIs(json.loads(members["metadata.json"])["onionhome_registry_unchecked"], True)
        self.assertNotIn("onionheaven_data_unchecked", meta)
        self.assertIn(backup.ONIONHOME_REGISTRY_UNCHECKED_WARNING, self.logs)
        self.assertEqual(backup.unchecked_notes(meta),
                         [backup.ONIONHOME_REGISTRY_UNCHECKED_NOTE])
        calls = self.container.calls_made()
        self.assertFalse([c for c in calls if "test -f" in c], calls)
        self.assertFalse([c for c in calls if c.startswith("cp onionpress-tor:")], calls)

    def test_a_timeout_asking_about_onionhomes_registry_does_not_abort_the_owners_backup(self):
        """It raised out of create_backup before."""
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            if list(argv[:5]) == ["docker", "exec", "onionpress-tor", "sh", "-c"]:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(backup.subprocess, "run", side_effect=run):
            meta, members = self._backup(include_hub_data=True, hub=False, onionhome=True)
        self.assertNotIn("onionhome/onionnames.db", members)
        self.assertIs(meta["onionhome_registry_unchecked"], True)
        self.assertIn(backup.ONIONHOME_REGISTRY_UNCHECKED_WARNING, self.logs)

    def test_onionhomes_name_registry_stays_in(self):
        """Not secret (names and addresses /api/name/lookup serves), and an
        app restore of a backup without it would bring OnionHome up with an
        empty registry, every name free to claim."""
        meta, members = self._backup(include_hub_data=True, hub=False, onionhome=True)
        self.assertIn("onionhome/onionnames.db", members)
        self.assertIs(meta["is_onionhome"], True)
        self.assertNotIn("onionhome_registry_unchecked", meta)

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

    def test_the_cli_backup_says_what_could_not_be_checked(self):
        cli = object.__new__(OnionPressCLI)
        cli.paths = mock.Mock(data_dir=self.data_dir)
        cli.containers = mock.Mock(get_onion_address=mock.Mock(return_value=mv.ADDR))
        cli.log = self.logs.append
        for meta, want in (
                ({"onionheaven_data_unchecked": True, "onionhome_registry_unchecked": True},
                 [backup.HUB_DATA_UNCHECKED_NOTE, backup.ONIONHOME_REGISTRY_UNCHECKED_NOTE]),
                ({"onionhome_registry_unchecked": True},
                 [backup.ONIONHOME_REGISTRY_UNCHECKED_NOTE]),
                ({"is_onionhome": False}, [])):
            with self.subTest(meta=meta):
                err = io.StringIO()
                with mock.patch("onionpress.backup.verify_wp_admin", return_value=(True, None)), \
                        mock.patch("onionpress.backup.create_backup", return_value=meta), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(err):
                    self.assertEqual(cli.cmd_backup(PW, os.path.join(self.tmp, "b.zip"),
                                                    "admin"), 0)
                self.assertEqual(err.getvalue().splitlines(),
                                 [f"WARNING: {note}" for note in want])

    def test_the_apps_finished_backup_says_what_could_not_be_checked(self):
        """src/menubar.py's Backup... (AppKit, so checked as source) runs
        `onionpress backup --json` (cmd_backup, include_hub_data=True) and
        shows the notes the CLI answers with."""
        cli_src = mv._read("src/onionpress/cli.py")
        body = cli_src[cli_src.index("    def cmd_backup("):cli_src.index("    def cmd_restore_check(")]
        self.assertIn("include_hub_data=True", body)
        self.assertIn("notes.extend(unchecked_notes(metadata))", body)
        self.assertIn("notes=notes)", body)
        text = mv._read("src/menubar.py")
        run = text.index('["backup", "--json", "-o", output_path, "--user", username]')
        body = text[run:text.index("threading.Thread(target=do_backup", run)]
        notes = body.index('for note in ans.get("notes") or []:')
        self.assertLess(notes, body.index("pw.finish(\n                msg,"))
        self.assertIn('msg += "\\n\\n" + note', body[notes:])

    def test_unchecked_notes(self):
        notes = backup.unchecked_notes
        self.assertEqual(notes({}), [])
        self.assertEqual(notes(None), [])
        self.assertEqual(notes(mock.Mock()), [])
        # Only a real true: metadata.json is read back from a file too.
        for value in ("true", 1, "yes", False, None):
            with self.subTest(value=value):
                self.assertEqual(notes({"onionheaven_data_unchecked": value,
                                        "onionhome_registry_unchecked": value}), [])
        self.assertEqual(notes({"onionhome_registry_unchecked": True,
                                "onionheaven_data_unchecked": True}),
                         [backup.HUB_DATA_UNCHECKED_NOTE,
                          backup.ONIONHOME_REGISTRY_UNCHECKED_NOTE])


class TestLinuxCreateBackup(unittest.TestCase):
    """linux/onionpress create_backup, run for real with docker stubbed and
    the shared Python helpers it calls."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
docker() {
    case "$1 $2" in
        "exec onionpress-tor") [ -z "$NO_ADDR" ] || return 1; echo "$ADDR" ;;
        "image inspect") return 0 ;;     # the key helpers' tor image is here
        "volume ls") return 0 ;;         # no key volume: nothing is read
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
    import os
    fails = os.environ.get("CHECK_FAILS", "")
    if fails == "crash":
        raise RuntimeError("the check crashed")
    if fails == "says-something":
        print("half a reason")
        sys.exit(1)
    if fails == "usage":
        print("usage: onionpress [-h] ...", file=sys.stderr)
        sys.exit(2)
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

    def _run(self, *args, recovery_key=False, no_address=False, check_fails="", **container):
        tmp = tempfile.mkdtemp(dir=self.tmp)       # a run of its own
        c = _Container(self, tmp, **container)
        self.container = c
        out = os.path.join(tmp, "backup.zip")
        home = os.path.join(tmp, "home")
        if recovery_key:
            from onionpress import hub_backup
            self.private_text, self.key_id = hub_backup.create_recovery_key(
                os.path.join(home, ".onionpress"))
        tmpdir = os.path.join(tmp, "t")
        os.makedirs(tmpdir, exist_ok=True)
        env = {
            "PATH": os.environ["PATH"], "HOME": home, "TMPDIR": tmpdir,
            "DATA_DIR": os.path.join(home, ".onionpress"),
            "LOG_FILE": os.path.join(tmp, "log"),
            "SRC": mv.SRC, "PY": sys.executable, "PW": PW, "ADDR": mv.ADDR,
            "WORDPRESS_DB_PASSWORD": "x", "ONIONPRESS_VERSION": "2.5.3",
            "DOCKER_HOST": DOCKER_HOST, "NO_ADDR": "1" if no_address else "",
            "CHECK_FAILS": check_fails,
        }
        is_onion = mv._bash_function(mv._read("linux/onionpress"), "is_onion_address")
        call = " ".join(["create_backup", '"$PW"', repr(out)] + list(args))
        r = subprocess.run(
            ["bash", "-c", self.PRELUDE + is_onion + mv._linux_key_helpers()
             + _linux_create_backup()
             + f'\n{call}\necho "rc=$? refusal=$BACKUP_REFUSAL"'],
            capture_output=True, text=True, timeout=120, env=env)
        try:
            with open(env["LOG_FILE"]) as f:
                log = f.read()
        except FileNotFoundError:
            log = ""
        self.stderr, self.stdout = r.stderr, r.stdout
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

    def _assert_unverified(self, stdout, log, out):
        self.assertIn(f"rc=3 refusal={backup.UNVERIFIED_BACKUP_REFUSED_MSG}\n", stdout, log)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self.container.hub_reads(), [])
        self.assertIn("refused (the Settings page's backup)", log)

    def test_without_hub_data_no_answer_from_onionheaven_is_refused(self):
        """Fails closed, as on the Mac: a hub whose onionheaven container
        does not answer cannot say it is no hub."""
        self._assert_unverified(*self._run("--without-hub-data",
                                           unanswered=("onionheaven",)))

    def test_without_hub_data_no_answer_about_onionhome_is_refused(self):
        self._assert_unverified(*self._run("--without-hub-data", hub=False, onionhome=True,
                                           unanswered=("onionpress-tor",)))

    def test_without_hub_data_no_address_is_refused(self):
        """No address from the tor container (it stays empty, as for the
        owner's backup): the canonical hub's and OnionHome's addresses
        cannot be ruled out."""
        self._assert_unverified(*self._run("--without-hub-data", hub=False, no_address=True))

    def test_without_hub_data_a_check_that_fails_is_refused_in_the_macs_words(self):
        """Only exit 3 says why on stdout. A check that crashes, prints
        something and fails, or is not known to an older CLI is refused
        with UNVERIFIED_BACKUP_REFUSED_MSG, the Mac's words for the case
        (the launcher's copy of them is held equal here)."""
        for fails in ("crash", "says-something", "usage"):
            with self.subTest(fails=fails):
                self._assert_unverified(*self._run("--without-hub-data", hub=False,
                                                   check_fails=fails))
                self.assertNotIn("half a reason", self.stdout)

    def test_the_owners_backup_needs_no_answer(self):
        """Without --without-hub-data nothing is asked: the owner's backup
        is made even when the containers do not answer, and says the hub
        data is not in it (metadata.json, HUB_DATA_UNCHECKED_NOTE on the
        terminal: the launcher's copy of the words is held equal here)."""
        stdout, log, out = self._run(hub=False, unanswered=("onionheaven", "onionpress-tor"))
        self.assertIn("rc=0 refusal=\n", stdout, log)
        self.assertTrue(os.path.exists(out))
        meta = json.loads(bsl._inner_names_and_bytes(out, PW)["metadata.json"])
        self.assertIs(meta["onionheaven_data_unchecked"], True)
        self.assertIn(f"WARNING: {backup.HUB_DATA_UNCHECKED_NOTE}\n", self.stderr)
        self.assertIn("did not say whether it holds OnionHeaven data", log)

    def test_a_registry_sqlite3_cannot_read_the_owners_backup_says_so(self):
        stdout, log, out = self._run(keys=False, sqlite3_fails=True)
        self.assertIn("rc=0 refusal=\n", stdout, log)
        members = bsl._inner_names_and_bytes(out, PW)
        _assert_no_hub_data(self, members)
        meta = json.loads(members["metadata.json"])
        self.assertIs(meta["onionheaven_data_unchecked"], True)
        self.assertIs(meta["has_onionheaven"], False)
        self.assertIn(backup.HUB_DATA_UNCHECKED_NOTE, self.stderr)

    def test_without_hub_data_a_registry_sqlite3_cannot_read_is_refused(self):
        self._assert_unverified(*self._run("--without-hub-data", keys=False,
                                           sqlite3_fails=True))

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
        self.assertNotIn("onionheaven_data_unchecked", meta)
        stdout, log, out = self._run(hub=False)          # the owner's
        meta = json.loads(bsl._inner_names_and_bytes(out, PW)["metadata.json"])
        self.assertNotIn("onionheaven_data_unchecked", meta)
        self.assertNotIn(backup.HUB_DATA_UNCHECKED_NOTE, self.stderr)

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
               "linux/onionpress-tray", "linux/onionpress-heartbeat.py"]

    def _python_sources(self):
        root = os.path.join(PROJECT_ROOT, "src")
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT)
                    yield rel, mv._read(rel)

    def test_the_handoff_to_wordpress_is_only_the_settings_pages_backup(self):
        # One hand-off, in the shared handler's create-backup (issue #366):
        # after the launcher's settings-page-backup, which is the page's
        # backup on both platforms.
        found = []
        for rel in self.SOURCES + [r for r, _ in self._python_sources()]:
            text = mv._read(rel)
            for m in self.HANDOFF.finditer(text):
                found.append(rel)
        self.assertEqual(sorted(set(found)), ["src/onionpress/handle_action.py"])
        handler = mv._read("src/onionpress/handle_action.py")
        self.assertEqual(len(self.HANDOFF.findall(handler)), 1)
        branch = handler[handler.index("def _create_backup(host):"):handler.index("# -- restore --")]
        self.assertLess(branch.index('["settings-page-backup", path]'),
                        self.HANDOFF.search(branch).start())

    def test_the_settings_pages_backups_leave_the_hub_data_out(self):
        cli_src = mv._read("src/onionpress/cli.py")
        mac = cli_src[cli_src.index("def cmd_settings_page_backup("):cli_src.index("def cmd_settings_page_restore(")]
        create = mac.index("create_backup(")
        call = mac[create:mac.index(")", mac.index("include_onion_key", create))]
        self.assertIn("include_hub_data=False", call)
        linux = _linux_case(mv._read("linux/onionpress"), "settings-page-backup")
        self.assertRegex(linux, r'create_backup "\$BACKUP_PASS" "\$2" --without-hub-data\b')
        # The Mac launcher's case is the CLI's command
        mac_launcher = _linux_case(mv._read("app/MacOS/onionpress"),
                                   "settings-page-backup|settings-page-restore")
        self.assertIn('bundled_python -m onionpress.cli --data-dir "$DATA_DIR" "$1" "${@:2}"', mac_launcher)

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
        # cmd_backup (the owner's) and cmd_settings_page_backup (the page's),
        # both in the CLI (issue #366); the menubar calls neither.
        self.assertEqual(len(calls), 2, calls)
        for rel, call in calls:
            with self.subTest(rel, call=call):
                self.assertRegex(call, r"include_hub_data=(True|False)\b")
        falses = [(rel, c) for rel, c in calls if "include_hub_data=False" in c]
        self.assertEqual(len(falses), 1)
        self.assertEqual(falses[0][0], "src/onionpress/cli.py")

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


def _linux_case(text, name):
    """The body of the `<name>)` case of a launcher's main, up to its ;;."""
    label = f"\n        {name})\n"
    start = text.index(label) + len(label)
    return text[start:text.index("\n            ;;\n", start)]


class TestLinuxSettingsPageBackupBranch(unittest.TestCase):
    """linux/onionpress's settings-page-backup case (what the shared
    handler runs for the page's create-backup), run for real with
    create_backup stubbed: it asks for the hub data to be left out, takes
    the password from the environment, and a refusal is exit 3 with the
    reason on stdout; the hand-off to WordPress is the handler's
    (tests/test_handle_action.py)."""

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
'''

    def _run(self, hub, refusal=backup.HUB_BACKUP_REFUSED_MSG):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = mv._read("linux/onionpress")
        case = _linux_case(text, "settings-page-backup")
        helpers = mv._bash_function(text, "read_backup_password")
        out = os.path.join(tmp, "OnionPress-x.zip")
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(tmp, "home"),
            "TMPDIR": tmp, "LOG_FILE": os.path.join(tmp, "log"),
            "ARGS": os.path.join(tmp, "args"), "ONIONPRESS_BACKUP_PASSWORD": "pw",
            "HUB": "1" if hub else "0", "DOCKER_HOST": DOCKER_HOST, "REFUSAL": refusal,
        }
        r = subprocess.run(["bash", "-c", self.PRELUDE + helpers
                            + f'set -- settings-page-backup "{out}"\n' + case + "\n"],
                           capture_output=True, text=True, timeout=60, env=env)
        with open(env["ARGS"]) as f:
            args = f.read().split()
        return r, args, out

    def test_a_hub_hears_why_it_was_refused(self):
        r, args, out = self._run(hub=True)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(args, ["pw", out, "--without-hub-data", "--without-host-secrets"])
        self.assertEqual(r.stdout.strip().splitlines()[-1], backup.HUB_BACKUP_REFUSED_MSG)
        self.assertFalse(os.path.exists(out))

    def test_a_node_that_could_not_be_checked_hears_why(self):
        r, args, out = self._run(hub=True, refusal=backup.UNVERIFIED_BACKUP_REFUSED_MSG)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout.strip().splitlines()[-1], backup.UNVERIFIED_BACKUP_REFUSED_MSG)

    def test_any_other_node_gets_its_backup(self):
        r, args, out = self._run(hub=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(args[2:], ["--without-hub-data", "--without-host-secrets"])
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"backup bytes")

    def test_the_password_never_reaches_argv(self):
        text = mv._read("linux/onionpress")
        case = _linux_case(text, "settings-page-backup")
        self.assertIn("read_backup_password", case)
        self.assertNotIn("$3", case)


class TestMacSettingsPageBackupBranch(unittest.TestCase):
    """The Mac's page backup, `onionpress settings-page-backup`
    (cli.cmd_settings_page_backup) run by the shared handler: a refusal
    (exit 3) reaches the page as the result's error, and nothing is handed
    to WordPress."""

    def test_the_refusal_is_the_pages_error(self):
        cli_src = mv._read("src/onionpress/cli.py")
        body = cli_src[cli_src.index("def cmd_settings_page_backup("):cli_src.index("def cmd_settings_page_restore(")]
        self.assertIn("except SettingsPageBackupRefused as e:\n"
                      "        return _confirm.answer_error(e, json_output, code=_confirm.EXIT_REFUSED)", body)
        # Its one note says the onion service key is not in it
        # (tests/test_settings_page_onion_key.py), never a hub's.
        self.assertEqual(re.findall(r'"note"\]? = ([\w.]+)', body), ["ONION_KEY_LEFT_OUT_NOTE"])
        handler = mv._read("src/onionpress/handle_action.py")
        branch = handler[handler.index("def _create_backup(host):"):handler.index("# -- restore --")]
        refused = branch.index("if r.returncode == confirm.EXIT_REFUSED:")
        self.assertLess(refused, branch.index('umask 077; cat > "$1"'))
        self.assertIn('"success": False, "error": refusal', branch[refused:])
        self.assertEqual(branch.count('result["note"]'), 1)

    def test_the_messages_are_plain_text(self):
        """They go into the page, and Linux's into JSON."""
        for text in (backup.HUB_BACKUP_REFUSED_MSG, backup.ONIONHOME_BACKUP_REFUSED_MSG,
                     backup.UNVERIFIED_BACKUP_REFUSED_MSG,
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
        unverified = backup.UNVERIFIED_BACKUP_REFUSED_MSG
        self.assertIsNone(refuse(mv.ADDR, "none", data_dir=self.tmp))
        # Not checked (a container did not answer, no address): refused,
        # fail closed.
        for state in (None, "", "hub?"):
            with self.subTest(hub_state=state):
                self.assertEqual(refuse(mv.ADDR, state, data_dir=self.tmp), unverified)
        for address in ("", None):
            with self.subTest(address=address):
                self.assertEqual(refuse(address, "none", data_dir=self.tmp), unverified)
        self.onionhome.return_value = None
        self.assertEqual(refuse(mv.ADDR, "none", data_dir=self.tmp), unverified)
        # What is known still says which it is.
        self.assertEqual(refuse(mv.ADDR, "hub", data_dir=self.tmp), hub)
        self.assertEqual(refuse(backup.ONIONHOME_ADDRESS, None, data_dir=self.tmp), home)
        self.onionhome.return_value = False
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
        unverified = backup.UNVERIFIED_BACKUP_REFUSED_MSG
        for hub_state, onionhome, address, rc, want in (
                ("hub", False, mv.ADDR, 3, backup.HUB_BACKUP_REFUSED_MSG),
                ("none", True, mv.ADDR, 3, backup.ONIONHOME_BACKUP_REFUSED_MSG),
                (None, False, mv.ADDR, 3, unverified),
                ("none", None, mv.ADDR, 3, unverified),
                ("none", False, "", 3, unverified),
                ("none", False, mv.ADDR, 0, "")):
            with self.subTest(hub_state=hub_state, onionhome=onionhome, address=address):
                self.onionhome.return_value = onionhome
                out = io.StringIO()
                with mock.patch.object(backup, "hub_data_state", return_value=hub_state), \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(OnionPressCLI.cmd_settings_page_backup_check(
                        address, self.tmp), rc)
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


class TestOnionHomeRegistryCheck(unittest.TestCase):
    """backup.onionhome_registry_present, which the refusal asks."""

    def test_onionhomes_registry_check_answers_only_yes_or_no(self):
        """A failed `docker exec` and a missing file both exit 1: the
        answer is on stdout, and anything but a plain yes or no is no
        answer (None), never "not OnionHome"."""
        cases = (
            (subprocess.CompletedProcess([], 0, "yes\n", ""), True),
            (subprocess.CompletedProcess([], 0, "no\n", ""), False),
            (subprocess.CompletedProcess([], 1, "", "Error response from daemon: "
                                         "container onionpress-tor is not running"), None),
            (subprocess.CompletedProcess([], 1, "no\n", ""), None),
            (subprocess.CompletedProcess([], 0, "", ""), None),
            (subprocess.CompletedProcess([], 0, "yes no\n", ""), None),
            (subprocess.TimeoutExpired(["docker"], 10), None),
            (FileNotFoundError("docker"), None),
        )
        for result, want in cases:
            with self.subTest(result=result):
                with mock.patch.object(backup.subprocess, "run",
                                       side_effect=[result]) as fake:
                    self.assertIs(backup.onionhome_registry_present(), want)
                argv = fake.call_args.args[0]
                self.assertEqual(argv[:6], ["docker", "exec", "onionpress-tor", "sh", "-c",
                                            'if [ -f "$1" ]; then echo yes; else echo no; fi'])
                self.assertEqual(argv[6:], ["sh", "/var/lib/onionnames/onionnames.db"])
        # The script itself, on a sandbox folder: yes only for a file.
        script = argv[5]
        tmp = tempfile.mkdtemp(prefix="op-onionhome-check-")
        self.addCleanup(shutil.rmtree, tmp, True)
        f = os.path.join(tmp, "onionnames.db")
        d = os.path.join(tmp, "a-folder")
        os.makedirs(d)
        with open(f, "w") as fh:
            fh.write("x")
        for path, want in ((f, "yes"), (d, "no"), (os.path.join(tmp, "none"), "no")):
            r = subprocess.run(["sh", "-c", script, "sh", path], capture_output=True,
                               text=True, timeout=10)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, want), path)


class TestHubDataStateScript(unittest.TestCase):
    """backup.HUB_DATA_STATE_SH, which hub_data_state runs in onionheaven:
    run here for real (sh, ls, sqlite3) on a folder standing in for
    /var/lib/onionheaven. "none" only when that is known; anything it
    cannot read is no answer (None), which the Settings page's backup
    refuses and the owner's backup names as left out. Its sqlite3 query
    ran under 2>/dev/null and a failure printed "none"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hub-state-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = os.path.join(self.tmp, "hub")
        os.makedirs(self.hub)
        self.registry = os.path.join(self.hub, "registry.db")

    def _state(self, path=None):
        """hub_data_state(), its `docker exec onionheaven sh -c` run here
        on the sandbox folder (*path*: the script's PATH)."""
        real_run = subprocess.run
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(argv[:5], ["docker", "exec", "onionheaven", "sh", "-c"])
            script = argv[5].replace(backup.ONIONHEAVEN_DATA_DIR, self.hub)
            env = dict(os.environ, PATH=path or os.environ["PATH"])
            return real_run(["/bin/sh", "-c", script], env=env, **kwargs)

        with mock.patch.object(backup.subprocess, "run", side_effect=run):
            state = backup.hub_data_state()
        self.assertEqual(len(calls), 1)
        return state

    def _registry(self, rows, table="registry", journal_mode=None):
        conn = sqlite3.connect(self.registry)
        if journal_mode:
            conn.execute(f"PRAGMA journal_mode={journal_mode}")
        conn.execute(f"CREATE TABLE {table} (content_address TEXT)")
        conn.executemany(f"INSERT INTO {table} VALUES (?)", [(A,)] * rows)
        conn.commit()
        conn.close()

    def test_known_answers(self):
        self.assertEqual(self._state(), "none")         # nothing at all
        self.assertFalse(os.path.lexists(self.registry), "never makes a registry.db")
        os.makedirs(os.path.join(self.hub, "keys"))
        self.assertEqual(self._state(), "none")         # an empty keys/
        open(self.registry, "wb").close()
        self.assertEqual(self._state(), "none")         # an empty file: no rows
        self.assertEqual(os.path.getsize(self.registry), 0)
        os.remove(self.registry)
        self._registry(rows=0, table="takeover_containers")
        self.assertEqual(self._state(), "none")         # no registry table: no rows
        os.remove(self.registry)
        self._registry(rows=0, journal_mode="WAL")
        self.assertEqual(self._state(), "none")         # a status query made the schema
        os.remove(self.registry)
        self._registry(rows=2)
        self.assertEqual(self._state(), "hub")          # registry rows, no keys yet
        os.remove(self.registry)
        os.makedirs(os.path.join(self.hub, "keys", A))
        self.assertEqual(self._state(), "hub")          # a registrant's key folder

    def test_a_registry_sqlite3_cannot_read_is_no_answer(self):
        self._registry(rows=1)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        failing = os.path.join(bin_dir, "sqlite3")
        with open(failing, "w") as f:
            f.write('#!/bin/sh\necho "Error: database is locked" >&2\nexit 5\n')
        os.chmod(failing, 0o755)
        # sqlite3 failing (locked past the busy timeout, an I/O error).
        self.assertIsNone(self._state(path=bin_dir + os.pathsep + os.environ["PATH"]))
        # No sqlite3 at all: only ls on PATH.
        os.remove(failing)
        os.symlink(shutil.which("ls"), os.path.join(bin_dir, "ls"))
        self.assertIsNone(self._state(path=bin_dir))
        # A damaged registry, or not a database.
        with open(self.registry, "wb") as f:
            f.write(b"not a database " * 64)
        self.assertIsNone(self._state())

    def test_a_registry_being_written_is_waited_for(self):
        """A writer's lock is waited out (sqlite3's busy timeout), not taken
        for no answer: a busy hub's Settings page backup is refused as a
        hub's, not as an unchecked node's, and its owner's backup holds
        its data."""
        import threading
        self._registry(rows=1, journal_mode="DELETE")
        writer = sqlite3.connect(self.registry, isolation_level=None,
                                 check_same_thread=False)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO registry VALUES ('b')")
        done = threading.Timer(1.5, writer.execute, ("COMMIT",))
        done.start()
        self.addCleanup(done.cancel)
        self.assertEqual(self._state(), "hub")
        done.join()

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads it anyway")
    def test_a_keys_folder_it_cannot_list_is_no_answer(self):
        keys = os.path.join(self.hub, "keys")
        os.makedirs(keys)
        os.chmod(keys, 0)
        self.addCleanup(os.chmod, keys, 0o700)
        self.assertIsNone(self._state())

    def test_no_data_folder_is_no_answer(self):
        shutil.rmtree(self.hub)
        self.assertIsNone(self._state())


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
        # A site node serving the backup's own address: the node check
        # passes and no change of address is asked about
        # (tests/test_settings_page_onion_key.py has those).
        for name, value in (("served_onion_address", mv.ADDR),
                            ("hub_data_state", "none"),
                            ("onionhome_registry_present", False)):
            p = mock.patch.object(backup, name, return_value=value)
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
        # The in-place restore is the CLI's settings-page-restore (issue
        # #366), never the install-from-backup `restore`.
        src = mv._read("src/onionpress/cli.py")
        body = src[src.index("def cmd_settings_page_restore("):src.index("def main(")]
        self.assertRegex(body, r"restore_from_backup\(backup_file, password, log, "
                               r"through_wordpress=True,")
        # The only caller in what ships.
        callers = [rel for rel in ("src/menubar.py", "src/onionpress/cli.py",
                                   "src/onionpress/setup_window.py", "src/onionpress/handle_action.py")
                   if "restore_from_backup(" in mv._read(rel)]
        self.assertEqual(callers, ["src/onionpress/cli.py"])
        self.assertEqual(src.count("restore_from_backup("), 1)
        handler = mv._read("src/onionpress/handle_action.py")
        self.assertIn('["settings-page-restore", local_zip, "--json"]', handler)
        self.assertNotIn('"restore", ', handler[handler.index("def _settings_page_restore("):])


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
            backup.restore_container_artifacts(staging, metadata or {}, logs.append,
                                               data_dir=self.tmp, **kw)
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

    # The tor container serves $CURRENT (read, not recorded as a change);
    # the node check answers $NODE_RC / $NODE_REFUSAL (a site node by
    # default: tests/test_settings_page_onion_key.py has the others).
    PRELUDE = r'''
log() { echo "[log] $1" | tee -a "$LOG_FILE"; }
docker() {
    if [ "$*" = "exec onionpress-tor cat /var/lib/tor/hidden_service/wordpress/hostname" ]; then
        echo "$CURRENT"; return 0
    fi
    echo "docker $*" >> "$CALLS"; return 0
}
db_client() { echo "db_client $*" >> "$CALLS"; return 0; }
db_import() { echo "db_import $*" >> "$CALLS"; return 0; }
setup_db_passwords() { :; }
python3() { "$PY" "$@"; }
op_py() {
    if [ "$1 $2 $3" = "--data-dir $DATA_DIR settings-page-restore-node-check" ]; then
        [ -n "${NODE_CHECKS:-}" ] && echo "$5" >> "$NODE_CHECKS"
        [ -n "${NODE_REFUSAL:-}" ] && echo "$NODE_REFUSAL"
        return "${NODE_RC:-0}"
    fi
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
            "DOCKER_HOST": DOCKER_HOST, "CURRENT": mv.ADDR,
            "NODE_CHECKS": os.path.join(self.tmp, "node-checks"),
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
    """linux/onionpress's settings-page-restore case (what the shared
    handler runs for the page's restore-backup): restore_backup
    --from-settings-page on the handler's copy, the password from the
    environment; a refusal is exit 3 with the reason as the last line of
    stdout and the heartbeat client started again."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
setup_db_passwords() { :; }
start_heartbeat_client() { echo start_heartbeat_client >> "$CALLS"; }
restore_backup() {
    echo "restore_backup $2 $3" >> "$CALLS"
    echo "[log] Restore: extracting backup archive..."
    if [ -n "$REFUSAL" ]; then
        echo "$REFUSAL"
        return 3
    fi
    echo "$ADDR"
    return 0
}
'''

    def _run(self, refusal):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = mv._read("linux/onionpress")
        case = _linux_case(text, "settings-page-restore")
        helpers = mv._bash_function(text, "read_backup_password")
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(tmp, "home"),
            "TMPDIR": tmp, "LOG_FILE": os.path.join(tmp, "log"),
            "CALLS": os.path.join(tmp, "calls"), "ONIONPRESS_BACKUP_PASSWORD": "pw",
            "REFUSAL": refusal, "ADDR": mv.ADDR, "DOCKER_HOST": DOCKER_HOST,
        }
        r = subprocess.run(["bash", "-c", self.PRELUDE + helpers
                            + 'set -- settings-page-restore /work/restore-upload.zip --json\n'
                            + case + "\n"],
                           capture_output=True, text=True, timeout=60, env=env)
        try:
            with open(env["CALLS"]) as f:
                calls = f.read().splitlines()
        except FileNotFoundError:
            calls = []
        return r, calls

    def test_the_refusal_reaches_the_page(self):
        r, calls = self._run(backup.HUB_DATA_RESTORE_REFUSED_MSG)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], backup.HUB_DATA_RESTORE_REFUSED_MSG)
        self.assertEqual(calls, ["restore_backup /work/restore-upload.zip --from-settings-page",
                                 "start_heartbeat_client"])

    def test_a_restore_prints_the_address_and_leaves_the_restart_to_the_handler(self):
        r, calls = self._run("")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], mv.ADDR)
        self.assertEqual(calls, ["restore_backup /work/restore-upload.zip --from-settings-page"])


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
