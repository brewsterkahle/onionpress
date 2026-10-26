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
# Every network request made here goes through the Tor SOCKS proxy via
# onionpress-core-update.php. Nothing in this script may talk to wordpress.org
# directly: a clearnet update check ties the user's real IP to an OnionPress
# install, which is the one thing this project exists to avoid.
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

# The report goes where the host can read it: /var/lib/onionpress, a folder
# www-data owns. This script runs as root, so nothing here may write through
# a name www-data could have put there: a link at security-report.txt made
# root rewrite the file it pointed to (docker-entrypoint.sh, say) with lines
# that carry file names and logins of the planter's choosing. The report is
# built in a file of root's own and published at exit (publish_report).
REPORT=/var/lib/onionpress/security-report.txt
# Quarantined files go somewhere www-data cannot reach at all: root's own
# folder in the container. In /var/lib/onionpress www-data could take them
# back, or swap the folder for a link to send root's mv anywhere.
QUARANTINE=/var/lib/onionpress-quarantine
WPROOT=/var/www/html
# wp-cli runs as www-data (onionpress-wp), never as root: it loads WordPress,
# whose plugins, themes and wp-config.php www-data can write, so as root a
# plugin bug became root here at every start. What it prints is therefore
# www-data's to choose; root only reads it, and acts on nothing it names
# but the downloaded core zip, checked by name first. The audit's own root
# work (the hash scan, quarantine, the report) is file operations only.
WP="onionpress-wp --path=$WPROOT"
# The WordPress branch this image was built and tested on (WORDPRESS_BRANCH
# in the Dockerfile). The patch step moves a site on an older branch up to
# it, never past it.
IMAGE_WP_BRANCH=$(cat /usr/local/share/onionpress/wordpress-branch 2>/dev/null || true)
HELPER=/usr/local/lib/onionpress-core-update.php

log()  { echo "[security-audit] $*"; }
warn() { echo "[security-audit] !! $*"; }

# All network work goes through the helper, which runs inside WordPress via
# `wp eval-file` and forces every request through the Tor SOCKS proxy.
#
# It exists because wp-cli's own `core check-update`, `core update` and
# `core verify-checksums` do NOT use the WordPress HTTP API — they call
# WP_CLI\Utils\http_request() and talk to api.wordpress.org directly. Verified
# by sabotaging the WP HTTP API's curl handle and watching both commands
# succeed anyway. So every startup audit used to phone wordpress.org from the
# user's real IP over clearnet, with clearnet DNS.
helper() { $WP eval-file "$HELPER" "$@" 2>/dev/null; }

# Pull one `key=value` line out of helper output. Repeated keys (mismatch=,
# missing=) are read with grep directly instead.
field() { printf '%s\n' "$1" | grep -m1 "^$2=" | cut -d'=' -f2-; }

# The container entrypoint runs us ~15s after start, which is routinely before
# Tor has a working SOCKS listener. Waiting is what keeps a cold boot from
# being reported as "up to date".
socks_ready() {
    php -r 'foreach (["onionheaven","onionpress-tor"] as $h) {
                $f = @fsockopen($h, 9050, $e, $s, 3);
                if ($f) { fclose($f); exit(0); }
            }
            exit(1);' >/dev/null 2>&1
}
wait_for_socks() {
    local waited=0
    while [ "$waited" -lt 120 ]; do
        socks_ready && return 0
        sleep 5
        waited=$((waited + 5))
    done
    return 1
}

findings=0
report_tmp=$(mktemp) || report_tmp=/dev/null
record() {
    findings=$((findings + 1))
    warn "$1"
    printf '%s\n' "$1" >> "$report_tmp"
}

# Replace the published report with ours. rm -f removes whatever is at the
# name (a link included, never its target); dd's conv=excl then creates the
# file with O_EXCL, which refuses any name that exists again by then, a
# link included, so a link planted in between is not followed either.
publish_report() {
    [ "$report_tmp" = /dev/null ] && return 0
    rm -f "$REPORT" 2>/dev/null
    dd if="$report_tmp" of="$REPORT" conv=excl status=none 2>/dev/null \
        || warn "could not write $REPORT (something else is at that name)"
    rm -f "$report_tmp"
}
trap publish_report EXIT

