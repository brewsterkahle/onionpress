#!/usr/bin/env python3
"""`onionpress setup` (issues #442.9-#442.11): the launchers' terminal
setup, now setup_logic.setup_from_terminal over the setup window's
install (install_fresh_wordpress).

  - `--from-backup` (stage_setup_from_backup): a backup's settings and key
    put in place, the key checked first, a backup without one never
    minting one, a wrong password or a mismatched key refused before
    anything is copied;
  - the terminal's answers (ask_setup_answers), driven by a fake terminal;
  - the flow's outcome codes, which tell the launcher what is left;
  - passwords only in the environment, never on any argv, and dropped
    from it once read."""

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from onionpress import cli, setup_logic  # noqa: E402
from onionpress.containers import IMPORT_KEY_PENDING  # noqa: E402
from onionpress.launcher_ops import BACKUP_PASSWORD_ENV  # noqa: E402
import test_backup_metadata_validation as fixtures  # noqa: E402

ADDR, PW = fixtures.ADDR, fixtures.PW


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config"), "w") as f:
            f.write("ADDRESS_PREFIX=op2\n")
        self.logs, self.said = [], []
        fixtures._cheap_kdf(self)

    def vanity(self):
        base = os.path.join(self.data, "shared", "vanity-keys")
        return sorted(os.listdir(base)) if os.path.isdir(base) else []


class TestStageFromBackup(Base):

    def stage(self, zip_path, password=PW):
        return setup_logic.stage_setup_from_backup(zip_path, password, self.data,
                                                   self.logs.append, self.said.append)

    def test_the_backups_key_is_put_in_place(self):
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata())
        self.assertEqual(self.stage(zip_path), (True, ADDR))
        self.assertEqual(self.vanity(), [ADDR])
        folder = os.path.join(self.data, "shared", "vanity-keys", ADDR)
        self.assertEqual(sorted(os.listdir(folder)),
                         ["hostname", "ks_hs_id.ed25519_expanded_private"])
        with open(os.path.join(folder, "hostname")) as f:
            self.assertEqual(f.read(), ADDR + "\n")
        self.assertIn(f"  Tor keys restored from backup ({ADDR}).", self.said)

    def test_no_key_keeps_this_installs_and_mints_nothing(self):
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata(), pem=None)
        self.assertEqual(self.stage(zip_path), (True, ""))
        self.assertEqual(self.vanity(), [])
        self.assertIn("  This backup holds no onion service key: this install keeps "
                      "its own address.", self.said)

    def test_a_wrong_password_is_refused_before_anything(self):
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata())
        self.assertEqual(self.stage(zip_path, "wrong-password"), (False, ""))
        self.assertEqual(self.vanity(), [])
        self.assertTrue(self.said[-1].startswith("ERROR: Failed to extract backup"))

    def test_a_key_its_metadata_says_is_another_address_is_refused(self):
        other = "op2" + "z" * 53 + ".onion"
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata(onion_address=other,
                                                                      has_onion_key=False))
        ok, address = self.stage(zip_path)
        if ok:  # metadata may only be stale: the key's address is the one used
            self.assertEqual(address, ADDR)
        else:
            self.assertEqual(self.vanity(), [])

    def test_the_temp_folder_always_goes(self):
        made = []
        real = tempfile.mkdtemp
        with mock.patch("tempfile.mkdtemp", side_effect=lambda **k: made.append(real(**k)) or made[-1]):
            self.stage(fixtures._make_backup(self.tmp, fixtures._metadata()))
            self.stage(os.path.join(self.tmp, "missing.zip"))
        self.assertEqual(len(made), 2)
        self.assertFalse(any(os.path.exists(d) for d in made))


