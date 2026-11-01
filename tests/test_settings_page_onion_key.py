#!/usr/bin/env python3
"""Code running in WordPress never reads or replaces the site's own onion
service key without the owner at the host.

Anything executing in WordPress (www-data) can write the Settings page's
request files, so the page's owner check is no boundary for what the host
does with them:

- The Settings page's backup leaves the onion service key out on both
  platforms (cli.cmd_settings_page_backup, the one path since issue #406:
  create_backup(include_onion_key=False): no tor-keys/, metadata
  excludes_onion_key, the note with the result and in README.txt). The
  app's Backup... and `onionpress backup` keep it.
- A backup without a key restores onto the install's own address, on every
  path (the page's restore, the app's and the CLI's install-from-backup,
  setup --from-backup): no key written, no key volume removed, no hub asked
  to let go, nothing new minted; with no address to keep it is refused.
- On a Mac, a page restore of a backup of another address asks the person
  at the Mac (a native dialog, Cancel the default, no answer is Cancel).
  Linux refuses it (cmd_settings_page_restore, ADDRESS_CHANGE_REFUSED_MSG),
  refuses the page's key import, and has no generate-vanity branch any more.
- On an OnionHeaven hub or OnionHome (or a node that cannot say) the page
  restores nothing, on either platform.

No docker: the container side is stubbed (a docker stub script, patched
helpers).
"""

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
import zipfile
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, cli, key_manager  # noqa: E402
from onionpress.cli import OnionPressCLI  # noqa: E402
from onionpress.platform import OS, resolve_paths  # noqa: E402
import test_backup_hub_data_off_wordpress as hd  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402
import test_backup_staging_links as bsl  # noqa: E402

PW = mv.PW
ADDR, OLD_ADDR = mv.ADDR, mv.OLD_ADDR
KEY = "ks_hs_id.ed25519_expanded_private"
DOCKER_HOST = "unix:///var/empty/no-docker-for-tests.sock"
PHP = "app/Resources/plugins/onionpress-settings.php"


def _branch(text, head, end):
    start = text.index(head)
    return text[start:text.index(end, start + len(head))]


def _keyless_backup(root, name, *, overrides=None, metadata=None):
    """A backup as the Mac Settings page makes one: no tor-keys/."""
    staging = os.path.join(root, name + ".staging")
    os.makedirs(os.path.join(staging, "database"))
    with open(os.path.join(staging, "database", "wordpress.sql"), "w") as f:
        f.write("SELECT 1;\n")
    meta = mv._metadata(excludes_onion_key=True)
    meta.update(metadata or {})
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump(meta, f)
    if overrides is not None:
        with open(os.path.join(staging, "config-overrides.json"), "w") as f:
            json.dump(overrides, f)
    out = os.path.join(root, name)
    backup.pack_backup(staging, out, PW, lambda m: None)
    shutil.rmtree(staging)
    return out


def _keyed_backup(root, name):
    """A backup with the site key of ADDR (as the app's Backup... makes)."""
    staging = hd._staging(root, name + ".staging")
    out = os.path.join(root, name)
    backup.pack_backup(staging, out, PW, lambda m: None)
    shutil.rmtree(staging)
    return out


def _menubar_method(name):
    src = mv._read("src/menubar.py")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in src/menubar.py")


class _Install:
    """~/.onionpress of an install serving OLD_ADDR."""

    def __init__(self, test, root):
        self.data = os.path.join(root, "home", ".onionpress")
        vanity = os.path.join(self.data, "shared", "vanity-keys", OLD_ADDR)
        os.makedirs(vanity)
        with open(os.path.join(vanity, "hostname"), "w") as f:
            f.write(OLD_ADDR + "\n")
        with open(os.path.join(self.data, "onion_address"), "w") as f:
            f.write(OLD_ADDR + "\n")
        self.config = "ADDRESS_PREFIX=abc\nONIONNAME=me\n"
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write(self.config)
        self.test = test

    def assert_identity_kept(self):
        t = self.test
        t.assertEqual(os.listdir(os.path.join(self.data, "shared", "vanity-keys")), [OLD_ADDR])
        t.assertEqual(os.listdir(os.path.join(self.data, "shared")), ["vanity-keys"])
        with open(os.path.join(self.data, "onion_address")) as f:
            t.assertEqual(f.read(), OLD_ADDR + "\n")
        with open(os.path.join(self.data, "config")) as f:
            t.assertEqual(f.read(), self.config)
        t.assertFalse(os.path.exists(os.path.join(self.data, ".import-key-pending")))


# ---------------------------------------------------------------------------
# The Mac's Settings page backup leaves the key out
# ---------------------------------------------------------------------------

