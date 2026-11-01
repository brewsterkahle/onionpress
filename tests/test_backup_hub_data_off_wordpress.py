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
        text = mv._read("src/onionpress/ui_client.py")
        body = text[text.index("def backup_done_message("):text.index("class RestoreUI")]
        notes = body.index('for note in answer.get("notes") or []:')
        self.assertIn('msg += "\\n\\n" + note', body[notes:])
        self.assertIn("BackupResult(True, backup_done_message(ans, plan), ans)", body)
        menubar = mv._read("src/menubar.py")
        run = menubar.index("ui_client.run_backup(self.launcher, plan")
        self.assertIn("result.message", menubar[run:menubar.index("threading.Thread(target=do_backup", run)])

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
        # Both launchers' cases are the CLI's command (issue #406).
        linux = _linux_case(mv._read("linux/onionpress"), "settings-page-backup")
        self.assertIn('op_py --data-dir "$DATA_DIR" settings-page-backup "${@:2}"', linux)
        self.assertNotIn("create_backup", linux)
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

    def test_the_tray_and_scrub_go_through_the_launchers_backup(self):
        """They write to ~/OnionPress/backups, never to WordPress."""
        self.assertIn('ui_client.run_backup(Launcher, plan', mv._read("linux/onionpress-tray"))
        self.assertIn('launcher.run_json("backup", "--check", "--json"', mv._read("src/onionpress/ui_client.py"))
        self.assertIn('args = ["backup", "--json", *(["-o", output] if output else []), *user_args]',
                      mv._read("src/onionpress/ui_client.py"))
        self.assertIn('[LAUNCHER_BIN, "backup", "--output"', mv._read("src/onionpress/scrub.py"))


def _linux_case(text, name):
    """The body of the `<name>)` case of a launcher's main, up to its ;;."""
    label = f"\n        {name})\n"
    start = text.index(label) + len(label)
    return text[start:text.index("\n            ;;\n", start)]


class TestLinuxSettingsPageBranches(unittest.TestCase):
    """linux/onionpress's settings-page-backup and settings-page-restore
    cases (what the shared handler runs for the page's create-backup and
    restore-backup), run for real with op_py stubbed: each is the CLI's
    command of the same name (issue #406), the handler's arguments passed
    on, the password left in the environment for the CLI to read, and the
    CLI's exit status the case's. What the CLI does is tested in Python
    (TestCreateBackup, TestRestoreFromBackupThroughWordPress and
    tests/test_settings_page_onion_key.py)."""

    PRELUDE = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
