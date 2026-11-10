"""T1 (b)'s chaos: faults put on a live node, and what the daemon must
make of each (issues #139 and #266 item 3), as plain functions so the
unit suite checks them without a VM (tests/test_e2e_harness.py).

Each fault is applied to the node's own stack or VM, held, then reverted;
the daemon's state document (`~/.onionpress/daemon-state.json`) is sampled
the whole time and its log is read before and after. The rules checked
are the product's (CLAUDE.md, "Onion Service Recovery"):

- The dual probe is the only measure of reachability, and an offline gap
  is evidence of nothing: no fault here may end in a descriptor
  republish (`reachability: … republish` in the daemon's log), since the
  key and descriptor were never at fault.
- A host with no route out reads "offline", never "stuck" ("Slow to
  connect — try Restart"); so does one whose network carries nothing
  past the route (a captive portal: Tor's own report, issue #139).
- A sick stack is not an offline host: tor paused reads "starting",
  never "offline".
- After the revert the site is "available" again, at the same address.

Host faults (no route, captive portal) change the VM's routing table and
firewall, so they run on a linux-vm node only, never on a Mac host.
"""

import json
import re
import time
from dataclasses import dataclass, field

TOR = "onionpress-tor"
STATE_FILE = ".onionpress/daemon-state.json"
DAEMON_LOGS = ".onionpress/logs"

# RecoveryWindow.request_republish's lines, sent or log-only.
REPUBLISH_RE = re.compile(r"reachability: .*(republish)")
# HealthChecker.network_reachable's line when Tor reaches no relay.
TOR_NO_RELAY_RE = re.compile(r"Tor reaches no relay \(([^)]*)\)")

# The chain the captive portal fault adds: TCP to anything but a private
# or loopback address is refused, established flows included (a portal is
# a new network: Tor's guard connections from before do not survive it;
# refusing only NEW left them up and the site reachable, first live run),
# so the VM keeps its route, its DNS answers and Lima's ssh, and nothing
# past the "portal" answers.
CAPTIVE_CHAIN = "OP-E2E-CAPTIVE"
PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                "169.254.0.0/16")


@dataclass
class Fault:
    name: str
    hold: int                    # seconds the fault stays on
    host: bool = False           # changes the VM, not only the stack
    expect_offline: bool = False  # the daemon must read "offline" meanwhile
    never_offline: bool = False   # ... and must not
    flaps: int = 1               # apply/revert cycles within the hold
    detail: str = ""


FAULTS = {
    "tor-paused": Fault(
        "tor-paused", hold=150, never_offline=True,
        detail="docker pause onionpress-tor: a sick stack, not an offline host"),
    "network-flap": Fault(
        "network-flap", hold=240, flaps=3,
        detail="docker network disconnect/connect of onionpress-tor, three times"),
    "no-route": Fault(
        "no-route", hold=150, host=True, expect_offline=True,
        detail="the VM's default routes deleted: interface up, no route"),
    "captive-portal": Fault(
        "captive-portal", hold=420, host=True, expect_offline=True,
        detail="route kept, every TCP packet past private ranges refused"),
}


# ── network flap ───────────────────────────────────────────────────────

def attachments(inspect_doc):
    """[(network, [aliases])] the container is attached to, from its
    `docker inspect` document. Docker's own aliases (the container's
    name and id prefixes) are left out: `network connect` adds those."""
    name = (inspect_doc.get("Name") or "").lstrip("/")
    cid = inspect_doc.get("Id") or ""
    nets = ((inspect_doc.get("NetworkSettings") or {}).get("Networks") or {})
    found = []
    for net in sorted(nets):
        aliases = []
        for alias in (nets[net] or {}).get("Aliases") or ():
            if alias == name or (cid and len(alias) >= 12 and cid.startswith(alias)):
                continue
            if alias not in aliases:
                aliases.append(alias)
        found.append((net, aliases))
    return found


def disconnect_argvs(container, attached):
    return [["network", "disconnect", net, container] for net, _ in attached]


def connect_argvs(container, attached):
    argvs = []
    for net, aliases in attached:
        argv = ["network", "connect"]
        for alias in aliases:
            argv += ["--alias", alias]
        argvs.append(argv + [net, container])
    return argvs


# ── no route ───────────────────────────────────────────────────────────

def default_routes(text):
    """The `ip route add` arguments that put back each line of `ip route
    show default` (v4 or v6). `expires` (a router advertisement's
    lifetime) is dropped: it is the kernel's to count, not ours to set;
    so is `nhid` (the kernel's nexthop object, which `ip` refuses beside
    `via`: seen on a Debian 13 guest)."""
    routes = []
    for line in text.splitlines():
        words = line.split()
        if not words or words[0] != "default":
            continue
        kept, skip = [], False
        for word in words:
            if skip:
                skip = False
                continue
            if word in ("expires", "nhid"):
                skip = True
                continue
            kept.append(word)
        routes.append(kept)
    return routes


def route_del_argvs(v4, v6):
    return ([["ip", "route", "del"] + r for r in v4]
            + [["ip", "-6", "route", "del"] + r for r in v6])