# Move a known-bad file into quarantine. $1 = its path as find printed it
# (under $WPROOT), $2 = its sha1. cd -P and the pwd check pin the folder
# find saw, so no link www-data swapped in among the path's folders since
# sends root's mv somewhere else; the file itself, if now a link, moves as
# a link.
quarantine() {
    local dir base
    dir=$(dirname -- "$1")
    base=$(basename -- "$1")
    ( cd -P -- "$dir" 2>/dev/null && [ "$(pwd -P)" = "$dir" ] \
        && mv -- "./$base" "$QUARANTINE/$base.$2" 2>/dev/null \
        && chmod 000 -- "$QUARANTINE/$base.$2" 2>/dev/null )
}

mkdir -p -m 700 "$QUARANTINE" 2>/dev/null || true
printf 'OnionPress security audit — %s\n\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$report_tmp" 2>/dev/null || true

# Nothing to do if WordPress isn't installed yet; multisite-init exits the
# same way on first boot and we'll be re-run on the next start.
if ! $WP core is-installed >/dev/null 2>&1; then
    log "WordPress not installed yet — skipping"
    exit 0
fi

# ---------------------------------------------------------------- PATCH
current=$($WP core version 2>/dev/null || echo unknown)
log "WordPress core version: $current"

# The helper offers the latest release of the current branch (7.0.x ->
# 7.0.3), which is where the security backports land, and of the image's
# branch when that is newer (7.0.x -> 7.1.3): a new branch comes only with
# an image whose release tested the multisite setup and the bundled plugins
# on it, never on its own.
#
# A failed check must never read as "up to date". The previous version of this
# block inferred "no update pending" from empty stdout, so any network error —
# and on a cold boot there is almost always a network error, Tor is still
# bootstrapping — silently logged "core is up to date" and moved on. That is
# the same silent-one-shot-failure class as the feeds/follow/registration
# bugs. Now every outcome is distinct: applied, genuinely current, or a
# recorded finding.
if ! wait_for_socks; then
    record "UPDATE CHECK SKIPPED: no Tor SOCKS proxy after 120s — core is still $current and security releases cannot be fetched. Check the onionheaven and onionpress-tor containers."
else
    check_out=""
    status=""
    attempt=1
    while [ "$attempt" -le 3 ]; do
        check_out=$(helper check "$IMAGE_WP_BRANCH")
        status=$(field "$check_out" status)
        [ "$status" = "ok" ] && break
        log "version check attempt $attempt/3 did not complete over Tor: $(field "$check_out" error)"
        [ "$attempt" -lt 3 ] && sleep 20
        attempt=$((attempt + 1))
    done

    if [ "$status" != "ok" ]; then
        record "UPDATE CHECK FAILED: could not reach WordPress.org over Tor after 3 attempts — core is still $current and may be missing a security release. Retry manually: docker exec -u www-data onionpress-wordpress wp eval-file $HELPER check"
    else
        target=$(field "$check_out" target)
        if [ -z "$target" ]; then
            log "core is up to date ($current)"
        else
            package=$(field "$check_out" package)
            log "core update available: $current -> $target — downloading over Tor"
            # The helper fetches only from downloads.wordpress.org and
            # checks every member of the zip against WordPress.org's
            # checksum list for $target before saying ok (issue #395).
            dl_out=$(helper download "$package" "$target")
            zip=$(field "$dl_out" path)
            if [ "$(field "$dl_out" status)" != "ok" ]; then
                record "UPDATE DOWNLOAD FAILED: $current -> $target over Tor ($(field "$dl_out" error)). Core is still $current."
            elif ! [[ "$zip" =~ ^/tmp/onionpress-wordpress-[0-9.]+(-[a-z-]+)?\.zip$ ]]; then
                # The helper runs as www-data: a path it names is not root's
                # to remove until it is the name the helper downloads to.
                record "UPDATE DOWNLOAD FAILED: $current -> $target: the download helper named an unexpected file. Core is still $current."
            else
                log "downloaded $(field "$dl_out" bytes) bytes — applying $target"
                # Pass the local zip so wp-cli does not re-fetch it over
                # clearnet; --version is what it uses to label the update.
                #
                # Success is the version WordPress reports afterwards, not
                # wp-cli's exit status. Once the update is in place, wp-cli
                # fetches wordpress.org's checksum lists to delete files the
                # old version had, with its own HTTP client (no Tor). The
                # container has no DNS outside Docker (docker-compose.yml), so
                # that fetch fails and wp-cli exits non-zero after a
                # completed update; WordPress's updater has already removed
                # the files it retires.
                $WP core update "$zip" --version="$target" >/dev/null 2>&1 || true
                if [ "$($WP core version 2>/dev/null)" = "$target" ]; then
                    # Multisite stores schema version per-network; core update
                    # alone leaves the DB half-migrated and the admin nags
                    # forever.
                    $WP core update-db --network >/dev/null 2>&1 || \
                        $WP core update-db >/dev/null 2>&1 || true
                    current=$($WP core version 2>/dev/null || echo "$target")
                    log "core updated to $current and DB migrated"
                else
                    record "AUTO-UPDATE FAILED: still on $current, wanted $target. Update manually: docker exec -u www-data onionpress-wordpress wp core update $zip --version=$target"
                fi
                # rm -f removes a link at that name, never its target.
                rm -f "$zip" 2>/dev/null || true
            fi
        fi
    fi
