"""The site every tier starts from: one install per node, started, set up.

T0 builds it step by step as its own checked tests. A later tier run on
its own (`e2e/run.sh --tier t1`) gets the same site from `ensure_ready`,
and a run of both takes T0's: the node is shared by every module of a
session, so whatever T0 did is not done again.
"""

import secrets

from .proc import wait_until

ADMIN_USER = "op-e2e-admin"
# One password per run, never written anywhere but the node; the harness
# log replaces it with ***.
ADMIN_PASSWORD = secrets.token_urlsafe(18)


def ensure_ready(e2e, node):
    """*node* installed, serving an onion address that is not the live
    install's, and WordPress set up with the run's admin. Each step only
    when T0 (or an earlier call) has not done it."""
    e2e.wait_installed(node)
    if not node.address:
        node.start()
        node.address = e2e.verify_address(node, node.wait_for_address())
    if not node.first_key_digest:
        node.first_key_digest = node.served_key_digest()
    node.wait_for_local_site()
    if not node.wp_admin_user:
        node.setup_wordpress(ADMIN_USER, ADMIN_PASSWORD)
    return node


TOR_PAGE_TIMEOUT = 300        # a page right after the health file answered


def tor_page(node, visitor, path, *, timeout=TOR_PAGE_TIMEOUT, every=10):
    """*visitor*'s fetch of *path* on *node*'s address over Tor, retried
    while no answer comes back. A request can time out on the circuit that
    served the health file seconds before (seen once on Ubuntu right after
    setup: the request never reached Apache), so only an answer counts;
    its status is the caller's to judge. A fetch that needed retries is
    noted in the report."""
    attempts = []

    def fetch():
        attempts.append(path)
        return visitor.tor_fetch(node.address, path, timeout=90)

    resp = wait_until(fetch, timeout=timeout, every=every,
                      label=f"{node.name}: {path} over Tor (visitor {visitor.name})",
                      timings=node.timings, log=node.log)
    if len(attempts) > 1:
        node.notes.append(f"{path} over Tor answered only after {len(attempts) - 1} "
                          f"failed attempts (the health file had answered)")
    return resp