def route_add_argvs(v4, v6):
    return ([["ip", "route", "replace"] + r for r in v4]
            + [["ip", "-6", "route", "replace"] + r for r in v6])


# ── captive portal ─────────────────────────────────────────────────────

def captive_on_argvs():
    """iptables argv: the chain, its rules, and a jump from OUTPUT (the
    host's and rootless Docker's own connections) and FORWARD (system
    Docker's containers), first in each."""
    argvs = [["iptables", "-N", CAPTIVE_CHAIN]]
    for net in PRIVATE_NETS:
        argvs.append(["iptables", "-A", CAPTIVE_CHAIN, "-d", net, "-j", "RETURN"])
    argvs.append(["iptables", "-A", CAPTIVE_CHAIN, "-p", "tcp",
                  "-j", "REJECT", "--reject-with", "tcp-reset"])
    for parent in ("OUTPUT", "FORWARD"):
        argvs.append(["iptables", "-I", parent, "1", "-j", CAPTIVE_CHAIN])
    return argvs


def captive_off_argvs():
    """Undo captive_on_argvs; each step may fail harmlessly when the on
    side stopped part way."""
    return ([["iptables", "-D", parent, "-j", CAPTIVE_CHAIN] for parent in ("OUTPUT", "FORWARD")]
            + [["iptables", "-F", CAPTIVE_CHAIN], ["iptables", "-X", CAPTIVE_CHAIN]])


# ── what the daemon said ───────────────────────────────────────────────

@dataclass
class Sample:
    t: float                 # seconds since the fault went on
    phase: str               # "fault" | "recovery"
    state: str               # display_state, "" if unreadable
    has_internet: object
    probe: str
    address: str


def sample_of(text, t, phase):
    """A Sample from daemon-state.json's text; state "" when it is not
    a document (the daemon writing it, or gone)."""
    try:
        doc = json.loads(text)
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError:
        return Sample(t, phase, "", None, "", "")
    probe = doc.get("probe") if isinstance(doc.get("probe"), dict) else {}
    return Sample(t, phase, str(doc.get("display_state") or ""), doc.get("has_internet"),
                  str(probe.get("code") or ""), str(doc.get("onion_address") or ""))


def count_matches(regex, log_text):
    return sum(1 for line in log_text.splitlines() if regex.search(line))


@dataclass
class Outcome:
    fault: str
    samples: list = field(default_factory=list)
    republish_lines: int = 0
    tor_no_relay: list = field(default_factory=list)
    recovered_after: object = None   # seconds after revert, or None
    routes_came_back: int = 0        # no-route: routes taken away again
    findings: list = field(default_factory=list)

    def states(self, phase):
        return [s.state for s in self.samples if s.phase == phase and s.state]

    def summary(self):
        seen = {}
        for s in self.samples:
            if s.state:
                seen.setdefault(s.phase, []).append(s.state)
        compact = {p: _runs(v) for p, v in seen.items()}
        return (f"{self.fault}: fault {compact.get('fault', [])}, recovery "
                f"{compact.get('recovery', [])}, recovered after {self.recovered_after}s, "
                f"republish lines {self.republish_lines}, tor no-relay {self.tor_no_relay or '-'}"
                + (f", default routes re-deleted {self.routes_came_back}" if self.routes_came_back else ""))


def _runs(states):
    """["starting", "starting", "offline"] -> ["starting×2", "offline"]."""
    out = []
    for s in states:
        if out and out[-1][0] == s:
            out[-1][1] += 1
        else:
            out.append([s, 1])
    return [s if n == 1 else f"{s}×{n}" for s, n in out]


def findings(fault, outcome, address):
    """What went against the rules above, one line each; [] when all held."""
    found = []
    during = outcome.states("fault")
    if not during:
        found.append(f"{fault.name}: no readable daemon state while the fault was on")
    if "stuck" in during:
        found.append(f"{fault.name}: the daemon read 'stuck' (Slow to connect) during the fault")
    if fault.expect_offline and "offline" not in during:
        found.append(f"{fault.name}: never read 'offline' during the fault ({_runs(during)})")
    if fault.never_offline and "offline" in during:
        found.append(f"{fault.name}: read 'offline' for a sick stack ({_runs(during)})")
    if outcome.republish_lines:
        found.append(f"{fault.name}: {outcome.republish_lines} republish line(s) in the daemon's log")
    if outcome.recovered_after is None:
        found.append(f"{fault.name}: not 'available' again after the revert "
                     f"({_runs(outcome.states('recovery'))})")
    moved = {s.address for s in outcome.samples if s.address and s.address != address}
    if moved:
        found.append(f"{fault.name}: the daemon served another address: {sorted(moved)}")
    return found


# ── running a fault on a node ──────────────────────────────────────────

class Clock:
    """time.monotonic/time.sleep, swappable in the unit suite."""
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


def _must(r, what):
    if not r.ok:
        raise RuntimeError(f"{what}: {r.describe()}")
    return r


def read_daemon_log(node):
    r = node.exec(["sh", "-c", f'cat "$HOME/{DAEMON_LOGS}"/onionpress*.log 2>/dev/null; true'],
                  timeout=60)
    return r.out


