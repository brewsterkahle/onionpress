"""The site's onion address as the tor container serves it (issue #478).

C Tor writes the site's address to its onion service's hostname file in
the tor container. This module is the one reader of that file for the app,
with one rule: what it returns is one well-formed v3 address
(key_manager.is_valid_onion_address) and nothing around it, or "". The
address goes into site URLs, backup file names and metadata, probe URLs,
control-port commands and the browser extension, so a damaged or planted
hostname file reads as no address at all.

Two ways in, for the two kinds of caller:
  - read_served_address(docker): through an onionpress.docker.Docker (or
    anything with its exec());
  - read_served_address_with(docker_bin): a plain subprocess, for callers
    that hold only a docker binary (the menubar's setup, setup_logic,
    multisite).
Both raise what docker raises; callers keep their own handling.

key_manager.extract_keys reads the same file to recover a public key, and
the standalone native messaging host (started by the browser, outside this
package) keeps its own copy of the rule; tests/test_strict_onion_addresses.py
has a census of every reader.
"""

import subprocess

from .key_manager import is_valid_onion_address

TOR_CONTAINER = "onionpress-tor"
HOSTNAME_FILE = "/var/lib/tor/hidden_service/wordpress/hostname"


def exec_args() -> list:
    """`docker` arguments that print the hostname file."""
    return ["exec", TOR_CONTAINER, "cat", HOSTNAME_FILE]


def served_address(text) -> str:
    """The address in the hostname file's *text*, or "" unless it is one
    well-formed v3 address."""
    addr = (text or "").strip()
    return addr if is_valid_onion_address(addr) else ""


def read_hostname_text(docker, *, timeout: int = 10, quiet: bool = True) -> str:
    """The hostname file's content, stripped, unchecked; "" when it could
    not be read. Only for a caller that reports a damaged file (health)."""
    result = docker.exec(TOR_CONTAINER, ["cat", HOSTNAME_FILE],
                         timeout=timeout, quiet=quiet)
    return result.output.strip() if result.ok else ""


def read_served_address(docker, *, timeout: int = 10, quiet: bool = True) -> str:
    """The served address through *docker* (a Docker), or ""."""
    return served_address(read_hostname_text(docker, timeout=timeout, quiet=quiet))


def read_served_address_with(docker_bin: str = "docker", *, env=None,
                             timeout: int = 10) -> str:
    """The served address through the *docker_bin* binary, or ""."""
    r = subprocess.run(
        [docker_bin] + exec_args(),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )
    return served_address(r.stdout) if r.returncode == 0 else ""