fi

# ---------------------------------------------------------------- AUDIT

# 1. Core file integrity. wp2shell drops its payload as a plugin rather
#    than patching core, so this is a broad tamper check, not a wp2shell
#    signature.
#
#    Two reasons this no longer uses `wp core verify-checksums`: that command
#    fetches the checksum list over clearnet (see helper), and it reports a
#    failed fetch on the same channel as a real mismatch. A DNS hiccup at boot
#    therefore wrote "CORE FILE INTEGRITY FAILED — cURL error 6" into the
#    user's security report, which reads exactly like a compromise. Not being
#    able to ask the question is not an answer to it.
verify_out=$(helper verify "$current")
verify_status=$(field "$verify_out" status)
if [ "$verify_status" = "ok" ]; then
    if [ "$(field "$verify_out" bad)" != "0" ]; then
        record "CORE FILE INTEGRITY FAILED ($current):"
        # Detail lines go straight to the report/log rather than through
        # record(): a pipeline's while-loop is a subshell, so the finding
        # counter would not survive it. The line above already counted this.
        while IFS='=' read -r kind path; do
            warn "  $kind: $path"
            printf '  %s: %s\n' "$kind" "$path" >> "$report_tmp" 2>/dev/null || true
        done < <(printf '%s\n' "$verify_out" | grep -E '^(mismatch|missing)=')
    else
        log "core file integrity verified against WordPress.org checksums"
    fi
else
    # Explicitly NOT a finding — just say we could not check.
    log "core file integrity NOT verified: $(field "$verify_out" error)"
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
        if quarantine "$path" "$sum"; then
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
        record "ROGUE ADMIN ACCOUNT matching wp2shell naming: '$ulogin' <${uemail:-}> — remove with: docker exec -u www-data onionpress-wordpress wp user delete '$ulogin' --network"
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
    printf 'No indicators of compromise found.\n' >> "$report_tmp" 2>/dev/null || true
else
    warn "=================================================="
    warn "$findings SECURITY FINDING(S) — review $REPORT"
    warn "Full guidance: https://github.com/brewsterkahle/onionpress/security"
    warn "=================================================="
    printf '\n%s finding(s). If any webshell or rogue admin is listed, treat the\n' "$findings" >> "$report_tmp" 2>/dev/null || true
    printf 'site as compromised: rotate wp-config salts, change all passwords,\n'   >> "$report_tmp" 2>/dev/null || true
    printf 'and restore content from a backup predating the compromise.\n'          >> "$report_tmp" 2>/dev/null || true
fi

exit 0
