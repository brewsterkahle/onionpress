#!/usr/bin/env python3
"""tor-watchdog.py takes an onion service's id only from one whole v3
address.

discover_services() read each service's id from its hostname file and put
it into DEL_ONION on the control port (sleep, forced republish). Those
files are not only Tor's: Linux's restore installed a backup's healthcheck
hostname there (test_backup_healthcheck_key_restore), and a CR/LF in it
added a control-port command. Now a hostname file that is not one whole
address (or is a link) is replaced by the address the public key derives
to, written in one step.

The watchdog is a standalone script, loaded with importlib; no control
port: send_cmd is a recorder, and the paths point at a temp dir.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import key_manager  # noqa: E402

WATCHDOG = os.path.join(PROJECT_ROOT, "app", "Resources", "docker", "tor", "tor-watchdog.py")
PRIV = b"\x09" * 64
PUB = key_manager.derive_public_key(PRIV)
ADDR = key_manager.derive_onion_address(PUB)
OTHER = key_manager.derive_onion_address(key_manager.derive_public_key(b"\x0b" * 64))


def _load():
    spec = importlib.util.spec_from_file_location("tor_watchdog_service_ids", WATCHDOG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    def __init__(self):
        self.commands = []

    def __call__(self, sock, cmd):
        self.commands.append(cmd)
        return "250 OK\r\n"


class TestServiceIds(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wd = _load()
        self.wd.HS_BASE_DIR = os.path.join(self.tmp, "hs")
        services = os.path.join(self.tmp, "onion-services.json")
        with open(services, "w") as f:
            json.dump([{"name": "healthcheck", "ports": ["80,127.0.0.1:8081"]}], f)
        real_open = open

        def fake_open(path, *a, **kw):
            if path == "/etc/tor/onion-services.json":
                path = services
            return real_open(path, *a, **kw)

        self.wd.open = fake_open
        self.wd.log = lambda msg: None
        self.dir = os.path.join(self.wd.HS_BASE_DIR, "healthcheck")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "hs_ed25519_secret_key"), "wb") as f:
            f.write(key_manager.CTOR_SECRET_HEADER + PRIV)
        with open(os.path.join(self.dir, "hs_ed25519_public_key"), "wb") as f:
            f.write(key_manager.CTOR_PUBLIC_HEADER + PUB)
        self.hostname = os.path.join(self.dir, "hostname")

    def _write_hostname(self, text):
        with open(self.hostname, "w", newline="") as f:
            f.write(text)

    def _del_commands(self):
        services = self.wd.discover_services()
        rec = _Recorder()
        self.wd.send_cmd = rec
        self.wd.del_all_services(object(), services)
        return services, rec.commands

    def test_a_well_formed_hostname_is_used_as_it_is(self):
        self._write_hostname(OTHER + "\n")      # well formed: Tor's to judge
        services, commands = self._del_commands()
        self.assertEqual(services[0]["service_id"], OTHER[:-6])
        self.assertEqual(commands, [f"DEL_ONION {OTHER[:-6]}"])

    def test_anything_else_becomes_the_public_keys_address(self):
        for content in (ADDR + "\r\nSETCONF SocksPort=0.0.0.0:9050\r\n",
                        "x\nDEL_ONION " + ADDR[:-6] + "\n",
                        ADDR[:-6] + "\n", "", "\x00" * 10):
            with self.subTest(content=content):
                if os.path.lexists(self.hostname):
                    os.unlink(self.hostname)
                self._write_hostname(content)
                services, commands = self._del_commands()
                self.assertEqual(commands, [f"DEL_ONION {ADDR[:-6]}"])
                with open(self.hostname) as f:
                    self.assertEqual(f.read(), ADDR + "\n")

    def test_a_link_is_not_followed_and_is_replaced(self):
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write(OTHER + "\n")
        os.symlink(target, self.hostname)
        services, commands = self._del_commands()
        self.assertEqual(commands, [f"DEL_ONION {ADDR[:-6]}"])
        self.assertFalse(os.path.islink(self.hostname))
        with open(target) as f:
            self.assertEqual(f.read(), OTHER + "\n")

    def test_a_fresh_install_still_derives_it(self):
        services, _ = self._del_commands()
        self.assertEqual(services[0]["service_id"], ADDR[:-6])
        with open(self.hostname) as f:
            self.assertEqual(f.read(), ADDR + "\n")


if __name__ == "__main__":
    unittest.main()
