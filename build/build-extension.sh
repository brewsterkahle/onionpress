#!/bin/bash
#
# Build the OnionPress browser extensions.
#
#   build/dist/onionpress-chrome.zip    from extension/
#   build/dist/onionpress-firefox.xpi   from extension/ + extension-firefox/
#
# WHY THIS EXISTS
#   build/onionpress-firefox.xpi is committed to the repo but nothing
#   generates it and nothing references it — `grep -r '\.xpi'` matches only
#   the word "expires". Its exact inputs are gone: its background.js matches
#   neither extension/ nor extension-firefox/ (it is an older revision), and
#   its manifest.json appears in NO commit in this repository. So there was no
#   way to rebuild the shipped extension from source.
#
# WHICH SOURCES, AND WHY
#   There are two Firefox manifests in the tree and they are not equivalent:
#
#     extension/manifest.firefox.json   v1.0.0, min 109.0. Requests
#         webRequest, webRequestBlocking, webNavigation and <all_urls>, and
#         declares content_scripts. Has NO data_collection_permissions.
#
#     extension-firefox/manifest.json   v1.1.0, min 142.0. Drops all four
#         broad permissions and the content script, and declares
#         data_collection_permissions — which addons.mozilla.org REQUIRES
#         from Firefox 142 onward.
#
#   This script builds Firefox from extension-firefox/. Using
#   manifest.firefox.json would re-request permissions the extension no longer
#   needs and produce an AMO-rejectable package.
#
#   extension-firefox/ holds only the files that differ (manifest.json,
#   offline.html, offline.js); everything else — icons, popup, background —
#   comes from extension/. offline.html in particular is deliberately
#   different: extension/'s uses an inline <script>, which violates the
#   extension CSP; extension-firefox/ externalises it to offline.js.
#
# REPRODUCIBILITY
#   Entries are added in sorted order with a fixed timestamp and `zip -X`
#   (no uid/gid/extra fields), so two builds of the same sources produce
#   byte-identical archives on any machine. The committed .xpi will NOT match
#   — see above, its inputs no longer exist.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHROME_SRC="$PROJECT_DIR/extension"
FIREFOX_OVERLAY="$PROJECT_DIR/extension-firefox"
DIST_DIR="$PROJECT_DIR/build/dist"

TARGETS=""
while [ $# -gt 0 ]; do
    case "$1" in
        chrome|firefox|all) TARGETS="$TARGETS $1" ;;
        -h|--help) sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done
[ -n "$TARGETS" ] && [ "$TARGETS" != " all" ] || TARGETS="chrome firefox"

command -v zip >/dev/null 2>&1 || { echo "ERROR: 'zip' not found." >&2; exit 1; }

mkdir -p "$DIST_DIR"

# A fixed mtime so the archive is byte-reproducible. Any constant works; this
# is the Unix epoch rounded to a DOS-compatible date (zip cannot store dates
# before 1980).
FIXED_DATE="198001010000.00"

# pack <staging-dir> <output-file>
# Sorted entry order + fixed timestamps + -X (drop uid/gid/extra fields).
pack() {
    local stage="$1" out="$2"
    rm -f "$out"
    find "$stage" -exec touch -t "$FIXED_DATE" {} +
    ( cd "$stage" && find . -type f | sed 's|^\./||' | sort | zip -q -X -D "$out" -@ )
    echo "  $(basename "$out")  ($(cd "$(dirname "$out")" && du -h "$(basename "$out")" | cut -f1))"
}

build_chrome() {
    echo "── Chrome ─────────────────────────────────────────────────────────"
    local stage
    stage=$(mktemp -d)
    trap 'rm -rf "$stage"' RETURN

    cp -R "$CHROME_SRC/." "$stage/"
    # The Firefox manifest is a sibling in this directory; it must not ship in
    # the Chrome package.
    rm -f "$stage/manifest.firefox.json"

    [ -f "$stage/manifest.json" ] || { echo "ERROR: no manifest.json in $CHROME_SRC" >&2; exit 1; }
    pack "$stage" "$DIST_DIR/onionpress-chrome.zip"
}

build_firefox() {
    echo "── Firefox ────────────────────────────────────────────────────────"
    local stage
    stage=$(mktemp -d)
    trap 'rm -rf "$stage"' RETURN

    # Base: everything shared (icons, popup, background).
    cp -R "$CHROME_SRC/." "$stage/"
    rm -f "$stage/manifest.json" "$stage/manifest.firefox.json"

    # Overlay: the Firefox-specific files, including its manifest.
    if [ ! -f "$FIREFOX_OVERLAY/manifest.json" ]; then
        echo "ERROR: $FIREFOX_OVERLAY/manifest.json not found." >&2
        echo "  That directory holds the current Firefox manifest (v1.1.0," >&2
        echo "  min 142.0). See the header of this script." >&2
        exit 1
    fi
    cp -R "$FIREFOX_OVERLAY/." "$stage/"

    # content.js is not referenced by the Firefox manifest — the content
    # script was dropped along with the broad host permissions. Shipping an
    # unreferenced file just invites AMO review questions.
    rm -f "$stage/content.js"

    # Fail loudly if the manifest references something we did not stage. A
    # missing icon or popup produces an extension that installs and then
    # misbehaves, which is far worse than a build error.
    local missing
    missing=$(MANIFEST="$stage/manifest.json" STAGE="$stage" python3 - <<'PY'
import json, os, re
stage = os.environ["STAGE"]
data = json.load(open(os.environ["MANIFEST"]))
refs = set()
def walk(node):
    if isinstance(node, dict):
        for value in node.values():
            walk(value)
    elif isinstance(node, list):
        for value in node:
            walk(value)
    elif isinstance(node, str) and re.search(r"\.(js|html|png|css|json)$", node):
        refs.add(node)
walk(data)
print(" ".join(sorted(r for r in refs if not os.path.exists(os.path.join(stage, r)))))
PY
)
    if [ -n "$missing" ]; then
        echo "ERROR: manifest.json references files that are not in the package:" >&2
        for f in $missing; do echo "  $f" >&2; done
        exit 1
    fi

    pack "$stage" "$DIST_DIR/onionpress-firefox.xpi"
}

echo "OnionPress extension build"
case " $TARGETS " in *" chrome "*) build_chrome ;; esac
case " $TARGETS " in *" firefox "*) build_firefox ;; esac

echo ""
echo "✅ Written to build/dist/"
echo ""
echo "Load unpacked for testing:"
echo "  Chrome   chrome://extensions -> Developer mode -> Load unpacked -> extension/"
echo "  Firefox  about:debugging -> This Firefox -> Load Temporary Add-on"
echo ""
echo "NOTE: build/onionpress-firefox.xpi (committed, v1.0.0) is NOT this file."
echo "It was built from sources that no longer exist in git. See this script's"
echo "header and docs/BUILDING.md."
