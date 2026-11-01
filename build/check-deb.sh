#!/bin/bash
#
# Check a built OnionPress .deb the way CI does, before it reaches anyone.
#
# Usage:
#   build/check-deb.sh [path/to/onionpress.deb] [--expect-version X.Y.Z] [--lintian]
#
# Linux only: it reads the package with dpkg-deb. With --lintian it also
# runs lintian and fails on any error tag that is not on the accepted list
# below. .github/workflows/linux-package.yml runs it on every build.
#
# WHY THESE CHECKS
#   Until October 2026 every released .deb was built on the maintainer's Mac
#   through build/build-linux.sh's pure-Python `ar` fallback, where neither
#   dpkg-deb nor lintian ever saw it. v2.5.3's package had 430 members, 215
#   of them macOS AppleDouble `._` files, every one owned by uid 502 (the Mac
#   user; dpkg installs the owner recorded in data.tar), a Description whose
#   third line began with ".onion" (a control statement to dpkg), a folded
#   Depends field, and an /etc file not marked as a conffile. None of that
#   was visible from `sudo apt install ./onionpress.deb`, which does not
#   check signatures or run lintian either.

set -uo pipefail

DEB="build/onionpress.deb"
EXPECT_VERSION=""
RUN_LINTIAN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --expect-version) EXPECT_VERSION="$2"; shift 2 ;;
        --lintian) RUN_LINTIAN=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
        *) DEB="$1"; shift ;;
    esac
done

# What the package must carry. The plugin scripts are what the launcher
# runs from $SCRIPTS_DIR to install Cache Enabler and the Link Fixer; a
# non-executable copy fails silently at the launcher's `[ -x ]` guard.
# python3-nacl is backup format 2's libsodium (src/onionpress/backup_crypto.py);
# the version is the python3-nacl of Debian 12/13 and Ubuntu 22.04/24.04.
# tests/test_linux_package_workflow.py ties the string to build-linux.sh.
REQUIRED_DEPENDENCY="python3-nacl (>= 1.5.0)"
REQUIRED_EXECUTABLES=(
    ./opt/onionpress/onionpress
    ./opt/onionpress/onionpress-tray
    ./opt/onionpress/scripts/install_plugin.sh
    ./opt/onionpress/scripts/activate_plugin.sh
    ./opt/onionpress/onionpress-wordpress-egress
    ./opt/onionpress/docker-rootless/dockerd-rootless.sh
    ./opt/onionpress/docker-rootless/dockerd-rootless-setuptool.sh
)
REQUIRED_FILES=(
    ./opt/onionpress/VERSION
    ./opt/onionpress/docker/docker-compose.yml
    ./opt/onionpress/lib/onionpress/__init__.py
    ./opt/onionpress/lib/onionpress/cli.py
    ./usr/lib/systemd/user/onionpress.service
    ./usr/lib/systemd/user/onionpress-daemon.service
    ./usr/lib/systemd/system/docker.service.d/onionpress-wordpress-egress.conf
    ./etc/xdg/autostart/onionpress-tray.desktop
    ./usr/share/applications/onionpress.desktop
    ./usr/share/metainfo/org.onionpress.OnionPress.metainfo.xml
)
REQUIRED_SYMLINKS=(
    "./usr/local/bin/onionpress -> /opt/onionpress/onionpress"
    "./usr/local/bin/onionpress-tray -> /opt/onionpress/onionpress-tray"
)
REQUIRED_CONFFILES=(
    /etc/xdg/autostart/onionpress-tray.desktop
)

# lintian error tags this package accepts, each for a reason. Anything
# else at error severity fails the check; warnings are shown, not fatal.
#   dir-or-file-in-opt, file-in-usr-local, dir-in-usr-local: OnionPress is
#     a third-party package installed to /opt/onionpress with its CLI
#     symlinked from /usr/local/bin, which Debian Policy reserves for the
#     local administrator. install.sh, the tray's SetupDialog and the docs
#     all name /usr/local/bin/onionpress; moving to /usr/bin is a separate
#     change (issue #316 touches the same paths).
#   no-copyright-file, no-changelog: /usr/share/doc/onionpress is not
#     shipped yet. AGPL-3.0 text is in the repository's LICENSE.
ACCEPTED_LINTIAN_ERRORS="dir-or-file-in-opt,file-in-usr-local,dir-in-usr-local,no-copyright-file,no-changelog"

status=0
fail() { echo "FAIL: $*" >&2; status=1; }
ok() { echo "  ok: $*"; }

command -v dpkg-deb >/dev/null 2>&1 || { echo "ERROR: dpkg-deb not found; this check runs on Debian/Ubuntu only" >&2; exit 2; }
[ -f "$DEB" ] || { echo "ERROR: no such file: $DEB" >&2; exit 2; }

echo "Checking $DEB"

# ─── Control fields ─────────────────────────────────────────────────────
field() { dpkg-deb --field "$DEB" "$1" 2>/dev/null; }

[ "$(field Package)" = "onionpress" ] || fail "Package is '$(field Package)', expected onionpress"
[ "$(field Architecture)" = "all" ] || fail "Architecture is '$(field Architecture)', expected all (scripts only)"
VERSION=$(field Version)
[ -n "$VERSION" ] || fail "no Version field"
if [ -n "$EXPECT_VERSION" ]; then
    [ "$VERSION" = "$EXPECT_VERSION" ] && ok "Version $VERSION" || fail "Version is '$VERSION', expected $EXPECT_VERSION"
else
    ok "Version $VERSION"
fi

DEPENDS=$(field Depends)
case "$DEPENDS" in
    *"$REQUIRED_DEPENDENCY"*) ok "Depends carries '$REQUIRED_DEPENDENCY'" ;;
    *) fail "Depends lacks '$REQUIRED_DEPENDENCY' (backup encryption); Depends is: $DEPENDS" ;;
