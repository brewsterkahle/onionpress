#!/usr/bin/env python3
"""Every container drops every capability, adds back only what it needs, and
runs with no-new-privileges.

Each compose service and each `docker run` in the shipped code (the takeover
workers and the launchers' one-shot helpers) starts from `cap_drop: ALL`
and adds exactly the capabilities in this file's allowlist for it. The
sets were found by starting each one with no capability and adding one at a
time until it did its real work (docker-compose.yml's header and the
comment at each service and site say which step needs which); a change to
a set needs the same live test, then a change here.

Some capabilities are never added: NET_RAW (auto-login's "local" scope and
/__op_proxy.php admit loopback and the gateway of WordPress's network by
source address, and NET_RAW would let root in a container on that network
ARP-spoof the gateway or forge its packets), NET_ADMIN, SYS_ADMIN,
SYS_PTRACE, SYS_MODULE, DAC_READ_SEARCH (open_by_handle_at, the "shocker"
escape: root reads other users' files here with DAC_OVERRIDE) and ALL; and
nothing runs privileged but the #144 egress helper (PRIVILEGED_SITES), held
to its exact shape. no-new-privileges keeps a compromised
unprivileged process (debian-tor, www-data, mysql) from getting back to
root through a setuid binary; every privilege drop in these images is root
calling setuid() itself (su run as root, gosu, Apache), which it allows.

The census reads every shipped shell script and Python file for `docker
run`, so a new one without the flags, or with capabilities this file does
not list for it, fails here. The same census holds every one-shot helper
(the ones that handle the onion service key above all) to the stack's own
tor image, pinned by digest, with its entrypoint overridden and no network:
only the takeover workers, which serve onion services, join one.

autoheal, autoheal-proxy, onionheaven-farm (the OnionHeaven farm
controller, which holds the Docker socket and runs the docker CLI, nothing
else) and cloudflared add nothing back.

No PyYAML and no Docker: the compose files are read with a line-based
splitter (as in test_docker_socket_scope.py), Python sources with ast, and
subprocess is mocked.
"""

