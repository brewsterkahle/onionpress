#!/usr/bin/env python3
"""A census of `onionpress` subcommands: every one `cli.main()` dispatches
goes through `main([...])` here with its handler stubbed.

The parser is built inside cli.main(), so the census takes it from there
(parse_args captured) and walks the argparse tree: every subparser, and
every nested one (config get|set|unset|list, hub-recovery-key
create|show|check|restore). A new `add_parser(...)` in cli.py fails
test_every_subcommand_has_a_row until this file has a row for it: the
row says which handler the subcommand reaches (an OnionPressCLI method,
a module function) with which parsed arguments, and what exit code main
returns for the stub's. The check/confirm plumbing (`--check --json`,
`--confirmed WHAT`, $ONIONPRESS_CONFIRMED; onionpress.confirm) is
covered for the subcommands that take it.

Nothing here runs a handler: OnionPressCLI and Docker are mocks, every
module function a stub, and the fast paths (the ones that must never
construct OnionPressCLI, since its setup creates config and secrets and
probes ports) are checked not to. Style after
tests/test_container_capabilities.py."""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import (  # noqa: E402
    backup, cli, config, confirm, containers, daemon, derivatives, handle_action,
    launcher_ops, multisite, onionheaven, redact, scrub, wordpress_egress,
)
from onionpress.platform import OS  # noqa: E402

RC = 7  # what every stub returns: main must hand it back unchanged
ONION = "a" * 56 + ".onion"
ANY = mock.ANY


def parser_tree():
    """{("config", "set"): parser, ...}: every subcommand cli.main() defines,
    taken from the parser it builds (parse_args is stopped before parsing)."""
    captured = {}

    class Captured(Exception):
        pass

    def grab(self, argv=None, namespace=None):
        captured["parser"] = self
        raise Captured

    with mock.patch.object(argparse.ArgumentParser, "parse_args", grab):
        with contextlib.suppress(Captured):
            cli.main(["start"])
    tree = {}

    def walk(parser, path):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    tree[path + (name,)] = sub
                    walk(sub, path + (name,))

    walk(captured["parser"], ())
    return tree


class Row:
    """One dispatch: *argv* reaches *stub* (see _target) called as *call*
    and main returns *rc*. *stub* is "CLI().name" (an OnionPressCLI
    instance method), "CLI.name" (called on the class) or "module.name"
    (a module function); *os_name* is what detect_os answers; *env* and
    *stdin* are what the run gets; *returns* is the stub's answer when it
    is not RC; *check* gets the stub's mock and the test for more asserts;
    *setup_cli* is False where OnionPressCLI must not be constructed."""

    def __init__(self, key, argv, stub, call, rc=RC, *, os_name="linux", env=None,
                 stdin="", returns=RC, check=None, setup_cli=None):
        self.key = tuple(key.split()) if isinstance(key, str) else key
        self.argv = argv
        self.stub = stub
        self.call = call
        self.rc = rc
        self.os_name = os_name
        self.env = env or {}
        self.stdin = stdin
        self.returns = returns
        self.check = check
        self.setup_cli = stub.startswith("CLI().") if setup_cli is None else setup_cli

    def __repr__(self):
        return f"Row({' '.join(self.key)}: {' '.join(self.argv)!r} -> {self.stub})"


def D(path):
    """A path inside the run's data directory (filled in by the test)."""
    return _DataDirPath(path)


class _DataDirPath(str):
    pass


HRK_PATH = os.path.join("tor-keys", "healthcheck")

# `ha_host` checks handle-action's Host: built without OnionPressCLI, from
# the launcher given or default_launcher's answer.
def ha_host(launcher):
    def check(test, m):
        host = m.call_args.args[0]
        test.assertIsInstance(host, handle_action.Host)
        test.assertEqual(host.launcher, launcher)
        test.assertEqual(host.os_name, "linux")
    return check


def positional_launcher(test, m):
    test.assertEqual(m.call_args.args[3], "/stub/launcher")


