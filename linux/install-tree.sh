#!/bin/bash

# The files OnionPress installs into /opt/onionpress on Linux, and how they
# get there. Every way in uses this one list: the .deb (build/build-linux.sh
# stages its tree with it; plain `onionpress update` installs a release's
# .deb), linux/install.sh and `onionpress update --from-checkout` (both
# install with it). Before, each kept its own copy commands, and `update`'s
# had drifted: it copied src/onion_auth.py and src/key_manager.py long after
# they moved (the cp failed unnoticed), never refreshed lib/onionpress, and
# its `cp -r` of docker/ and plugins/ landed in docker/docker/ and
# plugins/plugins/ on an existing install, so the old compose file stayed.
#
# Usage:
#   install-tree.sh stage   <source checkout> <new folder>
#       Assemble the tree in <new folder> (made here; it must not exist, or
#       be an empty folder).
#   install-tree.sh install <source checkout> <install dir>
#       Stage beside <install dir> (same filesystem), then rename the staged
#       tree into place. The old tree is removed only once the new one is in
#       place; if anything fails, the old tree stays where it was. Running
#       scripts keep reading the files they opened (a rename, never a copy
#       over a file in use).
#   install-tree.sh list
#       Print the list.
#
# Refuses, before copying anything, a source that is missing, a link, or
# holds anything but plain files and folders. tests/test_linux_install_tree.py
# runs it and ties build-linux.sh, install.sh and `update` to it.

set -euo pipefail
umask 022

# kind     source (in the checkout)                 destination (in the tree)          mode
MANIFEST='
file       linux/onionpress                         onionpress                         0755
file       linux/onionpress-tray                    onionpress-tray                    0755
tree       app/Resources/docker                     docker
tree       app/Resources/plugins                    plugins
tree       app/Resources/themes                     themes
tree       src/onionpress                           lib/onionpress
tree       linux/assets                             assets
file       src/onionpress/onion_auth.py             scripts/onion_auth.py              0644
file       src/onionpress/onion_keys.py             scripts/onion_keys.py              0644
file       src/onionpress/key_manager.py            scripts/key_manager.py             0644
file       linux/onionpress-heartbeat.py            scripts/onionpress-heartbeat.py    0644
file       app/Resources/scripts/install_plugin.sh  scripts/install_plugin.sh          0755
file       app/Resources/scripts/activate_plugin.sh scripts/activate_plugin.sh         0755
file       linux/onionpress.service                 onionpress.service                 0644
file       linux/onionpress-heartbeat.service       onionpress-heartbeat.service       0644
file       linux/onionpress-watcher.service         onionpress-watcher.service         0644
file       linux/onionpress-watcher.timer           onionpress-watcher.timer           0644
file       app/Resources/app-icon.png               app-icon.png                       0644
egress     src/onionpress/wordpress_egress.py       onionpress-wordpress-egress        0755
version    src/menubar.py                           VERSION                            0644
'

die() {
    echo "ERROR: $*" >&2
    exit 1
}

manifest() {
    printf '%s\n' "$MANIFEST" | grep -v '^[[:space:]]*$'
}

# Every source must be there, and be a plain file or a folder of plain
# files and folders: no links (a link in a checkout would have root copy
# whatever it points at into /opt), no devices or FIFOs.
check_sources() {
    local src="$1" kind path dest mode odd missing=""
    while read -r kind path dest mode; do
        case "$kind" in
            tree)
                if [ -L "$src/$path" ] || [ ! -d "$src/$path" ]; then
                    missing="$missing $path"
                    continue
                fi
                odd=$(find "$src/$path" ! -type f ! -type d -print -quit) \
                    || die "could not read $src/$path"
                [ -z "$odd" ] || die "$odd is not a plain file or folder; nothing was installed"
                ;;
            *)
                if [ -L "$src/$path" ] || [ ! -f "$src/$path" ]; then
                    missing="$missing $path"
                fi
                ;;
        esac
    done < <(manifest)
    [ -z "$missing" ] || die "the source checkout $src lacks:$missing; nothing was installed"
}

