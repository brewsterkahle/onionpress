"""What every node can do, whatever it runs on.

A subclass provides five primitives: `exec` (a command in the node's own
context: the Mac host with the node's environment, or the Linux VM as the
install's user), `docker` (a docker command against the node's own
daemon), `launcher` (the installed `onionpress` command), `read_file` and
`fetch_files` (for evidence). Everything a platform-neutral test uses is
built here from those, so a test is written once against Node.
"""

import json
import os
import re
from dataclasses import dataclass, field

from ..proc import Timings, wait_until

ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")

# The containers every node runs (docker-compose.yml container_name).
CORE_CONTAINERS = ("onionpress-tor", "onionpress-wordpress", "onionpress-db")

# The tor container's copy of the served key (CLAUDE.md, issue #311).
SERVED_KEY = "/var/lib/tor/hidden_service/wordpress/hs_ed25519_secret_key"
SERVED_HOSTNAME = "/var/lib/tor/hidden_service/wordpress/hostname"


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: str
    raw_headers: list = field(default_factory=list)

    def header(self, name):
        return self.headers.get(name.lower(), "")

    def cookies(self):
        return [v for k, v in self.raw_headers if k == "set-cookie"]


def parse_curl_include(text):
    """Parse `curl -i` output (possibly several header blocks, e.g. a
    100 Continue or a proxy's 200 before the real response)."""
    status, headers, raw, rest = 0, {}, [], text
    while True:
        m = re.match(r"HTTP/[\d.]+ (\d{3})[^\r\n]*\r?\n", rest)
        if not m:
            break
        status = int(m.group(1))
        rest = rest[m.end():]
        headers, raw = {}, []
        while True:
            line_end = rest.find("\n")
            if line_end < 0:
                line, rest = rest, ""
            else:
                line, rest = rest[:line_end], rest[line_end + 1:]
            line = line.rstrip("\r")
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
                raw.append((k.strip().lower(), v.strip()))
        if not rest.startswith("HTTP/"):
            break
    return HttpResponse(status, headers, rest, raw)


