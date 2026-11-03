#!/usr/bin/env python3
"""First-run install through `onionpress setup` on both platforms (issue
#527): the Mac menubar and the Linux tray run `setup --json` (or Linux's
`provision --json`) through the launcher instead of setup_logic in-process.

  - the onionname is registered with OnionHome before WordPress is
    installed under it (setup_onionname over app_commands.register_onionname,
    what `onionname set` runs), on both platforms; a taken name is asked
    again at a terminal, or stops the setup with {"error": "taken",
    "suggestions"} and nothing installed (the UI asks and runs it again);
    --register-later records it unregistered;
  - `--language`, `--json` (nothing asked; the answer one JSON object on
    stdout, progress on stderr); the contract's exit codes unchanged;
  - `onionname set --rename-admin` renames the WordPress admin through
    $wpdb with both logins on stdin (setup_logic.rename_wp_user);
  - `provision` takes flags (provision_from_flags);
  - the archive.org key fetch of a short-lived setup runs detached;
  - neither UI imports the install, the registrar, the hostname reader, the
    token minting or wp_files, and both hand the password in the
    environment.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from onionpress import app_commands, cli, setup_logic, ui_client  # noqa: E402

ADDR = "a" * 52 + "qd2a.onion"
PW = "longenough"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class Registrar:
    """OnionHome, scripted: one result per register() call."""

    def __init__(self, *statuses, suggestions=("bob-2",)):
        self.statuses = list(statuses)
        self.suggestions = list(suggestions)
        self.calls = []

    def register(self, name, address):
        self.calls.append((name, address))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return types.SimpleNamespace(ok=status == "ok", status=status,
                                     suggestions=self.suggestions, reason=status)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.config_file = os.path.join(self.data, "config")
        with open(self.config_file, "w") as f:
            f.write("ADDRESS_PREFIX=op2\n")
        self.paths = types.SimpleNamespace(data_dir=self.data, config_file=self.config_file)
        self.logs, self.said = [], []
        p = mock.patch.object(app_commands, "served_address", return_value=ADDR)
        self.served = p.start()
        self.addCleanup(p.stop)

    def config(self):
        return app_commands.onionname_record(self.config_file)


class TestSetupOnionname(Base):

    def name(self, user, registrar, **kw):
        report = {}
        got = setup_logic.setup_onionname(
            user, paths=self.paths, docker=object(), os_name="linux", log=self.logs.append,
            say=self.said.append, registrar=registrar, report=report, **kw)
        return got, report

    def test_registered_before_anything_else(self):
        reg = Registrar("ok")
        got, report = self.name("alice", reg)
        self.assertEqual(got, "alice")
        self.assertEqual(reg.calls, [("alice", ADDR)])
        self.assertEqual(self.config()["registered"], True)
        self.assertEqual(report, {"name": "alice"})

    def test_not_answering_is_recorded_unregistered(self):
        got, _ = self.name("alice", Registrar("unreachable"))
        self.assertEqual(got, "alice")
        self.assertEqual((self.config()["name"], self.config()["registered"]), ("alice", False))

    def test_register_later_asks_nobody(self):
        reg = Registrar("ok")
        got, _ = self.name("alice", reg, register_later=True)
        self.assertEqual(got, "alice")
        self.assertEqual(reg.calls, [])
        self.assertEqual(self.config()["registered"], False)

    def test_no_onionhome_asks_nobody_and_installs(self):
        """ONIONHOME_ADDRESS empty (the e2e harness's baseline): the name
        is recorded unregistered, OnionHome never asked, no retry marker."""
        with open(self.config_file, "a") as f:
            f.write("ONIONHOME_ADDRESS=\n")
        reg = Registrar("ok")
        got, report = self.name("alice", reg)
        self.assertEqual(got, "alice")
        self.assertEqual(reg.calls, [])
        self.served.assert_not_called()
        self.assertEqual((self.config()["name"], self.config()["registered"]), ("alice", False))
        self.assertTrue(any("OnionHome is off" in s for s in self.said))
        self.assertFalse(os.path.exists(os.path.join(self.data, app_commands.ONIONNAME_RETRY_MARKER)))

    def test_the_configured_onionhome_is_the_registrars(self):
        from onionpress import onionnames_registrar as onr
        other = "b" * 52 + "qd2a.onion"
        with open(self.config_file, "a") as f:
            f.write(f"ONIONHOME_ADDRESS={other}\n")
        made = []

        def make(docker, log=None, onionhome_address=None):
            made.append(onionhome_address)
            return Registrar("ok")

        with mock.patch.object(onr, "Registrar", side_effect=make):
            got, _ = self.name("alice", None)
        self.assertEqual((got, made), ("alice", [other]))

    def test_a_taken_name_without_a_terminal_stops_the_setup(self):
        got, report = self.name("alice", Registrar("collision"))
        self.assertEqual(got, "")
        self.assertEqual(report, {"error": "taken", "name": "alice", "suggestions": ["bob-2"]})
        self.assertEqual(self.config()["name"], "", "a taken name is not recorded")

    def test_a_taken_name_is_asked_again_at_a_terminal(self):
        typed = ["Not Valid", ""]       # the suggestion is the default
        reg = Registrar("collision", "ok")
        got, report = self.name("alice", reg, input_func=lambda prompt: typed.pop(0))
        self.assertEqual(got, "bob-2")
        self.assertEqual(reg.calls, [("alice", ADDR), ("bob-2", ADDR)])
        self.assertEqual(report, {"name": "bob-2"})
        self.assertTrue(any(s.startswith("  Invalid username:") for s in self.said))

    def test_no_answer_at_the_terminal_stops(self):
        def eof(_prompt):
            raise EOFError
        got, report = self.name("alice", Registrar("collision"), input_func=eof)
        self.assertEqual(got, "")
        self.assertEqual(report["error"], "taken")
        self.assertIn("--register-later", self.said[-1])

    def test_taken_every_time_gives_up(self):
        got, _ = self.name("alice", Registrar("collision"), input_func=lambda p: "bob-2")
        self.assertEqual(got, "")
        self.assertIn("no onionname could be registered", self.said[-1])

    def test_an_invalid_name_stops(self):
        got, report = self.name("no", Registrar("ok"))
        self.assertEqual(got, "")
        self.assertIn("error", report)


class FlowDocker:
    def container_running(self, name):
        return True


class TestSetupFlow(Base):

    def run_setup(self, registrar, *, env=None, **kw):
        self.install = mock.Mock(return_value=True)

        def wp(_docker, args, **_k):
            return mock.Mock(returncode=1 if args[:2] == ("core", "is-installed") else 0)
        report = {}
        with mock.patch.dict(os.environ, env or {setup_logic.SETUP_PASSWORD_ENV: PW}), \
             mock.patch.object(setup_logic, "_run_wp", side_effect=wp), \
             mock.patch.object(setup_logic, "install_fresh_wordpress", self.install), \
             mock.patch.object(setup_logic.onion_hostname, "read_served_address",
                               return_value=ADDR):
            rc = setup_logic.setup_from_terminal(
                data_dir=self.data, mac=False, launcher_bin="/l", docker=FlowDocker(),
                manager=mock.Mock(), log=self.logs.append, say=self.said.append,
                paths=self.paths, registrar=registrar, report=report, **kw)
        return rc, report

    def test_installed_under_the_registered_name_in_the_language(self):
        rc, report = self.run_setup(Registrar("ok"), user="alice", title="T",
                                    language="fr_FR", interactive=False)
        self.assertEqual(rc, setup_logic.SETUP_DONE)
        kw = self.install.call_args.kwargs
        self.assertEqual((kw["onionname"], kw["language"], kw["archive_keys_detached"]),
                         ("alice", "fr_FR", True))
        self.assertEqual(report, {"name": "alice"})

    def test_a_taken_name_installs_nothing(self):
        rc, report = self.run_setup(Registrar("collision"), user="alice", title="T",
                                    interactive=False)
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        self.install.assert_not_called()
        self.assertEqual(report["error"], "taken")

    def test_nothing_is_asked_without_a_terminal(self):
        for kw, why in (({"title": "T"}, "no username"),
                        ({"user": "Bad Name", "title": "T"}, "letters"),
                        ({"user": "alice"}, "no site title")):
            with self.subTest(kw=kw):
                rc, report = self.run_setup(Registrar("ok"), interactive=False, **kw)
                self.assertEqual(rc, setup_logic.SETUP_FAILED)
                self.assertIn(why, report["error"].lower() if why == "letters" else report["error"])
        rc, report = self.run_setup(Registrar("ok"), env={setup_logic.SETUP_PASSWORD_ENV: "short"},
                                    user="alice", title="T", interactive=False)
        self.assertIn("at least 6 characters", report["error"])
        self.install.assert_not_called()

    def test_an_unknown_language_is_refused(self):
        rc, report = self.run_setup(Registrar("ok"), user="alice", title="T", language="xx",
                                    interactive=False)
        self.assertEqual(rc, setup_logic.SETUP_FAILED)
        self.assertIn("unknown language", report["error"])


class TestSetupJson(unittest.TestCase):

    def run_cli(self, rc, report_update=None, *, raises=None):
        def flow(**kw):
            if raises:
                raise raises
            kw["report"].update(report_update or {})
            kw["say"]("progress")
            return rc
        with tempfile.TemporaryDirectory() as data, \
             mock.patch.object(setup_logic, "setup_from_terminal", side_effect=flow), \
             mock.patch.object(cli, "Docker"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            got = cli.main(["--data-dir", data, "setup", "--user", "alice", "--title", "T",
                            "--launcher", "/l", "--json"])
        self.assertEqual(got, rc if not raises else 1)
        self.assertIn("progress" if not raises else "ERROR: setup", err.getvalue())
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, "stdout holds the answer only")
        return json.loads(lines[0])

    def test_answers(self):
        self.assertEqual(self.run_cli(0, {"name": "alice"})["ok"], True)
        self.assertEqual(self.run_cli(0, {"name": "alice"})["name"], "alice")
        self.assertIn("onionname", self.run_cli(0))
        self.assertTrue(self.run_cli(10)["restart"])
        self.assertTrue(self.run_cli(2, {"error": "WordPress is already installed"})
                        ["already_installed"])
        taken = self.run_cli(1, {"error": "taken", "name": "alice", "suggestions": ["b-2"]})
        self.assertEqual((taken["error"], taken["suggestions"]), ("taken", ["b-2"]))
        self.assertNotIn("ok", taken)
        self.assertIn("see the OnionPress log", self.run_cli(1)["error"])

    def test_no_traceback(self):
        answer = self.run_cli(None, raises=RuntimeError("boom"))
        self.assertIn("boom", answer["error"])


class TestProvision(Base):

    def provision(self, registrar, *, env=None, ok=True, **kw):
        report = {}
        with mock.patch.dict(os.environ, env or {setup_logic.SETUP_PASSWORD_ENV: PW}), \
             mock.patch.object(setup_logic, "provision_existing_wordpress",
                               return_value=ok) as prov, \
             mock.patch.object(setup_logic, "_read_onion_address", return_value=ADDR):
            got = setup_logic.provision_from_flags(
                data_dir=self.data, docker=object(), os_name="linux", log=self.logs.append,
                say=self.said.append, paths=self.paths, registrar=registrar, report=report, **kw)
            self.env_after = dict(os.environ)
        return got, report, prov

    def test_the_flags_reach_provision_existing_wordpress(self):
        got, report, prov = self.provision(Registrar("ok"), user="alice", title="T",
                                           language="de_DE", adopt_existing=True)
        self.assertTrue(got)
        kw = prov.call_args.kwargs
        self.assertEqual((kw["onionname"], kw["password"], kw["site_title"], kw["language"],
                          kw["onion_addr"], kw["adopt_existing"]),
                         ("alice", PW, "T", "de_DE", ADDR, True))
        self.assertNotIn(setup_logic.SETUP_PASSWORD_ENV, self.env_after)

    def test_refusals(self):
        got, report, prov = self.provision(Registrar("ok"), user="alice",
                                           env={setup_logic.SETUP_PASSWORD_ENV: "x"})
        self.assertFalse(got)
        prov.assert_not_called()
        got, report, prov = self.provision(Registrar("collision"), user="alice")
        self.assertEqual(report["error"], "taken")
        prov.assert_not_called()
        got, report, prov = self.provision(Registrar("ok"), user="alice", ok=False)
        self.assertIn("--adopt-existing", report["error"])
        got, report, prov = self.provision(Registrar("ok"), user="alice", language="xx")
        self.assertIn("unknown language", report["error"])

    def test_the_subcommand(self):
        with tempfile.TemporaryDirectory() as data, \
             mock.patch.object(setup_logic, "provision_from_flags",
                               side_effect=lambda **kw: kw["report"].update(name="alice") or True), \
             mock.patch.object(cli, "Docker"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.main(["--data-dir", data, "provision", "--user", "alice", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        with tempfile.TemporaryDirectory() as data, \
             mock.patch.object(setup_logic, "provision_from_flags",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cli, "Docker"), \
             mock.patch("sys.stderr", new_callable=io.StringIO), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.main(["--data-dir", data, "provision", "--user", "alice", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn("boom", json.loads(out.getvalue())["error"])


class TestRenameAdmin(Base):

    def setUp(self):
        super().setUp()
        with open(self.config_file, "a") as f:
            f.write("ONIONNAME=alice\n")

    def test_logins_on_stdin_never_in_the_code(self):
        calls = []
        with mock.patch.object(setup_logic, "_run_wp",
                               side_effect=lambda d, args, **k: calls.append((args, k)) or
                               mock.Mock(returncode=0)):
            self.assertTrue(setup_logic.rename_wp_user("alice", "bob-2", docker=object(),
                                                       log=self.logs.append))
        [(args, kw)] = calls
        self.assertEqual(args, ["eval", setup_logic.WP_RENAME_USER_PHP])
        self.assertEqual(kw["input"], "alice\nbob-2")
        self.assertNotIn("alice", setup_logic.WP_RENAME_USER_PHP)
        for needed in ("$wpdb->update($wpdb->users", "clean_user_cache", '"site_admins"',
                       'get_user_by("login", $new)'):
            self.assertIn(needed, setup_logic.WP_RENAME_USER_PHP)

    def test_invalid_names_never_reach_wordpress(self):
        with mock.patch.object(setup_logic, "_run_wp") as wp:
            self.assertFalse(setup_logic.rename_wp_user("alice", "x'; DROP", docker=object(),
                                                        log=self.logs.append))
        wp.assert_not_called()

    def set(self, registrar, renamed=True):
        out = io.StringIO()
        with mock.patch.object(setup_logic, "rename_wp_user", return_value=renamed) as rename:
            rc = app_commands.onionname_set("bob-2", self.paths, object(), "linux",
                                            self.logs.append, json_output=True, out=out,
                                            registrar=registrar, rename_admin=True)
        return rc, json.loads(out.getvalue()), rename

    def test_the_admin_follows_the_new_name(self):
        rc, answer, rename = self.set(Registrar("ok"))
        self.assertEqual(rc, 0)
        self.assertTrue(answer["admin_renamed"])
        rename.assert_called_once_with("alice", "bob-2", docker=mock.ANY, log=mock.ANY)
        self.assertEqual(self.config()["name"], "bob-2")

    def test_a_taken_name_renames_nothing(self):
        rc, answer, rename = self.set(Registrar("collision"))
        self.assertEqual((rc, answer["error"]), (2, "taken"))
        rename.assert_not_called()

    def test_a_failed_rename_is_said(self):
        rc, answer, _ = self.set(Registrar("ok"), renamed=False)
        self.assertEqual(rc, 1)
        self.assertIn("still 'alice'", answer["error"])
        self.assertEqual(self.config()["name"], "bob-2", "the registered name is recorded")


class TestArchiveKeysDetached(Base):

    def test_a_short_lived_setup_spawns_the_fetch(self):
        with mock.patch.object(setup_logic, "_run_wp", return_value=mock.Mock(returncode=0,
                                                                             stderr="")), \
             mock.patch.object(setup_logic, "mark_wordpress_installed_by_onionpress"), \
             mock.patch.object(setup_logic, "provision_primary_subsite"), \
             mock.patch.object(setup_logic, "start_bundled_plugin_setup"), \
             mock.patch.object(setup_logic.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch("onionpress.start.run_detached_cli") as detached, \
             mock.patch("threading.Thread") as thread:
            self.assertTrue(setup_logic.install_fresh_wordpress(
                site_title="T", onionname="alice", password=PW, docker=object(),
                launcher_bin="/l", log_func=self.logs.append, data_dir=self.data,
                archive_keys_detached=True))
        detached.assert_called_once_with(["ensure-archive-s3-keys"], data_dir=self.data,
                                         log_file=os.path.join(self.data, "onionpress.log"))
        thread.assert_not_called()


class TestConfiguredOnionHome(unittest.TestCase):
    """onionnames_registrar.configured_onionhome: the config's
    ONIONHOME_ADDRESS, the key analytics sharing reads."""

    def answer(self, text):
        from onionpress import onionnames_registrar as onr
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config")
            with open(path, "w") as f:
                f.write(text)
            logs = []
            return onr.configured_onionhome(path, logs.append), logs

    def test_absent_is_the_default(self):
        from onionpress import onionnames_registrar as onr
        self.assertEqual(self.answer("ONIONNAME=alice\n"), (onr.DEFAULT_ONIONHOME_ADDRESS, []))

    def test_empty_is_none(self):
        self.assertEqual(self.answer("ONIONHOME_ADDRESS=\n"), ("", []))
        self.assertEqual(self.answer("ONIONHOME_ADDRESS=  \n"), ("", []))

    def test_another_address(self):
        other = "b" * 52 + "qd2a.onion"
        self.assertEqual(self.answer(f"ONIONHOME_ADDRESS={other}\n"), (other, []))

    def test_not_an_onion_address_is_none_and_said(self):
        addr, logs = self.answer("ONIONHOME_ADDRESS=example.com.onion\n")
        self.assertEqual(addr, "")
        self.assertTrue(logs and "not a valid onion address" in logs[0])

    def test_the_key_is_analytics_sharings(self):
        from onionpress import onionnames_registrar as onr
        self.assertIn(f'"{onr.ONIONHOME_ADDRESS_KEY}"', _read("src/onionpress/analytics_sharing.py"))


class TestTheUIsRunTheCli(unittest.TestCase):
    """Issue #527's done-when: neither UI imports the install, the
    registrar, the hostname reader, the token minting or wp_files; both
    run `setup` / `provision` / `onionname set` / `open` through the
    launcher, the password in the environment."""

    UIS = ("src/menubar.py", "linux/onionpress-tray")

    def test_no_ui_imports_the_hosts_work(self):
        for rel in self.UIS:
            src = _read(rel)
            with self.subTest(rel):
                for name in ("install_fresh_wordpress", "provision_existing_wordpress",
                             "onionnames_registrar", "Registrar(", "onion_hostname", "auto_login",
                             "wp_files", "login_target", "login_url", "self.docker",
                             "Docker(", "launcher_ops."):
                    self.assertNotIn(name, src)

    def test_the_setup_is_the_clis(self):
        menubar, tray = (_read(rel) for rel in self.UIS)
        self.assertIn('"setup", "--user", name, "--title", sw.site_title,', menubar)
        self.assertIn("env={ui_client.SETUP_PASSWORD_ENV: sw.admin_pass}", menubar)
        self.assertIn('verb = "provision" if self._status_wp_installed() else "setup"', tray)
        self.assertIn("env={ui_client.SETUP_PASSWORD_ENV: password}", tray)
        for src in (menubar, tray):
            self.assertIn('"--json"', src)
            self.assertIn('later = ["--register-later"]', src)
            self.assertIn('answer.get("error") == "taken"', src)
        self.assertIn('"onionname", "set", new_name, "--rename-admin", "--json"', menubar)

    def test_the_password_env_is_one_name(self):
        self.assertEqual(ui_client.SETUP_PASSWORD_ENV, setup_logic.SETUP_PASSWORD_ENV)
        self.assertIn("provision", ui_client.UNLOGGED_ARGS)

    def test_the_languages_are_one_list(self):
        pattern = re.compile(r'\("[^"]+", "([a-z]{2}(?:_[A-Z]{2})?)"\)')
        for rel in ("src/onionpress/setup_window.py", "linux/onionpress-tray"):
            with self.subTest(rel):
                self.assertEqual(tuple(pattern.findall(_read(rel))), setup_logic.SETUP_LANGUAGES)


class TestLauncherKeepsTheEnvironmentsPassword(unittest.TestCase):
    """A UI hands the password in the environment: run_setup keeps it
    unless --pass gives one."""

    def test_both_launchers(self):
        for rel, runner in (("app/MacOS/onionpress", "bundled_python"),
                            ("linux/onionpress", "op_py")):
            text = _read(rel)
            i = text.index("\nrun_setup() {")
            fn = text[i:text.index("\n}\n", i) + 3]
            script = (f'{runner}() {{ echo "env:$ONIONPRESS_SETUP_PASSWORD:$ONIONPRESS_IA_PASSWORD"; }}\n'
                      'DATA_DIR=/d; SCRIPT_DIR=/s; INSTALL_DIR=/s; _HUB_RECOVERY_KEY=\n' + fn
                      + 'ONIONPRESS_SETUP_PASSWORD=fromui ONIONPRESS_IA_PASSWORD=ia run_setup --user a\n'
                      + 'ONIONPRESS_SETUP_PASSWORD=fromui run_setup --user a --pass given\n')
            out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                                 timeout=30).stdout.splitlines()
            with self.subTest(rel):
                self.assertEqual(out, ["env:fromui:ia", "env:given:"])


if __name__ == "__main__":
    unittest.main()
