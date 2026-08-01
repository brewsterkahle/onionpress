#!/bin/bash
# OnionPress security audit + emergency patch.
#
# Runs on every container start, after multisite init. Two jobs:
#
#   1. PATCH  — pull WordPress core up to the latest security release now,
#               rather than waiting for the twice-daily wp_version_check
#               cron. Users may have been stranded on a vulnerable core for
#               weeks (see below), so the first boot after upgrading must
#               not leave them exposed for another 12 hours.
#
#   2. AUDIT  — look for evidence that the box was already compromised
#               before we patched it. Patching does not evict an attacker
#               who is already resident.
#
# Why this exists: builds through v2.4.107 shipped
# AUTOMATIC_UPDATER_DISABLED=true + WP_AUTO_UPDATE_CORE=false, and the
# compose file pins the WordPress image by digest. Together those left
# installs with no route to a core security release. wp2shell
# (CVE-2026-63030 + CVE-2026-60137) is an unauthenticated RCE against
# WordPress 6.9.0-6.9.4 and 7.0.0-7.0.1, added to CISA KEV on 2026-07-21
# and exploited in the wild. Affected OnionPress users could not receive
# 7.0.2 even though WordPress.org force-pushed it.
#
# Remediation policy is deliberately tiered by false-positive risk:
#   - auto-quarantine  ONLY exact known-malicious file hashes (no FP risk)
#   - report-only      heuristics (code patterns, suspicious dirs) and
#                      anything involving user accounts — never delete a
#                      user's own content or logins unattended
# Anything report-only is written to the report file and logged loudly for
# the human to action.

set -uo pipefail   # NOT -e: an audit failure must never block startup

REPORT=/var/lib/onionpress/security-report.txt
QUARANTINE=/var/lib/onionpress/quarantine
WPROOT=/var/www/html
WP="wp --allow-root --path=$WPROOT"

log()  { echo "[security-audit] $*"; }
warn() { echo "[security-audit] !! $*"; }

findings=0
record() {
    findings=$((findings + 1))
    warn "$1"
    printf '%s\n' "$1" >> "$REPORT"
}

mkdir -p "$(dirname "$REPORT")" "$QUARANTINE" 2>/dev/null || true
: > "$REPORT" 2>/dev/null || true
printf 'OnionPress security audit — %s\n\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$REPORT" 2>/dev/null || true

# Nothing to do if WordPress isn't installed yet; multisite-init exits the
# same way on first boot and we'll be re-run on the next start.
if ! $WP core is-installed >/dev/null 2>&1; then
    log "WordPress not installed yet — skipping"
    exit 0
fi

# ---------------------------------------------------------------- PATCH
current=$($WP core version 2>/dev/null || echo unknown)
log "WordPress core version: $current"

# --minor keeps us on the current major (7.0.x -> 7.0.2) which is where the
# security backports land. A major jump unattended could break multisite or
# the bundled mu-plugins.
if $WP core check-update --minor --field=version >/dev/null 2>&1 \
        && [ -n "$($WP core check-update --minor --field=version 2>/dev/null)" ]; then
    target=$($WP core check-update --minor --field=version 2>/dev/null | head -1)
    log "security update available: $current -> $target — applying now"
    if $WP core update --minor >/dev/null 2>&1; then
        # Multisite stores schema version per-network; core update alone
        # leaves the DB half-migrated and the admin nags forever.
        $WP core update-db --network >/dev/null 2>&1 || \
            $WP core update-db >/dev/null 2>&1 || true
        log "core updated to $($WP core version 2>/dev/null) and DB migrated"
    else
        record "AUTO-UPDATE FAILED: still on $current, wanted $target. Update manually: docker exec onionpress-wordpress wp core update --minor --allow-root"
    fi
else
    log "core is up to date"
fi

# ---------------------------------------------------------------- AUDIT

# 1. Core file integrity. wp2shell drops its payload as a plugin rather
#    than patching core, so this is a broad tamper check, not a wp2shell
#    signature. wp-config-docker.php is shipped by the upstream image and
#    is expected here.
checksum_out=$($WP core verify-checksums 2>&1 | grep -v 'wp-config-docker.php')
if echo "$checksum_out" | grep -qE '^(Warning|Error)'; then
    record "CORE FILE INTEGRITY FAILED:"
    printf '%s\n' "$checksum_out" | grep -E '^(Warning|Error)' >> "$REPORT" 2>/dev/null || true
    printf '%s\n' "$checksum_out" | grep -E '^(Warning|Error)' | while read -r l; do warn "  $l"; done
fi

