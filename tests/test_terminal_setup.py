#!/usr/bin/env python3
"""`onionpress setup` (issues #442.9-#442.11): the launchers' terminal
setup, now setup_logic.setup_from_terminal over the setup window's
install (install_fresh_wordpress).

  - `--from-backup` is the restore (issue #509): cli.cmd_restore's
    install-from-backup rebuild, passed in as restore(password, zip), the
    setup marked complete after it and the launcher told to restart;
    never a WordPress under temporary credentials; a failed restore sets
    nothing up;
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

    def a_backup(self):
        """Any file: the flow only checks the path names one; what is in it
        is the restore's business."""
        path = os.path.join(self.tmp, "site.zip")
        with open(path, "wb") as f:
            f.write(b"PK")
        return path


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

    def run_setup(self, docker, *, env=None, manager=None, install_ok=True, restore_rc=0,
                  **kw):
        manager = manager or mock.Mock(**{"migrate_key_volume.return_value": True,
                                          "key_volume_not_ready.return_value": None})
        self.restore = mock.Mock(return_value=restore_rc)

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
                getpass_func=lambda p: (_ for _ in ()).throw(EOFError()),
                restore=self.restore, **kw)
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
        manager.run_compose_to.assert_called_once_with(["up", "-d", "--no-deps", "tor"], out="log")

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

    def test_from_a_backup_runs_the_restore_and_restarts(self):
        """#509: the backup's site, as `onionpress restore` rebuilds it; no
        WordPress under temporary credentials; the setup marked complete;
        the launcher restarts the stack for the restored site."""
        zip_path = self.a_backup()
        rc, manager = self.run_setup(FakeDocker(), env={BACKUP_PASSWORD_ENV: PW},
                                     from_backup=zip_path)
        self.assertEqual(rc, setup_logic.SETUP_RESTART)
        self.restore.assert_called_once_with(PW, zip_path)
        self.install.assert_not_called()
        self.release.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.data, ".setup_complete")))
        self.assertNotIn(BACKUP_PASSWORD_ENV, self.env_after)
        self.assertFalse(any("temporary" in line for line in self.said), self.said)
        manager.run_compose_to.assert_not_called()

    def test_a_failed_restore_sets_nothing_up(self):
        rc, _ = self.run_setup(FakeDocker(), env={BACKUP_PASSWORD_ENV: PW},
                               from_backup=self.a_backup(), restore_rc=1)
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        self.install.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.data, ".setup_complete")))
        self.assertTrue(self.said[-1].startswith("  ERROR: the restore failed"), self.said)

    def test_a_backup_needs_its_password_and_file(self):
        for env, zip_path in (({}, self.a_backup()),
                              ({BACKUP_PASSWORD_ENV: PW}, os.path.join(self.tmp, "none.zip"))):
            with self.subTest(env=bool(env)):
                rc, _ = self.run_setup(FakeDocker(), env=env, from_backup=zip_path)
                self.assertEqual(rc, setup_logic.SETUP_FAILED)
                self.restore.assert_not_called()
                self.install.assert_not_called()

    def test_the_flow_needs_a_restore_for_a_backup(self):
        with self.assertRaises(TypeError):
            self.run_setup(FakeDocker(), env={BACKUP_PASSWORD_ENV: PW},
                           from_backup=self.a_backup(), restore=None)

    def test_archive_org_login_follows_a_restore_too(self):
        with mock.patch.object(setup_logic, "save_archive_login", return_value=True) as login:
            rc, _ = self.run_setup(FakeDocker(), ia_account="me@example.org",
                                   env={BACKUP_PASSWORD_ENV: PW, setup_logic.IA_PASSWORD_ENV: "ia"},
                                   from_backup=self.a_backup())
        self.assertEqual(rc, setup_logic.SETUP_RESTART)
        self.assertEqual(login.call_args.args, ("me@example.org", "ia"))
        self.assertNotIn(setup_logic.IA_PASSWORD_ENV, self.env_after)

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
            # The flow's restore is `onionpress restore`'s (#509): cmd_restore,
            # with the hub recovery key main() took from the environment.
            # OnionPressCLI is built only when the flow calls it.
            heavy.return_value.cmd_restore.return_value = 0
            self.assertEqual(kw["restore"]("pw", "/b.zip"), 0)
            heavy.return_value.cmd_restore.assert_called_once_with("pw", "/b.zip", "")

    def test_the_hub_recovery_key_reaches_the_restore(self):
        with mock.patch.object(setup_logic, "setup_from_terminal", return_value=10) as flow, \
             mock.patch.object(cli, "Docker"), \
             mock.patch.object(cli, "OnionPressCLI") as heavy, \
             mock.patch.dict(os.environ, {cli.RECOVERY_KEY_ENV: "hub-key"}):
            cli.main(["setup", "--from-backup", "/b.zip", "--launcher", "/l/onionpress"])
            flow.call_args.kwargs["restore"]("pw", "/b.zip")
        heavy.return_value.cmd_restore.assert_called_once_with("pw", "/b.zip", "hub-key")

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
            j = text.index("\nread_backup_password() {")
            fn = text[j:text.index("\n}\n", j) + 3] + fn
            script = (f'{runner}() {{ printf "%s|" "$@"; echo; '
                      'echo "env:$ONIONPRESS_SETUP_PASSWORD:$ONIONPRESS_IA_PASSWORD:'
                      '$ONIONPRESS_BACKUP_PASSWORD:$ONIONPRESS_HUB_RECOVERY_KEY"; }\n'
                      + ('bundled_python() { shift 2; printf "%s|" "$@"; echo; '
                         'echo "env:$ONIONPRESS_SETUP_PASSWORD:$ONIONPRESS_IA_PASSWORD:'
                         '$ONIONPRESS_BACKUP_PASSWORD:$ONIONPRESS_HUB_RECOVERY_KEY"; }\n'
                         if runner == "bundled_python" else "")
                      + 'DATA_DIR=/d; SCRIPT_DIR=/s; INSTALL_DIR=/s; _HUB_RECOVERY_KEY=hk\n' + fn
                      + 'run_setup --user alice --pass "p w" --ia-password ia '
                        '--from-backup bpw /b.zip --title T --pass\n')
            out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                 timeout=30).stdout.splitlines()
            with self.subTest(rel):
                argv = out[0].split("|")
                self.assertEqual(argv[:4], ["--data-dir", "/d", "setup", "--launcher"])
                self.assertEqual(argv[5:-1], ["--user", "alice", "--from-backup", "/b.zip",
                                              "--title", "T"])
                self.assertEqual(out[1], "env::ia:bpw:hk", "the last --pass had no value; "
                                 "the hub recovery key goes along for the restore (#509)")
                for secret in ("p w", "bpw", "ia", "hk"):
                    self.assertNotIn(secret, argv)

    def test_a_backup_password_from_the_environment(self):
        """`setup --from-backup <zip>` takes the password as backup and
        restore do: the live Linux check gave it in the environment and the
        zip was taken for the password."""
        import subprocess
        for rel, runner in (("app/MacOS/onionpress", "bundled_python"), ("linux/onionpress", "op_py")):
            text = self._read(rel)
            fns = ""
            for name in ("read_backup_password", "run_setup"):
                i = text.index(f"\n{name}() {{")
                fns += text[i:text.index("\n}\n", i) + 3]
            script = (f'{runner}() {{ [ "$1" = -m ] && shift 2; printf "%s|" "$@"; echo; '
                      'echo "env:$ONIONPRESS_BACKUP_PASSWORD"; }\n'
                      'DATA_DIR=/d; SCRIPT_DIR=/s; INSTALL_DIR=/s\n' + fns
                      + 'run_setup --from-backup /b.zip --user bob\n')
            r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                               env={"PATH": os.environ["PATH"],
                                    "ONIONPRESS_BACKUP_PASSWORD": "envpw"},
                               stdin=subprocess.DEVNULL, timeout=30)
            with self.subTest(rel):
                out = r.stdout.splitlines()
                argv = out[0].split("|")
                self.assertIn("--from-backup|/b.zip|--user|bob", "|".join(argv))
                self.assertEqual(out[1], "env:envpw")
                self.assertNotIn("envpw", argv)
                self.assertNotIn("visible to other users", r.stderr)

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
