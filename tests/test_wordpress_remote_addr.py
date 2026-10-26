"""No client chooses REMOTE_ADDR in the WordPress container.

The auto-login plugin's local scope and /__op_proxy.php (src/onion-forward.php)
admit a request by its source address. That means something only if
REMOTE_ADDR is the TCP peer: the tor container for onion visitors (Tor ->
socat -> wordpress:80), cloudflared for Cloudflare Tunnel visitors, the
gateway of WordPress's network (onionpress-wordpress-network) for the
publisher's browser through the published port.

  - No PROXY-protocol listener. The image enabled onionpress-proxy-protocol.conf,
    a *:81 vhost with RemoteIPProxyProtocol, for C Tor's
    HiddenServiceExportCircuitID. Nothing has connected to :81 since onion
    traffic moved to socat -> wordpress:80, but any container on
    onionpress-network could, and its PROXY header named any source address.
  - No X-Forwarded-For. The official wordpress image loads mod_remoteip with
    `RemoteIPHeader X-Forwarded-For` and every private range as an internal
    proxy (its remoteip.conf). Every peer WordPress has is in one, so any
    onion visitor could send `X-Forwarded-For: 127.0.0.1` and be loopback.
    The image turns the conf and the module off, and its build fails if the
    module is loaded.

These reach installs with the next WordPress image publish.
"""

import json
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WP_DIR = os.path.join(ROOT, "app", "Resources", "docker", "wordpress")
DOCKERFILE = os.path.join(WP_DIR, "Dockerfile")
TOR_ENTRYPOINT = os.path.join(ROOT, "app", "Resources", "docker", "tor", "entrypoint.sh")
COMPOSE = os.path.join(ROOT, "app", "Resources", "docker", "docker-compose.yml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _apache_confs():
    return sorted(os.path.join(WP_DIR, n) for n in os.listdir(WP_DIR) if n.endswith(".conf"))


def _code(conf):
    return "\n".join(line for line in conf.splitlines()
                     if not line.lstrip().startswith("#"))


class TestNoProxyProtocolListener(unittest.TestCase):

    def test_the_vhost_is_gone_from_the_image(self):
        self.assertFalse(os.path.exists(os.path.join(WP_DIR, "onionpress-proxy-protocol.conf")))
        dockerfile = _read(DOCKERFILE)
        self.assertNotIn("proxy-protocol", dockerfile)
        enconf = re.search(r"^RUN a2enconf (.+)$", dockerfile, re.M)
        self.assertIsNotNone(enconf)
        self.assertNotIn("onionpress-proxy-protocol", enconf.group(1).split())

    def test_no_onionpress_conf_opens_another_port_or_trusts_a_header(self):
        for path in _apache_confs():
            code = _code(_read(path))
            with self.subTest(conf=os.path.basename(path)):
                self.assertNotRegex(code, r"(?im)^\s*Listen\b")
                self.assertNotRegex(code, r"(?i)\bRemoteIP")
                for port in re.findall(r"(?i)<VirtualHost\s+[^>]*:(\d+)", code):
                    self.assertEqual(port, "80")

    def test_onion_visitors_arrive_on_port_80(self):
        entrypoint = _read(TOR_ENTRYPOINT)
        self.assertRegex(entrypoint,
                         r"(?m)^socat TCP-LISTEN:8080,reuseaddr,fork TCP:wordpress:80 &$")
        services = re.search(r"cat > /etc/tor/onion-services\.json << SERVICES_EOF\n"
                             r"(.*?)\nSERVICES_EOF", entrypoint, re.S)
        self.assertIsNotNone(services)
        # The heredoc expands ${HUB_API_PORT} (the hub API's port on a hub
        # or OnionHome only, issue #393); a plain node's is the text as is.
        wordpress = [s for s in json.loads(services.group(1).replace("${HUB_API_PORT}", ""))
                     if s["name"] == "wordpress"]
        self.assertEqual(len(wordpress), 1)
        self.assertIn("80,127.0.0.1:8080", wordpress[0]["ports"])

    def test_nothing_connects_to_another_wordpress_port(self):
        """No launcher, container script or compose file targets WordPress
        on anything but :80 (the published port maps to :80 too)."""
        port = re.compile(r"(?<![\w/.-])wordpress:(?!80\b)\d+(?![\w.-])")
        seen = 0
        for top in ("app", "linux", "src", "build", "docs"):
            for dirpath, dirs, names in os.walk(os.path.join(ROOT, top)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in names:
                    path = os.path.join(dirpath, name)
                    try:
                        text = _read(path)
                    except (UnicodeDecodeError, OSError):
                        continue
                    seen += 1
                    with self.subTest(file=os.path.relpath(path, ROOT)):
                        self.assertIsNone(port.search(text))
        self.assertGreater(seen, 50)
        self.assertRegex(_read(COMPOSE), r'(?m)^\s*- "\$\{ONIONPRESS_BIND_HOST:-127\.0\.0\.1\}:'
                                         r'\$\{ONIONPRESS_WP_PORT:-8080\}:80"$')


class TestNoForwardedFor(unittest.TestCase):

    DOCKERFILE = _read(DOCKERFILE)

    def _runs(self):
        """Each RUN instruction, continuation lines joined."""
        joined = re.sub(r"\\\n\s*", " ", self.DOCKERFILE)
        return [m.group(1) for m in re.finditer(r"(?m)^RUN (.+)$", joined)]

    def test_the_base_images_remoteip_is_turned_off(self):
        off = [r for r in self._runs() if "a2disconf remoteip" in r]
        self.assertEqual(len(off), 1)
        steps = [s.strip() for s in off[0].split("&&")]
        # The conf before the module: its directives need the module.
        self.assertLess(steps.index("a2disconf remoteip"), steps.index("a2dismod remoteip"))
        configtest = self.DOCKERFILE.index("RUN apache2ctl configtest")
        self.assertLess(self.DOCKERFILE.index("a2dismod remoteip"), configtest)

    def test_nothing_turns_it_back_on(self):
        for run in self._runs():
            for cmd in re.findall(r"\ba2en(?:mod|conf)\b([^&;|]*)", run):
                with self.subTest(run=run):
                    self.assertNotIn("remoteip", cmd.split())

    def test_the_build_fails_if_the_module_is_loaded(self):
        check = [r for r in self._runs() if r.startswith("apache2ctl configtest")]
        self.assertEqual(len(check), 1)
        self.assertIn("&& ! apache2ctl -M 2>/dev/null | grep -q ' remoteip_module '", check[0])
        # The php_module check comes first: it proves `apache2ctl -M` listed
        # modules at all, so an empty listing cannot pass the negated grep.
        self.assertLess(check[0].index("grep -q ' php_module '"),
                        check[0].index("! apache2ctl -M"))


if __name__ == "__main__":
    unittest.main()
