"""What T1a reads off a live node's containers, as plain functions over
`docker inspect` output, so the unit suite checks them without Docker
(tests/test_e2e_harness.py).

The rules are the product's (CLAUDE.md): every compose service drops every
capability and adds back only what tests/test_container_capabilities.py
lists for it, with no-new-privileges; only autoheal-proxy and
onionheaven-farm mount the Docker socket, read-only. The census is
imported from that test file, never copied, so the two cannot drift.
"""

import importlib.util
import os

from . import PROJECT_DIR

COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
SOCKET_SERVICES = frozenset({"autoheal-proxy", "onionheaven-farm"})


def load_capability_census():
    """{service: set of capabilities added back} for every compose service
    in tests/test_container_capabilities.py's COMPOSE_CAPS (both compose
    files), and its NEVER set."""
    path = os.path.join(PROJECT_DIR, "tests", "test_container_capabilities.py")
    spec = importlib.util.spec_from_file_location("onionpress_capability_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    services = {}
    for per_file in module.COMPOSE_CAPS.values():
        services.update(per_file)
    return services, set(module.NEVER)


def cap_names(values):
    """Docker writes capabilities with or without CAP_ (it changed across
    versions): {"CHOWN", ...}."""
    return {str(v).upper().removeprefix("CAP_") for v in values or ()}


def no_new_privileges(security_opt):
    return any(str(o).replace("=", ":").lower() == "no-new-privileges:true"
               for o in security_opt or ())


def service_of(doc):
    return ((doc.get("Config") or {}).get("Labels") or {}).get(COMPOSE_SERVICE_LABEL, "")


def capability_findings(inspect_docs, census, never):
    """What is wrong, one line per problem, in every container `docker
    inspect` described: an unknown container, a capability not dropped,
    one added back beyond its service's set or from NEVER, no
    no-new-privileges, privileged. [] when all hold."""
    found = []
    for doc in inspect_docs:
        name = (doc.get("Name") or "").lstrip("/")
        host = doc.get("HostConfig") or {}
        service = service_of(doc)
        if service not in census:
            found.append(f"{name}: not a compose service the census knows ({service or 'no label'})")
            continue
        if host.get("Privileged"):
            found.append(f"{name}: privileged")
        if "ALL" not in cap_names(host.get("CapDrop")):
            found.append(f"{name}: CapDrop is {host.get('CapDrop')!r}, not ALL")
        added = cap_names(host.get("CapAdd"))
        if added & never:
            found.append(f"{name}: adds back {sorted(added & never)}, which nothing may")
        extra = added - census[service]
        if extra:
            found.append(f"{name}: adds back {sorted(extra)} beyond the census's "
                         f"{sorted(census[service])}")
        if not no_new_privileges(host.get("SecurityOpt")):
            found.append(f"{name}: no no-new-privileges ({host.get('SecurityOpt')!r})")
    return found


def socket_findings(inspect_docs):
    """Every container mounting a Docker socket that is not one of
    SOCKET_SERVICES, or mounting it writable. [] when all hold."""
    found = []
    for doc in inspect_docs:
        name = (doc.get("Name") or "").lstrip("/")
        for mount in doc.get("Mounts") or ():
            paths = (str(mount.get("Source") or ""), str(mount.get("Destination") or ""))
            if not any(p.endswith("docker.sock") for p in paths):
                continue
            if service_of(doc) not in SOCKET_SERVICES:
                found.append(f"{name}: mounts the Docker socket ({paths[0]})")
            elif mount.get("RW"):
                found.append(f"{name}: mounts the Docker socket writable")
    return found


def default_gateway(route_text):
    """The default gateway from a container's /proc/net/route (the
    container may have no `ip`), or ""."""
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        raw = fields[2]
        if len(raw) != 8 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            continue
        octets = [int(raw[i:i + 2], 16) for i in range(0, 8, 2)]
        return ".".join(str(o) for o in reversed(octets))
    return ""