import ast
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER_DIR = os.path.join(PROJECT_ROOT, "app", "Resources", "docker")
sys.path.insert(0, os.path.join(DOCKER_DIR, "tor"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import onionheaven_common as oc  # noqa: E402

_farm_spec = importlib.util.spec_from_file_location(
    "onionheaven_farm", os.path.join(DOCKER_DIR, "tor", "onionheaven-farm.py"))
farm = importlib.util.module_from_spec(_farm_spec)
_farm_spec.loader.exec_module(farm)

COMPOSE = "app/Resources/docker/docker-compose.yml"
CLOUDFLARE_COMPOSE = "app/Resources/docker/docker-compose.cloudflare.yml"
COMPOSE_FILES = (COMPOSE, CLOUDFLARE_COMPOSE)

NNP = "no-new-privileges:true"

# Added back by nothing, whatever a live test seems to show.
NEVER = {"ALL", "NET_RAW", "NET_ADMIN", "SYS_ADMIN", "SYS_PTRACE",
         "SYS_MODULE", "DAC_READ_SEARCH", "SYS_RAWIO", "BPF", "MAC_ADMIN"}

# The tor image as a takeover worker: su to debian-tor (SETGID, SETUID); the
# watchdog, queue manager and tor-manager read the control cookie in
# debian-tor's 0700 /var/lib/tor (DAC_OVERRIDE). /var/lib/tor comes from the
# image, so the entrypoint's chown and chmod of it change nothing.
WORKER = {"DAC_OVERRIDE", "SETGID", "SETUID"}

# What each compose service adds back after `cap_drop: [ALL]`.
COMPOSE_CAPS = {
    COMPOSE: {
        "tor": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
        "wordpress": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL",
                      "NET_BIND_SERVICE", "SETGID", "SETUID"},
        "db": {"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"},
        "onionheaven": WORKER,
        "autoheal": set(),
        "autoheal-proxy": set(),
        "onionheaven-farm": set(),
    },
    CLOUDFLARE_COMPOSE: {
        "cloudflared": set(),
    },
}

# Every `docker run` in the shipped code, by (file, enclosing function), and
# what each one adds back, in source order.
READ_USER_KEY = {"DAC_OVERRIDE"}             # root reads key files it does not own
COPY_INTO_TOR_STATE = {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
DOCKER_RUN_SITES = {
    ("app/Resources/docker/tor/onionheaven-farm.py", "worker_run_argv"): [WORKER],
    ("src/onionpress/containers.py", "start_farm_worker"): [WORKER],
    # Reads arti's files (its own user's, 0700) into a copy root owns (the
    # launchers' migrate_key_volume runs it, Phase 4 #120).
    ("src/onionpress/containers.py", "migrate_key_volume"): [READ_USER_KEY],
    # The first-run key seed (both launchers' start, #442.5, and the
    # restore) and Linux's copy of C Tor's key files into its state.
    ("src/onionpress/containers.py", "run_key_seed"): [READ_USER_KEY],
    ("src/onionpress/containers.py", "run_tor_state_seed"): [COPY_INTO_TOR_STATE],
    # create_backup's four reads of debian-tor's (or arti's) key files out
    # of the volumes.
    ("linux/onionpress", "key_volume_cat"): [READ_USER_KEY],
    # `--entrypoint test -x mkp224o`: a file test.
    ("src/onionpress/launcher_ops.py", "tor_image_has_mkp224o"): [set()],
    # mkp224o writes into the user's folder.
    ("src/onionpress/launcher_ops.py", "generate_vanity_in_container"): [READ_USER_KEY],
}

# The privileged exceptions, each held to its exact shape below. Only the
# WordPress egress helper on Linux system Docker (wordpress_egress.py): it
# runs the host's own iptables in PID 1's namespaces, which no capability
# set short of --privileged allows under Docker's default profiles (see its
# module docstring). The launcher is a docker-group user, already root on
# the host, so this grants nothing the caller lacks; the container joins no
# network, mounts nothing and is removed when it exits.
PRIVILEGED_SITES = {
    ("src/onionpress/wordpress_egress.py", "helper_argv"): 1,
}

# The takeover workers serve onion services: they join onionpress-network
# and run the image's own entrypoint. Every other run is a one-shot helper
# with its entrypoint overridden and no network: nothing it does (reading or
# writing a key, a file test, mkp224o, iptables in the host's namespaces)
# needs one, and the onion service key never gets the chance to leave.
WORKER_SITES = {
    ("app/Resources/docker/tor/onionheaven-farm.py", "worker_run_argv"),
    ("src/onionpress/containers.py", "start_farm_worker"),
}

# The image each run names. The launchers' helpers run KEY_HELPER_IMAGE
# (the stack's tor image: ONIONPRESS_TOR_IMAGE, else its pin), the macOS
# takeover worker the compose file's onionheaven reference, and every Python
# site a variable named `image` (the pinned tor image: launcher_ops's
# DEFAULT_TOR_IMAGE, ContainerManager.ensure_tor_image, onionheaven_image,
# and the egress helper's caller; tests elsewhere hold each to that). Never
# a floating tag such as `alpine`: a registry or a mirror could swap it for
# an image that copies the key.
TOR_PIN = r"ghcr\.io/brewsterkahle/onionpress-tor:latest@sha256:[0-9a-f]{64}"
KEY_HELPER_IMAGE_TOKEN = '"$KEY_HELPER_IMAGE"'
WORKER_IMAGE_TOKEN = re.compile(
    r'^"\$\{ONIONHEAVEN_IMAGE:-\$\{ONIONPRESS_TOR_IMAGE:-' + TOR_PIN + r'\}\}"$')

class _Expr(str):
    """A Python expression in a docker run argv, by its source text: an
    option's value, or the image, never an option itself."""


# docker run options that take no value.
_BOOLEAN_FLAGS = {"--rm", "-d", "--detach", "-i", "--interactive", "-t",
                  "--tty", "-it", "--privileged", "--init", "--read-only"}


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _services(text):
    """{service: block text} for a compose file, comment lines dropped."""
    services, current, in_services = {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        if not line.startswith(" "):
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = []
        elif current:
            services[current].append(line)
    return {name: "\n".join(lines) for name, lines in services.items()}


def _list_items(block, key):
    """The `- item` lines under `key:` in a service block, unquoted; None
    when the key is absent. Flow lists (`key: [A, B]`) count as absent, so
    a test fails rather than misreads one."""
    m = re.search(rf"^    {key}:\s*\n((?:      - .*\n?)+)", block + "\n", re.M)
    if not m:
        return None
    items = []
    for line in m.group(1).splitlines():
        item = line.strip()[2:].strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            item = item[1:-1]
        items.append(item)
    return items


def _pairs(argv):
    """(flag, value) for every adjacent pair of a docker run argv."""
    return set(zip(argv, argv[1:]))


def _options(tokens):
    """The options of a `docker run` whose arguments are *tokens*, up to the
    image: {"cap_drop": [...], "cap_add": [...], "security_opt": [...],
    "network": [...], "entrypoint": [...], "volume": [...], "mount": [...],
    "privileged": bool, "image": token, "ended": bool}; "ended" is false
    when no image followed the options. A Python expression is an _Expr."""
    opts = {"cap_drop": [], "cap_add": [], "security_opt": [],
            "network": [], "entrypoint": [], "volume": [], "mount": [],
            "privileged": False, "image": None, "ended": False}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (not isinstance(tok, str) or isinstance(tok, _Expr)
                or not tok.startswith("-")):
            opts["image"], opts["ended"] = tok, True
            break
        flag, eq, value = tok.partition("=")
        if flag in _BOOLEAN_FLAGS:
            opts["privileged"] |= flag == "--privileged" and value not in ("false",)
            i += 1
            continue
        if not eq:
            value = tokens[i + 1] if i + 1 < len(tokens) else None
            i += 1
        i += 1
        key = {"--cap-drop": "cap_drop", "--cap-add": "cap_add",
               "--security-opt": "security_opt", "--network": "network",
               "--net": "network", "--entrypoint": "entrypoint",
               "-v": "volume", "--volume": "volume",
               "--mount": "mount"}.get(flag)
        if key:
            opts[key].append(value)
    return opts


# ---------------------------------------------------------------------------
# The census: every `docker run` in shipped code
# ---------------------------------------------------------------------------

def _shipped_files():
    """(relative path, text, is_python) for every shipped source file that
    could run a container: the app's and the .deb's scripts and Python."""
    for top in ("src", "linux", os.path.join("app", "MacOS"),
                os.path.join("app", "Resources")):
        for dirpath, dirnames, filenames in os.walk(os.path.join(PROJECT_ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("wordlists", "__pycache__")]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                except (UnicodeDecodeError, OSError):
                    continue
                first = text.split("\n", 1)[0]
                rel = os.path.relpath(path, PROJECT_ROOT)
                if name.endswith(".py") or (first.startswith("#!") and "python" in first):
                    yield rel, text, True
                elif name.endswith(".sh") or (first.startswith("#!") and "sh" in first):
                    yield rel, text, False


_BASH_FUNCTION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")


def _bash_sites(text):
    """(enclosing function, option tokens) for each `docker run` in a
    shell script, outside comments. Continuation lines are joined; the
    options end at the image, before any quoted script."""
    lines = text.splitlines()
    func = None
    for n, line in enumerate(lines):
        m = _BASH_FUNCTION.match(line)
        if m:
            func = m.group(1)
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"\bdocker\s+run\b", line):
            command = line[m.end():]
            k = n
            while command.rstrip().endswith("\\") and k + 1 < len(lines):
                k += 1
                command = command.rstrip()[:-1] + " " + lines[k]
            yield func, command.split()


def _python_sites(text):
    """(enclosing function, option tokens) for each docker run argv list in
    a Python source: ["docker", "run", ...] or a Docker wrapper's
    ["run", ...]. Every "run" string in the file must be one of them."""
    tree = ast.parse(text)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None

    def token(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return _Expr(ast.unparse(node))

    def const(node):
        return node.value if isinstance(node, ast.Constant) else None

    sites, claimed = [], set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        elts = node.elts
        if const(elts[0]) == "run":
            start = 1
        elif len(elts) > 1 and const(elts[1]) == "run" and (
                const(elts[0]) == "docker"
                or "docker" in ast.unparse(elts[0]).lower()):
            start = 2
        else:
            continue
        claimed.add(id(elts[start - 1]))
        tokens = [token(e) for e in elts[start:]]
        sites.append((enclosing(node), tokens))
    strays = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and n.value == "run" and id(n) not in claimed]
    return sites, strays


# "run" strings that are not docker: wp-cli's `cron event run`.
NOT_DOCKER_RUN = {("src/onionpress/wayback_smoke.py", 1)}


class TestDockerRunCensus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sites, cls.strays = {}, {}
        for rel, text, is_python in _shipped_files():
            if is_python:
                found, strays = _python_sites(text)
                if strays:
                    cls.strays[rel] = strays
            else:
                found = list(_bash_sites(text))
            for func, tokens in found:
                cls.sites.setdefault((rel, func), []).append(_options(tokens))

    def test_census_is_complete(self):
        """Every `docker run` in shipped code is listed here, and every
        listed one still exists: a new one needs its set worked out (see
        docker-compose.yml's header) and added to DOCKER_RUN_SITES."""
        found = {site: len(opts) for site, opts in self.sites.items()}
        expected = {site: len(caps) for site, caps in DOCKER_RUN_SITES.items()}
        expected.update(PRIVILEGED_SITES)
        self.assertEqual(found, expected)
        # The scanner still finds sites (#442.5 made the launchers' three
        # key seeds and the restore's one shared pair in containers.py).
        self.assertGreaterEqual(sum(found.values()), 9)

    def test_only_the_listed_sites_are_privileged(self):
        for site, runs in self.sites.items():
            for n, opts in enumerate(runs):
                with self.subTest(site=site, run=n):
                    self.assertEqual(opts["privileged"], site in PRIVILEGED_SITES)

    def test_the_privileged_helper_keeps_its_shape(self):
        """--privileged is the whole capability set, so what bounds the
        egress helper is everything else: removed when done, no network,
        no mounts, no-new-privileges, its own fixed entrypoint and script."""
        from onionpress import wordpress_egress
        argv = wordpress_egress.helper_argv("img@sha256:" + "a" * 64)
        image_at = argv.index("img@sha256:" + "a" * 64)
        options = argv[:image_at]
        self.assertEqual(options[:2], ["run", "--rm"])
        pairs = _pairs(options)
        self.assertIn(("--network", "none"), pairs)
        self.assertIn(("--pid", "host"), pairs)
        self.assertIn(("--security-opt", NNP), pairs)
        self.assertIn(("--entrypoint", "nsenter"), pairs)
        self.assertEqual(options.count("--privileged"), 1)
        for flag in ("-v", "--volume", "--mount", "--volumes-from", "--device",
                     "-p", "--publish", "-P", "--publish-all", "--ipc", "--uts",
                     "--userns", "--cgroupns", "-e", "--env", "--env-file",
                     "--cap-add", "--security-opt=no-new-privileges:false"):
            self.assertNotIn(flag, options)
        self.assertEqual([o for o in options if o.startswith("--network")], ["--network"])
        self.assertEqual(argv[image_at + 1:image_at + 9],
                         ["-t", "1", "-m", "-n", "--", "sh", "-c",
                          wordpress_egress.RULES_SCRIPT])

    def test_every_run_string_is_a_docker_run(self):
        """A "run" string the census did not take as a docker run argv is
        either known (NOT_DOCKER_RUN) or a docker run the parser missed."""
        strays = {(rel, len(lines)) for rel, lines in self.strays.items()}
        self.assertEqual(strays, NOT_DOCKER_RUN)

    def test_every_run_drops_all_and_adds_only_its_own(self):
        for site, runs in self.sites.items():
            expected = DOCKER_RUN_SITES.get(site)
            if expected is None:
                continue
            for n, (opts, caps) in enumerate(zip(runs, expected)):
                with self.subTest(site=site, run=n):
                    self.assertTrue(opts["ended"], "no image after the options")
                    self.assertEqual(opts["cap_drop"], ["ALL"])
                    self.assertEqual(set(opts["cap_add"]), caps)
                    self.assertEqual(len(opts["cap_add"]), len(caps))
                    self.assertFalse(NEVER & set(opts["cap_add"]))
                    self.assertIn(NNP, opts["security_opt"])
                    self.assertFalse(any("no-new-privileges:false" in o
                                         for o in opts["security_opt"]))
                    self.assertFalse(opts["privileged"])

    def test_only_the_takeover_workers_join_a_network(self):
        """A one-shot helper joins no network: the key it handles, or the
        file test, mkp224o and iptables run, need none. They ran on Docker's
        default bridge, from where a swapped image could have sent the key
        anywhere. The workers serve onion services from onionpress-network."""
        for site, runs in self.sites.items():
            for n, opts in enumerate(runs):
                with self.subTest(site=site, run=n):
                    expected = (["onionpress-network"] if site in WORKER_SITES
                                else ["none"])
                    self.assertEqual(opts["network"], expected)
        self.assertLessEqual(WORKER_SITES, set(self.sites))

    def test_every_helper_overrides_the_entrypoint(self):
        """The tor image's own entrypoint starts the tor service's setup;
        a helper replaces it with exactly the one program it runs."""
        for site, runs in self.sites.items():
            for n, opts in enumerate(runs):
                with self.subTest(site=site, run=n):
                    if site in WORKER_SITES:
                        self.assertEqual(opts["entrypoint"], [])
                    else:
                        self.assertEqual(len(opts["entrypoint"]), 1)

    def test_every_run_names_a_pinned_image(self):
        """No `alpine`, `busybox` or other floating tag: each run names the
        image listed for its kind of site (see TOR_PIN above)."""
        for (rel, func), runs in self.sites.items():
            for n, opts in enumerate(runs):
                image = opts["image"]
                with self.subTest(site=(rel, func), run=n, image=image):
                    if rel.endswith(".py"):
                        self.assertIsInstance(image, _Expr)
                        self.assertEqual(image, "image")
                    elif (rel, func) in WORKER_SITES:
                        self.assertRegex(image, WORKER_IMAGE_TOKEN)
                    else:
                        self.assertEqual(image, KEY_HELPER_IMAGE_TOKEN)

    def test_helpers_read_their_sources_read_only(self):
        """What a helper only reads (the old key volume, the user's key
        folder, a volume a backup reads from) is mounted read-only."""
        for site, runs in self.sites.items():
            if site in WORKER_SITES:
                continue
            for n, opts in enumerate(runs):
                with self.subTest(site=site, run=n):
                    for volume in opts["volume"]:
                        volume = volume.strip("'\"")
                        if re.search(r":/(src|old)(:|$)", volume):
                            self.assertTrue(volume.endswith(":ro"), volume)
                    for mount in opts["mount"]:
                        if "target=/src" in mount:
                            self.assertRegex(mount, r",readonly['\"]?$")

    def test_parser_reads_python_expressions(self):
        sites, strays = _python_sites(
            'def f(image, d):\n'
            '    run(["run", "--rm", "--network", "none", "-v", f"{d}:/src:ro",\n'
            '         "--entrypoint", "cat", image, "/src/x"])\n')
        self.assertEqual(strays, [])
        opts = _options(sites[0][1])
        self.assertEqual((opts["image"], opts["network"], opts["entrypoint"]),
                         ("image", ["none"], ["cat"]))
        self.assertIsInstance(opts["image"], _Expr)
        self.assertEqual(opts["volume"], ["f'{d}:/src:ro'"])

    def test_allowlists_never_add_the_forbidden(self):
        for site, caps in DOCKER_RUN_SITES.items():
            for c in caps:
                self.assertFalse(NEVER & c, site)
        for services in COMPOSE_CAPS.values():
            for name, caps in services.items():
                self.assertFalse(NEVER & caps, name)

    def test_parser_reads_the_shapes_in_use(self):
        """The parser stops at the image, so a capability flag after it
        (an argument to the image's entrypoint) does not count."""
        opts = _options(["--rm", "--cap-drop", "ALL", "-v", "a:/b", "alpine",
                         "--cap-add", "SYS_ADMIN"])
        self.assertEqual((opts["cap_drop"], opts["cap_add"], opts["image"]),
                         (["ALL"], [], "alpine"))
        opts = _options(["-d", "--cap-add=KILL", "--privileged", None])
        self.assertEqual(opts["cap_add"], ["KILL"])
        self.assertTrue(opts["privileged"])
        sites = list(_bash_sites(
            "f() {\n    # docker run --privileged x\n    if docker run --rm \\\n"
            "        --cap-drop ALL \\\n        alpine sh -c '\n    echo'\n}\n"))
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0][0], "f")
        self.assertEqual(_options(sites[0][1])["cap_drop"], ["ALL"])


class TestKeyHelperImage(unittest.TestCase):
    """KEY_HELPER_IMAGE, the one image the launchers' helpers run."""

    LAUNCHERS = ("app/MacOS/onionpress", "linux/onionpress")

    def test_it_is_the_stack_tor_image(self):
        """ONIONPRESS_TOR_IMAGE (a locally built stack), else exactly the
        tor pin build/image-pins.env holds, in the `${...:-...}` form
        build/refresh-image-digests.sh rewrites."""
        pins = dict(line.split("=", 1) for line in _read("build/image-pins.env").splitlines()
                    if line and not line.startswith("#") and "=" in line)
        for rel in self.LAUNCHERS:
            with self.subTest(launcher=rel):
                lines = [l for l in _read(rel).splitlines()
                         if l.startswith("KEY_HELPER_IMAGE=")]
                self.assertEqual(lines, [
                    'KEY_HELPER_IMAGE="${ONIONPRESS_TOR_IMAGE:-'
                    + pins["ONIONPRESS_TOR_IMAGE"].strip() + '}"'])

    def test_it_is_set_after_the_config_is_read(self):
        """The launchers read ONIONPRESS_TOR_IMAGE from ~/.onionpress/config
        into the environment; KEY_HELPER_IMAGE must come after that."""
        for rel in self.LAUNCHERS:
            text = _read(rel)
            with self.subTest(launcher=rel):
                self.assertLess(text.index("for _img_var in ONIONPRESS_TOR_IMAGE"),
                                text.index("\nKEY_HELPER_IMAGE="))

    def test_no_floating_helper_image_in_shipped_code(self):
        """`alpine` (or busybox) appears in no shipped code: comments aside,
        no shell line names it, and no Python string starts with it."""
        floating = re.compile(r"\b(alpine|busybox)\b")
        seen = 0
        for rel, text, is_python in _shipped_files():
            seen += 1
            with self.subTest(path=rel):
                if is_python:
                    for node in ast.walk(ast.parse(text)):
                        if isinstance(node, ast.Constant) and isinstance(node.value, str):
                            self.assertNotRegex(node.value, r"^(alpine|busybox)(\b|$)")
                else:
                    for line in text.splitlines():
                        if not line.lstrip().startswith("#"):
                            self.assertNotRegex(line, floating)
        self.assertGreater(seen, 20)


# ---------------------------------------------------------------------------
# Compose services
# ---------------------------------------------------------------------------

class TestComposeServices(unittest.TestCase):

    def setUp(self):
        self.files = {path: _services(_read(path)) for path in COMPOSE_FILES}

    def test_splitter_sees_every_service(self):
        """The checks below loop over what the splitter finds; make sure it
        finds them all, so a service it misses cannot slip through."""
        self.assertLessEqual(
            {"tor", "wordpress", "db", "autoheal", "autoheal-proxy",
             "onionheaven-farm", "onionheaven"},
            set(self.files[COMPOSE]))
        self.assertIn("cloudflared", self.files[CLOUDFLARE_COMPOSE])
        for path, services in self.files.items():
            with self.subTest(path=path):
                names = re.findall(r"(?m)^    container_name:", _read(path))
                self.assertEqual(len(names), len(services))

    def test_every_service_is_listed(self):
        for path, services in self.files.items():
            with self.subTest(path=path):
                self.assertEqual(set(services), set(COMPOSE_CAPS[path]))

    def test_every_service_drops_all(self):
        for path, services in self.files.items():
            for name, block in services.items():
                with self.subTest(path=path, service=name):
                    self.assertEqual(
                        _list_items(block, "cap_drop"), ["ALL"],
                        f"{name}: `cap_drop: [ALL]` (block style), then "
                        "cap_add only what it needs. With NET_RAW, root in "
                        "the container can forge packets from the "
                        "WordPress network's gateway, which auto-login and "
                        "/__op_proxy.php admit.")

    def test_every_service_adds_only_its_own(self):
        for path, services in self.files.items():
            for name, block in services.items():
                with self.subTest(path=path, service=name):
                    adds = _list_items(block, "cap_add") or []
                    self.assertEqual(sorted(adds), sorted(set(adds)))
                    self.assertEqual(
                        set(adds), COMPOSE_CAPS[path][name],
                        "a changed set needs a live test first (see "
                        "docker-compose.yml's header), then COMPOSE_CAPS")
                    self.assertFalse(NEVER & set(adds))
                    self.assertNotRegex(block, r"(?m)^    cap_(add|drop):[ \t]*\S")
                    self.assertNotRegex(block, r"(?m)^    privileged:\s*(true|yes)\b")

    def test_every_service_has_no_new_privileges(self):
        for path, services in self.files.items():
            for name, block in services.items():
                with self.subTest(path=path, service=name):
                    opts = _list_items(block, "security_opt") or []
                    self.assertIn(NNP, opts)
                    self.assertFalse(any("no-new-privileges:false" in o for o in opts))

    def test_autoheal_relies_on_owning_the_proxy_socket(self):
        """autoheal holds no DAC_OVERRIDE: it can connect to the proxy's
        socket (0600 by default) only because both run as uid 0, the
        socket's owner. Keep both at uid 0 and the owner's write bit set."""
        proxy = self.files[COMPOSE]["autoheal-proxy"]
        self.assertRegex(proxy, r'(?m)^    user: "0:0"\s*$')
        self.assertNotRegex(self.files[COMPOSE]["autoheal"], r"(?m)^    user:")
        for arg in _list_items(proxy, "command"):
            m = re.match(r"^-proxysocketendpointfilemode=([0-7]+)$", arg)
            if "proxysocketendpointfilemode" in arg:
                self.assertIsNotNone(m, arg)
                self.assertTrue(int(m.group(1), 8) & 0o200, arg)

    def test_healthchecks_use_no_raw_sockets(self):
        for path, services in self.files.items():
            for name, block in services.items():
                m = re.search(r"(?m)^      test:\s*(.*)$", block)
                if m:
                    with self.subTest(path=path, service=name):
                        self.assertNotRegex(m.group(1), r"\b(ping6?|arping)\b")


# ---------------------------------------------------------------------------
# Takeover workers (docker run, outside compose)
# ---------------------------------------------------------------------------

def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


class TestTakeoverWorkers(unittest.TestCase):
    """The farm controller (onionheaven-farm.py) starts the takeover workers
    with `docker run` when the onionheaven container's heartbeat asks. They
    serve taken-over onion addresses from onionpress-network, so they get
    the compose services' hardening."""

    def assertHardened(self, argv):
        pairs = _pairs(argv)
        self.assertIn(("--cap-drop", "ALL"), pairs)
        self.assertEqual(argv.count("--cap-drop"), 1)
        adds = [v for f, v in zip(argv, argv[1:]) if f == "--cap-add"]
        self.assertEqual(sorted(adds), sorted(WORKER))
        self.assertIn(("--security-opt", NNP), pairs)
        self.assertNotIn("--privileged", argv)
        # Flags, not arguments to the image's entrypoint.
        self.assertLess(argv.index("--cap-drop"), len(argv) - 1)
        self.assertLess(argv.index("--security-opt"), len(argv) - 1)

    def test_spawn_worker(self):
        image = "img@sha256:" + "a" * 64
        argv = farm.worker_run_argv("onionheaven-takeover-0", image)
        self.assertEqual(argv[0], "run")
        self.assertEqual(argv[-1], image)
        self.assertHardened(argv[:-1])

        # op_spawn runs exactly that argv and nothing else of its own.
        def fake(argv, timeout):
            return _done("" if argv[0] == "ps" else "cid\n")
        with mock.patch.dict(os.environ, {"ONIONHEAVEN_IMAGE": image}), \
                mock.patch.object(farm, "log"), \
                mock.patch.object(farm, "docker", side_effect=fake) as run:
            reply = farm.op_spawn({"op": "spawn", "name": "onionheaven-takeover-0"})
        self.assertTrue(reply["ok"], reply)
        runs = [c.args[0] for c in run.call_args_list if c.args[0][0] == "run"]
        self.assertEqual(runs, [argv])

    def test_dead_code_spawners_agree(self):
        """Not called today, but they must not start a worker without it."""
        from onionpress.containers import ContainerManager
        from onionpress.docker import Docker
        docker = mock.Mock(spec=Docker)
        docker.run.return_value = mock.Mock(ok=True, stderr="")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = mock.Mock(config_file=os.path.join(tmp, "config"), docker_dir=tmp)
        cm = ContainerManager(docker, paths, port_config=mock.Mock())
        cm.start_farm_worker(0, image="img")
        argv = docker.run.call_args.args[0]
        self.assertEqual(argv[-1], "img")
        self.assertHardened(argv[:-1])

    def test_mac_launcher_starts_no_worker(self):
        """Its unused _start_takeover_worker is gone; a worker it started
        again would have to pass the checks above."""
        self.assertNotIn("TAKEOVER_WORKER=1", _read("app/MacOS/onionpress"))


# ---------------------------------------------------------------------------
# What the images do, as far as these settings are concerned
# ---------------------------------------------------------------------------

def _image_files():
    for sub in ("tor", "wordpress"):
        root = os.path.join(DOCKER_DIR, sub)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "wordlists"]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        yield os.path.relpath(path, PROJECT_ROOT), f.read()
                except UnicodeDecodeError:
                    continue