class TestAnswers(Base):

    def answers(self, typed, secret, **given):
        typed, secret = list(typed), list(secret)
        return setup_logic.ask_setup_answers(
            input_func=lambda prompt: self.said.append(prompt) or typed.pop(0),
            getpass_func=lambda prompt: self.said.append(prompt) or secret.pop(0),
            say=self.said.append, **given)

    def test_asks_what_is_not_given(self):
        with mock.patch.object(setup_logic, "suggest_onionname", return_value="sunny"), \
             mock.patch.object(setup_logic, "default_site_title", return_value="I OnionPress"):
            got = self.answers(["", ""], ["s3cret!", "s3cret!"])
        self.assertEqual(got, ("sunny", "s3cret!", "I OnionPress"))
        self.assertIn("  Username [sunny]: ", self.said)

    def test_a_bad_username_short_or_mismatched_password_asks_again(self):
        with mock.patch.object(setup_logic, "suggest_onionname", return_value=""):
            got = self.answers(["Bad Name", "alice", "My Site"],
                               ["abc", "longenough", "different", "longenough", "longenough"])
        self.assertEqual(got, ("alice", "longenough", "My Site"))
        self.assertTrue(any(s.startswith("  Invalid username:") for s in self.said))
        self.assertIn("  Passwords do not match. Try again.", self.said)

    def test_nothing_asked_when_everything_is_given(self):
        got = self.answers([], [], user="alice", password="longenough", title="T")
        self.assertEqual(got, ("alice", "longenough", "T"))
        self.assertEqual(self.said, [])

    def test_a_given_short_password_is_asked_again(self):
        got = self.answers([], ["longenough", "longenough"], user="alice", password="x",
                           title="T")
        self.assertEqual(got[1], "longenough")
        self.assertIn("  Password must be at least 6 characters.", self.said)


class FakeDocker:
    docker_bin = "docker"

    def __init__(self, installed=False, running=True, tor=True):
        self.installed, self.running, self.tor = installed, running, tor
        self.env = {}

    def container_running(self, name):
        return self.tor


