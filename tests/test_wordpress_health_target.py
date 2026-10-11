"""The static reachability target in the WordPress image matches the probe.

Issue #266: the app's Tor-network probe fetches PROBE_PATH, which Apache
serves from a file baked into the image with no PHP involved, and checks
the body for PROBE_BODY_MARKER. The path and the marker live in
src/onionpress/health.py; the Apache Alias and the file live in
app/Resources/docker/wordpress/. If either side moves without the other,
every install probes "/" again (after a 404) and WordPress latency goes
back to masquerading as a Tor failure — silently, since the fallback is
designed to keep working. These checks make that drift a test failure.
"""

import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from onionpress.health import PROBE_PATH, PROBE_BODY_MARKER  # noqa: E402

WP_DIR = os.path.join(ROOT, "app", "Resources", "docker", "wordpress")
CONF = os.path.join(WP_DIR, "onionpress-health.conf")
BODY = os.path.join(WP_DIR, "onionpress-health.txt")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")


class TestWordPressHealthTarget(unittest.TestCase):
    def test_alias_serves_probe_path_from_the_image(self):
        conf = open(CONF, encoding="utf-8").read()
        m = re.search(r"^Alias\s+(\S+)\s+(\S+)\s*$", conf, re.M)
        self.assertIsNotNone(m, "no Alias line in onionpress-health.conf")
        self.assertEqual(m.group(1), PROBE_PATH)
        target = m.group(2)
        dockerfile = open(DOCKERFILE, encoding="utf-8").read()
        self.assertIn(f"COPY onionpress-health.txt {target}", dockerfile)
        # Apache denies everything outside DocumentRoot unless told otherwise.
        self.assertIn(f"<Directory {os.path.dirname(target)}>", conf)
        self.assertIn("Require all granted", conf)

    def test_body_carries_the_marker(self):
        self.assertIn(PROBE_BODY_MARKER, open(BODY, encoding="utf-8").read())

    def test_conf_is_enabled_in_the_image(self):
        dockerfile = open(DOCKERFILE, encoding="utf-8").read()
        self.assertIn("COPY onionpress-health.conf /etc/apache2/conf-available/", dockerfile)
        enconf = re.search(r"^RUN a2enconf (.+)$", dockerfile, re.M)
        self.assertIsNotNone(enconf)
        self.assertIn("onionpress-health", enconf.group(1).split())


if __name__ == "__main__":
    unittest.main()