ROWS = [
    # ---- OnionPressCLI's own commands (constructed with the data dir) ----
    Row("start", [], "CLI().cmd_start", mock.call()),
    Row("start", ["start"], "CLI().cmd_start", mock.call()),
    Row("stop", ["stop"], "CLI().cmd_stop", mock.call()),
    Row("restart", ["restart"], "CLI().cmd_restart", mock.call()),
    Row("status", ["status"], "CLI().cmd_status", mock.call()),
    Row("address", ["address"], "CLI().cmd_address", mock.call()),
    Row("logs", ["logs"], "CLI().cmd_logs", mock.call()),
    Row("get", ["get", "-i", "--max-time", "5", f"http://{ONION}/x"],
        "CLI().cmd_get", mock.call(f"http://{ONION}/x", headers=True, max_time=5)),
    Row("get", ["get", ONION], "CLI().cmd_get",
        mock.call(ONION, headers=False, max_time=cli.GET_DEFAULT_MAX_TIME)),
    Row("backup", ["backup", "-o", "/out/b.zip", "--user", "me"], "CLI().cmd_backup",
        mock.call("pw", "/out/b.zip", "me", check=False, json_output=False),
        env={cli.BACKUP_PASSWORD_ENV: "pw"}),
    Row("backup", ["backup", "--check", "--json"], "CLI().cmd_backup",
        mock.call("pw", None, None, check=True, json_output=True),
        env={cli.BACKUP_PASSWORD_ENV: "pw"}),
    Row("backup", ["backup"], "CLI().cmd_backup",
        mock.call("from-stdin", None, None, check=False, json_output=False),
        stdin="from-stdin\n"),
    Row("restore", ["restore", "/b.zip"], "CLI().cmd_restore",
        mock.call("pw", "/b.zip", "", confirmed=frozenset(), json_output=False),
        env={cli.BACKUP_PASSWORD_ENV: "pw"}),
    Row("restore", ["restore", "/b.zip", "--confirmed", "sealed-hub-data-left-out", "--json"],
        "CLI().cmd_restore",
        mock.call("pw", "/b.zip", "hubkey", confirmed=frozenset({"sealed-hub-data-left-out"}),
                  json_output=True),
        env={cli.BACKUP_PASSWORD_ENV: "pw", cli.RECOVERY_KEY_ENV: "hubkey"}),
    Row("restore", ["restore", "/b.zip", "--check", "--json"], "CLI().cmd_restore_check",
        mock.call("pw", "/b.zip", "", frozenset({"reset"}), True),
        env={cli.BACKUP_PASSWORD_ENV: "pw", confirm.CONFIRMED_ENV: "reset"}),
    Row("reset", ["reset", "--yes"], "CLI().cmd_reset",
        mock.call(yes=True, check=False, json_output=False, confirmed=frozenset())),
    Row("reset", ["reset", "--check", "--json", "--confirmed", "reset",
                  "--confirmed", "hub-holds-key"], "CLI().cmd_reset",
        mock.call(yes=False, check=True, json_output=True,
                  confirmed=frozenset({"reset", "hub-holds-key"}))),
    Row("update-images", ["update-images", "--json"], "CLI().cmd_update_images",
        mock.call(json_output=True)),
    Row("smoke-test-wayback", ["smoke-test-wayback"], "CLI().cmd_smoke_test_wayback", mock.call()),
    Row("check-for-update", ["check-for-update", "--json", "--current", "1.2.3"],
        "CLI().cmd_check_for_update", mock.call(json_output=True, current="1.2.3")),
    Row("generate-vanity", ["generate-vanity"], "CLI().cmd_generate_vanity", mock.call()),
    Row("admin-password", ["admin-password"], "CLI().cmd_admin_password", mock.call()),
    Row("import-backup-artifacts", ["import-backup-artifacts", "--staging", "/st"],
        "CLI().cmd_import_backup_artifacts", mock.call("/st")),
    Row("regenerate-derivatives", ["regenerate-derivatives"],
        "CLI().cmd_regenerate_derivatives", mock.call()),
    Row("prune-image-derivatives", ["prune-image-derivatives", "--uploads-dir", "/st/uploads"],
        "CLI().cmd_prune_image_derivatives", mock.call("/st/uploads")),
    Row("pack-backup", ["pack-backup", "--staging", "/st", "--output", "/b.zip"],
        "CLI().cmd_pack_backup", mock.call("/st", "/b.zip", "pw"), stdin="pw\n"),
    Row("extract-backup", ["extract-backup", "--archive", "/b.zip", "--dest", "/d"],
        "CLI().cmd_extract_backup", mock.call("/b.zip", "/d", "pw"), stdin="pw\n"),

    # ---- the Mac bundle's own: no OnionPressCLI setup ----
    Row("update", ["update", "--check", "--yes", "--json"], "cli.cmd_update",
        mock.call(ANY, ANY, check=True, yes=True, json_output=True), os_name="macos"),
    Row("uninstall", ["uninstall", "--check", "--json", "--confirmed", "uninstall"],
        "cli.cmd_uninstall",
        mock.call(ANY, ANY, ANY, "/stub/launcher", check=True, json_output=True,
                  confirmed=frozenset({"uninstall"})),
        os_name="macos", check=positional_launcher),

    # ---- the Settings page's requests (issue #366) ----
    Row("handle-action", ["handle-action", "--json", "--confirmed", "address-change"],
        "handle_action.handle",
        mock.call(ANY, confirmed=frozenset({"address-change"}), cancel=False),
        rc=0, returns={"handled": "restore-backup"}, check=ha_host("/stub/launcher")),
    Row("handle-action", ["handle-action", "--launcher", "/my/launcher", "--cancel"],
        "handle_action.handle", mock.call(ANY, confirmed=frozenset(), cancel=True),
        rc=0, returns={"handled": None}, check=ha_host("/my/launcher")),
    Row("apply-config", ["apply-config"], "handle_action.apply_config",
        mock.call(ANY, out=ANY), rc=0, returns=[],
        check=lambda t, m: t.assertIs(m.call_args.kwargs["out"], sys.stdout)),
    Row("apply-config", ["apply-config"], "handle_action.apply_config",
        mock.call(ANY, out=ANY), rc=1, returns=None),
    Row("settings-page-backup", ["settings-page-backup", "/out.zip", "--json"],
        "cli.cmd_settings_page_backup", mock.call("/out.zip", "pw", D(""), True),
        env={cli.BACKUP_PASSWORD_ENV: "pw"}),
    Row("settings-page-restore",
        ["settings-page-restore", "/up.zip", "--json", "--confirmed", "address-change"],
        "cli.cmd_settings_page_restore",
        mock.call("/up.zip", "pw", D(""), True, frozenset({"address-change"})),
        env={cli.BACKUP_PASSWORD_ENV: "pw"}),
    Row("prepare-restore", ["prepare-restore", "/b.zip"], "cli.cmd_prepare_restore",
        mock.call("/b.zip", "pw", D(""), False), stdin="pw\n"),

    # ---- config and OnionHeaven: the config file only (issue #365) ----
    Row("config get", ["config", "get", "VM_CPU"], "config.run_config_command",
        mock.call(ANY, D(""), ANY, os_name="linux", app_bundle=ANY),
        check=lambda t, m: t.assertEqual(
            (m.call_args.args[0].config_command, m.call_args.args[0].key), ("get", "VM_CPU"))),
    Row("config set", ["config", "set", "ADDRESS_PREFIX", "op", "--check", "--json",
                       "--confirmed", "prefix-change"], "config.run_config_command",
        mock.call(ANY, D(""), ANY, os_name="linux", app_bundle=ANY),
        check=lambda t, m: t.assertEqual(
            (m.call_args.args[0].config_command, m.call_args.args[0].key,
             m.call_args.args[0].value, m.call_args.args[0].check, m.call_args.args[0].json,
             m.call_args.args[0].confirmed),
            ("set", "ADDRESS_PREFIX", "op", True, True, ["prefix-change"]))),
    Row("config unset", ["config", "unset", "VM_CPU", "--json"], "config.run_config_command",
        mock.call(ANY, D(""), ANY, os_name="linux", app_bundle=ANY),
        check=lambda t, m: t.assertEqual(
            (m.call_args.args[0].config_command, m.call_args.args[0].json), ("unset", True))),
    Row("config list", ["config", "list"], "config.run_config_command",
        mock.call(ANY, D(""), ANY, os_name="linux", app_bundle=ANY),
        check=lambda t, m: t.assertEqual(m.call_args.args[0].config_command, "list")),
    Row("onionheaven", ["onionheaven"], "onionheaven.run_onionheaven_command",
        mock.call("status", ANY, json_output=False)),
    Row("onionheaven", ["onionheaven", "disable", "--json"], "onionheaven.run_onionheaven_command",
        mock.call("disable", ANY, json_output=True)),
    Row("onionheaven-unregister", ["onionheaven-unregister", "--reason", "reset",
                                   "--keep-address", ONION], "cli.release_onionheaven",
        mock.call("reset", keep_address=ONION, data_dir=D("")), rc=0, returns=[]),
    Row("onionheaven-unregister", ["onionheaven-unregister"], "cli.release_onionheaven",
        mock.call("unregister", keep_address=None, data_dir=D("")), rc=1,
        returns=[{"why": "unanswered"}]),
    Row("onionheaven-unregister", ["onionheaven-unregister"], "cli.release_onionheaven",
        mock.call("unregister", keep_address=None, data_dir=D("")), rc=3,
        returns=[{"why": "no-key"}]),
    Row("onionheaven-registrations", ["onionheaven-registrations"],
        "onionheaven.registrations_left", mock.call(ANY), rc=0, returns=[(ONION, "hub.onion")]),

    # ---- a hub's backup recovery key ----
    Row("hub-recovery-key create", ["hub-recovery-key", "create", "--force"],
        "CLI.cmd_hub_recovery_key_create", mock.call(D(""), force=True)),
    Row("hub-recovery-key show", ["hub-recovery-key", "show"],
        "CLI.cmd_hub_recovery_key_show", mock.call(D(""))),
    Row("hub-recovery-key check", ["hub-recovery-key", "check"],
        "CLI.cmd_hub_recovery_key_check", mock.call(D(""), "hubkey"),
        env={cli.RECOVERY_KEY_ENV: "hubkey"}),
    Row("hub-recovery-key restore", ["hub-recovery-key", "restore", "/b.zip"],
        "CLI.cmd_hub_recovery_key_restore", mock.call("/b.zip", "pw", "hubkey"),
        env={cli.RECOVERY_KEY_ENV: "hubkey", cli.BACKUP_PASSWORD_ENV: "pw"}),

    # ---- the launchers' backup and restore helpers ----
    Row("backup-address", ["backup-address", "--staging", "/st"],
        "CLI.cmd_backup_address", mock.call("/st")),
    Row("healthcheck-key-address", ["healthcheck-key-address", "--dir", HRK_PATH],
        "CLI.cmd_healthcheck_key_address", mock.call(HRK_PATH)),
    Row("restore-config", ["restore-config", "--staging", "/st", "--config", "/c", "--address", ONION],
        "CLI.cmd_restore_config", mock.call("/st", "/c", ONION)),
    Row("scrub-staging", ["scrub-staging", "--dir", "/st/wp-content"],
        "CLI.cmd_scrub_staging", mock.call("/st/wp-content")),
    Row("stage-wp-content", ["stage-wp-content", "--dest", "/st/wp-content"],
        "CLI.cmd_stage_wp_content", mock.call("/st/wp-content")),
    Row("stage-hub-data", ["stage-hub-data", "--dest", "/st/onionheaven"],
        "CLI.cmd_stage_hub_data", mock.call("/st/onionheaven")),
    Row("settings-page-restore-check", ["settings-page-restore-check", "--staging", "/st"],
        "CLI.cmd_settings_page_restore_check", mock.call("/st")),
    Row("wordpress-safe-config", ["wordpress-safe-config", "--config", "/c", "--dest", "/d"],
        "CLI.cmd_wordpress_safe_config", mock.call("/c", "/d")),
    Row("restore-hub-data", ["restore-hub-data", "--src", "/st/onionheaven"],
        "CLI.cmd_restore_hub_data", mock.call("/st/onionheaven")),
    Row("stage-hub-backup", ["stage-hub-backup", "--staging", "/st"],
        "CLI.cmd_stage_hub_backup", mock.call("/st", D(""))),
    Row("settings-page-backup-check", ["settings-page-backup-check", "--address", ONION],
        "CLI.cmd_settings_page_backup_check", mock.call(ONION, D(""))),
    Row("settings-page-restore-node-check", ["settings-page-restore-node-check"],
        "CLI.cmd_settings_page_restore_node_check", mock.call("", D(""))),
    Row("put-wp-tree", ["put-wp-tree", "--src", "/s", "--dest", "/var/www/html/x", "--merge"],
        "CLI.cmd_put_wp_tree", mock.call("/s", "/var/www/html/x", True)),

    # ---- the launchers' read-only helpers ----
    Row("safe-config", ["safe-config"], "config.redact_config",
        mock.call({"A": "1", "SECRET": "x"}), rc=0, returns={"A": "1"}),
    Row("wordpress-logs", ["wordpress-logs", "--log-file", "/l.log"],
        "redact.wordpress_log_excerpt", mock.call("/l.log", D("")), rc=0, returns="tail\n"),
    Row("wordpress-status", ["wordpress-status"], "redact.status_for_wordpress",
        mock.call({"a": 1}), rc=0, returns={"a": 1}, stdin='{"a": 1}'),
    Row("apply-settings-updates", ["apply-settings-updates"], "config.apply_settings_updates",
        mock.call(D("config"), {"VM_CPU": "2"}), rc=0, stdin='{"VM_CPU": "2"}',
        returns=mock.Mock(changes=[("VM_CPU", "1", "2")], accepted=["VM_CPU"], rejected=["X"])),
    # The Mac daemon (#367): no OnionPressCLI; the launcher given, else
    # this install's.
    Row("daemon", ["daemon", "status", "--json"], "daemon.run_daemon_command",
        mock.call("status", ANY, ANY, launcher_script="/stub/launcher", json_output=True,
                  version=cli.__version__, no_offline=False), os_name="macos",
        check=lambda t, m: t.assertEqual(m.call_args.args[1].data_dir, t.dir)),
    Row("daemon", ["daemon", "stop", "--no-offline", "--launcher", "/l"],
        "daemon.run_daemon_command",
        mock.call("stop", ANY, ANY, launcher_script="/l", json_output=False,
                  version=cli.__version__, no_offline=True), os_name="macos"),
    Row("daemon", ["daemon"], "daemon.run_daemon_command",
        mock.call("serve", ANY, ANY, launcher_script="/stub/launcher", json_output=False,
                  version=cli.__version__, no_offline=False), os_name="macos"),
    # Phase 4 (#120): the launchers' retire_old_onionheaven_containers() is
    # ContainerManager's, built without the port probe, log lines on stdout.
    Row("retire-old-onionheaven-containers", ["retire-old-onionheaven-containers"],
        "cli.ContainerManager", mock.call(ANY, ANY, ANY, log_func=ANY), rc=0,
        returns=mock.Mock(), env={"ONIONPRESS_PORT_OFFSET": "10000"},
        check=lambda t, m: (
            m.return_value.retire_old_onionheaven_containers.assert_called_once_with(),
            t.assertEqual(m.call_args.args[2].wp_port, 18080))),
    Row("migrate-tor-network", ["migrate-tor-network"],
        "cli.ContainerManager", mock.call(ANY, ANY, ANY, log_func=ANY), rc=0,
        returns=mock.Mock(),
        check=lambda t, m: (
            m.return_value.migrate_tor_network.assert_called_once_with(),
            m.return_value.retire_old_onionheaven_containers.assert_not_called(),
            t.assertEqual(m.call_args.args[2].wp_port, 8080))),
    # Phase 4 (#120): the launchers' migrate_key_volume() is ContainerManager's;
    # unlike the two above its status matters: 1 stops the start.
    Row("migrate-key-volume", ["migrate-key-volume"],
        "cli.ContainerManager", mock.call(ANY, ANY, ANY, log_func=ANY), rc=0,
        returns=mock.Mock(**{"migrate_key_volume.return_value": True}),
        env={"ONIONPRESS_PORT_OFFSET": "10000"},
        check=lambda t, m: (
            m.return_value.migrate_key_volume.assert_called_once_with(),
            m.return_value.migrate_tor_network.assert_not_called(),
            t.assertEqual(m.call_args.args[2].wp_port, 18080))),
    Row("migrate-key-volume", ["migrate-key-volume"],
        "cli.ContainerManager", mock.call(ANY, ANY, ANY, log_func=ANY), rc=1,
        returns=mock.Mock(**{"migrate_key_volume.return_value": False})),
    # Phase 4 (#120): the Linux launcher's db_import() is backup.db_import,
    # what restore_container_artifacts runs; the password is stdin's first
    # line, never argv. A failure raises there and is exit 1 here
    # (tests/test_db_import_sandbox.py).
    Row("db-import", ["db-import", "--sql-file", "/st/database/wordpress.sql"],
        "backup.db_import", mock.call("/st/database/wordpress.sql", "pw"), rc=0,
        returns=None, stdin="pw\n"),
    # Phase 4 (#120): the launchers' regenerate_derivatives_pending() is this.
    Row("regenerate-derivatives-pending", ["regenerate-derivatives-pending"],
        "derivatives.regeneration_pending", mock.call(D(""), ANY), rc=0, returns=True),
    Row("regenerate-derivatives-pending", ["regenerate-derivatives-pending"],
        "derivatives.regeneration_pending", mock.call(D(""), ANY), rc=1, returns=False),
    # Phase 4 (#120): the launchers' using_local_images() is this.
    Row("using-local-images", ["using-local-images"], "containers.using_local_images",
        mock.call(D("config")), rc=0, returns=True),
    Row("using-local-images", ["using-local-images"], "containers.using_local_images",
        mock.call(D("config")), rc=1, returns=False),
    Row("wordpress-egress", ["wordpress-egress", "--check", "--wp-port", "8080", "--bin-dir", "/b"],
        "wordpress_egress.ensure", mock.call(ANY, ANY, action="check", wp_port=8080),
        rc=0, returns=mock.Mock(ok=True, line=lambda: "ok"),
        check=lambda t, m: t.assertEqual(m.call_args.args[0].bin_dir, "/b")),
    Row("wordpress-egress", ["wordpress-egress"], "wordpress_egress.ensure",
        mock.call(ANY, ANY, action="apply", wp_port=9090), rc=1,
        returns=mock.Mock(ok=False, line=lambda: "failed"), env={"ONIONPRESS_WP_PORT": "9090"}),
    Row("open-login", ["open-login", "http://localhost:8080/wp-admin/"], "launcher_ops.open_login",
        mock.call("http://localhost:8080/wp-admin/"), rc=0, returns=None),
    Row("verify-admin-password", ["verify-admin-password"], "backup.verify_wp_admin_password_any",
        mock.call("pw"), rc=0, returns=(True, ""), stdin="pw\n", setup_cli=True),
    Row("verify-admin-password", ["verify-admin-password"], "backup.verify_wp_admin_password_any",
        mock.call("pw"), rc=1, returns=(False, "no"), stdin="pw\n", setup_cli=True),

    # ---- WordPress provisioning (multisite) and the lifecycle test ----
    Row("provision-post-install", ["provision-post-install", "--themes-dir", "/t", "--plugins-dir", "/p"],
        "multisite.provision_post_install",
        mock.call(themes_dir="/t", plugins_dir="/p", log_func=print), setup_cli=True),
    Row("configure-ia-plugin", ["configure-ia-plugin"], "multisite.configure_ia_plugin",
        mock.call(log_func=print), rc=0, returns=True, setup_cli=True),
    Row("configure-cache-enabler", ["configure-cache-enabler"], "multisite.configure_cache_enabler",
        mock.call(log_func=print), rc=1, returns=False, setup_cli=True),
    Row("apply-cache-policy", ["apply-cache-policy"], "multisite.apply_cache_policy",
        mock.call(log_func=print), rc=0, returns=True, setup_cli=True),
    Row("deactivate-wp-statistics", ["deactivate-wp-statistics"], "multisite.deactivate_wp_statistics",
        mock.call(log_func=print), rc=0, returns=True, setup_cli=True),
    Row("ensure-archive-s3-keys", ["ensure-archive-s3-keys"], "multisite.ensure_archive_s3_keys",
        mock.call(log_func=print), rc=1, returns=False, setup_cli=True),
    Row("scrub", ["scrub", "--clean"], "scrub.run_scrub",
        mock.call(password=None, clean=True), setup_cli=True),
]

