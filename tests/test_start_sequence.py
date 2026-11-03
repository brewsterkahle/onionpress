#!/usr/bin/env python3
"""A launcher's start once Docker answers (issue #442.8):
start.start_containers, which both launchers run through
`onionpress start-containers` (Linux `--until-core`, then its install-state
steps and `start-post-up`).

The order is the safety: a pending key import, then the key volume's
migration, then the first run's key, each before anything that could start
Tor and each stopping the start when it cannot be done; only then the core
services and, after them, Tor."""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress import start  # noqa: E402

MAC = os.path.join(ROOT, "app", "MacOS", "onionpress")
LINUX = os.path.join(ROOT, "linux", "onionpress")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _function(text, name):
    i = text.index(f"\n{name}() {{")
    return text[i:text.index("\n}\n", i)]


class TestOrder(unittest.TestCase):

    def run_start(self, fail=None, installed=True, until_core=False):
        order = []
        manager = mock.Mock()
        manager.apply_pending_key_import.side_effect = (
            lambda: order.append("import") or fail != "import")
        manager.migrate_key_volume.side_effect = (
            lambda: order.append("migrate") or fail != "migrate")
        seed = mock.Mock()
        seed.return_value.run.side_effect = lambda: order.append("seed") or fail != "seed"
        lines = []
        with mock.patch.object(start, "KeySeed", seed), \
             mock.patch.object(start, "start_core",
                               side_effect=lambda *a, **k: order.append("core")
                               or (fail != "core", installed)), \
             mock.patch.object(start, "post_up",
                               side_effect=lambda *a, **k: order.append("post-up")):
            rc = start.start_containers(manager, mac=True, wp_port=8080, errors=None,
                                        until_core=until_core, log=lines.append)
        return rc, order, lines

    def test_the_key_steps_come_first_in_order(self):
        rc, order, _ = self.run_start()
        self.assertEqual((rc, order), (start.INSTALLED, ["import", "migrate", "seed", "core",
                                                         "post-up"]))

    def test_each_key_step_stops_the_start(self):
        for step, done in (("import", ["import"]), ("migrate", ["import", "migrate"]),
                           ("seed", ["import", "migrate", "seed"]),
                           ("core", ["import", "migrate", "seed", "core"])):
            with self.subTest(step=step):
                rc, order, lines = self.run_start(fail=step)
                self.assertEqual((rc, order), (start.STOP, done))
        _, _, lines = self.run_start(fail="migrate")
        self.assertEqual(lines, ["ERROR: cannot start without the onion service key volume"])

    def test_not_installed_and_until_core(self):
        rc, order, _ = self.run_start(installed=False)
        self.assertEqual(rc, start.NOT_INSTALLED)
        self.assertIn("post-up", order)
        rc, order, _ = self.run_start(until_core=True)
        self.assertEqual((rc, order[-1]), (start.INSTALLED, "core"))


class TestLaunchers(unittest.TestCase):

    def test_the_bash_start_is_the_wait_then_the_delegation(self):
        for path, wait in ((MAC, "if ! wait_for_docker_or_recover_vm 180 start; then"),
                           (LINUX, "if ! wait_for_docker 180; then")):
            with self.subTest(path=os.path.relpath(path, ROOT)):
                body = _function(_read(path), "start_containers")
                self.assertLess(body.index(wait), body.index("start-containers"))
                self.assertIn('*) return 1 ;;', body)
                for gone in ("docker compose", "docker_volume_state", "migrate_key_volume",
                             "docker run"):
                    self.assertNotIn(gone, body)
        text = _read(MAC)
        mac = _function(text, "start_containers")
        self.assertIn('note_start_errors "$errors"', mac)
        self.assertIn("--bin-dir \"$BIN_DIR\"", mac)
        for gone in ("apply_pending_key_import", "seed_onion_key", "start_core_services",
                     "start_post_up"):
            self.assertNotIn(f"\n{gone}() {{", text)

    def test_linux_runs_its_host_steps_between(self):
        text = _read(LINUX)
        body = _function(text, "start_containers")
        self.assertIn('ONIONPRESS_BIND_HOST="$(wordpress_bind_host_for_start)" \\\n'
                      '        op_py --data-dir "$DATA_DIR" start-containers --until-core', body)
        core = body.index("start-containers --until-core")
        post = body.index('start_post_up "$wp_installed"')
        for between in ("after_wordpress_installed", "bind_wordpress_port_for_install_state false",
                        "note_if_lan_looks_local"):
            self.assertTrue(core < body.index(between) < post, between)
        # Issue #504: no watcher for the onionheaven container (a core service).
        self.assertNotIn("activation_watcher", text)
        for gone in ("apply_pending_key_import", "seed_onion_key", "start_core_services"):
            self.assertNotIn(f"\n{gone}() {{", text)


if __name__ == "__main__":
    unittest.main()
