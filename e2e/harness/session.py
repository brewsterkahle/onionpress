"""One e2e run: options, host probe, safety, builds, nodes, report.

e2e/conftest.py makes one Session in pytest_configure and hands its nodes
to the tests; nothing here depends on pytest.
"""

import hashlib
import os
import pwd
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import PROJECT_DIR, platforms, safety
from .nodes.linux_vm import DebianVMNode, lima_instances
from .nodes.mac import MacNode, pick_port_offset
from .proc import NodeLog, run
from .report import RunReport

RELEASE_REPO = "brewsterkahle/onionpress"
ROOT_MARKER = ".ope2e-root"


@dataclass
class Options:
    platforms: list = None          # None: everything the host offers
    max_nodes: int = 4
    mac_app: str = ""
    mac_dmg: str = ""
    deb: str = ""
    release: str = ""               # a tag, or "latest"
    scratch_root: str = safety.DEFAULT_SCRATCH_ROOT
    results_dir: str = ""
    keep: bool = False
    limactl: str = ""
    tiers: list = field(default_factory=list)


def real_home():
    """The user's real home, whatever $HOME says."""
    return pwd.getpwuid(os.getuid()).pw_dir


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Session:
    def __init__(self, opts):
        self.opts = opts
        self.real_home = real_home()
        self.root = safety.check_scratch_root(opts.scratch_root, self.real_home)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.results_dir = opts.results_dir or os.path.join(
            PROJECT_DIR, "e2e-results", f"{stamp}-{os.uname().nodename.split('.')[0]}")
        os.makedirs(self.results_dir, exist_ok=True)
        self.log = NodeLog(os.path.join(self.results_dir, "harness.log"))
        self.live_address = safety.live_onion_address(self.real_home)
        self.report = RunReport(tiers=list(opts.tiers))
        self.report.start()
        self.nodes = {}                 # platform -> Node
        self._installs = {}             # node name -> Future
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="install")
        self._lock = threading.Lock()

    # ── setup ───────────────────────────────────────────────────────────
    def prepare(self):
        """Probe, choose platforms, resolve builds, clean leftovers, make
        the node objects (nothing is installed yet)."""
        self._claim_root()
        mac_app_hint = self.opts.mac_app or self._existing_app_hint()
        facts = platforms.probe_host(mac_app=mac_app_hint)
        if self.opts.limactl:
            facts.tools["limactl"] = self.opts.limactl
        self.report.host = facts.label
        offer_list = platforms.offers(facts)
        chosen, not_tested = platforms.choose(offer_list, self.opts.platforms)
        self.log.write("host: " + facts.label)
        for line in platforms.describe(offer_list):
            self.log.write(line)

        if "macos" in chosen:
            source, why = self._mac_build()
            if source is None:
                not_tested["macos"] = why
                del chosen["macos"]
            else:
                self.report.build["mac"] = self._describe_source(source)
        if "debian" in chosen:
            source = self._deb_build()
            self.report.build["deb"] = self._describe_source(source)
        self.report.not_tested.update(not_tested)

        self._cleanup_leftovers(facts.tools.get("limactl"))
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, ROOT_MARKER), "w", encoding="utf-8") as f:
            f.write(self.results_dir + "\n")

        budget = self.opts.max_nodes
        for plat in platforms.PLATFORMS:
            if plat not in chosen:
                continue
            if budget <= 0:
                self.report.not_tested[plat] = "--max-nodes reached"
                continue
            budget -= 1
            if plat == "macos":
                name = "mac-1"
                self.nodes[plat] = MacNode(
                    name, self._node_log(name), self.results_dir, root=self.root,
                    real_home=self.real_home, app_source=self._mac_source,
                    port_offset=pick_port_offset())
            elif plat == "debian":
                name = "debian-1"
                self.nodes[plat] = DebianVMNode(
                    name, self._node_log(name), self.results_dir, root=self.root,
                    limactl=facts.tools["limactl"], deb_source=self._deb_source)
            self.report.roles[f"node[{plat}]"] = name
        return self

    def _node_log(self, name):
        return NodeLog(os.path.join(self.results_dir, "nodes", name, "harness.log"))

    def _claim_root(self):
        """The scratch root is the harness's alone: refuse one that exists
        without our marker rather than delete someone's files."""
        if os.path.exists(self.root) and os.listdir(self.root) and \
                not os.path.exists(os.path.join(self.root, ROOT_MARKER)):
            raise safety.UnsafeTarget(f"{self.root} exists and was not made by the e2e harness; "
                                      "pass another --scratch-root or remove it yourself")

    def _cleanup_leftovers(self, limactl):
        """Remove what an earlier run (killed, or --keep) left under root."""
        if not os.path.isdir(self.root):
            return
        if limactl:
            lima_home = os.path.join(self.root, "lima")
            for vm in lima_instances(limactl, lima_home):
                self.log.write(f"leftover Lima VM {vm}: deleting")
                run([limactl, "delete", "--force", vm],
                    env={"LIMA_HOME": lima_home, "HOME": self.real_home, "PATH": "/usr/bin:/bin"},
                    timeout=600, log=self.log)
        for entry in sorted(os.listdir(self.root)):
            node_dir = os.path.join(self.root, entry)
            app = os.path.join(node_dir, "OnionPress.app")
            if os.path.isdir(app):
                self.log.write(f"leftover Mac node {entry}: quitting and deleting its VM")
                leftover = MacNode(entry, self.log, self.results_dir, root=self.root,
                                   real_home=self.real_home, app_source=("app", app), port_offset=0)
                leftover.destroy()
        shutil.rmtree(self.root, ignore_errors=True)

    # ── builds ──────────────────────────────────────────────────────────
    def _existing_app_hint(self):
        built = os.path.join(PROJECT_DIR, "OnionPress.app")
        if os.path.isdir(built):
            return built
        return "/Applications/OnionPress.app" if os.path.isdir("/Applications/OnionPress.app") else None

    def _download_release_asset(self, name):
        dest = os.path.join(self.root + "-downloads", self.opts.release)
        os.makedirs(dest, exist_ok=True)
        tag = [] if self.opts.release == "latest" else [self.opts.release]
        for asset in (name, "SHA256SUMS"):
            run(["gh", "release", "download", *tag, "-R", RELEASE_REPO, "-p", asset,
                 "-D", dest, "--clobber"], timeout=1800, log=self.log, check=True)
        path = os.path.join(dest, name)
        with open(os.path.join(dest, "SHA256SUMS"), encoding="utf-8") as f:
            sums = dict(reversed(line.split()) for line in f if len(line.split()) == 2)
        if sums.get(name) != sha256(path):
            raise safety.UnsafeTarget(f"{name} of release {self.opts.release} does not match "
                                      "its SHA256SUMS line")
        return path

    def _mac_build(self):
        o = self.opts
        if o.mac_app:
            self._mac_source = ("app", os.path.abspath(o.mac_app))
        elif o.mac_dmg:
            self._mac_source = ("dmg", os.path.abspath(o.mac_dmg))
        elif o.release:
            self._mac_source = ("dmg", self._download_release_asset("onionpress.dmg"))
        elif os.path.isdir(os.path.join(PROJECT_DIR, "OnionPress.app")):
            self._mac_source = ("app", os.path.join(PROJECT_DIR, "OnionPress.app"))
        elif os.path.isfile(os.path.join(PROJECT_DIR, "build", "onionpress.dmg")):
            self._mac_source = ("dmg", os.path.join(PROJECT_DIR, "build", "onionpress.dmg"))
        else:
            return None, ("no Mac build: run `make dmg`, or pass --mac-app, --mac-dmg "
                          "or --release")
        return self._mac_source, ""

    def _deb_build(self):
        o = self.opts
        if o.deb:
            self._deb_source = ("deb", os.path.abspath(o.deb))
        elif o.release:
            self._deb_source = ("deb", self._download_release_asset("onionpress.deb"))
        else:
            self._deb_source = ("source", PROJECT_DIR)
        return self._deb_source

    def _describe_source(self, source):
        kind, path = source
        if kind == "source":
            r = run(["git", "-C", path, "describe", "--always", "--dirty"], timeout=30)
            return f"built in the VM from the checkout ({r.out.strip()})"
        if os.path.isfile(path):
            return f"{os.path.basename(path)} sha256 {sha256(path)[:16]}…"
        return path

    # ── installs ────────────────────────────────────────────────────────
    def prewarm(self, platforms_needed):
        """Start every needed node's install in the background, so a VM
        boots while another platform's tests run."""
        for plat in platforms_needed:
            node = self.nodes.get(plat)
            if node and node.name not in self._installs:
                self._installs[node.name] = self._executor.submit(self._timed_install, node)

    def _timed_install(self, node):
        started = time.monotonic()
        try:
            node.install()
        finally:
            node.timings.add(f"{node.name}: install", time.monotonic() - started, node.installed)

    def wait_installed(self, node, timeout=7200):
        self.prewarm([node.platform])
        self._installs[node.name].result(timeout=timeout)
        return node

    def verify_address(self, node, address):
        safety.check_not_live(address, self.live_address)
        return address

    def visitor_for(self, node):
        """Another installed, running node to fetch from over Tor, else the
        node itself (its own tor client still goes through the network)."""
        for other in self.nodes.values():
            if other is not node and other.installed and getattr(other, "address", ""):
                return other
        return node

    # ── teardown ────────────────────────────────────────────────────────
    def finish(self):
        for plat, node in self.nodes.items():
            if not node.installed and node.name not in self._installs:
                continue
            future = self._installs.get(node.name)
            if future is not None and not future.done():
                self.log.write(f"{node.name}: install still running at teardown; waiting")
                try:
                    future.result(timeout=3600)
                except Exception:  # noqa: BLE001
                    pass
            ev = os.path.join(self.results_dir, "nodes", node.name)
            try:
                self.report.tested[plat] = node.facts()
            except Exception as e:  # noqa: BLE001
                self.report.tested[plat] = {"kind": node.kind, "facts": f"unavailable ({e})"}
            try:
                node.collect_evidence(ev)
            except Exception as e:  # noqa: BLE001
                self.log.write(f"{node.name}: evidence collection failed: {e}")
            for path, line, what in safety.tripwire_scan(
                    [os.path.join(ev, "data-dir"), *self._container_logs(ev)]):
                self.report.tripwire.append([node.name, os.path.relpath(path, self.results_dir),
                                             line, what])
            self.report.notes += [f"{node.name}: {n}" for n in node.notes + node.install_findings]
            self.report.timings[node.name] = [[t.label, t.seconds, t.ok]
                                              for t in node.timings.entries]
            try:
                node.destroy(keep=self.opts.keep)
            except Exception as e:  # noqa: BLE001
                self.log.write(f"{node.name}: teardown failed: {e}")
                self.report.notes.append(f"{node.name}: teardown failed ({e}); "
                                         f"leftovers under {self.root} are removed by the next run")
        # Platforms chosen but never installed (every test deselected).
        for plat, node in self.nodes.items():
            if plat not in self.report.tested:
                self.report.not_tested.setdefault(plat, "no selected test used it")
        if self.opts.keep:
            self.report.notes.append(f"--keep: nodes left running under {self.root}")
        self._executor.shutdown(wait=False)
        self.report.finish()
        with open(os.path.join(self.results_dir, "results.json"), "w", encoding="utf-8") as f:
            f.write(self.report.to_json())
        with open(os.path.join(self.results_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(self.report.render_markdown())
        return self.report

    @staticmethod
    def _container_logs(ev):
        cdir = os.path.join(ev, "containers")
        if not os.path.isdir(cdir):
            return []
        return [os.path.join(cdir, n) for n in sorted(os.listdir(cdir)) if n.endswith(".log")]
