"""The safety guard and the tripwire (docs/E2E-TESTING.md §1).

1. Never the live install: every node lives under a scratch root outside
   the real home, gets an environment built from nothing (no inherited
   DOCKER_HOST, COLIMA_HOME, ...), and is refused the moment it answers
   with the live install's onion address. A Mac node's daemon runs under
   the label the product derives from its data dir (#505,
   daemon_client.agent_label), never the live install's
   `org.onionpress.daemon`; the node refuses to start while its own label
   is loaded for anything outside the scratch root, or if its data dir
   would get the bare label.
2. Nothing reaches a public OnionPress service: every node's config is
   written before its first start (BASELINE_CONFIG), and at teardown the
   tripwire looks for the canonical OnionHeaven hub and OnionHome
   addresses in everything the node logged.
"""

import os
import re
import sys

from . import PROJECT_DIR

# The default scratch root. Short on purpose: Colima/Lima put Unix
# sockets several levels below it (104-character limit on macOS).
DEFAULT_SCRATCH_ROOT = "/tmp/ope2e"
MAX_SCRATCH_ROOT_LEN = 40

# OnionPress's launchd label for the account's own ~/.onionpress
# (src/onionpress/daemon_client.py AGENT_LABEL): the live install's.
DAEMON_AGENT_LABEL = "org.onionpress.daemon"

# Written to ~/.onionpress/config before a node's first start. OnionPress
# creates the file only when it is missing, so these hold.
BASELINE_CONFIG = {
    # On by default: the heartbeat would ship this node's onion key to the
    # canonical hub. A tier that tests OnionHeaven points it at its own hub.
    "REGISTER_WITH_ONIONHEAVEN": "no",
    "SHARE_ANALYTICS_WITH_ONIONHOME": "no",
    # The Link Fixer submits links in posts to the public Wayback Machine.
    "INSTALL_IA_PLUGIN": "no",
    # A Mac node must not install a login item.
    "LAUNCH_ON_LOGIN": "no",
}

_ONION = r"[a-z2-7]{56}\.onion"


class UnsafeTarget(Exception):
    """The run would touch something it must not; nothing was started."""


def _source_constant(relpath, name):
    with open(os.path.join(PROJECT_DIR, relpath), encoding="utf-8") as f:
        text = f.read()
    m = re.search(rf'"?{re.escape(name)}"?\s*[:=]\s*"({_ONION})"', text)
    if not m:
        raise RuntimeError(f"{name} not found in {relpath}")
    return m.group(1)


def public_service_addresses():
    """{what: address} of the public services no test node may contact,
    read from the source so they cannot drift from the product's."""
    return {
        "the canonical OnionHeaven hub": _source_constant(
            "src/onionpress/onionheaven.py", "DEFAULT_ONIONHEAVEN_ADDRESS"),
        "OnionHome": _source_constant("src/onionpress/config.py", "ONIONHOME_ADDRESS"),
    }


def check_scratch_root(root, real_home):
    """The scratch root's real path, or UnsafeTarget."""
    if not os.path.isabs(root):
        raise UnsafeTarget(f"scratch root {root!r} is not an absolute path")
    if len(root) > MAX_SCRATCH_ROOT_LEN:
        raise UnsafeTarget(f"scratch root {root!r} is longer than {MAX_SCRATCH_ROOT_LEN} "
                           "characters (Colima and Lima sockets live below it)")
    if any(c.isspace() for c in root):
        raise UnsafeTarget(f"scratch root {root!r} contains whitespace (Colima socket paths)")
    real = os.path.realpath(root)
    home = os.path.realpath(real_home)
    if real == home or real.startswith(home + os.sep) or home.startswith(real + os.sep):
        raise UnsafeTarget(f"scratch root {real} overlaps the real home {home}")
    return real


def live_onion_address(real_home):
    """The live install's cached onion address on this machine, or ''."""
    try:
        with open(os.path.join(real_home, ".onionpress", "onion_address"), encoding="utf-8") as f:
            value = f.read().strip()
    except OSError:
        return ""
    return value if re.fullmatch(_ONION, value) else ""


def check_not_live(address, live_address):
    if live_address and address == live_address:
        raise UnsafeTarget(f"a test node answered with the live install's address {address}: "
                           "it reached the live install. Stopping.")


def node_agent_label(data_dir):
    """The daemon's launchd label for a node whose data dir is *data_dir*:
    the product's own rule (daemon_client.agent_label, issue #505), so it
    cannot drift from what the node's daemon registers. UnsafeTarget if
    that is the live install's bare label (the data dir is the account's
    own ~/.onionpress)."""
    src = os.path.join(PROJECT_DIR, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from onionpress import daemon_client
    label = daemon_client.agent_label(data_dir)
    if label == DAEMON_AGENT_LABEL:
        raise UnsafeTarget(f"data dir {data_dir} gets the live install's launchd label "
                           f"{DAEMON_AGENT_LABEL}")
    return label


def check_daemon_agent(launchctl_print_output, rc, scratch_root, label=DAEMON_AGENT_LABEL):
    """UnsafeTarget when the gui-domain agent *label* is loaded for a
    program outside scratch_root (the live install's, or a leftover of
    another run). `launchctl print` output and its exit code are passed
    in; rc != 0 means not loaded."""
    if rc != 0:
        return
    program = ""
    for line in launchctl_print_output.splitlines():
        line = line.strip()
        if line.startswith("program = "):
            program = line.split("=", 1)[1].strip()
            break
    if not program:
        # Arguments-only agents print the first argument on its own line.
        m = re.search(r"arguments = \{\s*(\S+)", launchctl_print_output)
        program = m.group(1) if m else ""
    root = os.path.realpath(scratch_root)
    if program and os.path.realpath(program).startswith(root + os.sep):
        return
    raise UnsafeTarget(
        f"launchd agent {label} is loaded for {program or 'an unknown program'}. "
        "A Mac test node would take over that agent (or it ours). Remove that agent, "
        "or use another scratch root.")


def baseline_config_text(extra=None):
    values = dict(BASELINE_CONFIG)
    values.update(extra or {})
    lines = ["# Written by the OnionPress e2e harness before the first start."]
    lines += [f"{k}={v}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def tripwire_scan(paths, addresses=None):
    """[(path, line number, what)] for every mention of a public service
    address in the given files (directories are walked)."""
    addresses = addresses or public_service_addresses()
    hits = []
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirs, names in os.walk(p):
                files += [os.path.join(dirpath, n) for n in sorted(names)]
        elif os.path.isfile(p):
            files.append(p)
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    for what, addr in addresses.items():
                        if addr in line or addr[:16] in line:
                            hits.append((path, n, what))
        except OSError:
            continue
    return hits