class FaultDriver:
    """Applies, flaps and reverts one fault on one node. `host` faults
    need `node.vm_run` (a linux-vm node)."""

    def __init__(self, node, fault):
        self.node = node
        self.fault = fault
        self._attached = None
        self._routes = None

    def _sudo(self, argv, what, check=True):
        r = self.node.vm_run(["sudo", *argv], timeout=60)
        return _must(r, what) if check else r

    def prepare(self):
        name = self.fault.name
        if name == "network-flap":
            r = _must(self.node.docker(["inspect", TOR]), "docker inspect onionpress-tor")
            self._attached = attachments(json.loads(r.out)[0])
            if not self._attached:
                raise RuntimeError("onionpress-tor is on no network")
        elif name == "no-route":
            v4, v6 = self._current_routes()
            if not v4 and not v6:
                raise RuntimeError("the VM has no default route to take away")
            self._routes = (v4, v6)

    def _current_routes(self):
        v4 = default_routes(self._sudo(["ip", "route", "show", "default"], "ip route").out)
        v6 = default_routes(self._sudo(["ip", "-6", "route", "show", "default"], "ip -6 route",
                                       check=False).out)
        return v4, v6

    def hold(self):
        """Called at every sample while the fault is on: a default route
        that came back (a DHCP renewal, a router advertisement: the
        VM's network is still up) is taken away again. How many times
        goes into the Outcome."""
        if self.fault.name != "no-route":
            return 0
        v4, v6 = self._current_routes()
        for argv in route_del_argvs(v4, v6):
            self._sudo(argv, " ".join(argv))
        return len(v4) + len(v6)

    def on(self):
        name = self.fault.name
        if name == "tor-paused":
            _must(self.node.docker(["pause", TOR]), "docker pause")
        elif name == "network-flap":
            for argv in disconnect_argvs(TOR, self._attached):
                _must(self.node.docker(argv), " ".join(argv))
        elif name == "no-route":
            for argv in route_del_argvs(*self._routes):
                self._sudo(argv, " ".join(argv))
        elif name == "captive-portal":
            for argv in captive_on_argvs():
                self._sudo(argv, " ".join(argv))

    def off(self):
        """Revert; every step is tried even when one fails, and the
        failures are raised together at the end."""
        name, errors = self.fault.name, []
        if name == "tor-paused":
            argvs = [("docker", ["unpause", TOR])]
        elif name == "network-flap":
            argvs = [("docker", a) for a in connect_argvs(TOR, self._attached)]
        elif name == "no-route":
            argvs = [("sudo", a) for a in route_add_argvs(*self._routes)]
        else:
            argvs = [("sudo", a) for a in captive_off_argvs()]
        for how, argv in argvs:
            r = (self.node.docker(argv) if how == "docker"
                 else self.node.vm_run(["sudo", *argv], timeout=60))
            if not r.ok and name != "captive-portal":
                errors.append(f"{' '.join(argv)}: {r.describe()}")
        if errors:
            raise RuntimeError("revert failed: " + "; ".join(errors))


def run_fault(node, fault, address, *, every=10, recover_timeout=900, clock=Clock):
    """Apply *fault* to *node*, sampling the daemon's state every *every*
    seconds through the hold and until it reads "available" again (at
    most *recover_timeout* after the revert). Returns an Outcome with its
    findings; the fault is always reverted."""
    driver = FaultDriver(node, fault)
    driver.prepare()
    outcome = Outcome(fault.name)
    log_before = read_daemon_log(node)
    start = clock.now()

    def take(phase):
        outcome.samples.append(sample_of(node.read_file(STATE_FILE), round(clock.now() - start), phase))
        return outcome.samples[-1]

    flap_len = fault.hold / (2 * fault.flaps) if fault.flaps > 1 else fault.hold
    try:
        for i in range(fault.flaps):
            driver.on()
            node.log.write(f"chaos: {fault.name} on ({i + 1}/{fault.flaps})")
            on_until = clock.now() + flap_len
            while clock.now() < on_until:
                outcome.routes_came_back += driver.hold()
                take("fault")
                clock.sleep(every)
            if i + 1 < fault.flaps:
                driver.off()
                node.log.write(f"chaos: {fault.name} off between flaps")
                off_until = clock.now() + flap_len
                while clock.now() < off_until:
                    take("fault")
                    clock.sleep(every)
    finally:
        driver.off()
        node.log.write(f"chaos: {fault.name} reverted")
    reverted = clock.now()
    while clock.now() - reverted <= recover_timeout:
        if take("recovery").state == "available":
            outcome.recovered_after = round(clock.now() - reverted)
            break
        clock.sleep(every)
    log_after = read_daemon_log(node)
    new_log = log_after[len(log_before):] if log_after.startswith(log_before) else log_after
    outcome.republish_lines = count_matches(REPUBLISH_RE, new_log)
    outcome.tor_no_relay = TOR_NO_RELAY_RE.findall(new_log)
    outcome.findings = findings(fault, outcome, address)
    return outcome
