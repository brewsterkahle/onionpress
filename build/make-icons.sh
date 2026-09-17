#!/bin/bash
#
# Regenerate the macOS app icons from their sources.
#
#   app/Resources/app-icon.png     <- app/Resources/app-icon-source.png
#   app/Resources/AppIcon.icns     <- app/Resources/app-icon.png
#   app/Resources/menubar-icon-*.png
#                                  <- assets/branding/icon-menubar*.png
#
# macOS only: uses sips and iconutil.
#
# WHY THIS EXISTS
#   These were committed binaries with no recipe. The .icns in particular is a
#   3 MB artifact nobody could rebuild, so a branding change meant either
#   hand-crafting it again or leaving it stale.
#
# WHAT IS EXACT AND WHAT IS NOT
#   app-icon.png and AppIcon.icns regenerate BYTE-IDENTICALLY to the committed
#   files (verified with cmp). --verify checks that and is the reason to trust
#   this script.
#
#   The three menubar PNGs do NOT. They were made with ImageMagick, not sips —
#   their embedded tEXt chunks (date:create, date:timestamp) and
#   exif:PixelXDimension 761 / PixelYDimension 895 prove both the tool and the
#   source (assets/branding/icon-menubar.png, 761x895). The recipe below is
#   reconstructed from that metadata and from the pixel relationships between
#   the files; it produces equivalent icons, not identical bytes, and it has
#   not been executed against the committed files because ImageMagick was not
#   installed on the machine where it was written. Treat it as documented
#   intent rather than a verified reproduction, and eyeball the result.
#
# USAGE
#   build/make-icons.sh            # regenerate everything it can
#   build/make-icons.sh --verify   # rebuild to a temp dir and diff; no writes
#   build/make-icons.sh --icns     # just the app icon chain

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RES="$PROJECT_DIR/app/Resources"
BRANDING="$PROJECT_DIR/assets/branding"

VERIFY=0
DO_ICNS=1
DO_MENUBAR=1

while [ $# -gt 0 ]; do
    case "$1" in
        --verify)  VERIFY=1 ;;
        --icns)    DO_MENUBAR=0 ;;
        --menubar) DO_ICNS=0 ;;
        -h|--help) sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: macOS only — needs sips and iconutil." >&2
    exit 1
fi
command -v sips >/dev/null 2>&1 || { echo "ERROR: sips not found." >&2; exit 1; }
command -v iconutil >/dev/null 2>&1 || { echo "ERROR: iconutil not found." >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# emit <generated-file> <destination> — install or, under --verify, diff.
emit() {
    local src="$1" dest="$2" name
    name="${dest#$PROJECT_DIR/}"
    if [ "$VERIFY" = "1" ]; then
        if [ ! -f "$dest" ]; then
            echo "  MISSING   $name (nothing committed to compare against)"
            return 0
        fi
        if cmp -s "$src" "$dest"; then
            echo "  IDENTICAL $name"
        else
            echo "  DIFFERS   $name ($(stat -f%z "$src") vs $(stat -f%z "$dest") bytes)"
        fi
    else
        cp "$src" "$dest"
        echo "  wrote     $name"
    fi
}

# ─── App icon chain ─────────────────────────────────────────────────────

if [ "$DO_ICNS" = "1" ]; then
    echo "── App icon ───────────────────────────────────────────────────────"

    [ -f "$RES/app-icon-source.png" ] || {
        echo "ERROR: $RES/app-icon-source.png not found." >&2; exit 1; }

    # NOTE: -z, not -Z. `sips -z H W` ignores aspect ratio, and that is
    # load-bearing here: the master is 992x1072 and is deliberately squashed
    # to a square 1024x1024. "Fixing" this to `sips -Z 1024` yields 948x1024
    # and silently changes every icon layer and the .icns.
    sips -z 1024 1024 "$RES/app-icon-source.png" --out "$WORK/app-icon.png" >/dev/null
    emit "$WORK/app-icon.png" "$RES/app-icon.png"

    # Build the iconset from the 1024px master. The master is copied in
    # unresized as icon_512x512@2x.png — resampling it to its own size would
    # re-encode and change the bytes.
    #
    # Do NOT build the iconset by exporting the existing .icns with
    # `iconutil --convert iconset`: that round-trip is lossy. The ic04 and
    # ic05 layers are stored as raw ARGB and get re-encoded, yielding a file
    # of identical length that differs in exactly two bytes.
    ICONSET="$WORK/AppIcon.iconset"
    mkdir -p "$ICONSET"
    cp "$WORK/app-icon.png" "$ICONSET/icon_512x512@2x.png"
    while read -r size name; do
        [ -n "$size" ] || continue
        sips -z "$size" "$size" "$WORK/app-icon.png" --out "$ICONSET/$name.png" >/dev/null
    done <<'EOF'
16 icon_16x16
32 icon_16x16@2x
32 icon_32x32
64 icon_32x32@2x
128 icon_128x128
256 icon_128x128@2x
256 icon_256x256
512 icon_256x256@2x
512 icon_512x512
EOF
    iconutil --convert icns "$ICONSET" --output "$WORK/AppIcon.icns"
    emit "$WORK/AppIcon.icns" "$RES/AppIcon.icns"
fi

# ─── Menubar icons ──────────────────────────────────────────────────────

if [ "$DO_MENUBAR" = "1" ]; then
    echo "── Menubar icons ──────────────────────────────────────────────────"

    if ! command -v magick >/dev/null 2>&1 && ! command -v convert >/dev/null 2>&1; then
        cat >&2 <<'EOF'
  SKIPPED: ImageMagick not installed.

  The committed menubar PNGs were produced with ImageMagick (their tEXt
  chunks say so), and sips cannot do the two operations involved — a
  non-linear Rec.709 luma grayscale, and an alpha knockout of a flat
  backdrop. Install it and re-run:

      brew install imagemagick

  The committed icons remain valid; this step only regenerates them.
EOF
    else
        IM=$(command -v magick || command -v convert)

        # running: the colour icon, scaled to menubar height.
        # 75x88 matches the committed file; the source is 761x895, whose
        # aspect ratio this preserves.
        "$IM" "$BRANDING/icon-menubar.png" -resize 75x88 "$WORK/menubar-icon-running.png"
        emit "$WORK/menubar-icon-running.png" "$RES/menubar-icon-running.png"

        # stopped: grayscale of running.
        #
        # -grayscale Rec709Luma, NOT -colorspace Gray. ImageMagick 7's
        # -colorspace Gray is gamma-aware/linear and does not match the
        # committed file (mean error ~8.5 vs ~0.3 for Rec709Luma). Alpha is
        # preserved either way.
        "$IM" "$WORK/menubar-icon-running.png" -grayscale Rec709Luma \
            "$WORK/menubar-icon-stopped.png"
        emit "$WORK/menubar-icon-stopped.png" "$RES/menubar-icon-stopped.png"

        # starting: the 44x44 source with its flat gray backdrop knocked out
        # to transparency, leaving binary alpha.
        "$IM" "$BRANDING/icon-menubar-starting.png" \
            -alpha set -channel RGBA -fuzz 5% -transparent gray \
            "$WORK/menubar-icon-starting.png"
        emit "$WORK/menubar-icon-starting.png" "$RES/menubar-icon-starting.png"
    fi
fi

echo ""
if [ "$VERIFY" = "1" ]; then
    echo "Verify complete (nothing written)."
else
    echo "Done. Review with: git diff --stat app/Resources/"
fi