class TestImages(unittest.TestCase):

    def test_nothing_uses_raw_sockets(self):
        """Nothing in our images opens a raw or packet socket, so NET_RAW
        costs nothing. Something that needs one has to say so here first."""
        raw = re.compile(
            r"\bSOCK_RAW\b|\bAF_PACKET\b|\bIPPROTO_ICMP\b|\barping\b|\btcpdump\b"
            r"|\bsetcap\b|\biputils\b|\bping6?\s+-[cnqW]"
            r"|\b(?:IP[46]?|IP)-(?:SENDTO|RECV|RECVFROM|DATAGRAM):|\bINTERFACE:")
        seen = 0
        for rel, text in _image_files():
            seen += 1
            with self.subTest(path=rel):
                self.assertIsNone(raw.search(text))
        self.assertGreater(seen, 10)

    def test_privilege_drops_run_as_root(self):
        """no-new-privileges blocks setuid binaries, not root calling
        setuid(). The tor image's processes start as root (the Dockerfile
        ends on USER root) and drop to debian-tor with su; su is run only
        by root-run code (the entrypoint and the queue manager it starts),
        and nothing calls sudo. A non-root process calling su or sudo
        would fail under no-new-privileges."""
        tor_dockerfile = _read("app/Resources/docker/tor/Dockerfile")
        users = re.findall(r"(?m)^USER\s+(\S+)", tor_dockerfile)
        self.assertEqual(users[-1], "root")
        self.assertNotRegex(_read("app/Resources/docker/wordpress/Dockerfile"),
                            r"(?m)^USER\s")

        su_callers = set()
        for rel, text in _image_files():
            code = "\n".join(l for l in text.splitlines()
                             if not l.lstrip().startswith("#"))
            with self.subTest(path=rel):
                self.assertNotRegex(code, r"\bsudo\b")
            if re.search(r"""(^|[\s;&|(])su\s+-|["']su["']\s*,""", code, re.M):
                su_callers.add(os.path.basename(rel))
        self.assertEqual(su_callers, {"entrypoint.sh", "onionheaven-queue-manager.py"})

    def test_no_root_process_signals_another_user(self):
        """No tor-image container keeps KILL. Root there signals only
        root's processes: the host's USR1/USR2 to the watchdog, the
        entrypoint's and onionheaven_common's restarts of the heartbeat, the
        queue manager's kill of the first `tor -f` process (su, root's: the
        worker then restarts). Something that signals Tor itself, or any
        other user's process, needs KILL back."""
        kills = set()
        for rel, text in _image_files():
            if "/tor/" not in rel.replace(os.sep, "/"):
                continue
            for line in text.splitlines():
                code = line.split("#", 1)[0] if rel.endswith(".sh") else line
                if re.search(r"\bkill\s+(-\S+\s+)?\$|\"kill\"|os\.kill\(|\bpkill\b|\bkillall\b",
                             code) and not re.search(r"kill\s+-0\b", code):
                    kills.add((os.path.basename(rel), code.strip()))
        self.assertEqual({name for name, _ in kills},
                         {"entrypoint.sh", "onionheaven-queue-manager.py",
                          "onionheaven_common.py"}, kills)


if __name__ == "__main__":
    unittest.main()
