"""T0 — what the install needed that a user should not have to do.

Its own module, so a finding here never holds up the stack scenario in
test_t0_stack.py (an incremental module skips its remaining steps after a
failed one, and pytest may run a platform-only test first)."""

import pytest

pytestmark = [pytest.mark.tier("t0")]


@pytest.mark.platform("linux")
def test_package_finished_its_own_setup(node, e2e):
    """`apt install ./onionpress.deb` leaves OnionPress running, with no
    manual step."""
    e2e.wait_installed(node)
    assert not node.install_findings, "; ".join(node.install_findings)
