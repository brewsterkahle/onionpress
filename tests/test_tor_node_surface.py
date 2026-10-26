"""Issue #393: a plain node's onion address carries no hub API, and the
two socat servers are bounded.

Every node's onion service mapped port 8083 to web-server.py (the hub's
HTTP API: stdlib HTTP, JSON, SQLite, PEM parsing), running as root in
the container that holds the site's key and Tor's control cookie,
whether or not the node was a hub. The healthcheck and redirect services
forked a root sh per connection with no idle timeout and no cap, and the
healthcheck GET made three requests to WordPress per anonymous hit.

Now: 8083 goes on the onion address only when onionheaven_common's
hub_api_exposed says the node is a hub (the opt-in or the canonical
address) or OnionHome; both listeners run with `-T 30` and
`max-children=64` (the Dockerfile checks socat knows the option); and the
healthcheck keeps its WordPress facts for CACHE_TTL seconds.
Running web-server.py as a dedicated uid is still open in the issue.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOR_DIR = os.path.join(PROJECT_ROOT, "app/Resources/docker/tor")
sys.path.insert(0, TOR_DIR)
import onionheaven_common as oc  # noqa: E402

HUB = oc.ONIONHEAVEN_HUB_ADDRESS
HOME = oc.ONIONHOME_ADDRESS
OTHER = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz2345.onion"


def _read(name):
    with open(os.path.join(TOR_DIR, name), encoding="utf-8") as f:
        return f.read()


class TestHubApiExposed(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-node-surface-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hostname = os.path.join(self.tmp, "hostname")
        p = mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": ""})
        p.start()
        self.addCleanup(p.stop)

    def _write(self, text):
        with open(self.hostname, "w") as f:
            f.write(text)

    def test_only_the_hub_and_onionhome_expose_it(self):
        for addr, exposed in ((HUB, True), (HOME, True), (OTHER, False)):
            with self.subTest(address=addr[:8]):
                self._write(addr + "\n")
                self.assertIs(oc.hub_api_exposed(path=self.hostname), exposed)
                self.assertIs(oc.hub_api_exposed(addr), exposed)

    def test_no_address_or_garbage_is_no_hub(self):
        self.assertFalse(oc.hub_api_exposed(path=self.hostname))      # no file
        self._write("not an address\n")
        self.assertFalse(oc.hub_api_exposed(path=self.hostname))
        self.assertIsNone(oc.read_own_address(self.hostname))

    def test_the_opt_in_exposes_it_on_any_address(self):
        self._write(OTHER + "\n")
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "yes"}):
            self.assertTrue(oc.hub_api_exposed(path=self.hostname))
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "Yes "}):
            self.assertTrue(oc.hub_api_exposed(path=self.hostname))
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_ACCEPT_REGISTRATIONS": "true"}):
            self.assertFalse(oc.hub_api_exposed(path=self.hostname))

    def test_web_server_takes_the_address_from_the_shared_module(self):
        src = _read("web-server.py")
        self.assertIn("_ONIONHOME_ADDRESS = ONIONHOME_ADDRESS", src)
        self.assertNotIn('_ONIONHOME_ADDRESS = "op2home', src)


class TestEntrypoint(unittest.TestCase):

    def setUp(self):
        self.src = _read("entrypoint.sh")
        start = self.src.index("HUB_API_PORT=\"\"")
        end = self.src.index("\nSERVICES_EOF\n", start)
        self.block = self.src[start:end + len("\nSERVICES_EOF\n")]

    def test_8083_is_conditional_on_hub_api_exposed(self):
        self.assertIn("from onionheaven_common import hub_api_exposed", self.block)
        self.assertIn('HUB_API_PORT=\', "8083,127.0.0.1:8083"\'', self.block)
        self.assertIn('{"name": "wordpress", "ports": ["80,127.0.0.1:8080"${HUB_API_PORT}]}', self.block)
        self.assertEqual(self.src.count("8083,127.0.0.1:8083"), 1)
        self.assertIn('{"name": "healthcheck", "ports": ["80,127.0.0.1:8081"]}', self.block)

    def test_the_heredoc_expands_and_writes_valid_json_both_ways(self):
        heredoc = self.block[self.block.index("cat > /etc/tor/onion-services.json"):]
        for port, want in (("", ["80,127.0.0.1:8080"]),
                           (', "8083,127.0.0.1:8083"', ["80,127.0.0.1:8080", "8083,127.0.0.1:8083"])):
            with self.subTest(port=port or "(none)"):
                with tempfile.TemporaryDirectory() as tmp:
                    script = heredoc.replace("/etc/tor/onion-services.json",
                                             os.path.join(tmp, "s.json"))
                    r = subprocess.run(["sh", "-c", f"HUB_API_PORT='{port}'\n" + script],
                                       capture_output=True, text=True, timeout=30)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    import json
                    with open(os.path.join(tmp, "s.json")) as f:
                        services = json.load(f)
                self.assertEqual(services[0], {"name": "wordpress", "ports": want})

    def test_web_server_still_listens_inside_the_network(self):
        # The directory plugin and name sync on OnionHome call
        # onionpress-tor:8083 directly; the listener itself is unchanged.
        self.assertIn("python3 /web-server.py &", self.src)


class TestSocatServers(unittest.TestCase):

    def test_both_listeners_time_out_idle_connections_and_cap_children(self):
        for name, port in (("healthcheck-server.sh", "HEALTHCHECK_PORT"),
                           ("onionheaven-redirect.sh", "REDIRECT_PORT")):
            with self.subTest(script=name):
                src = _read(name)
                self.assertRegex(src, r"(?m)^exec socat -T 30 TCP-LISTEN:\$\{" + port
                                 + r"\},reuseaddr,fork,max-children=64 SYSTEM:")

    def test_the_image_checks_socat_knows_max_children(self):
        self.assertIn("RUN socat -hhh 2>&1 | grep -q 'max-children'", _read("Dockerfile"))

    def test_the_wordpress_forward_is_untouched(self):
        self.assertIn("socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &", _read("entrypoint.sh"))


class TestHealthcheckCache(unittest.TestCase):
    """handle_get under sh with a fake wget: the three WordPress probes
    run once per CACHE_TTL, whatever the number of hits."""

    FAKE_WGET = """#!/bin/sh
