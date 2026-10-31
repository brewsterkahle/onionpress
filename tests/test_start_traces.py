#!/usr/bin/env python3
"""What each launcher's start_containers does, recorded (issue #442.1).

Every scenario runs both launchers' start (tests/start_trace.py: the real
top level, a stateful fake docker, the Python start steps for real) and
compares the trace with its fixture in tests/fixtures/start-traces/. The
moves of #442 (start_containers into `onionpress start-containers`) are
checked against these: a slice changes a fixture only on purpose, with
OP_UPDATE_START_TRACES=1, and its commit says why.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import start_trace  # noqa: E402
from start_trace import ADDRESS, GENERATED, Scenario  # noqa: E402

KEY_FILES = {
    f"shared/vanity-keys/{ADDRESS}/hostname": ADDRESS + "\n",
    f"shared/vanity-keys/{ADDRESS}/ks_hs_id.ed25519_expanded_private": "PEM\n",
    f"shared/vanity-keys/{ADDRESS}/hs_ed25519_secret_key": "secret\n",
    f"shared/vanity-keys/{ADDRESS}/hs_ed25519_public_key": "public\n",
}
FRESH = {"volumes": [], "wp_installed": False, "plugins": {}}
OLD_KEYS = ["onionpress-onion-keys", "onionpress-arti-state", "onionpress-tor-state",
            "onionpress-tor-keys"]

SUPERSEDED_TOR = "ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:" + "e" * 64

SCENARIOS = [
    Scenario("restart"),
    Scenario("docker-never-answers", env={"STUB_RC_WAIT_FOR_DOCKER_OR_RECOVER_VM": "1",
                                          "STUB_RC_WAIT_FOR_DOCKER": "1"}),
    Scenario("first-run-imported-key", docker=FRESH, files=KEY_FILES),
    Scenario("first-run-generated", docker=FRESH, env={"FAKE_VANITY": GENERATED}),
    Scenario("first-run-generation-fails", docker=FRESH),
    Scenario("first-run-bad-prefix", docker=FRESH, config="ADDRESS_PREFIX=Elph1n\n",
             env={"FAKE_VANITY": GENERATED}),
    Scenario("first-run-vanity-prefix", docker=FRESH, config="ADDRESS_PREFIX=\nVANITY_PREFIX=ab\n",
             env={"FAKE_VANITY": GENERATED}),
    Scenario("first-run-seed-fails", files=KEY_FILES, docker=dict(
        FRESH, rules=[{"match": r"^run .*onionpress-onion-keys:/dest", "rc": 1}])),
    Scenario("import-key-pending-removed", files={".import-key-pending": ""},
             docker={"volumes": OLD_KEYS}),
    Scenario("import-key-pending-in-use", files={".import-key-pending": ""},
             docker={"volumes": OLD_KEYS, "in_use": {"onionpress-tor-state": ["c1"]}}),
    Scenario("import-key-pending-stuck", files={".import-key-pending": ""},
             docker={"volumes": OLD_KEYS, "in_use": {"onionpress-tor-state": ["c1"]},
                     "stuck": ["onionpress-tor-state"]}),
    Scenario("import-key-pending-unanswered", files={".import-key-pending": ""},
             docker={"volumes": OLD_KEYS, "volume_ls_fail": 1}),
    Scenario("volume-ls-unanswered", docker={"volume_ls_fail": -1}),
    Scenario("key-migration-fails", docker={
        "volumes": ["onionpress-arti-state"],
        "rules": [{"match": "migrate-key-volume", "rc": 1}]}),
    Scenario("core-up-fails-twice", docker={"rules": [
        {"match": r"up -d wordpress db onionheaven autoheal", "rc": 1, "times": 2}]}),
    Scenario("core-up-fails", docker={"rules": [
        {"match": r"up -d wordpress db onionheaven autoheal", "rc": 1}]}),
    Scenario("tor-up-fails", docker={"rules": [{"match": r"up -d tor onionheaven", "rc": 1}]}),
    Scenario("wordpress-slow", env={"STUB_RC_CURL": "7"}),
    Scenario("wordpress-not-installed", docker={"wp_installed": False, "plugins": {}}),
    Scenario("steps-fail", docker={"htaccess": False},
             env={"STUB_RC_PY_PROVISION_POST_INSTALL": "1",
                  "STUB_RC_PY_DEACTIVATE_WP_STATISTICS": "1",
                  "STUB_RC_PY_CONFIGURE_CACHE_ENABLER": "1",
                  "OP_EGRESS_FAIL": "1"}),
    Scenario("plugins-missing", docker={"plugins": {}}),
    Scenario("plugins-off", config="INSTALL_IA_PLUGIN=no\nINSTALL_CACHE_ENABLER=no\n"),
    Scenario("derivatives-pending", files={".regenerate-derivatives-pending": ""}),
    Scenario("install-from-backup", platforms=("mac",),
             files={"staging/": None, ".install-from-backup": "{tmp}/home/.onionpress/staging\n"}),
    Scenario("install-from-backup-fails", platforms=("mac",),
             files={"staging/": None, ".install-from-backup": "{tmp}/home/.onionpress/staging\n"},
             env={"STUB_RC_PY_IMPORT_BACKUP_ARTIFACTS": "1"}),
    Scenario("cloudflare-token", config="CLOUDFLARE_TUNNEL_TOKEN=tok\n"),
    Scenario("cloudflare-token-not-installed", config="CLOUDFLARE_TUNNEL_TOKEN=tok\n",
             docker={"wp_installed": False, "plugins": {}}),
    Scenario("cloudflare-token-removed", docker={"cloudflared": True}),
    Scenario("local-images", config="ONIONPRESS_TOR_IMAGE=onionpress-tor:local\n"
                                     "ONIONPRESS_WORDPRESS_IMAGE=onionpress-wordpress:local\n"),
    # A release moved the tor pin and the mariadb image is not here yet:
    # the old tor images go (one still in use stays), mariadb is left.
    Scenario("images-superseded", docker={"rules": [
        {"match": r"config --images$",
         "stdout": f"{SUPERSEDED_TOR}\nghcr.io/brewsterkahle/onionpress-mariadb:13.0.2"
                   f"@sha256:{'d' * 64}\n"},
        {"match": r"^image inspect --format \{\{\.Id\}\} ghcr\.io/brewsterkahle/onionpress-mariadb",
         "rc": 1},
        {"match": r"^image ls .* ghcr\.io/brewsterkahle/onionpress-tor$",
         "stdout": f"sha256:{'1' * 64} <none>\nsha256:{'2' * 64} <none>\nsha256:{'3' * 64} dev\n"},
        {"match": rf"^image rm sha256:{'2' * 64}$", "rc": 1}]}),
    Scenario("port-moves-after-setup", docker={"wp_port_host": "127.0.0.1"},
             files={".wordpress-installed": ""}, config="ONIONPRESS_BIND_HOST=0.0.0.0\n",
             platforms=("linux",)),
    Scenario("port-held-before-setup", docker={"wp_installed": False, "plugins": {},
                                               "wp_port_host": "0.0.0.0"},
             files={".wordpress-installed": ""}, config="ONIONPRESS_BIND_HOST=0.0.0.0\n",
             platforms=("linux",)),
]


class TestStartTraces(unittest.TestCase):
    """Each scenario on each launcher, against its fixture."""


def _make(platform, scenario):
    def test(self):
        start_trace.check(self, platform, scenario)
    test.__doc__ = f"{platform}: {scenario.name}"
    return test


for _scenario in SCENARIOS:
    for _platform in _scenario.platforms:
        setattr(TestStartTraces,
                f"test_{_platform}_{_scenario.name.replace('-', '_')}",
                _make(_platform, _scenario))


class TestFixturesAreUsed(unittest.TestCase):
    def test_every_fixture_has_a_scenario(self):
        wanted = {(p, s.name) for s in SCENARIOS for p in s.platforms}
        found = set()
        for platform in os.listdir(start_trace.FIXTURES):
            for name in os.listdir(os.path.join(start_trace.FIXTURES, platform)):
                found.add((platform, name[:-len(".trace")]))
        self.assertEqual(found - wanted, set(), "fixtures no scenario records")
        self.assertEqual(wanted - found, set(), "scenarios without a fixture")


if __name__ == "__main__":
    unittest.main()