setup_db_passwords() { echo setup_db_passwords >> "$CALLS"; }
op_py() {
    echo "op_py $*" >> "$CALLS"
    echo "password=${ONIONPRESS_BACKUP_PASSWORD:-none}" >> "$CALLS"
    echo "$ANSWER"
    return "$RC"
}
'''

    def _run(self, case, *args, rc=0, answer=""):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        text = mv._read("linux/onionpress")
        body = _linux_case(text, case)
        env = {
            "PATH": os.environ["PATH"], "HOME": os.path.join(tmp, "home"),
            "TMPDIR": tmp, "LOG_FILE": os.path.join(tmp, "log"), "DATA_DIR": "/d",
            "CALLS": os.path.join(tmp, "calls"), "ONIONPRESS_BACKUP_PASSWORD": "pw",
            "RC": str(rc), "ANSWER": answer, "DOCKER_HOST": DOCKER_HOST,
        }
        argv = " ".join(repr(a) for a in (case,) + args)
        r = subprocess.run(["bash", "-c", self.PRELUDE + f"set -- {argv}\n" + body + "\n"],
                           capture_output=True, text=True, timeout=60, env=env)
        with open(env["CALLS"]) as f:
            calls = f.read().splitlines()
        return r, calls

    def test_the_backup_is_the_clis(self):
        r, calls = self._run("settings-page-backup", "/work/out.zip", "--json",
                             answer='{"ok": true}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, ["setup_db_passwords",
                                 "op_py --data-dir /d settings-page-backup /work/out.zip --json",
                                 "password=pw"])
        self.assertEqual(r.stdout.strip().splitlines()[-1], '{"ok": true}')

    def test_a_refusal_is_the_clis_exit_3_and_answer(self):
        answer = json.dumps({"error": backup.HUB_BACKUP_REFUSED_MSG})
        r, calls = self._run("settings-page-backup", "/work/out.zip", "--json", rc=3, answer=answer)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout.strip().splitlines()[-1], answer)

    def test_the_restore_is_the_clis_with_the_handlers_flags(self):
        r, calls = self._run("settings-page-restore", "/work/restore-upload.zip", "--json",
                             "--confirmed", "address-change")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, ["setup_db_passwords",
                                 "op_py --data-dir /d settings-page-restore "
                                 "/work/restore-upload.zip --json --confirmed address-change",
                                 "password=pw"])

    def test_a_refused_restore_is_exit_3_with_nothing_else_run(self):
        r, calls = self._run("settings-page-restore", "/work/restore-upload.zip", "--json",
                             rc=3, answer=json.dumps({"error": backup.ADDRESS_CHANGE_REFUSED_MSG}))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(len([c for c in calls if c.startswith("op_py")]), 1)
        # No heartbeat or restart from the case: the CLI stops and starts
        # the heartbeat client (onionpress.heartbeat_unit), the handler restarts.
        for case in ("settings-page-backup", "settings-page-restore"):
            body = _linux_case(mv._read("linux/onionpress"), case)
            self.assertNotIn("heartbeat", body.replace("heartbeat_unit", "").replace(
                "heartbeat client", ""))
            self.assertNotIn("$3", body)
            self.assertNotIn("read_backup_password", body)


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

    def test_the_addresses_are_the_tor_images(self):
        tor_dir = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor")
        sys.path.insert(0, tor_dir)
        try:
            import onionheaven_common as oc
        finally:
            sys.path.remove(tor_dir)
        self.assertEqual(backup.ONIONHEAVEN_HUB_ADDRESS, oc.ONIONHEAVEN_HUB_ADDRESS)
        # web-server.py takes OnionHome's address from the shared module
        # (issue #393: the entrypoint asks it whether to expose port 8083).
        self.assertEqual(backup.ONIONHOME_ADDRESS, oc.ONIONHOME_ADDRESS)
        web = mv._read("app/Resources/docker/tor/web-server.py")
        self.assertIn("_ONIONHOME_ADDRESS = ONIONHOME_ADDRESS", web)
        from onionpress import onionnames_registrar
        self.assertEqual(backup.ONIONHOME_ADDRESS, onionnames_registrar.DEFAULT_ONIONHOME_ADDRESS)

    def test_not_on_server_active(self):
        """status.json's onionheaven.server_active is the status page's
        view (by address since #345; before, true on every Linux node):
        no refusal may rest on it."""
        text = mv._read("src/onionpress/backup.py")
        start = text.index("settings_page_backup_refusal")
        self.assertNotIn("server_active", text[start:start + 3000].replace(
            "status.json's onionheaven.server_active", "").replace(
            "status.json's server_active", ""))
        # And the Linux launcher no longer has a check of its own.
        self.assertNotIn("server_active", _linux_case(mv._read("linux/onionpress"),
                                                      "settings-page-backup"))


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


# ---------------------------------------------------------------------------
# The Settings page tells the owner
# ---------------------------------------------------------------------------

class TestSettingsPageSaysSo(unittest.TestCase):

    def setUp(self):
        self.php = mv._read("app/Resources/plugins/onionpress-settings.php")
        self.backup = _branch(self.php, "<h2>Backup</h2>", "<!-- Restore -->")
        self.restore = _branch(self.php, "<h2>Restore</h2>", "</form>")

    def test_the_backup_section(self):
        # On every node: status.json's server_active is the status page's
        # view (true on every Linux node before #345), so the page does not
        # try to tell.
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