class TestFlow(Base):

    def run_setup(self, docker, *, env=None, manager=None, install_ok=True, **kw):
        manager = manager or mock.Mock(**{"migrate_key_volume.return_value": True,
                                          "key_volume_not_ready.return_value": None})

        def wp(_bin, args, **_k):
            if args[:2] == ("core", "is-installed"):
                return mock.Mock(returncode=0 if docker.installed else 1)
            return mock.Mock(returncode=0 if docker.running else 1)
        self.install = mock.Mock(return_value=install_ok)
        with mock.patch.dict(os.environ, env or {}), \
             mock.patch.object(setup_logic, "_run_wp", side_effect=wp), \
             mock.patch.object(setup_logic, "install_fresh_wordpress", self.install), \
             mock.patch.object(setup_logic.onion_hostname, "read_served_address_with",
                               return_value=ADDR), \
             mock.patch("onionpress.onionheaven.release_onionheaven") as release:
            self.release = release
            rc = setup_logic.setup_from_terminal(
                data_dir=self.data, mac=True, launcher_bin="/app/onionpress", docker=docker,
                manager=manager, log=self.logs.append, say=self.said.append,
                input_func=lambda p: (_ for _ in ()).throw(EOFError()),
                getpass_func=lambda p: (_ for _ in ()).throw(EOFError()), **kw)
            self.env_after = dict(os.environ)
        return rc, manager

    def test_already_installed(self):
        rc, _ = self.run_setup(FakeDocker(installed=True))
        self.assertEqual(rc, setup_logic.SETUP_ALREADY_INSTALLED)
        self.assertTrue(os.path.exists(os.path.join(self.data, ".setup_complete")))
        self.install.assert_not_called()

    def test_wordpress_not_running(self):
        rc, _ = self.run_setup(FakeDocker(running=False))
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        self.assertEqual(self.said, ["WordPress container is not running. Start OnionPress first."])

    def test_the_setup_windows_install_with_the_given_answers(self):
        rc, manager = self.run_setup(
            FakeDocker(tor=False), env={setup_logic.SETUP_PASSWORD_ENV: "longenough"},
            user="alice", title="My Site")
        self.assertEqual(rc, setup_logic.SETUP_DONE)
        kw = self.install.call_args.kwargs
        self.assertEqual((kw["onionname"], kw["password"], kw["site_title"], kw["onion_addr"],
                          kw["launcher_bin"], kw["data_dir"]),
                         ("alice", "longenough", "My Site", ADDR, "/app/onionpress", self.data))
        self.assertNotIn(setup_logic.SETUP_PASSWORD_ENV, self.env_after,
                         "the password leaves the environment once read")
        # Tor started, its key volume migrated first.
        manager.migrate_key_volume.assert_called_once_with()
        manager.run_compose_to.assert_called_once_with(["up", "-d", "tor"], out="log")

    def test_tor_never_without_its_key_volume(self):
        manager = mock.Mock(**{"migrate_key_volume.return_value": False})
        rc, _ = self.run_setup(FakeDocker(tor=False), manager=manager,
                               env={setup_logic.SETUP_PASSWORD_ENV: "longenough"},
                               user="alice", title="T")
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        manager.run_compose_to.assert_not_called()

    def test_no_terminal_and_nothing_given_fails_cleanly(self):
        rc, _ = self.run_setup(FakeDocker())
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        self.assertTrue(self.said[-1].startswith("ERROR: no terminal to ask on"))

    def test_a_failed_install_fails(self):
        rc, _ = self.run_setup(FakeDocker(), install_ok=False,
                               env={setup_logic.SETUP_PASSWORD_ENV: "longenough"},
                               user="alice", title="T")
        self.assertEqual(rc, setup_logic.SETUP_FAILED)

    def test_from_a_backup_with_a_key_flags_the_import_and_restarts(self):
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata())
        rc, manager = self.run_setup(FakeDocker(), env={BACKUP_PASSWORD_ENV: PW},
                                     from_backup=zip_path)
        self.assertEqual(rc, setup_logic.SETUP_RESTART)
        kw = self.install.call_args.kwargs
        self.assertEqual((kw["onionname"], kw["password"]), ("admin", PW))
        self.release.assert_called_once_with("restore", keep_address=ADDR, data_dir=self.data)
        self.assertTrue(os.path.exists(os.path.join(self.data, IMPORT_KEY_PENDING)))
        self.assertNotIn(BACKUP_PASSWORD_ENV, self.env_after)
        manager.run_compose_to.assert_not_called()

    def test_from_a_backup_without_a_key_keeps_this_one(self):
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata(), pem=None)
        rc, _ = self.run_setup(FakeDocker(), env={BACKUP_PASSWORD_ENV: PW},
                               from_backup=zip_path)
        self.assertEqual(rc, setup_logic.SETUP_RESTART)
        self.release.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.data, IMPORT_KEY_PENDING)))

    def test_a_refused_backup_installs_nothing(self):
        zip_path = fixtures._make_backup(self.tmp, fixtures._metadata())
        rc, _ = self.run_setup(FakeDocker(), env={BACKUP_PASSWORD_ENV: "wrong"},
                               from_backup=zip_path)
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        self.install.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.data, IMPORT_KEY_PENDING)))

    def test_archive_org_login_saves_the_keys_on_stdin(self):
        calls = []
        with mock.patch("onionpress.multisite.fetch_archive_s3_keys",
                        return_value={"access": "AK", "secret": "SK"}), \
             mock.patch.object(setup_logic, "_run_wp",
                               side_effect=lambda b, args, **k: calls.append((args, k)) or
                               mock.Mock(returncode=0)):
            self.assertTrue(setup_logic.save_archive_login(
                "me@example.org", "pw", docker_bin="docker", docker_env=None,
                log=self.logs.append))
        self.assertEqual([(a, k["input"]) for a, k in calls], [
            (("option", "update", "onionpress_archive_s3_access"), "AK"),
            (("option", "update", "onionpress_archive_s3_secret"), "SK")])


