"""T0 — stack smoke (docs/E2E-TESTING.md §6): one fresh install per
platform, from install to a site served over Tor that keeps its address
across a stop and start and a restart, with the installer the owner's
alone throughout.

One scenario per node: the steps run in order, and after a failed step
the rest are skipped on that platform (they would only fail for the same
reason).
"""

import json

import pytest

from harness.nodes.base import CORE_CONTAINERS
from harness.proc import wait_until
from harness.scenario import ADMIN_PASSWORD, ADMIN_USER

pytestmark = [pytest.mark.tier("t0"), pytest.mark.incremental, pytest.mark.platform("any")]

TOR_REACH_TIMEOUT = 1200      # a fresh descriptor can take several minutes


def _health_ok(resp):
    return resp.status == 200 and "onionpress-health" in resp.body


def _wait_reachable(e2e, node, label):
    visitor = e2e.visitor_for(node)

    def fetch():
        resp = visitor.tor_fetch(node.address, "/.onionpress-health", timeout=90)
        return resp if _health_ok(resp) else None

    wait_until(fetch, timeout=TOR_REACH_TIMEOUT, every=20,
               label=f"{node.name}: {label} over Tor (visitor {visitor.name})",
               timings=node.timings, log=node.log)
    return visitor


def test_install(node, e2e):
    """A fresh install of the build under test, its config written first."""
    e2e.wait_installed(node)
    cfg = node.config()
    assert cfg.get("REGISTER_WITH_ONIONHEAVEN") == "no"


def test_first_start(node, e2e):
    """The first start brings up tor, WordPress and the database and serves
    an onion address — never the live install's."""
    node.start()
    address = node.wait_for_address()
    e2e.verify_address(node, address)
    node.address = address
    assert node.served_hostname() == address
    states = node.container_states()
    for name in CORE_CONTAINERS:
        assert states.get(name) == "running", f"{name}: {states.get(name, 'missing')}"
    node.first_key_digest = node.served_key_digest()
    assert node.first_key_digest, "no served key in the tor container"


INSTALLER_PAGES = ("/wp-admin/install.php", "/wp-admin/setup-config.php")


def test_installer_owner_only(node):
    """Before setup the installer answers only the owner (issue #464): the
    published port gets it, a request from the tor container (where every
    Tor visitor's arrives from) gets 403."""
    node.wait_for_local_site()
    resp = node.http_local("/wp-admin/install.php")
    assert resp.status == 200, f"install.php answered the owner {resp.status}"
    for page in INSTALLER_PAGES:
        resp = node.http_from_tor(page)
        assert resp.status == 403, f"{page} answered the tor container {resp.status}"


def test_wordpress_setup(node):
    """Headless setup creates the admin, whose password logs in (and a wrong
    one does not)."""
    node.wait_for_local_site()
    node.setup_wordpress(ADMIN_USER, ADMIN_PASSWORD)
    first = []

    def logged_in():
        resp = node.wp_login(ADMIN_USER, ADMIN_PASSWORD)
        first.append(resp)
        return resp if any("wordpress_logged_in" in c for c in resp.cookies()) else None

    # Right after setup returns, a login can still be turned away (seen:
    # a bare 302 to http://localhost/ before the network settles), so it
    # is retried; a first attempt that failed is noted in the report.
    good = wait_until(logged_in, timeout=120, every=5, label=f"{node.name}: admin login",
                      timings=node.timings, log=node.log)
    assert good.status == 302, f"login answered {good.status}"
    if len(first) > 1:
        bad = first[0]
        node.notes.append(f"the first admin login after `onionpress setup` was turned away "
                          f"({bad.status} to {bad.header('location') or 'nowhere'}); "
                          f"it succeeded after {len(first) - 1} retries")
    bad = node.wp_login(ADMIN_USER, ADMIN_PASSWORD + "-wrong")
    assert not any("wordpress_logged_in" in c for c in bad.cookies())


def test_site_served_locally(node):
    """The published port serves the health file and the site."""
    assert _health_ok(node.http_local("/.onionpress-health"))
    resp = node.http_local("/")
    hops = 0
    while resp.status in (301, 302, 303, 307, 308) and hops < 3:
        location = resp.header("location")
        path = "/" + location.split("://", 1)[-1].split("/", 1)[-1] if "://" in location else location
        resp = node.http_local(path or "/")
        hops += 1
    assert resp.status == 200, f"/ answered {resp.status}"
    assert "<html" in resp.body.lower()


def test_onion_reachable(node, e2e, record_property):
    """A visitor reaches the site over the Tor network."""
    visitor = _wait_reachable(e2e, node, "health file")
    record_property("visitor", visitor.name)
    resp = visitor.tor_fetch(node.address, "/", timeout=90)
    assert resp.status in (200, 301, 302), f"/ over Tor answered {resp.status}"
    # The installer rule stays after setup (issue #464).
    for page in INSTALLER_PAGES:
        resp = visitor.tor_fetch(node.address, page, timeout=90)
        assert resp.status == 403, f"{page} over Tor answered {resp.status}"


def test_status_and_address_agree(node):
    """`onionpress status` (the CLI's status document since #529) lists the
    running containers and the address Tor serves; `address` names it too."""
    r = node.launcher(["status"], timeout=60)
    assert r.ok, r.describe()
    doc = json.loads(r.out or "{}")
    running = {row.get("Name") for row in doc.get("services", []) if row.get("State") == "running"}
    for name in CORE_CONTAINERS:
        assert name in running, f"status does not list {name} running: {sorted(running)}"
    assert doc.get("onion_address") == node.address, f"status names {doc.get('onion_address')!r}"
    assert node.onion_address() == node.address


def _same_site_back(e2e, node, label):
    """Same address, same key, nothing minted or replaced, the site back
    locally and over Tor."""
    assert node.wait_for_address() == node.address
    assert node.served_key_digest() == node.first_key_digest, "the served key changed"
    assert not node.exists(".onionpress/replaced-keys"), "a key was replaced"
    node.wait_for_local_site()
    _wait_reachable(e2e, node, label)


def test_stop_start_keeps_address(node, e2e):
    """`onionpress stop`, then `start`: the same site comes back."""
    node.stop()
    wait_until(lambda: node.container_states().get("onionpress-tor") != "running",
               timeout=300, every=5, label=f"{node.name}: tor stopped",
               timings=node.timings, log=node.log)
    node.start()
    _same_site_back(e2e, node, "after stop and start")


def test_restart_keeps_address(node, e2e):
    """`onionpress restart` (the menubar's and tray's Restart): the same
    site comes back."""
    started = node.container_started_at("onionpress-tor")
    r = node.launcher(["restart"], timeout=1800)
    assert r.ok, r.describe()
    assert node.container_started_at("onionpress-tor") != started, "tor was not restarted"
    _same_site_back(e2e, node, "after restart")


def test_no_public_service_contact(node):
    """No registration with any hub or with OnionHome, and OnionHeaven
    still off (the log tripwire at teardown checks the rest)."""
    assert node.config().get("REGISTER_WITH_ONIONHEAVEN") == "no"
    record = node.read_file(".onionpress/onionheaven-registration.json")
    if record.strip():
        data = json.loads(record)
        assert not data.get("registered"), f"registration record says registered: {data}"
    # OnionHeaven is off, so onionnames are too: the setup's onionname is
    # recorded, never registered.
    config = node.config()
    assert config.get("ONIONHOME_ADDRESS") == ""
    assert config.get("ONIONNAME_REGISTERED") != "yes", "the onionname was registered"
    assert not config.get("ONIONNAME_ADDRESS"), "the onionname was registered"