MODULES = {
    "cli": cli, "config": config, "handle_action": handle_action, "onionheaven": onionheaven,
    "multisite": multisite, "scrub": scrub, "redact": redact, "backup": backup,
    "wordpress_egress": wordpress_egress, "launcher_ops": launcher_ops,
    "containers": containers, "daemon": daemon, "derivatives": derivatives,
}


# Subcommands that only group others: without a subcommand each prints
# its help and exits 1 (TestDispatch); their children are the census's.
PARENTS = {("config",), ("hub-recovery-key",)}


def leaves(tree):
    return {key for key in tree if not any(other[:len(key)] == key and other != key
                                           for other in tree)}


class TestCensus(unittest.TestCase):

    def test_every_subcommand_has_a_row(self):
        tree = parser_tree()
        rows = {row.key for row in ROWS}
        self.assertEqual(leaves(tree) - rows, set(),
                         "subcommands cli.main() defines without a row in ROWS")
        self.assertEqual(set(tree) - leaves(tree), PARENTS,
                         "a new grouping subcommand: list it in PARENTS and test its help")

    def test_every_row_names_a_subcommand(self):
        defined = leaves(parser_tree())
        self.assertEqual({row.key for row in ROWS} - defined, set(),
                         "rows for subcommands cli.main() no longer defines")

    def test_the_tree_has_the_nested_commands(self):
        tree = parser_tree()
        self.assertIn(("config", "set"), tree)
        self.assertIn(("hub-recovery-key", "restore"), tree)
        self.assertGreater(len(tree), 60)

    def test_confirm_flags_sit_on_the_commands_with_questions(self):
        tree = parser_tree()
        with_check = {k for k, p in tree.items()
                      if any(a.dest == "check" for a in p._actions)
                      and any(a.dest == "confirmed" for a in p._actions)}
        self.assertEqual(with_check, {("backup",), ("restore",), ("reset",), ("uninstall",),
                                      ("config", "set")})
        confirmed_only = {k for k, p in tree.items()
                          if any(a.dest == "confirmed" for a in p._actions)} - with_check
        self.assertEqual(confirmed_only, {("handle-action",), ("settings-page-restore",)})


