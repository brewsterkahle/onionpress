"""The site every tier starts from: one install per node, started, set up.

T0 builds it step by step as its own checked tests. A later tier run on
its own (`e2e/run.sh --tier t1`) gets the same site from `ensure_ready`,
and a run of both takes T0's: the node is shared by every module of a
session, so whatever T0 did is not done again.
"""

import secrets

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