class Node:
    platform = ""       # "macos" | "linux"
    kind = ""           # "mac" | "linux-vm" | ...

    def __init__(self, name, log, results_dir):
        self.name = name
        self.log = log
        self.results_dir = results_dir
        self.timings = Timings()
        self.wp_admin_user = ""
        self.wp_admin_password = ""
        self.installed = False
        self.address = ""           # set once the node serves one
        self.first_key_digest = ""  # the served key at the first start
        self.install_findings = []  # what the install needed that a user should not
        self.notes = []             # observations for the report

    # ── primitives (subclasses) ─────────────────────────────────────────
    def exec(self, argv, *, input=None, timeout=120):
        raise NotImplementedError

    def docker(self, argv, *, input=None, timeout=120):
        raise NotImplementedError

    def launcher(self, argv, *, input=None, timeout=600):
        raise NotImplementedError

    def read_file(self, path):
        """Contents of a file in the install's data dir or '' (path relative
        to the install user's home, e.g. '.onionpress/config')."""
        raise NotImplementedError

    def fetch_files(self, rel_paths, dest):
        """Copy files/dirs (relative to the install user's home) into dest."""
        raise NotImplementedError

    def home_path(self, rel):
        """Absolute path, in the node, of a path relative to the install
        user's home."""
        raise NotImplementedError

    def install(self):
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def destroy(self):
        raise NotImplementedError

    def setup_wordpress(self, user, password):
        raise NotImplementedError

    @property
    def wp_port(self):
        raise NotImplementedError

    def facts(self):
        """What the report says this node was (os, arch, docker, install)."""
        return {}

    # ── built from the primitives ───────────────────────────────────────
    def exists(self, rel):
        return self.exec(["test", "-e", self.home_path(rel)], timeout=30).ok

    def config(self):
        values = {}
        for line in self.read_file(".onionpress/config").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
        return values

    def onion_address(self):
        r = self.launcher(["address"], timeout=60)
        lines = [ln.strip() for ln in r.out.splitlines() if ln.strip()]
        addr = lines[-1] if lines else ""
        return addr if r.ok and ONION_RE.match(addr) else ""

    def served_hostname(self):
        r = self.docker(["exec", "onionpress-tor", "cat", SERVED_HOSTNAME])
        return r.out.strip() if r.ok else ""

    def served_key_digest(self):
        r = self.docker(["exec", "onionpress-tor", "sha256sum", SERVED_KEY])
        return r.out.split()[0] if r.ok and r.out.strip() else ""

    def container_states(self):
        r = self.docker(["ps", "-a", "--format", "{{.Names}}\t{{.State}}"])
        states = {}
        for line in r.out.splitlines():
            if "\t" in line:
                name, state = line.split("\t", 1)
                states[name] = state
        return states

    def http_local(self, path, *, method="GET", data=None, headers=(), timeout=30):
        """A request to WordPress's published port, from the node itself."""
        url = f"http://localhost:{self.wp_port}{path}"
        argv = ["curl", "-sS", "-i", "--max-time", str(timeout), "-X", method]
        for h in headers:
            argv += ["-H", h]
        if data is not None:
            argv += ["--data-binary", "@-"]
        argv.append(url)
        r = self.exec(argv, input=data, timeout=timeout + 15)
        if not r.ok:
            raise RuntimeError(r.describe())
        return parse_curl_include(r.out)

    def tor_fetch(self, address, path="/", *, timeout=120):
        """Fetch http://<address><path> over Tor from this node's tor
        container (the visitor; never the Mac host's SOCKS port)."""
        url = f"http://{address}{path}"
        r = self.docker(["exec", "onionpress-tor", "curl", "-sS", "-i", "--max-time", str(timeout),
                         "--socks5-hostname", "127.0.0.1:9050", url], timeout=timeout + 30)
        if not r.ok:
            raise RuntimeError(r.describe())
        return parse_curl_include(r.out)

    def wait_for_address(self, timeout=900):
        return wait_until(self.onion_address, timeout=timeout, every=10,
                          label=f"{self.name}: onion address", timings=self.timings, log=self.log)

    def wait_for_local_site(self, timeout=600):
        def ok():
            resp = self.http_local("/.onionpress-health")
            return resp.status == 200 and "onionpress-health" in resp.body
        return wait_until(ok, timeout=timeout, every=10, label=f"{self.name}: local site",
                          timings=self.timings, log=self.log)

    def wp_login(self, user, password):
        """POST wp-login.php; the response (a logged-in cookie on success)."""
        from urllib.parse import urlencode
        body = urlencode({"log": user, "pwd": password, "wp-submit": "Log In",
                          "redirect_to": "/wp-admin/", "testcookie": "1"})
        return self.http_local("/wp-login.php", method="POST", data=body, headers=(
            "Content-Type: application/x-www-form-urlencoded",
            "Cookie: wordpress_test_cookie=WP%20Cookie%20check"))

    def collect_evidence(self, dest):
        """Logs, config, container logs and inspect output into dest."""
        os.makedirs(dest, exist_ok=True)
        try:
            # docker-rootless-setup.log: what the .deb's postinst saw when it
            # tried rootless Docker (Linux only); without it a docker-group
            # fallback cannot be explained from the report.
            self.fetch_files([".onionpress/config", ".onionpress/logs", ".onionpress/onionpress.log",
                              ".onionpress/launcher.log", ".onionpress/onionheaven-registration.json",
                              ".onionpress/docker-rootless-setup.log"],
                             os.path.join(dest, "data-dir"))
        except Exception as e:  # noqa: BLE001 - evidence is best effort
            self.log.write(f"evidence: data dir copy failed: {e}")
        states = {}
        try:
            states = self.container_states()
        except Exception as e:  # noqa: BLE001
            self.log.write(f"evidence: docker ps failed: {e}")
        cdir = os.path.join(dest, "containers")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "states.json"), "w", encoding="utf-8") as f:
            json.dump(states, f, indent=2)
        for name in states:
            r = self.docker(["logs", "--timestamps", "--tail", "5000", name], timeout=60)
            with open(os.path.join(cdir, f"{name}.log"), "w", encoding="utf-8") as f:
                f.write(r.out + r.err)
            r = self.docker(["inspect", name], timeout=60)
            with open(os.path.join(cdir, f"{name}.inspect.json"), "w", encoding="utf-8") as f:
                f.write(r.out)