class TestMacSettingsPageBackupLeavesTheKeyOut(unittest.TestCase):

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp(prefix="op-sp-key-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.logs = []

    def _backup(self, include_onion_key):
        hd._Container(self, self.tmp, hub=False)
        out = os.path.join(self.tmp, f"backup-{include_onion_key}.zip")
        meta = backup.create_backup(ADDR, "admin", PW, out, "2.5.3", self.logs.append,
                                    data_dir=self.data_dir, include_hub_data=False,
                                    include_host_secrets=False,
                                    include_onion_key=include_onion_key)
        return meta, out, bsl._inner_names_and_bytes(out, PW)

    def test_no_key_in_it_and_it_says_so(self):
        meta, out, members = self._backup(include_onion_key=False)
        self.assertFalse([n for n in members if n.lstrip("./").startswith("tor-keys")],
                         sorted(members))
        pem = key_manager.build_openssh_key(mv.PRIV, mv.PUB)
        for name, data in members.items():
            with self.subTest(name):
                self.assertNotIn(mv.PRIV[:32], data)
                self.assertNotIn(pem[40:120], data)
        self.assertIs(meta["excludes_onion_key"], True)
        # Still recorded: the address the backup was made from.
        self.assertEqual(meta["onion_address"], ADDR)
        staged = json.loads(next(d for n, d in members.items()
                                 if n.endswith("metadata.json")))
        self.assertIs(staged["excludes_onion_key"], True)
        with zipfile.ZipFile(out) as zf:
            readme = zf.read("README.txt").decode()
        self.assertIn("does not contain the site's onion service key", readme)
        self.assertIn("Backup... in the", readme)

    def test_the_owners_backup_keeps_it(self):
        meta, out, members = self._backup(include_onion_key=True)
        self.assertNotIn("excludes_onion_key", meta)
        self.assertTrue([n for n in members if n.endswith("tor-keys/" + KEY)], sorted(members))
        with zipfile.ZipFile(out) as zf:
            self.assertNotIn("onion service key: it was", zf.read("README.txt").decode())

    def test_the_note(self):
        self.assertEqual(backup.ONION_KEY_LEFT_OUT_NOTE,
                         "This backup does not contain your onion address's key; to "
                         "move your address to another machine, use Backup... in the "
                         "OnionPress menu.")
        self.assertRegex(backup.ONION_KEY_LEFT_OUT_NOTE, r"\A[A-Za-z0-9 .,:;'()-]+\Z")

    def test_the_settings_page_asks_for_it_and_passes_the_note_on(self):
        # The Mac's page backup is `onionpress settings-page-backup`
        # (cli.cmd_settings_page_backup); handle-action hands it to
        # WordPress with the CLI's note (issue #366).
        cli_src = mv._read("src/onionpress/cli.py")
        body = cli_src[cli_src.index("def cmd_settings_page_backup("):cli_src.index("def cmd_settings_page_restore(")]
        create = body.index("create_backup(")
        call = body[create:body.index(")\n", create)]
        self.assertIn("include_onion_key=False", call)
        self.assertIn('facts["note"] = ONION_KEY_LEFT_OUT_NOTE', body)
        handler = mv._read("src/onionpress/handle_action.py")
        branch = handler[handler.index("def _create_backup(host):"):handler.index("# -- restore --")]
        self.assertLess(branch.index('umask 077; cat > "$1"'),
                        branch.index('result["note"] = ans["note"]'))
        self.assertLess(branch.index('result["note"] = ans["note"]'),
                        branch.index('host.write_result("backup-result.json", result)'))

    def test_every_python_create_backup_call_says_which_it_is(self):
        calls = []
        for dirpath, _, files in os.walk(os.path.join(PROJECT_ROOT, "src")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT)
                text = mv._read(rel)
                for m in re.finditer(r"\bcreate_backup\(", text):
                    if text[max(0, m.start() - 4):m.start()] == "def ":
                        continue
                    line_start = text.rfind("\n", 0, m.start()) + 1
                    if "#" in text[line_start:m.start()]:
                        continue
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
                self.assertRegex(call, r"include_onion_key=(True|False)\b")
        falses = [rel for rel, c in calls if "include_onion_key=False" in c]
        self.assertEqual(falses, ["src/onionpress/cli.py"])


# ---------------------------------------------------------------------------
# A backup without a key keeps the install's address
# ---------------------------------------------------------------------------

class TestStagedOnionKeyAddress(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_keyless_and_keyed(self):
        keyless = os.path.join(self.tmp, "keyless")
        os.makedirs(keyless)
        self.assertEqual(backup.staged_onion_key_address(keyless, {"excludes_onion_key": True}), "")
        self.assertEqual(backup.staged_onion_key_address(keyless, {}), "")
        keyed = mv._make_staging(self.tmp, mv._metadata(), name="keyed")
        self.assertEqual(backup.staged_onion_key_address(keyed, {}), ADDR)

    def test_a_key_in_a_backup_that_says_it_has_none_is_refused(self):
        """Restore dialogs say from the flag that the address stays."""
        meta = mv._metadata(excludes_onion_key=True)
        staging = mv._make_staging(self.tmp, meta, name="lying")
        with self.assertRaises(ValueError) as cm:
            backup.staged_onion_key_address(staging, meta)
        self.assertEqual(str(cm.exception), backup.KEY_IN_KEYLESS_BACKUP_MSG)


class TestRestoreFromBackupKeepsTheAddress(unittest.TestCase):
    """restore_from_backup (the Mac Settings page's restore) of a backup
    without a key: nothing of the identity is touched."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = _Install(self, self.tmp)
        self.logs = []
        for target, kw in (
                ("onionpress.backup.subprocess.run", {"new": mv._NoDocker(self)}),
                ("onionpress.key_manager.write_private_key",
                 {"side_effect": AssertionError("key written")}),
                ("onionpress.backup.served_onion_address", {"return_value": OLD_ADDR}),
                ("onionpress.backup.hub_data_state", {"return_value": "none"}),
                ("onionpress.backup.onionhome_registry_present", {"return_value": False})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.artifacts = mock.Mock()
        p = mock.patch.object(backup, "restore_container_artifacts", self.artifacts)
        p.start()
        self.addCleanup(p.stop)

    def test_the_settings_page_restore(self):
        # The backed-up site's prefix is not this address's: not taken.
        self.assertFalse(OLD_ADDR.startswith(ADDR[:3]))
        path = _keyless_backup(self.tmp, "k.zip", overrides={"ADDRESS_PREFIX": ADDR[:3]})
        confirm, released = mock.Mock(return_value=True), mock.Mock()
        meta = backup.restore_from_backup(path, PW, self.logs.append,
                                          data_dir=self.install.data,
                                          through_wordpress=True,
                                          before_key_change=released,
                                          confirm_address_change=confirm)
        self.assertEqual(meta["onion_address"], OLD_ADDR)
        confirm.assert_not_called()
        released.assert_not_called()
        self.artifacts.assert_called_once()
        self.assertIs(self.artifacts.call_args.kwargs["hub_volumes"], False)
        self.install.assert_identity_kept()
        self.assertTrue([m for m in self.logs if "keeps its key and its address" in m])

    def test_without_an_address_to_keep_it_is_refused(self):
        path = _keyless_backup(self.tmp, "k.zip")
        with mock.patch.object(backup, "served_onion_address", return_value=""):
            with self.assertRaises(ValueError) as cm:
                backup.restore_from_backup(path, PW, self.logs.append,
                                           data_dir=self.install.data)
        self.assertEqual(str(cm.exception), backup.NO_KEY_TO_KEEP_MSG)
        self.artifacts.assert_not_called()
        self.install.assert_identity_kept()


class TestInstallFromBackupKeepsTheAddress(unittest.TestCase):
    """prepare_install_from_backup: the app's Restore..., `onionpress
    restore` (both platforms) and the welcome screen."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = _Install(self, self.tmp)
        self.logs = []
        p = mock.patch("onionpress.backup.subprocess.run", mv._NoDocker(self))
        p.start()
        self.addCleanup(p.stop)

    def test_the_installed_address_stays(self):
        path = _keyless_backup(self.tmp, "k.zip", overrides={"ADDRESS_PREFIX": ADDR[:3]})
        with mock.patch.object(backup, "served_onion_address", return_value=OLD_ADDR):
            staging, meta = backup.prepare_install_from_backup(
                path, PW, self.logs.append, data_dir=self.install.data)
        self.assertEqual(meta["onion_address"], OLD_ADDR)
        self.assertEqual(backup.staged_onion_key_address(staging, meta), "")
        with open(os.path.join(self.install.data, backup.INSTALL_FROM_BACKUP_MARKER)) as f:
            self.assertEqual(f.read(), staging + "\n")
        # vanity-keys/, the cached address and the config as they were.
        os.remove(os.path.join(self.install.data, backup.INSTALL_FROM_BACKUP_MARKER))
        shutil.rmtree(staging)
        self.install.assert_identity_kept()

    def test_no_address_to_keep_no_new_one(self):
        """A new install (the welcome screen) or one not running: refused
        before anything is written, never a freshly minted address."""
        path = _keyless_backup(self.tmp, "k.zip")
        with mock.patch.object(backup, "served_onion_address", return_value=""):
            with self.assertRaises(ValueError) as cm:
                backup.prepare_install_from_backup(path, PW, self.logs.append,
                                                   data_dir=self.install.data)
        self.assertEqual(str(cm.exception), backup.NO_KEY_TO_KEEP_MSG)
        self.assertFalse(os.path.exists(os.path.join(self.install.data, "restore-staging")))
        self.assertFalse(os.path.exists(
            os.path.join(self.install.data, backup.INSTALL_FROM_BACKUP_MARKER)))
        self.install.assert_identity_kept()

    def test_a_backup_with_a_key_still_seeds_it(self):
        path = _keyed_backup(self.tmp, "keyed.zip")
        with mock.patch.object(backup, "served_onion_address",
                               side_effect=AssertionError("not asked")):
            _, meta = backup.prepare_install_from_backup(
                path, PW, self.logs.append, data_dir=self.install.data)
        self.assertEqual(meta["onion_address"], ADDR)
        self.assertEqual(os.listdir(os.path.join(self.install.data, "shared", "vanity-keys")),
                         [ADDR])

    def test_the_welcome_screen_says_so_first(self):
        src = mv._read("src/onionpress/setup_window.py")
        body = src[src.index("def restoreFromBackupClicked_"):]
        body = body[:body.index("\n    def ", 10)]
        check = body.index('meta.get("excludes_onion_key") is True')
        self.assertLess(body.index("self._on_check_backup(zip_path, password)"), check)
        self.assertLess(check, body.index("self.restore_mode = True"))
        # The facts come from `onionpress prepare-restore --check --json`
        # (issue #475), which reads the backup with peek_backup_metadata.
        self.assertIn('"prepare-restore", zip_path, "--check", "--json", password=password',
                      mv._read("src/menubar.py"))
        cli_src = mv._read("src/onionpress/cli.py")
        body = cli_src[cli_src.index("def cmd_prepare_restore_check"):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn("peek_backup_metadata(backup_file, password)", body)
        self.assertIn('excludes_onion_key=meta.get("excludes_onion_key") is True', body)

    def test_the_apps_restore_dialog_says_so(self):
        src = mv._read("src/menubar.py")
        body = src[src.index("    def restore(self, _):"):]
        body = body[:body.index("\n    @rumps.clicked", 1)]
        self.assertIn("_MacRestoreUI(self)", body)
        self.assertIn("ui_client.restore_facts_text(facts)",
                      src[src.index("class _MacRestoreUI"):src.index("class OnionPressApp")])
        # The facts come from `onionpress restore --check --json`; the
        # text is ui_client's (both UIs).
        text = mv._read("src/onionpress/ui_client.py")
        text = text[text.index("def restore_facts_text("):text.index("def plan_restore(")]
        self.assertIn('facts.get("excludes_onion_key") is True', text)
        self.assertIn("keeps its onion address", text)
        cli_src = mv._read("src/onionpress/cli.py")
        self.assertIn('"excludes_onion_key": metadata.get("excludes_onion_key") is True', cli_src)


class TestCliRestoreKeepsTheKeyVolumes(unittest.TestCase):
    """cmd_restore (the app's Restore... and `onionpress restore`)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.staging)

    def _restore(self, address, keyed):
        if keyed:
            shutil.rmtree(self.staging)
            self.staging = mv._make_staging(self.tmp, mv._metadata(), name="keyed")
        obj = OnionPressCLI.__new__(OnionPressCLI)
        obj.paths = resolve_paths(data_dir=os.path.join(self.tmp, "data"), app_bundle="")
        logs = []
        obj.log = logs.append
        obj.containers = mock.Mock()
        obj.containers.ensure_tor_image.return_value = "tor@sha256:" + "0" * 64
        obj.docker = mock.Mock()
        obj.docker.run.return_value = mock.Mock(ok=True, output="")
        obj.cmd_import_backup_artifacts = mock.Mock(return_value=0)
        released = []
        with mock.patch("onionpress.backup.read_backup_metadata", return_value={}), \
                mock.patch("onionpress.backup.prepare_install_from_backup",
                           return_value=(self.staging, {"onion_address": address})), \
                mock.patch("onionpress.backup.restore_staged_name_registry"), \
                mock.patch("onionpress.derivatives.spawn_regeneration"), \
                mock.patch.object(cli, "release_onionheaven",
                                  lambda reason, **kw: released.append(reason) or []), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = obj.cmd_restore("pw", "/nonexistent.zip")
        argvs = [c.args[0] for c in obj.docker.run.call_args_list]
        return rc, argvs, released, logs

    def test_a_backup_without_a_key(self):
        rc, argvs, released, logs = self._restore(OLD_ADDR, keyed=False)
        self.assertEqual(rc, 0, logs)
        removed = [a[2] for a in argvs if a[:2] == ["volume", "rm"]]
        self.assertEqual(removed, ["onionpress-db-data", "onionpress-wordpress-data",
                                   "onionpress-persistent-data"])
        self.assertFalse([a for a in argvs if a[0] == "run"], argvs)
        self.assertEqual(released, [])
        self.assertTrue([m for m in logs if "keeps its key volumes" in m], logs)

    def test_a_backup_with_one_replaces_them(self):
        rc, argvs, released, logs = self._restore(ADDR, keyed=True)
        self.assertEqual(rc, 0, logs)
        removed = [a[2] for a in argvs if a[:2] == ["volume", "rm"]]
        self.assertIn("onionpress-onion-keys", removed)
        self.assertIn("onionpress-tor-state", removed)
        self.assertTrue([a for a in argvs if a[0] == "run"], argvs)
        self.assertEqual(released, ["restore"])


class TestLaunchersKeepTheAddress(unittest.TestCase):
    """setup --from-backup of both launchers, run for real."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mv._cheap_kdf(self)

    def _fresh_data_dir(self):
        return mv.TestLauncherRestorePaths._fresh_data_dir(self)

    def _setup_from_backup(self, path):
        """`setup --from-backup`'s host side (#509: the restore's
        prepare_install_from_backup): (rc, the refusal); the log is read
        with _log()."""
        return mv.TestLauncherRestorePaths._setup_from_backup(self, path)

    def _log(self):
        with open(os.path.join(self.tmp, "log")) as f:
            return f.read()

    def test_setup_from_a_backup_without_a_key(self):
        # `setup --from-backup` is the restore since #509: a keyless backup
        # keeps the address Tor serves (read from the tor container; a
        # setup runs with the stack up).
        path = _keyless_backup(self.tmp, "k.zip")
        data = self._fresh_data_dir()
        with mock.patch("onionpress.backup.served_onion_address", return_value=OLD_ADDR):
            rc, _ = self._setup_from_backup(path)
        self.assertEqual(rc, 0)
        self.assertIn(f"this install keeps its key and its address {OLD_ADDR}", self._log())
        self.assertEqual(os.listdir(os.path.join(data, "shared", "vanity-keys")), [OLD_ADDR])
        self.assertFalse(os.path.exists(os.path.join(data, ".import-key-pending")))

    def test_setup_from_a_backup_with_a_key(self):
        path = _keyed_backup(self.tmp, "keyed.zip")
        data = self._fresh_data_dir()
        rc, _ = self._setup_from_backup(path)
        self.assertEqual(rc, 0)
        self.assertIn(f"Restore: synced vanity-keys for {ADDR}", self._log())
        self.assertNotIn(OLD_ADDR, os.listdir(os.path.join(data, "shared", "vanity-keys")))

    def test_the_setup_command_leaves_the_key_to_the_restore(self):
        """setup_logic.setup_from_terminal touches no key of its own (#509):
        the backup's setup is cli.cmd_restore (the hubs asked to let the
        replaced key go, the key volume seeded), passed in by cli._run_setup
        (tests/test_terminal_setup.py runs the flow)."""
        text = mv._read("src/onionpress/setup_logic.py")
        flow = text[text.index("def setup_from_terminal("):]
        for what in ("release_onionheaven", "IMPORT_KEY_PENDING", "vanity-keys"):
            self.assertNotIn(what, flow, what)
        self.assertIn("if restore(backup_password, from_backup) != 0:", flow)
        runner = mv._read("src/onionpress/cli.py")
        runner = runner[runner.index("def _run_setup("):runner.index("def _add_start_post_up(")]
        self.assertIn(".cmd_restore(password, zip_path, run.hub_recovery_key)", runner)


# ---------------------------------------------------------------------------
# A Settings page restore that would change the address
# ---------------------------------------------------------------------------

class _KeyWritten(Exception):
    pass


class TestMacSettingsPageRestoreAsks(unittest.TestCase):
    """restore_from_backup(through_wordpress=True, confirm_address_change=):
    a backup of another address than the one served is restored only on an
    explicit yes, asked before anything is touched."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = _Install(self, self.tmp)
        self.logs = []
        self.served = OLD_ADDR
        for target, kw in (
                ("onionpress.backup.subprocess.run", {"new": mv._NoDocker(self)}),
                ("onionpress.key_manager.write_private_key", {"side_effect": _KeyWritten}),
                ("onionpress.backup.served_onion_address",
                 {"side_effect": lambda: self.served}),
                ("onionpress.backup.hub_data_state", {"return_value": "none"}),
                ("onionpress.backup.onionhome_registry_present", {"return_value": False})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.path = _keyed_backup(self.tmp, "other.zip")

    def _restore(self, confirm, path=None):
        released = mock.Mock()
        kw = {} if confirm is False else {"confirm_address_change": confirm}
        backup.restore_from_backup(path or self.path, PW, self.logs.append,
                                   data_dir=self.install.data, through_wordpress=True,
                                   before_key_change=released, **kw)
        return released

    def test_cancel_no_answer_or_no_question_restores_nothing(self):
        for label, confirm in (("cancel", mock.Mock(return_value=False)),
                               ("no answer", mock.Mock(return_value=None)),
                               ("a truthy non-answer", mock.Mock(return_value=1)),
                               ("no question", False)):
            with self.subTest(label):
                with self.assertRaises(backup.SettingsPageRestoreRefused) as cm:
                    self._restore(confirm)
                self.assertEqual(str(cm.exception), backup.ADDRESS_CHANGE_NOT_CONFIRMED_MSG)
                if confirm is not False:
                    confirm.assert_called_once_with(OLD_ADDR, ADDR)
                self.install.assert_identity_kept()

    def test_restore_replaces_it(self):
        confirm = mock.Mock(return_value=True)
        with self.assertRaises(_KeyWritten):
            self._restore(confirm)
        confirm.assert_called_once_with(OLD_ADDR, ADDR)
        self.assertTrue([m for m in self.logs if "as confirmed at this Mac" in m])

    def test_the_same_address_is_not_asked_about(self):
        self.served = ADDR
        confirm = mock.Mock(return_value=False)
        with self.assertRaises(_KeyWritten):
            self._restore(confirm)
        confirm.assert_not_called()

    def test_a_backup_without_a_key_is_not_asked_about(self):
        confirm = mock.Mock(return_value=False)
        with mock.patch.object(backup, "restore_container_artifacts"):
            self._restore(confirm, _keyless_backup(self.tmp, "k.zip"))
        confirm.assert_not_called()
        self.install.assert_identity_kept()

    def test_the_menubar_asks_the_person_at_the_mac(self):
        """`onionpress handle-action` stops at {"confirm": "address-change"}
        for a restore of another address; the menubar's
        _ask_settings_page_question asks in a native dialog."""
        from onionpress import ui_client
        ns = {"ui_client": ui_client, "rumps": mock.Mock()}
        exec(textwrap.dedent(_menubar_method("_ask_cli_question")), ns)
        exec(textwrap.dedent(_menubar_method("_ask_settings_page_question")), ns)
        question = {"confirm": "address-change", "current": OLD_ADDR, "new": ADDR,
                    "action": "restore-backup", "handled": "restore-backup"}
        for answer, want in ((1, True), (0, False), (None, False)):
            with self.subTest(answer=answer):
                app = types.SimpleNamespace(
                    log=mock.Mock(), show_native_alert=mock.Mock(return_value=answer))
                app._ask_cli_question = types.MethodType(ns["_ask_cli_question"], app)
                self.assertIs(ns["_ask_settings_page_question"](app, question), want)
                message = app.show_native_alert.call_args.kwargs["message"]
                self.assertIn(OLD_ADDR, message)
                self.assertIn(ADDR, message)
                self.assertIn("restore a backup of another onion address", message)
                kwargs = app.show_native_alert.call_args.kwargs
                self.assertEqual(kwargs["buttons"], ["Cancel", "Restore"])
                self.assertEqual(kwargs["default_button"], 0)
                self.assertEqual(kwargs["cancel_button"], 0)

    def test_the_settings_page_restore_goes_through_it(self):
        # The CLI's settings-page-restore stops at the question unless
        # --confirmed address-change was given (onionpress.confirm); the
        # handler keeps the question for the menubar and runs it again.
        cli_src = mv._read("src/onionpress/cli.py")
        body = cli_src[cli_src.index("def cmd_settings_page_restore("):cli_src.index("def main(")]
        self.assertIn("confirm_address_change=confirm_address_change", body)
        self.assertIn('_confirm.require("address-change", confirmed, current=current, new=new)', body)
        self.assertIn("through_wordpress=True", body)
        handler = mv._read("src/onionpress/handle_action.py")
        self.assertIn('if "address-change" in confirmed:\n        args += ["--confirmed", "address-change"]', handler)
        menubar = mv._read("src/menubar.py")
        self.assertIn('["--confirmed", question["confirm"]] if answered_yes else ["--cancel"]', menubar)
        # The dialog gives up after 5 minutes: no answer is Cancel.
        alert = _menubar_method("show_native_alert")
        self.assertIn("max_wait = 300", alert)


class TestLinuxSettingsPageRestoreRefusesAnotherAddress(unittest.TestCase):
    """cli.cmd_settings_page_restore on Linux (issue #406: the one path,
    the Mac's): a backup of another address than the one served is refused
    before anything is touched (ADDRESS_CHANGE_REFUSED_MSG, exit 3: no one
    at this host to ask), a backup of this address or without a key is
    restored, and the heartbeat client unit is stopped before the hubs are
    asked to let go of the old key and started again after."""

    def setUp(self):
        mv._cheap_kdf(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = _Install(self, self.tmp)
        self.logs = []
        self.served = OLD_ADDR
        self.heartbeat = []
        for target, kw in (
                ("onionpress.backup.subprocess.run", {"new": mv._NoDocker(self)}),
                ("onionpress.key_manager.write_private_key", {"side_effect": _KeyWritten}),
                ("onionpress.backup.served_onion_address",
                 {"side_effect": lambda: self.served}),
                ("onionpress.backup.hub_data_state", {"return_value": "none"}),
                ("onionpress.backup.onionhome_registry_present", {"return_value": False}),
                ("onionpress.cli._settings_page_log", {"return_value": self.logs.append}),
                ("onionpress.cli.detect_os", {"return_value": OS.LINUX}),
                ("onionpress.cli.release_onionheaven",
                 {"side_effect": lambda *a, **k: self.heartbeat.append("release")}),
                ("onionpress.heartbeat_unit.stop",
                 {"side_effect": lambda log, run=None: self.heartbeat.append("stop")}),
                ("onionpress.heartbeat_unit.start",
                 {"side_effect": lambda log, run=None: self.heartbeat.append("start")})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def _restore(self, path, confirmed=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.cmd_settings_page_restore(path, PW, self.install.data, True,
                                               frozenset(confirmed))
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_another_address_is_refused_untouched_even_when_confirmed(self):
        path = _keyed_backup(self.tmp, "other.zip")
        for confirmed in ((), ("address-change",)):
            with self.subTest(confirmed=confirmed):
                rc, answer = self._restore(path, confirmed)
                self.assertEqual(rc, 3)
                self.assertEqual(answer, {"error": backup.ADDRESS_CHANGE_REFUSED_MSG})
                self.install.assert_identity_kept()
                self.assertEqual(self.heartbeat, [])
                self.assertTrue([m for m in self.logs if "refused (the Settings page's restore)" in m])

    def test_this_address_is_restored_with_the_heartbeat_paused(self):
        self.served = ADDR
        with mock.patch.object(backup, "restore_container_artifacts"), \
                mock.patch.object(backup, "apply_config_overrides"):
            rc, answer = self._restore(_keyed_backup(self.tmp, "same.zip"))
        # write_private_key raises _KeyWritten (the stub): the error path,
        # after the heartbeat was stopped and the hubs asked, and the
        # heartbeat is started again all the same.
        self.assertEqual(rc, 1)
        self.assertEqual(self.heartbeat, ["stop", "release", "start"])

    def test_a_backup_without_a_key_keeps_it_and_asks_nothing(self):
        with mock.patch.object(backup, "restore_container_artifacts"), \
                mock.patch.object(backup, "apply_config_overrides"):
            rc, answer = self._restore(_keyless_backup(self.tmp, "k.zip"))
        self.assertEqual((rc, answer), (0, {"ok": True}))
        self.install.assert_identity_kept()
        self.assertEqual(self.heartbeat, [])
        self.assertTrue([m for m in self.logs if f"keeps its key and its address {OLD_ADDR}" in m])

    def test_the_words_are_one_string(self):
        # The refusal is the Python constant, in the handler's answer and
        # on the page; no bash copy of it is left.
        self.assertNotIn("never replaces this site's address", mv._read("linux/onionpress"))
        self.assertIn("never changes this site's onion address", mv._read(PHP))


class TestLinuxSettingsPageKeyActions(unittest.TestCase):

    def test_no_generate_vanity_and_import_is_refused(self):
        # One handler for both platforms (onionpress.handle_action, issue
        # #366): no generate-vanity verb, and the import refused on Linux
        # before any launcher runs.
        handler = mv._read("src/onionpress/handle_action.py")
        self.assertNotIn("generate-vanity", handler)
        self.assertNotIn("mkp224o", handler)
        self.assertNotIn("generate-vanity", handler[handler.index("ACTIONS = ("):])
        branch = handler[handler.index("def _import_key_file("):handler.index("def _import_key(")]
        self.assertIn('if host.os_name != "macos":', branch)
        self.assertIn("IMPORT_REFUSED_LINUX_MSG", branch)
        self.assertIn("onionpress import-key", handler[handler.index("IMPORT_REFUSED_LINUX_MSG = ("):])
        refusal = branch[branch.index('if host.os_name != "macos":'):branch.index("if not key:")]
        self.assertNotIn("run_launcher", refusal)
        text = mv._read("linux/onionpress")
        handle = _branch(text, "\n        handle-action)\n", "\n            ;;\n")
        self.assertNotIn("generate-vanity", handle)
        self.assertNotIn("import-key", handle)

    def test_the_page_no_longer_asks_for_generate_vanity(self):
        php = mv._read(PHP)
        self.assertNotIn("generate-vanity", php)
        allowed = re.search(r"if \( ! in_array\( \$action, array\(([^)]*)\), true \) \)", php)
        self.assertIsNotNone(allowed)
        self.assertNotIn("vanity", allowed.group(1))
        self.assertIn("'import-key-file'", allowed.group(1))


# ---------------------------------------------------------------------------
# No Settings page restore on an OnionHeaven hub or OnionHome
# ---------------------------------------------------------------------------

class TestSettingsPageRestoreNodeRefusal(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(backup, "onionhome_registry_present", return_value=False)
        self.onionhome = p.start()
        self.addCleanup(p.stop)

    def test_who_is_refused(self):
        refuse = backup.settings_page_restore_node_refusal
        hub, home = backup.HUB_RESTORE_HERE_REFUSED_MSG, backup.ONIONHOME_RESTORE_HERE_REFUSED_MSG
        unverified = backup.UNVERIFIED_RESTORE_REFUSED_MSG
        self.assertIsNone(refuse(ADDR, "none", data_dir=self.tmp))
        for state in (None, "", "hub?"):
            with self.subTest(hub_state=state):
                self.assertEqual(refuse(ADDR, state, data_dir=self.tmp), unverified)
        self.assertEqual(refuse("", "none", data_dir=self.tmp), unverified)
        self.assertEqual(refuse(ADDR, "hub", data_dir=self.tmp), hub)
        self.assertEqual(refuse(backup.ONIONHEAVEN_HUB_ADDRESS, "none", data_dir=self.tmp), hub)
        self.assertEqual(refuse(backup.ONIONHOME_ADDRESS, "none", data_dir=self.tmp), home)
        with open(os.path.join(self.tmp, "config"), "w") as f:
            f.write("ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes\n")
        self.assertEqual(refuse(ADDR, "none", data_dir=self.tmp), hub)
        os.remove(os.path.join(self.tmp, "config"))
        self.onionhome.return_value = True
        self.assertEqual(refuse(ADDR, "none", data_dir=self.tmp), home)
        self.onionhome.return_value = None
        self.assertEqual(refuse(ADDR, "none", data_dir=self.tmp), unverified)

    def test_the_backup_refusal_tells_nodes_apart_the_same_way(self):
        pairs = ((backup.HUB_BACKUP_REFUSED_MSG, backup.HUB_RESTORE_HERE_REFUSED_MSG),
                 (backup.ONIONHOME_BACKUP_REFUSED_MSG, backup.ONIONHOME_RESTORE_HERE_REFUSED_MSG),
                 (backup.UNVERIFIED_BACKUP_REFUSED_MSG, backup.UNVERIFIED_RESTORE_REFUSED_MSG),
                 (None, None))
        cases = [(ADDR, "none", False), (ADDR, "hub", False), (ADDR, None, False),
                 ("", "none", False), (ADDR, "none", True), (ADDR, "none", None),
                 (backup.ONIONHOME_ADDRESS, "none", False),
                 (backup.ONIONHEAVEN_HUB_ADDRESS, "none", False)]
        for address, state, onionhome in cases:
            with self.subTest(address=address, state=state, onionhome=onionhome):
                self.onionhome.return_value = onionhome
                got = (backup.settings_page_backup_refusal(address, state, data_dir=self.tmp),
                       backup.settings_page_restore_node_refusal(address, state,
                                                                 data_dir=self.tmp))
                self.assertIn(got, pairs)

    def test_the_messages_are_plain_text(self):
        for text in (backup.HUB_RESTORE_HERE_REFUSED_MSG,
                     backup.ONIONHOME_RESTORE_HERE_REFUSED_MSG,
                     backup.UNVERIFIED_RESTORE_REFUSED_MSG,
                     backup.ADDRESS_CHANGE_NOT_CONFIRMED_MSG,
                     backup.NO_KEY_TO_KEEP_MSG, backup.KEY_IN_KEYLESS_BACKUP_MSG):
            self.assertRegex(text, r"\A[A-Za-z0-9 .,:;'()-]+\Z")
        # One placeholder, shown escaped by the page.
        self.assertRegex(backup.ADDRESS_CHANGE_REFUSED_MSG, r"\A[A-Za-z0-9 .,:;'()<>-]+\Z")


class TestMacSettingsPageRestoreOnAHub(unittest.TestCase):
    """restore_from_backup(through_wordpress=True) on a hub, OnionHome or a
    node that cannot say: refused before the upload is even opened."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = _Install(self, self.tmp)
        for target, kw in (
                ("onionpress.backup.subprocess.run", {"new": mv._NoDocker(self)}),
                ("onionpress.backup.extract_archive",
                 {"side_effect": AssertionError("upload opened")}),
                ("onionpress.key_manager.write_private_key",
                 {"side_effect": AssertionError("key written")})):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_refused(self):
        for label, served, state, onionhome, want in (
                ("hub", backup.ONIONHEAVEN_HUB_ADDRESS, "none", False,
                 backup.HUB_RESTORE_HERE_REFUSED_MSG),
                ("hub data", OLD_ADDR, "hub", False, backup.HUB_RESTORE_HERE_REFUSED_MSG),
                ("onionhome", OLD_ADDR, "none", True, backup.ONIONHOME_RESTORE_HERE_REFUSED_MSG),
                ("unknown hub", OLD_ADDR, None, False, backup.UNVERIFIED_RESTORE_REFUSED_MSG),
                ("unknown onionhome", OLD_ADDR, "none", None,
                 backup.UNVERIFIED_RESTORE_REFUSED_MSG),
                ("no address", "", "none", False, backup.UNVERIFIED_RESTORE_REFUSED_MSG)):
            with self.subTest(label), \
                    mock.patch.object(backup, "served_onion_address", return_value=served), \
                    mock.patch.object(backup, "hub_data_state", return_value=state), \
                    mock.patch.object(backup, "onionhome_registry_present",
                                      return_value=onionhome):
                with self.assertRaises(backup.SettingsPageRestoreRefused) as cm:
                    backup.restore_from_backup("/nonexistent.zip", PW, lambda m: None,
                                               data_dir=self.install.data,
                                               through_wordpress=True,
                                               confirm_address_change=mock.Mock(
                                                   side_effect=AssertionError("asked")))
                self.assertEqual(str(cm.exception), want)
                self.install.assert_identity_kept()

    def test_the_apps_restore_is_not_asked(self):
        """Only the Settings page's restore checks the node."""
        with mock.patch.object(backup, "hub_data_state",
                               side_effect=AssertionError("node checked")), \
                self.assertRaises(AssertionError) as cm:
            backup.restore_from_backup("/nonexistent.zip", PW, lambda m: None,
                                       data_dir=self.install.data)
        self.assertEqual(str(cm.exception), "upload opened")


# ---------------------------------------------------------------------------
# The Settings page says what each platform allows
# ---------------------------------------------------------------------------

class TestSettingsPageSaysSo(unittest.TestCase):

    def setUp(self):
        self.php = mv._read(PHP)

    def _platforms(self, section):
        """(Linux text, Mac text) of the section's first platform switch."""
        start = section.index("<?php if ( $current_platform === 'linux' ) : ?>")
        middle = section.index("<?php else : ?>", start)
        end = section.index("<?php endif; ?>", middle)
        return section[start:middle], section[middle:end]

    def test_backup(self):
        section = _branch(self.php, "<h2>Backup</h2>", "<!-- Restore -->")
        self.assertNotIn("Tor keys", section)
        linux, mac = self._platforms(section)
        for text in (linux, mac):
            self.assertIn("does not contain your onion address's key", text)
        self.assertIn("<code>onionpress backup</code>", linux)
        self.assertIn("Backup&hellip; in the OnionPress menu", mac)

    def test_restore(self):
        section = _branch(self.php, "<h2>Restore</h2>", "</form>")
        linux, mac = self._platforms(section)
        self.assertIn("never changes this site's onion address", linux)
        self.assertIn("<code>onionpress restore &lt;backup file&gt;</code>", linux)
        self.assertIn("clicks Restore in the dialog", mac)
        self.assertIn("On an OnionHeaven hub or on OnionHome, nothing is restored here",
                      section)

    def test_import_key(self):
        section = self.php[self.php.index("<h2>Import Onion Service Key</h2>"):]
        section = section[:section.index("    </div>\n    <style>")]
        linux, mac = self._platforms(section)
        self.assertIn("<code>onionpress import-key</code>", linux)
        self.assertNotIn("<form", linux)
        self.assertIn('id="op-import-key-form"', mac)
        self.assertIn("has to confirm the replacement there", mac)


if __name__ == "__main__":
    unittest.main()