echo "wget $*" >> "$WGET_LOG"
case "$*" in
  *wp/v2/posts*) printf '[{"date":"2026-10-25T10:00:00"}]' ;;
  *wp/v2/sites*) printf '[{"id":1},{"id":2}]' ;;
esac
exit 0
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="op-hc-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache_dir = os.path.join(self.tmp, "cache")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.tmp, "msgs"))
        with open(os.path.join(self.bin, "wget"), "w") as f:
            f.write(self.FAKE_WGET)
        os.chmod(os.path.join(self.bin, "wget"), stat.S_IRWXU)
        self.log = os.path.join(self.tmp, "wget.log")
        self.script = os.path.join(self.tmp, "svc.sh")
        with open(self.script, "w") as f:
            f.write(_read("healthcheck-server.sh")
                    .replace('CACHE_DIR="/run/healthcheck"', f'CACHE_DIR="{self.cache_dir}"')
                    .replace("/var/lib/tor/healthcheck-messages", os.path.join(self.tmp, "msgs")))

    def _get(self):
        r = subprocess.run(["sh", self.script, "--handle-request"], input="GET / HTTP/1.0\r\n\r\n",
                           capture_output=True, text=True, timeout=30,
                           env={"PATH": self.bin + ":/usr/bin:/bin", "WGET_LOG": self.log})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("HTTP/1.0 200 OK"), r.stdout)
        return r.stdout

    def _wget_calls(self):
        try:
            with open(self.log) as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def test_three_probes_once_per_ttl(self):
        out = self._get()
        self.assertEqual(self._wget_calls(), 3)
        self.assertIn('"wordpress":true', out)
        self.assertIn('"last_post":"2026-10-25T10:00:00"', out)
        self.assertIn('"sites":2', out)
        for _ in range(5):
            self.assertIn('"sites":2', self._get())
        self.assertEqual(self._wget_calls(), 3, "the cache answered the next hits")
        with open(os.path.join(self.cache_dir, "wordpress-facts")) as f:
            self.assertEqual(f.read(), "true\nok\n\"2026-10-25T10:00:00\"\n2\n")

    def test_a_stale_or_damaged_cache_is_probed_again(self):
        self._get()
        cache = os.path.join(self.cache_dir, "wordpress-facts")
        old = os.path.getmtime(cache) - 120
        os.utime(cache, (old, old))
        self._get()
        self.assertEqual(self._wget_calls(), 6)
        with open(cache, "w") as f:
            f.write("junk\n")
        self._get()
        self.assertEqual(self._wget_calls(), 9)
        self.assertIn('"sites":2', self._get())
        self.assertEqual(self._wget_calls(), 9)

    def test_an_unwritable_cache_dir_still_answers(self):
        with open(self.script) as f:
            text = f.read()
        with open(self.script, "w") as f:
            f.write(text.replace(f'CACHE_DIR="{self.cache_dir}"', 'CACHE_DIR="/proc/no-such-dir"'))
        self.assertIn('"sites":2', self._get())
        self.assertIn('"sites":2', self._get())
        self.assertEqual(self._wget_calls(), 6)


if __name__ == "__main__":
    unittest.main()