# The version, as build/build-linux.sh reads it (src/menubar.py's
# self.version, the canonical source the bump script writes).
source_version() {
    local v
    v=$(grep 'self\.version *= *"' "$1/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
    [[ "$v" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "no version in $1/src/menubar.py"
    echo "$v"
}

stage_tree() {
    local src="$1" dest="$2" kind path to mode
    src=$(cd "$src" && pwd -P) || die "no source checkout at $1"
    check_sources "$src"
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        [ -d "$dest" ] && [ ! -L "$dest" ] && [ -z "$(ls -A "$dest")" ] \
            || die "$dest exists and is not an empty folder"
    else
        mkdir -p "$dest"
    fi
    while read -r kind path to mode; do
        mkdir -p "$(dirname "$dest/$to")"
        case "$kind" in
            file)
                cp "$src/$path" "$dest/$to"
                chmod "$mode" "$dest/$to"
                ;;
            tree)
                mkdir -p "$dest/$to"
                cp -R "$src/$path/." "$dest/$to/"
                ;;
            egress)
                # The WordPress network's egress rules as a plain sh
                # script, for the docker.service drop-in (it runs as root
                # before dockerd starts, so no Python): RULES_SCRIPT, cut
                # out of wordpress_egress.py. tests/test_wordpress_egress.py
                # checks that this cut is the script.
                { echo '#!/bin/sh'
                  awk '/^RULES_SCRIPT = r"""$/ { f = 1; next } f && /^"""$/ { exit } f' \
                      "$src/$path"
                } > "$dest/$to"
                chmod "$mode" "$dest/$to"
                grep -q '^esac$' "$dest/$to" \
                    || die "could not cut RULES_SCRIPT out of $path"
                ;;
            version)
                source_version "$src" > "$dest/$to"
                chmod "$mode" "$dest/$to"
                ;;
            *)
                die "unknown kind in the list: $kind"
                ;;
        esac
    done < <(manifest)
    # Bytecode and Finder litter never ship: the test suite imports the
    # tor container's scripts, so the docker tree collects .pyc.
    find "$dest" -type d -name __pycache__ -prune -exec rm -rf {} +
    find "$dest" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) -delete
}

install_tree() {
    local src="$1" target="${2%/}" parent base new old
    [ -n "$target" ] || die "no install folder"
    parent=$(dirname "$target")
    base=$(basename "$target")
    [ -d "$parent" ] || die "$parent does not exist"
    [ ! -L "$target" ] || die "$target is a link; not replacing it"
    new=$(mktemp -d "$parent/.$base.new.XXXXXX")
    if ! ( stage_tree "$src" "$new" ); then
        rm -rf "$new"
        die "nothing was installed; $target is unchanged"
    fi
    chmod 0755 "$new"
    if [ -e "$target" ]; then
        old=$(mktemp -d "$parent/.$base.old.XXXXXX")
        if ! mv "$target" "$old/$base"; then
            rm -rf "$new"
            rmdir "$old" 2>/dev/null || true
            die "could not move $target aside; it is unchanged"
        fi
        if ! mv "$new" "$target"; then
            if mv "$old/$base" "$target"; then
                rm -rf "$new" "$old"
                die "could not put the new tree in place; $target is unchanged"
            fi
            die "could not put the new tree in place, nor the old one back: it is at $old/$base"
        fi
        rm -rf "$old"
    else
        mv "$new" "$target"
    fi
    echo "Installed OnionPress $(cat "$target/VERSION") to $target"
}

case "${1:-}" in
    stage)
        [ $# -eq 3 ] || die "usage: $0 stage <source checkout> <new folder>"
        stage_tree "$2" "$3"
        ;;
    install)
        [ $# -eq 3 ] || die "usage: $0 install <source checkout> <install dir>"
        install_tree "$2" "$3"
        ;;
    list)
        manifest
        ;;
    *)
        die "usage: $0 stage|install|list ..."
        ;;
esac