class TestDispatch(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="op-dispatch-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for var in (cli.BACKUP_PASSWORD_ENV, cli.RECOVERY_KEY_ENV, confirm.CONFIRMED_ENV,
                    "ONIONPRESS_WP_PORT", "ONIONPRESS_LAUNCHER", "DOCKER_HOST"):
            os.environ.pop(var, None)

    def _fill(self, value):
        if isinstance(value, _DataDirPath):
            return os.path.join(self.dir, value) if value else self.dir
        return value

    def _expected(self, call):
        args = tuple(self._fill(a) for a in call.args)
        kwargs = {k: self._fill(v) for k, v in call.kwargs.items()}
        return mock.call(*args, **kwargs)

    def _target(self, stack, MockCLI, stub):
        kind, _, name = stub.partition(".")
        if kind == "CLI()":
            return getattr(MockCLI.return_value, name)
        if kind == "CLI":
            return getattr(MockCLI, name)
        return stack.enter_context(mock.patch.object(MODULES[kind], name))

    def run_row(self, row):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            MockCLI = stack.enter_context(mock.patch.object(cli, "OnionPressCLI"))
            stack.enter_context(mock.patch.object(cli, "Docker"))
            stack.enter_context(mock.patch.object(
                cli, "detect_os", return_value=OS.MACOS if row.os_name == "macos" else OS.LINUX))
            stack.enter_context(mock.patch.object(
                handle_action, "default_launcher", return_value="/stub/launcher"))
            stack.enter_context(mock.patch.object(cli, "_hub_context", return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(
                config, "read_config", return_value={"A": "1", "SECRET": "x"}))
            stack.enter_context(mock.patch.dict(os.environ, row.env))
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(row.stdin)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            target = self._target(stack, MockCLI, row.stub)
            target.return_value = row.returns
            rc = cli.main(["--data-dir", self.dir, *row.argv])
            self.assertEqual(rc, row.rc, f"{row}: stdout {out.getvalue()!r} stderr {err.getvalue()!r}")
            self.assertEqual(target.call_args_list, [self._expected(row.call)], repr(row))
            if row.setup_cli:
                MockCLI.assert_called_once_with(data_dir=self.dir)
            else:
                MockCLI.assert_not_called()
            if row.check:
                row.check(self, target)
            # Secrets leave the environment before any handler runs.
            for var in (cli.BACKUP_PASSWORD_ENV, cli.RECOVERY_KEY_ENV):
                self.assertNotIn(var, os.environ, f"{row}: {var} still in the environment")
        return out.getvalue(), err.getvalue()

    def test_every_row(self):
        for row in ROWS:
            with self.subTest(row=repr(row)):
                self.run_row(row)

    def test_handle_action_prints_the_result_as_json_only_when_asked(self):
        out, _ = self.run_row(ROWS[[r.argv[:1] == ["handle-action"] for r in ROWS].index(True)])
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {"handled": "restore-backup"})
        row = next(r for r in ROWS if r.argv[:1] == ["handle-action"] and "--cancel" in r.argv)
        out, _ = self.run_row(row)
        self.assertEqual(out, "")

    def test_the_read_only_helpers_print_what_the_stubs_answer(self):
        out, _ = self.run_row(next(r for r in ROWS if r.key == ("safe-config",)))
        self.assertEqual(json.loads(out), {"A": "1"})
        out, _ = self.run_row(next(r for r in ROWS if r.key == ("wordpress-logs",)))
        self.assertEqual(out, "tail\n")
        out, _ = self.run_row(next(r for r in ROWS if r.key == ("wordpress-status",)))
        self.assertEqual(json.loads(out), {"a": 1})
        out, _ = self.run_row(next(r for r in ROWS if r.key == ("apply-settings-updates",)))
        self.assertEqual(out.splitlines(), ["changed VM_CPU", "rejected X"])
        out, _ = self.run_row(next(r for r in ROWS if r.key == ("onionheaven-registrations",)))
        self.assertEqual(out, f"{ONION} hub.onion\n")

    # ---- the paths that stop before any handler ----

    def _main(self, argv, env=None, stdin="", os_name="linux"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            MockCLI = stack.enter_context(mock.patch.object(cli, "OnionPressCLI"))
            stack.enter_context(mock.patch.object(cli, "Docker"))
            stack.enter_context(mock.patch.object(
                cli, "detect_os", return_value=OS.MACOS if os_name == "macos" else OS.LINUX))
            stack.enter_context(mock.patch.dict(os.environ, env or {}))
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = cli.main(["--data-dir", self.dir, *argv])
            MockCLI.return_value.assert_not_called()
        return rc, out.getvalue(), err.getvalue()

    def test_backup_and_restore_need_a_password(self):
        rc, out, _ = self._main(["backup", "--json"])
        self.assertEqual(rc, confirm.EXIT_FAILED)
        self.assertIn("password is required", json.loads(out.strip().splitlines()[-1])["error"])
        rc, _, err = self._main(["restore", "/b.zip"])
        self.assertEqual(rc, confirm.EXIT_FAILED)
        self.assertIn("password is required", err)

    def test_restore_check_without_a_password_asks_for_one(self):
        with mock.patch.object(backup, "backup_filename_username", return_value="me"):
            rc, out, _ = self._main(["restore", "/OnionPress-me-2026.zip", "--check", "--json"])
        self.assertEqual(rc, confirm.EXIT_ASK)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]),
                         {"ask": "password", "backup_user": "me"})

    def test_the_settings_page_commands_need_a_password(self):
        for argv in (["settings-page-backup", "/o.zip", "--json"],
                     ["settings-page-restore", "/u.zip", "--json"],
                     ["prepare-restore", "/b.zip", "--json"]):
            with self.subTest(argv=argv):
                rc, out, _ = self._main(argv)
                self.assertEqual(rc, confirm.EXIT_FAILED)
                self.assertIn("password", json.loads(out.strip().splitlines()[-1])["error"])

    def test_hub_recovery_key_restore_needs_a_password_and_no_subcommand_is_help(self):
        rc, _, err = self._main(["hub-recovery-key", "restore", "/b.zip"])
        self.assertEqual((rc, "password is required" in err), (1, True))
        rc, out, _ = self._main(["hub-recovery-key"])
        self.assertEqual(rc, 1)
        self.assertIn("usage: onionpress hub-recovery-key", out)

    def test_config_without_a_subcommand_is_help(self):
        rc, out, _ = self._main(["config"])
        self.assertEqual(rc, 1)
        self.assertIn("usage: onionpress config", out)

    def test_pack_extract_and_verify_need_a_password_on_stdin(self):
        for argv in (["pack-backup", "--staging", "/st", "--output", "/b.zip"],
                     ["extract-backup", "--archive", "/b.zip", "--dest", "/d"]):
            with self.subTest(argv=argv):
                rc, _, err = self._main(argv, stdin="\n")
                self.assertEqual((rc, "no password on stdin" in err), (1, True))
        rc, _, _ = self._main(["verify-admin-password"], stdin="")
        self.assertEqual(rc, 1)

    def test_the_mac_only_commands_elsewhere(self):
        # `update` is the Linux launcher's own (the .deb's checked
        # install), so the Python CLI has no Linux branch: help, exit 1.
        rc, out, _ = self._main(["update"])
        self.assertEqual(rc, 1)
        self.assertIn("usage: onionpress", out)
        rc, out, _ = self._main(["uninstall", "--json"])
        self.assertEqual(rc, confirm.EXIT_FAILED)
        self.assertIn("Linux launcher", json.loads(out.strip().splitlines()[-1])["error"])

    def test_unregister_refuses_a_bad_keep_address(self):
        with mock.patch.object(cli, "release_onionheaven") as rel:
            rc, _, err = self._main(["onionheaven-unregister", "--keep-address", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("not an onion address", err)
        rel.assert_not_called()

    def test_wordpress_status_wants_a_json_object(self):
        rc, _, err = self._main(["wordpress-status"], stdin="not json")
        self.assertEqual((rc, "not JSON" in err), (1, True))
        rc, _, err = self._main(["wordpress-status"], stdin="[1]")
        self.assertEqual((rc, "not a JSON object" in err), (1, True))
        rc, _, err = self._main(["apply-settings-updates"], stdin="{")
        self.assertEqual((rc, "not valid JSON" in err), (1, True))

    def test_open_login_reports_a_runtime_error(self):
        with mock.patch.object(launcher_ops, "open_login", side_effect=RuntimeError("no token")):
            rc, _, err = self._main(["open-login", "http://localhost:8080/"])
        self.assertEqual((rc, "ERROR: no token" in err), (1, True))

    def test_wordpress_logs_reports_an_os_error(self):
        with mock.patch.object(redact, "wordpress_log_excerpt", side_effect=FileNotFoundError(2, "gone")):
            rc, _, err = self._main(["wordpress-logs", "--log-file", "/l"])
        self.assertEqual((rc, "ERROR: gone" in err), (1, True))

    def test_an_unknown_confirmation_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            self._main(["reset", "--confirmed", "bogus"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
