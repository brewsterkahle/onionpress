#!/bin/bash
# OnionPress: give www-data back what root left in the web root.
#
# Runs as root at every container start, before multisite-init and the
# security audit. wp-cli ran as root here until it ran as www-data
# (onionpress-wp, and `docker exec -u www-data` from the host): the audit's
# `wp core update` left core files root's, an update's working folders in
# wp-content/upgrade root's, and the host's calls (media regenerate, Cache
# Enabler's settings) root's files in wp-content. www-data cannot replace
# any of them, so WordPress's own updates fail on them, and so would the
# audit's core update now that it runs as www-data.
#
# Everything under /var/www/html is www-data's (the official image unpacks
# WordPress as www-data), so the rule is simple: root's files there become
# www-data's. Root acts on folders www-data controls, so:
#   - find never follows a link, and -execdir runs chown inside the folder
#     find holds open, on the entry's bare name: a link swapped in among the
#     folders since find looked sends nothing elsewhere, and chown -h never
#     follows a link at the name itself.
#   - My Creations (the user's ~/OnionPress folder, bind-mounted at
#     wp-content/creations) is never entered: its files are the host user's,
#     and under rootless Docker they show as root's in here. A mount point
#     cannot be renamed or removed, so that folder is named "creations"
#     wherever www-data moves the folders above it, and `-name creations
#     -prune` stops find there. Docker follows a link at the mount's path
#     when it starts the container, though, so mounts_ok first checks that
#     every mount below the web root is such a folder or lies inside one,
#     and that nothing is mounted over or above the web root itself.

set -uo pipefail   # NOT -e: this must never block startup

WPROOT=/var/www/html
MOUNTINFO=/proc/self/mountinfo
ROOT_UID=0

# 0 when the mounts at and below $WPROOT are as described above: one mount
# at $WPROOT, none above it but /, and below it only folders named
# "creations" that are mounts themselves, and mounts inside those.
mounts_ok() {
    awk -v root="$WPROOT" '
        { mp = $5 }
        mp == root { at_root++; next }
        index(root "/", mp "/") == 1 { above = 1; next }
        index(mp, root "/") == 1 { below[mp] = 1 }
        END {
            if (above || at_root != 1) exit 1
            for (m in below) {
                n = split(substr(m, length(root) + 2), part, "/")
                p = root
                ok = 0
                for (i = 1; i <= n; i++) {
                    p = p "/" part[i]
                    if (part[i] == "creations" && (p in below)) { ok = 1; break }
                }
                if (!ok) exit 1
            }
        }' "$MOUNTINFO"
}

# Give each of root's files and folders under $WPROOT to www-data. Prints
# the names it gave.
give_back() {
    find "$WPROOT" -xdev -name creations -prune -o -user "$ROOT_UID" -print \
        -execdir chown -h www-data:www-data -- {} +
}

if [ ! -r "$MOUNTINFO" ] || ! mounts_ok; then
    echo "a mount at or below $WPROOT is not where it belongs — no owner changed there"
    exit 0
fi
given=$(give_back 2>/dev/null | wc -l | tr -d ' ')
if [ "${given:-0}" != 0 ]; then
    echo "gave www-data $given file(s) and folder(s) root had left in $WPROOT"
fi
exit 0
