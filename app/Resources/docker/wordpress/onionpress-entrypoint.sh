#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OnionPress: Copyright (C) 2026 Internet Archive and OnionPress contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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

# This container's default gateway from the kernel's routing table (hex,
# host byte order), for onionpress-install-page.conf: the only source but
# loopback that may reach WordPress's installer (issue #464). The same
# parse as onionpress-auto-login.php's onionpress_auto_login_gateway().
# Each field is checked before the arithmetic sees it. Nothing found: the
# variable is empty and the conf admits loopback only.
gateway_from_route() {
    local table="${1:-/proc/net/route}" iface dest gw flags rest
    [ -r "$table" ] || return 1
    while read -r iface dest gw flags rest; do
        [ "$dest" = "00000000" ] || continue
        [ "$gw" != "00000000" ] || continue
        [[ "$gw" =~ ^[0-9A-Fa-f]{8}$ ]] || continue
        [[ "$flags" =~ ^[0-9A-Fa-f]{1,4}$ ]] || continue
        [ $(( 16#$flags & 3 )) -eq 3 ] || continue
        printf '%d.%d.%d.%d\n' "$(( 16#${gw:6:2} ))" "$(( 16#${gw:4:2} ))" \
            "$(( 16#${gw:2:2} ))" "$(( 16#${gw:0:2} ))"
        return 0
    done < "$table"
    return 1
}
ONIONPRESS_WP_GATEWAY=$(gateway_from_route) || ONIONPRESS_WP_GATEWAY=""
export ONIONPRESS_WP_GATEWAY

# Hand off to the standard WordPress entrypoint
exec docker-entrypoint.sh "$@"
