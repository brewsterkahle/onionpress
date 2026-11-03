"""T1 (a) — the hardening holds on live containers (docs/E2E-TESTING.md §6,
"Security properties on live containers").

Read-only: nothing here changes the node, so it runs first among T1's
modules and needs no Tor waits. Each check stands alone (not
incremental): one rule broken says nothing about the others.

- WordPress resolves no name outside Docker (`dns: 127.0.0.1`), and
  neither does MariaDB; container names still resolve.
- WordPress reaches nothing outside Docker (issue #144's ONIONPRESS-WP
  chain): not the internet, not the host's published port. MariaDB sits
  on the same bridge under the same rule, so WordPress stands for both.
  The tor container, which must reach the internet (the positive
  control), reaches no host service either (ONIONPRESS-TOR).
- Every container drops every capability and adds back only the census's
  set for its service, with no-new-privileges (harness.hardening, over
  tests/test_container_capabilities.py).
- Only autoheal-proxy and onionheaven-farm mount the Docker socket,
  read-only.
- The data dir is the user's alone (issue #397).
"""

import json

import pytest

from harness import hardening

pytestmark = [pytest.mark.tier("t1"), pytest.mark.platform("any")]

# An internet address that answers plain HTTP: the positive control and
# the address WordPress must not reach. Only a GET, nothing written.
INTERNET = "1.1.1.1"
# On a Mac, Lima maps this address in the VM to the Mac's own loopback,
# where Colima forwards WordPress's published port (wordpress_egress.py).
LIMA_HOST_ALIAS = "192.168.5.2"
CONNECT_TIMEOUT = 8


def _connect(node, container, host, port):
    """(connected, detail): whether *container* opened a TCP connection to
    host:port and got an HTTP answer. curl's exit status alone can't tell
    a refusal by the firewall from a closed port, so the published port is
    used as the host target: the node's own request to it is answered
    (wait_for_local_site), so anything short of an answer here is the
    rule."""
    r = node.docker(["exec", container, "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", str(CONNECT_TIMEOUT), f"http://{host}:{port}/"],
                    timeout=CONNECT_TIMEOUT + 30)
    # No curl (or no container) would read as "could not connect".
    assert r.rc not in (125, 126, 127), f"could not run curl in {container}: {r.describe()}"
    code = r.out.strip()
    return (r.ok and code not in ("", "000")), f"rc {r.rc}, http {code or '-'}: {r.err.strip()[-200:]}"


def _host_targets(node, container):
    """Where *container* would find the node's own services: the default
    gateway of its network (the Docker host), and on a Mac the Lima
    alias of the Mac's loopback."""
    route = node.docker(["exec", container, "cat", "/proc/net/route"])
    targets = []
    gateway = hardening.default_gateway(route.out) if route.ok else ""
    if gateway:
        targets.append(gateway)
    if node.platform == "macos":
        targets.append(LIMA_HOST_ALIAS)
    assert targets, f"no host address found for {container}: {route.describe()}"
    return targets


def _inspect_all(node):
    names = node.docker(["ps", "--format", "{{.Names}}"]).out.split()
    assert names, "no running containers"
    r = node.docker(["inspect", *names], timeout=60)
    assert r.ok, r.describe()
    return json.loads(r.out)


def test_no_name_resolves_outside_docker(ready_node):
    node = ready_node
    for container, peer in (("onionpress-wordpress", "db"), ("onionpress-db", "wordpress")):
        outside = node.docker(["exec", container, "getent", "hosts", "example.com"])
        assert not outside.ok and not outside.out.strip(), \
            f"{container} resolved example.com: {outside.out.strip()!r}"
        inside = node.docker(["exec", container, "getent", "hosts", peer])
        assert inside.ok and inside.out.strip(), f"{container} cannot resolve {peer}: {inside.describe()}"


def test_wordpress_reaches_nothing_outside_docker(ready_node):
    node = ready_node
    connected, detail = _connect(node, "onionpress-tor", INTERNET, 80)
    assert connected, f"the tor container cannot reach the internet, so this proves nothing: {detail}"
    connected, detail = _connect(node, "onionpress-wordpress", INTERNET, 80)
    assert not connected, f"WordPress reached {INTERNET}:80 ({detail})"
    for host in _host_targets(node, "onionpress-wordpress"):
        connected, detail = _connect(node, "onionpress-wordpress", host, node.wp_port)
        assert not connected, f"WordPress reached the host's published port at {host}:{node.wp_port} ({detail})"


def test_tor_side_reaches_no_host_service(ready_node):
    node = ready_node
    for host in _host_targets(node, "onionpress-tor"):
        connected, detail = _connect(node, "onionpress-tor", host, node.wp_port)
        assert not connected, f"the tor container reached the host at {host}:{node.wp_port} ({detail})"


def test_capabilities_as_the_census_says(ready_node):
    census, never = hardening.load_capability_census()
    findings = hardening.capability_findings(_inspect_all(ready_node), census, never)
    assert not findings, "\n".join(findings)


def test_docker_socket_only_where_allowed(ready_node):
    docs = _inspect_all(ready_node)
    findings = hardening.socket_findings(docs)
    assert not findings, "\n".join(findings)
    mounting = {hardening.service_of(d) for d in docs
                if any(str(m.get("Destination", "")).endswith("docker.sock") for m in d.get("Mounts") or ())}
    assert mounting, "no container mounts the socket: autoheal-proxy and onionheaven-farm should"


@pytest.mark.parametrize("rel, mode", [(".onionpress", "700"),
                                       (".onionpress/config", "600"),
                                       (".onionpress/secrets", "600")])
def test_data_dir_is_the_users(ready_node, rel, mode):
    path = ready_node.home_path(rel)
    r = ready_node.exec(["find", path, "-prune", "-perm", mode])
    listing = ready_node.exec(["ls", "-ld", path]).out.strip()
    assert r.ok and r.out.strip() == path, f"{rel} is not {mode}: {listing}"