esac
if [ "$(printf '%s' "$DEPENDS" | wc -l)" != "0" ]; then
    fail "Depends is folded over several lines; a binary package's control file wants one line"
fi
# Compose v1 (the `docker-compose` 1.x package of Ubuntu and Debian 12) has
# no `docker compose`; a bare docker-compose alternative let apt choose it
# on Ubuntu 24.04. Every Compose alternative must be v2.
if printf '%s' "$DEPENDS" | grep -qE '(^|[,|] *)docker-compose *([,|]|$)'; then
    fail "Depends lets Compose v1 satisfy it (a bare docker-compose alternative); Depends is: $DEPENDS"
else
    ok "Depends admits no Compose v1"
fi

# ─── Members: owners, modes, names ──────────────────────────────────────
CONTENTS=$(dpkg-deb --contents "$DEB") || { fail "dpkg-deb --contents failed"; exit 1; }
MEMBERS=$(printf '%s\n' "$CONTENTS" | wc -l | tr -d ' ')
echo "  $MEMBERS members"

# Column 2 of `dpkg-deb --contents` is owner/group. dpkg installs exactly
# that, so anything but root/root becomes some other user's files on the
# target machine (uid 502 is the Mac user who built v2.5.3).
NOT_ROOT=$(printf '%s\n' "$CONTENTS" | awk '$2 != "root/root" { print "    " $2 "  " $6 }')
if [ -n "$NOT_ROOT" ]; then
    fail "members not owned by root/root (dpkg installs the recorded owner):"$'\n'"$NOT_ROOT"
else
    ok "every member is root/root"
fi

# macOS's bsdtar writes extended attributes as `._name` members unless
# COPYFILE_DISABLE is set; `.DS_Store` and `__pycache__` are build-host
# litter of the same kind.
LITTER=$(printf '%s\n' "$CONTENTS" | awk '{ print $6 }' | grep -E '(^|/)(\._[^/]*|\.DS_Store|__pycache__)(/|$)' || true)
if [ -n "$LITTER" ]; then
    fail "build-host litter in the package ($(printf '%s\n' "$LITTER" | wc -l | tr -d ' ') members):"$'\n'"$(printf '%s\n' "$LITTER" | head -5 | sed 's/^/    /')"
else
    ok "no AppleDouble, .DS_Store or __pycache__ members"
fi

member_line() { printf '%s\n' "$CONTENTS" | awk -v p="$1" '$6 == p { print; exit }'; }

for path in "${REQUIRED_EXECUTABLES[@]}"; do
    line=$(member_line "$path")
    if [ -z "$line" ]; then
        fail "missing: $path"
    elif [ "${line:0:10}" != "-rwxr-xr-x" ]; then
        fail "$path is ${line:0:10}, expected -rwxr-xr-x"
    fi
done
ok "required executables present and 0755 (${#REQUIRED_EXECUTABLES[@]})"

for path in "${REQUIRED_FILES[@]}"; do
    line=$(member_line "$path")
    if [ -z "$line" ]; then
        fail "missing: $path"
    elif [ "${line:0:1}" != "-" ]; then
        fail "$path is not a regular file: ${line:0:10}"
    fi
done
ok "required files present (${#REQUIRED_FILES[@]})"

for link in "${REQUIRED_SYMLINKS[@]}"; do
    path="${link%% -> *}"
    line=$(member_line "$path")
    if [ -z "$line" ]; then
        fail "missing symlink: $path"
    elif [ "${line:0:1}" != "l" ] || [ "${line##* -> }" != "${link##* -> }" ]; then
        fail "$path should be a symlink to ${link##* -> }; got: $line"
    fi
done
ok "CLI symlinks in /usr/local/bin"

# ─── Maintainer scripts and conffiles ───────────────────────────────────
CONTROL_TAR=$(dpkg-deb --ctrl-tarfile "$DEB" | tar -tvf - 2>/dev/null)
for script in postinst prerm postrm; do
    line=$(printf '%s\n' "$CONTROL_TAR" | awk -v p="./$script" '$6 == p { print; exit }')
    if [ -z "$line" ]; then
        fail "no $script maintainer script"
    elif [ "${line:0:10}" != "-rwxr-xr-x" ]; then
        fail "$script is ${line:0:10}, expected -rwxr-xr-x"
    fi
done
ok "postinst, prerm, postrm present and executable"

CONFFILES=$(dpkg-deb --ctrl-tarfile "$DEB" | tar -xOf - ./conffiles 2>/dev/null || true)
for path in "${REQUIRED_CONFFILES[@]}"; do
    printf '%s\n' "$CONFFILES" | grep -qx "$path" || fail "$path is not listed in DEBIAN/conffiles (a user's edit would be overwritten on upgrade)"
done
ok "conffiles"

# ─── lintian ────────────────────────────────────────────────────────────
if [ "$RUN_LINTIAN" = "1" ]; then
    if ! command -v lintian >/dev/null 2>&1; then
        fail "--lintian given but lintian is not installed (apt-get install lintian)"
    else
        echo "  lintian ($(lintian --version)); accepted error tags: $ACCEPTED_LINTIAN_ERRORS"
        # --fail-on error: warnings are printed for the log but do not fail.
        if lintian --fail-on error --suppress-tags "$ACCEPTED_LINTIAN_ERRORS" \
                --tag-display-limit 0 --no-cfg "$DEB"; then
            ok "lintian: no errors beyond the accepted tags"
        else
            fail "lintian reported errors (see above)"
        fi
    fi
fi

echo ""
if [ "$status" = "0" ]; then
    echo "✅ $DEB passes"
else
    echo "❌ $DEB fails" >&2
fi
exit "$status"
