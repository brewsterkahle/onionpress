"""Tests for src/onionpress/launcher_ops.py."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.launcher_ops import get_running_wp_port


class TestGetRunningWpPort(unittest.TestCase):
    """Reads `docker port <container> 80` and returns the published host
    port, or None when there isn't one to trust."""

    def _run(self, stdout, returncode=0):
        return mock.patch(
            "onionpress.launcher_ops.subprocess.run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=""),
        )

    def test_parses_the_published_port(self):
        with self._run("0.0.0.0:18080\n"):
            self.assertEqual(get_running_wp_port(), 18080)

    def test_default_offset_port(self):
        with self._run("0.0.0.0:8080\n"):
            self.assertEqual(get_running_wp_port(), 8080)

    def test_a_port_below_our_allocation_range_is_not_ours(self):
        """Offsets are 8080 + k*10000, so nothing we allocate is ever below
        8080 — a published port under it belongs to some other mapping
        entirely and must not be read back as "our" WordPress port."""
        with self._run("0.0.0.0:80\n"):
            self.assertIsNone(get_running_wp_port())

    def test_container_not_running(self):
        with self._run("", returncode=1):
            self.assertIsNone(get_running_wp_port())


if __name__ == "__main__":
    unittest.main()
