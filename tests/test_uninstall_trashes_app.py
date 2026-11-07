"""The Mac uninstall moves OnionPress.app to the Trash, unless another
account (or another data folder of this one) still uses it: one bundle in
/Applications serves every account on a multi-user Mac. Only the bundle the
running code belongs to, and only one installed in an Applications folder
(uninstall.trash_app)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from onionpress import uninstall, ui_client  # noqa: E402
from onionpress.platform import resolve_paths  # noqa: E402

PROJECT_ROOT = os.path.dirname(HERE)


def account(name, home, uid=501):
    return SimpleNamespace(pw_name=name, pw_dir=home, pw_uid=uid)


class _Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="op-trash-app-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def home(self, name, installed=True):
        home = os.path.join(self.tmp, "Users", name)
        os.makedirs(os.path.join(home, ".onionpress") if installed else home)
        return home


class TestOtherInstalls(_Tmp):

    def test_only_this_install_is_nobody_else(self):
        me = self.home("me")
        names, unsure = uninstall.other_installs(
            os.path.join(me, ".onionpress"), [account("me", me)])
        self.assertEqual((names, unsure), ([], False))

    def test_another_account_with_an_install(self):
        me, other = self.home("me"), self.home("ana")
        names, unsure = uninstall.other_installs(
            os.path.join(me, ".onionpress"), [account("me", me), account("ana", other, 502)])
        self.assertEqual((names, unsure), (["ana"], False))

    def test_this_accounts_default_counts_when_another_folder_is_uninstalled(self):
        me = self.home("me")
        names, _ = uninstall.other_installs(os.path.join(self.tmp, "scratch-data"),
                                            [account("me", me)])
        self.assertEqual(names, ["me"])

    def test_accounts_without_one_and_system_accounts_are_skipped(self):
        me = self.home("me")
        bare = self.home("bob", installed=False)
        system = self.home("_www")
        names, unsure = uninstall.other_installs(
            os.path.join(me, ".onionpress"),
            [account("me", me), account("bob", bare, 503), account("_www", system, 70),
             account("nobody", "/var/empty", 4294967294)])
        self.assertEqual((names, unsure), ([], False))

    def test_a_home_that_cannot_be_looked_into_is_unsure(self):
        me = self.home("me")
        locked = os.path.join(self.tmp, "Users", "locked")
        real_lstat = os.lstat

        def lstat(p, *a, **kw):
            if p.startswith(locked):
                raise PermissionError(13, "Permission denied", p)
            return real_lstat(p, *a, **kw)
        with mock.patch.object(uninstall.os, "lstat", side_effect=lstat):
            names, unsure = uninstall.other_installs(
                os.path.join(me, ".onionpress"), [account("me", me), account("locked", locked, 504)])
        self.assertEqual((names, unsure), ([], True))


class TestTrashApp(_Tmp):

    def setUp(self):
        super().setUp()
        self.apps = os.path.join(self.tmp, "Applications")
        self.app = os.path.join(self.apps, "OnionPress.app")
        os.makedirs(os.path.join(self.app, "Contents", "Resources"))
        with open(os.path.join(self.app, "Contents", "Info.plist"), "w") as f:
            f.write("<plist/>")
        self.inside = os.path.join(self.app, "Contents", "Resources", "uninstall.py")
        self.me = self.home("me")
        self.data_dir = os.path.join(self.me, ".onionpress")
        shutil.rmtree(self.data_dir)  # the uninstall removed it before this step
        p = mock.patch.object(uninstall, "APP_FOLDERS", (self.apps,))
        p.start()
        self.addCleanup(p.stop)
        self.logged = []

    def trash_app(self, accounts=None, trash=None, app=None, running_from=None):
        trash = trash or mock.Mock(side_effect=lambda p: os.path.join(self.tmp, "Trash", "OnionPress.app"))
        self.trash = trash
        return uninstall.trash_app(app if app is not None else self.app, self.data_dir,
                                   self.logged.append,
                                   accounts=accounts if accounts is not None else [account("me", self.me)],
                                   trash=trash, running_from=running_from or self.inside)

    def test_nobody_else_uses_it_so_it_goes_to_the_trash(self):
        result = self.trash_app()
        self.trash.assert_called_once_with(self.app)
        self.assertEqual(result, {"trashed": os.path.join(self.tmp, "Trash", "OnionPress.app")})

    def test_another_account_keeps_it(self):
        other = self.home("ana")
        result = self.trash_app(accounts=[account("me", self.me), account("ana", other, 502)])
        self.trash.assert_not_called()
        self.assertEqual(result, {"kept": self.app, "reason": "other-accounts", "accounts": ["ana"]})

    def test_unsure_keeps_it(self):
        with mock.patch.object(uninstall, "other_installs", return_value=([], True)):
            result = self.trash_app()
        self.trash.assert_not_called()
        self.assertEqual(result["reason"], "unsure")

    def test_a_bundle_outside_applications_stays(self):
        elsewhere = os.path.join(self.tmp, "Work", "OnionPress.app")
        shutil.copytree(self.app, elsewhere)
        result = self.trash_app(app=elsewhere,
                                running_from=os.path.join(elsewhere, "Contents", "x.py"))
        self.trash.assert_not_called()
        self.assertEqual(result, {"kept": elsewhere, "reason": "not-installed"})

    def test_a_checkouts_cli_never_trashes_the_installed_app(self):
        # find_app_bundle falls back to /Applications/OnionPress.app for a
        # CLI run from a checkout: that is not this code's bundle.
        result = self.trash_app(running_from=os.path.join(self.tmp, "checkout", "uninstall.py"))
        self.trash.assert_not_called()
        self.assertEqual(result["reason"], "not-installed")

    def test_no_bundle_is_nothing_to_say(self):
        self.assertEqual(uninstall.trash_app("", self.data_dir, self.logged.append), {})

    def test_a_failed_move_keeps_it_and_says_so(self):
        result = self.trash_app(trash=mock.Mock(side_effect=OSError("Operation not permitted")))
        self.assertEqual(result, {"kept": self.app, "reason": "trash-failed"})
        self.assertTrue(any("could not move" in m for m in self.logged))

    def test_the_rename_fallback_lands_in_the_trash_folder(self):
        # Without Foundation (PyObjC), a rename into ~/.Trash, never a copy
        # or a write into the bundle; a second one gets a new name.
        trash = os.path.join(self.me, ".Trash")
        os.makedirs(os.path.join(trash, "OnionPress.app"))
        with mock.patch.dict(sys.modules, {"Foundation": None}), \
                mock.patch.object(uninstall.os.path, "expanduser",
                                  side_effect=lambda p: p.replace("~", self.me, 1)):
            where = uninstall._trash_item(self.app)
        self.assertEqual(where, os.path.join(trash, "OnionPress 2.app"))
        self.assertFalse(os.path.exists(self.app))
        self.assertTrue(os.path.isfile(os.path.join(where, "Contents", "Info.plist")))


class TestTheUninstallTrashesLast(unittest.TestCase):

    def test_run_trashes_after_the_data_dir_and_reports_it(self):
        tmp = tempfile.mkdtemp(prefix="op-uninstall-app-")
        self.addCleanup(shutil.rmtree, tmp, True)
        data_dir = os.path.join(tmp, "data")
        os.makedirs(os.path.join(data_dir, "colima"))
        paths = resolve_paths(data_dir=data_dir, app_bundle="/Applications/OnionPress.app")
        seen = {}

        def trash_app(app_bundle, data, log):
            seen["data_dir_gone"] = not os.path.exists(data)
            return {"trashed": "/Users/me/.Trash/OnionPress.app"}
        app = {}
        with mock.patch.object(uninstall.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(uninstall, "hubs_holding_key", return_value=[]), \
                mock.patch.object(uninstall.login_item, "remove", return_value=True), \
                mock.patch.object(uninstall.op_daemon, "stop_agent", return_value=True), \
                mock.patch.object(uninstall.op_daemon, "remove_agent"), \
                mock.patch.object(uninstall.time, "sleep"), \
                mock.patch.object(uninstall, "trash_app", side_effect=trash_app) as trash:
            uninstall.run(paths, mock.Mock(), lambda m: None, "/x/onionpress", app_out=app)
        trash.assert_called_once()
        self.assertTrue(seen["data_dir_gone"])
        self.assertEqual(app, {"trashed": "/Users/me/.Trash/OnionPress.app"})

    def test_linux_trashes_nothing(self):
        with open(os.path.join(PROJECT_ROOT, "src", "onionpress", "uninstall.py"),
                  encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def run("):]
        self.assertLess(body.index("_remove_linux(paths, docker, log)\n        return remaining"),
                        body.index("trash_app("))


class TestTheWording(unittest.TestCase):

    def test_each_outcome(self):
        line = ui_client.uninstall_app_line
        self.assertEqual(line({}), "")
        self.assertEqual(line({"trashed": "/x"}), "OnionPress.app is in the Trash.")
        self.assertIn("installed for ana, bo", line({"kept": "/x", "reason": "other-accounts",
                                                     "accounts": ["ana", "bo"]}))
        self.assertIn("could not tell", line({"kept": "/x", "reason": "unsure"}))
        for reason in ("not-installed", "trash-failed"):
            self.assertEqual(line({"kept": "/x", "reason": reason}),
                             "Final step: move OnionPress.app to the Trash.")

    def test_the_cli_answers_it_and_the_menubar_shows_it(self):
        from onionpress import cli
        with mock.patch.object(uninstall, "check", return_value={}), \
                mock.patch.object(uninstall, "run",
                                  side_effect=lambda *a, app_out=None, **kw:
                                  (app_out.update({"trashed": "/t"}), [])[1]), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rc = cli.cmd_uninstall(mock.Mock(), mock.Mock(), lambda m: None, "/x/onionpress",
                                   json_output=True, confirmed={"uninstall"})
        self.assertEqual(rc, 0)
        answer = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(answer["app"], {"trashed": "/t"})
        with open(os.path.join(PROJECT_ROOT, "src", "menubar.py"), encoding="utf-8") as f:
            menubar = f.read()
        self.assertIn('ui_client.uninstall_app_line(ans.get("app")', menubar)
        self.assertNotIn("Move OnionPress.app to the Trash", menubar)


if __name__ == "__main__":
    unittest.main()