# 2. Known-malicious file hashes (Wiz, wp2shell campaign). Exact-match, so
#    quarantining is safe to do unattended.
KNOWN_BAD="2a1410d8e2a8337ac2171cedea8c0fdc47c647a0
58eca847e9eae9e6b08cc211f1559817b71bc4cc
ebea44890f434d5d67ede22009a3f4bb5cac33f8
d9a220c8039f1c4d72cae7ccb8b3a33dec8815be
e9756e2338f84746007235e4cab7a70d5b3ca47f"
while read -r sum path; do
    [ -z "${sum:-}" ] && continue
    if printf '%s\n' "$KNOWN_BAD" | grep -qi "^${sum}$"; then
        dest="$QUARANTINE/$(basename "$path").$sum"
        if mv "$path" "$dest" 2>/dev/null; then
            chmod 000 "$dest" 2>/dev/null || true
            record "QUARANTINED known wp2shell webshell: $path (sha1 $sum) -> $dest"
        else
            record "KNOWN WEBSHELL PRESENT but could not quarantine: $path (sha1 $sum) — DELETE THIS FILE"
        fi
    fi
done < <(find "$WPROOT" -name '*.php' -size -200k -exec sha1sum {} + 2>/dev/null)

# 3. Webshell code patterns. Heuristic — report only. Our own
#    __op_proxy.php legitimately forwards requests, so it is excluded by
#    path rather than by pattern.
pattern='eval\(\s*\$_(POST|GET|REQUEST)|eval\(\s*gzuncompress|eval\(\s*base64_decode|\$_GET\[.c.\]\s*\)|assert\(\s*\$_|shell_exec\(\s*\$_|passthru\(\s*\$_|system\(\s*\$_'
while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    case "$hit" in
        "$WPROOT/__op_proxy.php") continue ;;
    esac
    record "SUSPICIOUS CODE PATTERN (possible webshell, verify by hand): $hit"
done < <(grep -rlE "$pattern" "$WPROOT/wp-content" "$WPROOT"/*.php --include='*.php' 2>/dev/null)

# 4. Attacker plugin directories: <plausible-name>-<6 hex>/<same>.php
while IFS= read -r d; do
    [ -z "$d" ] && continue
    record "SUSPICIOUS PLUGIN DIRECTORY (wp2shell drop pattern): wp-content/plugins/$d"
done < <(ls -1 "$WPROOT/wp-content/plugins" 2>/dev/null | grep -E -- '-[0-9a-f]{6}$')

# 5. Rogue administrator accounts. Report only — never delete a login
#    unattended; a false positive would lock the owner out of their site.
#    Uses `wp user list` rather than `wp db query`: the upstream WordPress
#    image ships no mysql client binary, so `wp db query` always fails here.
while IFS=$'\t' read -r ulogin uemail; do
    [ -z "${ulogin:-}" ] && continue
    if printf '%s' "$ulogin" | grep -qE '^(wpsvc_|wp2_|w2s_)[0-9a-f]+$' \
            || printf '%s' "${uemail:-}" | grep -qE '@(wp2shell|shellcode|wordpress-svc\.internal|wordpress-noreply\.net|x\.lol)'; then
        record "ROGUE ADMIN ACCOUNT matching wp2shell naming: '$ulogin' <${uemail:-}> — remove with: wp user delete '$ulogin' --network --allow-root"
    fi
done < <($WP user list --network --fields=user_login,user_email --format=tsv 2>/dev/null \
         || $WP user list --fields=user_login,user_email --format=tsv 2>/dev/null)

# 6. Attacker-registered REST namespace (variant 3 of the campaign).
if $WP eval 'echo implode(",", array_keys(rest_get_server()->get_namespaces()));' 2>/dev/null | tr ',' '\n' | grep -qiE '^morning/'; then
    record "ATTACKER REST NAMESPACE registered (wp2shell variant 3): 'morning/v1' — a webshell plugin is active"
fi

# ---------------------------------------------------------------- REPORT
if [ "$findings" -eq 0 ]; then
    log "no indicators of compromise found"
    printf 'No indicators of compromise found.\n' >> "$REPORT" 2>/dev/null || true
else
    warn "=================================================="
    warn "$findings SECURITY FINDING(S) — review $REPORT"
    warn "Full guidance: https://github.com/brewsterkahle/onionpress/security"
    warn "=================================================="
    printf '\n%s finding(s). If any webshell or rogue admin is listed, treat the\n' "$findings" >> "$REPORT" 2>/dev/null || true
    printf 'site as compromised: rotate wp-config salts, change all passwords,\n'   >> "$REPORT" 2>/dev/null || true
    printf 'and restore content from a backup predating the compromise.\n'          >> "$REPORT" 2>/dev/null || true
fi

exit 0
