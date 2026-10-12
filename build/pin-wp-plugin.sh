#!/bin/bash
# Print the pin line for a WordPress.org plugin release, for
# app/Resources/scripts/install_plugin.sh.
#
# WHY
#   install_plugin.sh installs one release of each plugin and unpacks its zip
#   into the web root only if the zip's SHA256 matches the pin there. This
#   fetches the versioned zip (https://downloads.wordpress.org/plugin/
#   <slug>.<version>.zip, the URL install_plugin.sh fetches over Tor) and
#   prints the line to paste into plugin_pin(). Read the release's changelog
#   (and diff, for a plugin that does more than it says) before pinning it:
#   the pin is the statement that this zip was looked at.
#
# USAGE
#   build/pin-wp-plugin.sh cache-enabler 1.8.17
#   build/pin-wp-plugin.sh internet-archive-wayback-machine-link-fixer 1.4.2
#
#   Run it twice, a while apart: WordPress.org builds plugin zips itself, and
#   a hash that differs between two fetches of one version is not pinnable.
#   The fetch is a maintainer's, from the build machine; set ALL_PROXY
#   (socks5h://127.0.0.1:9050) to make it over Tor.

set -euo pipefail

SLUG="${1:-}"
VERSION="${2:-}"
if [ -z "$SLUG" ] || [ -z "$VERSION" ] || [ "$SLUG" = "-h" ] || [ "$SLUG" = "--help" ]; then
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi
if ! [[ "$SLUG" =~ ^[a-z0-9-]+$ ]] || ! [[ "$VERSION" =~ ^[0-9][0-9A-Za-z.-]*$ ]]; then
    echo "not a plugin slug and version: $SLUG $VERSION" >&2
    exit 1
fi

dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
url="https://downloads.wordpress.org/plugin/${SLUG}.${VERSION}.zip"
curl -fsSL --proto '=https' --tlsv1.2 -o "$dir/plugin.zip" "$url"
if [ "$(head -c 2 "$dir/plugin.zip")" != "PK" ]; then
    echo "$url did not return a zip" >&2
    exit 1
fi
if command -v shasum >/dev/null 2>&1; then
    sum=$(shasum -a 256 "$dir/plugin.zip" | awk '{print $1}')
else
    sum=$(sha256sum "$dir/plugin.zip" | awk '{print $1}')
fi
echo "$url" >&2
echo "Paste into plugin_pin() in app/Resources/scripts/install_plugin.sh:" >&2
printf '        %s)\n            echo "%s %s" ;;\n' "$SLUG" "$VERSION" "$sum"
