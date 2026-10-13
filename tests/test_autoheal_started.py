"""autoheal runs on every platform (issue #324).

docker-compose.yml defines autoheal (and autoheal-proxy, its only route to
Docker). Through 2.5.3 only the Mac launcher started it: the Linux launcher
brought up wordpress, db, onionheaven and tor, so a container marked
unhealthy on Linux was never restarted. Every start path must name it.
"""

import os
import re
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")
COMPOSE = "app/Resources/docker/docker-compose.yml"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestAutohealStarted(unittest.TestCase):

    def test_compose_defines_autoheal_and_its_proxy(self):
        compose = _read(COMPOSE)
        self.assertRegex(compose, r"(?m)^  autoheal:\n")
        self.assertRegex(compose, r"(?m)^  autoheal-proxy:\n")
        # `up -d autoheal` brings the proxy along only through depends_on.
        block = re.search(r"(?ms)^  autoheal:\n(.*?)(?=^  \S)", compose).group(1)
        self.assertRegex(block, r"(?m)^    depends_on:\n      - autoheal-proxy$")

    def test_each_launcher_starts_autoheal_with_wordpress(self):
        for rel in LAUNCHERS:
            src = _read(rel)
            # Commands only, not the log messages that quote them.
            ups = re.findall(r"(?m)^\s*(?:if\s+)?(docker compose up -d wordpress db [^\n>|']*)", src)
            self.assertTrue(ups, f"{rel}: no `docker compose up -d wordpress db ...`")
            for cmd in ups:
                self.assertIn("autoheal", cmd.split(), f"{rel}: {cmd!r}")

    def test_python_container_manager_starts_autoheal(self):
        from onionpress import containers
        self.assertIn("autoheal", containers.CORE_SERVICES)


if __name__ == "__main__":
    unittest.main()
