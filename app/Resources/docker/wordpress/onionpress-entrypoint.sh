#!/bin/bash
# OnionPress WordPress entrypoint wrapper.
# Runs the multisite init in the background (after a delay for DB to be ready),
# then hands off to the standard WordPress entrypoint.

# Run multisite init in background after WordPress and DB are up, then the
# security audit. The audit runs second and unconditionally: it needs the
# auto-update constants that multisite-init writes, and it must still run
# on installs where multisite-init takes its "already converted" early exit.
# First, www-data gets back what root's wp-cli left in the web root (both
# scripts run wp-cli as www-data, which must be able to replace it).
(
    sleep 15  # Wait for WordPress + MariaDB to be ready
    /usr/local/bin/onionpress-web-root-owner.sh 2>&1 | while read -r line; do
        echo "[web-root-owner] $line"
    done
    /usr/local/bin/onionpress-multisite-init.sh 2>&1 | while read -r line; do
        echo "[multisite-init] $line"
    done
    /usr/local/bin/onionpress-security-audit.sh 2>&1 | while read -r line; do
        echo "$line"
    done
) &

# Hand off to the standard WordPress entrypoint
exec docker-entrypoint.sh "$@"
