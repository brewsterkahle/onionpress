#!/usr/bin/env python3
"""Every Settings-page key reaches something that reads it (issue #353).

The page offered three takeover tunables on an OnionHeaven hub, and mostly
nothing happened on save: ONIONHEAVEN_HUB_THRESHOLD was read nowhere (it is
gone); ONIONHEAVEN_MAX_SERVICES reached the onionheaven container as
ONIONHEAVEN_TAKEOVER_SCALE_THRESHOLD, a name nothing reads, with one
default in config.DEFAULTS (10) and another in the containers and on the
page (50); ONIONHEAVEN_PROPAGATION_DELAY was exported by the Linux launcher
only, never by the Mac launcher or ContainerManager's compose environment,
and reached the onionheaven container but not tor, whose web-server.py
reads it too.

A census: every key in config.SETTINGS_PAGE_KEYS has a row naming a reader
outside config.py and the page. A key the containers read goes through
docker-compose.yml, so for those the row names the environment variable
compose maps it to, the services that get it and the image's reader; the
default must then be one number in config, compose and the image, and both
launchers and ContainerManager must export the key.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress import config as op_config  # noqa: E402

COMPOSE = "app/Resources/docker/docker-compose.yml"
PAGE = "app/Resources/plugins/onionpress-settings.php"
LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")

# Keys the host itself acts on: (file, the expression that reads the key).
HOST_READERS = {
    # The launcher's, since #428 (issue #36): the menubar reads it nowhere.
    "UPDATE_ON_LAUNCH": ("app/MacOS/onionpress", 'grep "^UPDATE_ON_LAUNCH=" "$DATA_DIR/config"'),
    "LAUNCH_ON_LOGIN": ("src/menubar.py", 'read_config_value("LAUNCH_ON_LOGIN"'),
    "START_ON_BOOT": ("linux/onionpress", 'grep "^START_ON_BOOT="'),
    "PREVENT_SLEEP": ("src/onionpress/power.py", '_read_config("PREVENT_SLEEP"'),
    "VM_MEMORY": ("src/onionpress/colima.py", '"VM_MEMORY"'),
    "REGISTER_WITH_ONIONHEAVEN": ("src/onionpress/onionheaven.py",
                                  'read_config_value("REGISTER_WITH_ONIONHEAVEN"'),
    "SHARE_ANALYTICS_WITH_ONIONHOME": ("src/onionpress/daemon.py",
                                       'read_config_value("SHARE_ANALYTICS_WITH_ONIONHOME")'),
    "CLOUDFLARE_TUNNEL_TOKEN": ("src/onionpress/containers.py",
                                'read_value(self.paths.config_file, "CLOUDFLARE_TUNNEL_TOKEN"'),
}

# Keys the containers read: the environment name compose gives the value,
# the compose services that must carry it, and the image file that reads it.
CONTAINER_READERS = {
    "ONIONHEAVEN_PROPAGATION_DELAY": (
        "TOR_PROPAGATION_DELAY", ("tor", "onionheaven"),
        ("app/Resources/docker/tor/onionheaven_common.py", "PROPAGATION_DELAY")),
    "ONIONHEAVEN_MAX_SERVICES": (
        "ONIONHEAVEN_MAX_SERVICES", ("onionheaven", "onionheaven-farm"),
        ("app/Resources/docker/tor/onionheaven_common.py", "MAX_SERVICES_PER_WORKER")),
}


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _service(compose, name):
    start = compose.index(f"\n  {name}:\n")
    m = re.compile(r"\n  [a-z][a-z0-9_-]*:\n").search(compose, start + 1)
    return compose[start:m.start() if m else len(compose)]


def _compose_env(compose, service):
    """{NAME: (config key, default)} for a service's `- NAME=${KEY:-default}`
    lines."""
    env = {}
    for m in re.finditer(r"^\s+- ([A-Z_]+)=\$\{([A-Z_]+):-([^}]*)\}\s*$",
                         _service(compose, service), re.M):
        env[m.group(1)] = (m.group(2), m.group(3))
    return env


class TestEveryKeyHasAReader(unittest.TestCase):
    def test_census_covers_the_page_keys_exactly(self):
        rows = set(HOST_READERS) | set(CONTAINER_READERS)
        self.assertEqual(rows, set(op_config.SETTINGS_PAGE_KEYS),
                         "a Settings-page key without a reader row (a setting "
                         "nothing acts on), or a row for a key the page no "
                         "longer has")
        self.assertTrue(set(HOST_READERS).isdisjoint(CONTAINER_READERS))

    def test_host_readers_read_their_key(self):
        for key, (rel, expression) in HOST_READERS.items():
            with self.subTest(key=key):
                self.assertNotIn(rel, ("src/onionpress/config.py", PAGE))
                self.assertIn(expression, _read(rel))

    def test_the_removed_threshold_is_gone_everywhere(self):
        shipped = [COMPOSE, PAGE, "src/onionpress/config.py", *LAUNCHERS,
                   "app/Resources/config-template.txt"]
        for rel in shipped:
            with self.subTest(file=rel):
                self.assertNotIn("ONIONHEAVEN_HUB_THRESHOLD", _read(rel))
        self.assertNotIn("ONIONHEAVEN_TAKEOVER_SCALE_THRESHOLD", _read(COMPOSE))


class TestContainerKeysReachTheImage(unittest.TestCase):
    def setUp(self):
        self.compose = _read(COMPOSE)

    def test_compose_maps_the_key_to_every_service_that_reads_it(self):
        for key, (env_name, services, _) in CONTAINER_READERS.items():
            for service in services:
                with self.subTest(key=key, service=service):
                    env = _compose_env(self.compose, service)
                    self.assertIn(env_name, env, f"{service} does not get {env_name}")
                    self.assertEqual(env[env_name][0], key)

    def test_the_image_reads_the_name_compose_gives(self):
        for key, (env_name, _, (rel, constant)) in CONTAINER_READERS.items():
            with self.subTest(key=key):
                m = re.search(
                    rf'^{constant} = int\(os\.environ\.get\("([A-Z_]+)", "(\d+)"\)\)$',
                    _read(rel), re.M)
                self.assertIsNotNone(m, f"{rel} does not read {constant} from the environment")
                self.assertEqual(m.group(1), env_name)

    def test_one_default_in_config_compose_and_the_image(self):
        for key, (env_name, services, (rel, constant)) in CONTAINER_READERS.items():
            with self.subTest(key=key):
                default = op_config.default_value(key)
                self.assertTrue(default, f"{key} has no default")
                self.assertTrue(op_config.SETTINGS_PAGE_KEYS[key](default))
                for service in services:
                    self.assertEqual(_compose_env(self.compose, service)[env_name][1],
                                     default, f"{service}'s default for {env_name}")
                m = re.search(rf'^{constant} = int\(os\.environ\.get\("{env_name}", "(\d+)"\)\)$',
                              _read(rel), re.M)
                self.assertEqual(m.group(1), default, f"{rel}'s default for {env_name}")
                page = re.search(rf"'{key}' => array\((.*?)\n        \),", _read(PAGE), re.S)
                self.assertIsNotNone(page, f"the page has no {key} field")
                self.assertIn(f"'placeholder' => '{default}'", page.group(1))

    def test_the_farm_controller_agrees_with_the_heartbeat_monitor(self):
        # onionheaven-farm.py tells each worker its cap from the same
        # variable; compose gives both services the same value.
        farm = _read("app/Resources/docker/tor/onionheaven-farm.py")
        self.assertIn('MAX_SERVICES_PER_WORKER = int(os.environ.get("ONIONHEAVEN_MAX_SERVICES", '
                      f'"{op_config.default_value("ONIONHEAVEN_MAX_SERVICES")}"))', farm)
        self.assertIn('f"MAX_TAKEOVER_SERVICES={MAX_SERVICES_PER_WORKER}"', farm)


class TestEveryComposeCallerExportsTheKeys(unittest.TestCase):
    """Compose reads `${KEY:-default}` from its caller's environment: both
    launchers and ContainerManager's compose environment must carry every
    container key from the config, or a hub's saved setting is the default."""

    def test_launchers_read_and_export_them(self):
        for rel in LAUNCHERS:
            text = _read(rel)
            for key in CONTAINER_READERS:
                with self.subTest(launcher=rel, key=key):
                    self.assertIn(f'grep "^{key}="', text)
                    self.assertTrue(re.search(rf"^export .*\b{key}\b", text, re.M),
                                    f"{rel} does not export {key}")

    def test_python_compose_env_carries_them(self):
        from onionpress import containers

        tmp = tempfile.mkdtemp(prefix="op-keys-read-")
        self.addCleanup(shutil.rmtree, tmp, True)
        config = os.path.join(tmp, "config")
        paths = types.SimpleNamespace(config_file=config, documents_dir=tmp)
        ports = types.SimpleNamespace(wp_port=8080, socks_port=9050, proxy_port=9077, offset=0)
        mgr = containers.ContainerManager(docker=None, paths=paths, port_config=ports)
        with mock.patch.object(mgr, "_secrets_env", return_value={}):
            env = mgr._build_env()
            for key in CONTAINER_READERS:
                self.assertEqual(env[key], op_config.default_value(key), key)
            with open(config, "w") as f:
                f.write("ONIONHEAVEN_MAX_SERVICES=20\nONIONHEAVEN_PROPAGATION_DELAY=600\n")
            env = mgr._build_env()
            self.assertEqual(env["ONIONHEAVEN_MAX_SERVICES"], "20")
            self.assertEqual(env["ONIONHEAVEN_PROPAGATION_DELAY"], "600")
            # A hand-edited value the config check refuses would crash the
            # containers' int(): the default goes instead.
            with open(config, "w") as f:
                f.write("ONIONHEAVEN_MAX_SERVICES=lots\nONIONHEAVEN_PROPAGATION_DELAY=30\n")
            env = mgr._build_env()
            self.assertEqual(env["ONIONHEAVEN_MAX_SERVICES"], "50")
            self.assertEqual(env["ONIONHEAVEN_PROPAGATION_DELAY"], "180")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestMacLauncherExportBlock(unittest.TestCase):
    """The Mac launcher's read of the two keys, run under bash: the config's
    whole numbers, else the defaults."""

    def _block(self):
        text = _read("app/MacOS/onionpress")
        start = text.index('ONIONHEAVEN_MAX_SERVICES="${ONIONHEAVEN_MAX_SERVICES:-50}"')
        end = text.index("export ONIONHEAVEN_MAX_SERVICES ONIONHEAVEN_PROPAGATION_DELAY", start)
        return text[start:end] + "export ONIONHEAVEN_MAX_SERVICES ONIONHEAVEN_PROPAGATION_DELAY\n"

    def _run(self, config_text):
        with tempfile.TemporaryDirectory(prefix="op-mac-export-") as tmp:
            if config_text is not None:
                with open(os.path.join(tmp, "config"), "w") as f:
                    f.write(config_text)
            # Under the launcher's own `set -e`: a refused value must not end it.
            script = ("set -e\n" + self._block()
                      + 'printf "%s %s\\n" "$ONIONHEAVEN_MAX_SERVICES" "$ONIONHEAVEN_PROPAGATION_DELAY"\n')
            r = subprocess.run(["bash", "-c", script], env={"DATA_DIR": tmp, "PATH": os.environ["PATH"]},
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_values_defaults_and_garbage(self):
        self.assertEqual(self._run(None), "50 180")
        self.assertEqual(self._run("ONIONHEAVEN_MAX_SERVICES=20\nONIONHEAVEN_PROPAGATION_DELAY=600\n"),
                         "20 600")
        self.assertEqual(self._run("ONIONHEAVEN_MAX_SERVICES=lots\nONIONHEAVEN_PROPAGATION_DELAY=\n"),
                         "50 180")


if __name__ == "__main__":
    unittest.main()
