"""What this host can test (docs/E2E-TESTING.md §2).

`probe_host()` gathers facts about the machine; `offers()` turns them into
one Offer per (platform, node kind): available or not, and why. The run
tests only available offers and lists every other platform under "Not
tested" with its reason, so a run on one OS never reads as covering
another.

Both functions take their inputs as arguments (with real defaults), so
the unit tests can probe a fake Mac or a fake Linux box.
"""

import os
import platform as _platform
import shutil
from dataclasses import dataclass, field

# The platforms a run can report on, in report column order.
PLATFORMS = ("macos", "debian")

PLATFORM_LABELS = {"macos": "macOS", "debian": "Debian 13"}

# Node kinds per platform, in order of preference.
KINDS = {
    "macos": ("mac",),
    "debian": ("linux-vm", "linux-user", "linux-host"),
}

# Kinds the harness can drive today; the others are designed (§2) but not
# built yet, and are reported as such.
IMPLEMENTED_KINDS = frozenset({"mac", "linux-vm"})

# Colima's vz VMs need macOS 13.
MIN_MACOS = (13, 0)


@dataclass
class HostFacts:
    os: str                 # "darwin" | "linux" | other sys.platform values
    os_version: str         # "27.0", or /etc/os-release's VERSION_ID
    arch: str               # "arm64" | "x86_64" | ...
    distro: str = ""        # /etc/os-release ID on Linux ("debian", "ubuntu", ...)
    kvm: bool = False       # /dev/kvm usable (Linux)
    tools: dict = field(default_factory=dict)   # name -> path or None

    @property
    def label(self):
        if self.os == "darwin":
            return f"macOS {self.os_version} {self.arch}"
        if self.os == "linux":
            return f"{self.distro or 'Linux'} {self.os_version} {self.arch}".strip()
        return f"{self.os} {self.arch}"


@dataclass
class Offer:
    platform: str
    kind: str
    available: bool
    reason: str = ""        # why not, when not available


def _read_os_release(path="/etc/os-release"):
    facts = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    facts[k] = v.strip().strip('"')
    except OSError:
        pass
    return facts


def find_limactl(which=shutil.which, mac_app=None, exists=os.path.isfile):
    """limactl on PATH, else the one inside a Mac OnionPress.app (it ships
    Lima 2.x with its templates, debian-13 among them)."""
    found = which("limactl")
    if found:
        return found
    if mac_app:
        bundled = os.path.join(mac_app, "Contents", "Resources", "bin", "limactl")
        if exists(bundled):
            return bundled
    return None


def probe_host(*, sys_platform=None, mac_ver=None, machine=None, os_release=None,
               which=shutil.which, kvm_ok=None, mac_app=None):
    sys_platform = sys_platform or _platform.system().lower()
    machine = machine or _platform.machine()
    arch = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine.lower(), machine.lower())
    tools = {"limactl": find_limactl(which, mac_app), "qemu-system": None}
    if sys_platform == "darwin":
        version = mac_ver if mac_ver is not None else _platform.mac_ver()[0]
        return HostFacts("darwin", version, arch, tools=tools)
    if sys_platform == "linux":
        rel = os_release if os_release is not None else _read_os_release()
        if kvm_ok is None:
            kvm_ok = os.access("/dev/kvm", os.R_OK | os.W_OK)
        qemu = f"qemu-system-{'aarch64' if arch == 'arm64' else arch}"
        tools["qemu-system"] = which(qemu)
        return HostFacts("linux", rel.get("VERSION_ID", ""), arch,
                         distro=rel.get("ID", ""), kvm=bool(kvm_ok), tools=tools)
    return HostFacts(sys_platform, "", arch, tools=tools)


def _version_tuple(text):
    parts = []
    for p in (text or "").split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def offers(facts):
    """Every (platform, kind) this harness knows, available or not."""
    out = []

    # macOS: only on a Mac (macOS cannot be virtualised elsewhere).
    if facts.os != "darwin":
        out.append(Offer("macos", "mac", False,
                         "not a Mac host (macOS cannot run in a VM on other hardware)"))
    elif _version_tuple(facts.os_version) < MIN_MACOS:
        out.append(Offer("macos", "mac", False,
                         f"macOS {facts.os_version} is older than "
                         f"{'.'.join(map(str, MIN_MACOS))} (Colima's vz VMs)"))
    else:
        out.append(Offer("macos", "mac", True))

    # Debian in a VM: Lima on a Mac (vz), or Lima + QEMU + KVM on Linux.
    limactl = facts.tools.get("limactl")
    if facts.os == "darwin":
        out.append(Offer("debian", "linux-vm", bool(limactl),
                         "" if limactl else "no limactl (install Lima, or pass --mac-app with "
                         "an OnionPress.app, which bundles it)"))
    elif facts.os == "linux":
        missing = [n for n, ok in (("limactl", limactl), ("/dev/kvm", facts.kvm),
                                   ("qemu-system", facts.tools.get("qemu-system"))) if not ok]
        out.append(Offer("debian", "linux-vm", not missing,
                         "missing " + ", ".join(missing) if missing else ""))
    else:
        out.append(Offer("debian", "linux-vm", False, f"unsupported host OS {facts.os}"))

    # Debian without a VM: only on a Debian host, and not built yet.
    for kind in ("linux-user", "linux-host"):
        if facts.os == "linux" and facts.distro == "debian":
            out.append(Offer("debian", kind, False, f"{kind} nodes are not implemented yet"))
        else:
            out.append(Offer("debian", kind, False, "needs a Debian host"))
    return out


def choose(offer_list, wanted=None):
    """{platform: Offer} for the platforms this run tests (the first
    available kind each), and {platform: reason} for the others.

    wanted: platforms asked for with --platforms (None: all). A platform
    not wanted is listed as not tested "not selected"."""
    chosen, not_tested = {}, {}
    for plat in PLATFORMS:
        mine = [o for o in offer_list if o.platform == plat]
        if wanted is not None and plat not in wanted:
            not_tested[plat] = "not selected (--platforms)"
            continue
        usable = [o for o in mine if o.available and o.kind in IMPLEMENTED_KINDS]
        if usable:
            chosen[plat] = usable[0]
        else:
            reasons = [f"{o.kind}: {o.reason}" for o in mine if o.reason]
            not_tested[plat] = "; ".join(reasons) or "no node kind available"
    return chosen, not_tested


def describe(offer_list):
    """Lines for `run.sh --probe`."""
    lines = []
    for plat in PLATFORMS:
        lines.append(f"{PLATFORM_LABELS[plat]}:")
        for o in offer_list:
            if o.platform != plat:
                continue
            state = "available" if o.available else "not available"
            lines.append(f"  {o.kind:<11} {state}" + (f" — {o.reason}" if o.reason else ""))
    return lines
