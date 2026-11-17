"""T1 (b) — chaos: tor paused, its network flapped, the host with no
route and behind a captive portal (issues #139 and #266 item 3;
docs/E2E-TESTING.md §6, "Onion service health").

Each fault is put on, held, and taken off while the daemon's state is
sampled (harness.chaos, where the rules and their reasons are). Each
stands alone: the node must be "available" before a fault starts, so a
fault that left it broken skips nothing but says so in the next one's
wait. Host faults change the VM's routes and firewall: Linux only.
"""

import pytest

from harness import chaos
from harness.proc import wait_until

pytestmark = [pytest.mark.tier("t1"), pytest.mark.platform("any")]

READY_TIMEOUT = 900


def _available(node):
    sample = chaos.sample_of(node.read_file(chaos.STATE_FILE), 0, "before")
    return sample.state == "available"


def _run(node, name):
    fault = chaos.FAULTS[name]
    wait_until(lambda: _available(node), timeout=READY_TIMEOUT, every=15,
               label=f"{node.name}: available before {name}", timings=node.timings, log=node.log)
    outcome = chaos.run_fault(node, fault, node.address)
    node.notes.append(outcome.summary())
    assert not outcome.findings, "\n".join(outcome.findings)


def test_tor_paused(ready_node):
    _run(ready_node, "tor-paused")


def test_network_flap(ready_node):
    _run(ready_node, "network-flap")


@pytest.mark.platform("linux")
def test_no_route(ready_node):
    _run(ready_node, "no-route")


@pytest.mark.platform("linux")
def test_captive_portal(ready_node):
    _run(ready_node, "captive-portal")