class TestSubcommand(unittest.TestCase):
    def test_the_cli_runs_the_flow_with_the_launcher(self):
        with mock.patch.object(setup_logic, "setup_from_terminal", return_value=10) as flow, \
             mock.patch.object(cli, "Docker"), \
             mock.patch.object(cli, "OnionPressCLI") as heavy:
            rc = cli.main(["setup", "--user", "alice", "--title", "T",
                           "--from-backup", "/b.zip", "--launcher", "/l/onionpress"])
        self.assertEqual(rc, 10)
        heavy.assert_not_called()
        kw = flow.call_args.kwargs
        self.assertEqual((kw["user"], kw["title"], kw["from_backup"], kw["launcher_bin"]),
                         ("alice", "T", "/b.zip", "/l/onionpress"))


    def test_email_is_ignored_out_loud(self):
        """The old bash setup used --email; a script passing it learns it no
        longer does, and the email goes nowhere (not to the flow, not the log)."""
        for argv, warned in ((["--email", "a@example.org"], True), ([], False)):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as data, \
                 mock.patch.object(setup_logic, "setup_from_terminal", return_value=0) as flow, \
                 mock.patch.object(cli, "Docker"), \
                 mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                rc = cli.main(["--data-dir", data, "setup", "--user", "alice",
                               "--launcher", "/l/onionpress", *argv])
                self.assertEqual(rc, 0)
                self.assertEqual("--email is ignored" in err.getvalue(), warned)
                self.assertNotIn("email", flow.call_args.kwargs)
                log = os.path.join(data, "onionpress.log")
                text = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
                self.assertEqual("--email given and ignored" in text, warned)
                self.assertNotIn("a@example.org", text)


class TestLaunchers(unittest.TestCase):
    """The `setup)` case is run_setup and the launcher's own steps; no
    password reaches the CLI's argv."""

    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_passwords_move_to_the_environment(self):
        import subprocess
        for rel, runner in (("app/MacOS/onionpress", "bundled_python"), ("linux/onionpress", "op_py")):
            text = self._read(rel)
            i = text.index("\nrun_setup() {")
            fn = text[i:text.index("\n}\n", i) + 3]
            script = (f'{runner}() {{ printf "%s|" "$@"; echo; '
                      'echo "env:$ONIONPRESS_SETUP_PASSWORD:$ONIONPRESS_IA_PASSWORD:'
                      '$ONIONPRESS_BACKUP_PASSWORD"; }\n'
                      + ('bundled_python() { shift 2; printf "%s|" "$@"; echo; '
                         'echo "env:$ONIONPRESS_SETUP_PASSWORD:$ONIONPRESS_IA_PASSWORD:'
                         '$ONIONPRESS_BACKUP_PASSWORD"; }\n' if runner == "bundled_python" else "")
                      + 'DATA_DIR=/d; SCRIPT_DIR=/s; INSTALL_DIR=/s\n' + fn
                      + 'run_setup --user alice --pass "p w" --ia-password ia '
                        '--from-backup bpw /b.zip --title T --pass\n')
            out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                 timeout=30).stdout.splitlines()
            with self.subTest(rel):
                argv = out[0].split("|")
                self.assertEqual(argv[:4], ["--data-dir", "/d", "setup", "--launcher"])
                self.assertEqual(argv[5:-1], ["--user", "alice", "--from-backup", "/b.zip",
                                              "--title", "T"])
                self.assertEqual(out[1], "env::ia:bpw", "the last --pass had no value")
                for secret in ("p w", "bpw", "ia"):
                    self.assertNotIn(secret, argv)

    def test_the_case_restarts_only_for_a_backup(self):
        for rel in ("app/MacOS/onionpress", "linux/onionpress"):
            text = self._read(rel)
            i = text.index("\n        setup)\n")
            case = text[i:text.index("\n            ;;\n", text.index("*) exit 1 ;;", i))]
            with self.subTest(rel):
                self.assertIn('run_setup "$@" || setup_rc=$?', case)
                ten = case[case.index("                10)"):case.index("*) exit 1 ;;")]
                for step in ("stop_containers", "start_containers", "wait_for_services"):
                    self.assertIn(step, ten)
                self.assertNotIn("setup_wordpress", text)
                self.assertNotIn("\nwp_set_user_password() {", text)
                self.assertNotIn("core multisite-install", text)


if __name__ == "__main__":
    unittest.main()
