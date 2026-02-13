#!/usr/bin/env python3
"""Generate the DMG background image for OnionPress.

Creates a 640x660 background with:
- Cream/off-white base matching the product page aesthetic
- The OnionPress logo displayed in the upper area
- A dashed arrow from app icon position to Applications folder position
- "Drag to install" text near the arrow
- Installation story guide image at the bottom

Usage:
    python3 create-dmg-background.py <output_path> [--logo <logo_path>] [--story <story_path>]
"""

import argparse
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# Retina scale — render at 2x pixels, mark as 144 DPI so Finder
# displays at the same point size but with double the detail
SCALE = 7
RETINA_DPI = 72 * SCALE

# Layout constants (in points, multiplied by SCALE for pixel coords)
WIDTH = 640 * SCALE
HEIGHT = 620 * SCALE
BG_COLOR = (245, 237, 224)  # #F5EDE0 cream/off-white
ACCENT_COLOR = (184, 150, 214)  # #B896D6 purple
TEXT_COLOR = (120, 100, 140)  # Muted purple for text

# Icon positions (centers) — must match AppleScript icon positions
APP_ICON_X = 160 * SCALE
APPS_ICON_X = 480 * SCALE
ICON_Y = 245 * SCALE

# Arrow parameters
ARROW_Y = ICON_Y - 10 * SCALE
ARROW_START_X = APP_ICON_X + 75 * SCALE
ARROW_END_X = APPS_ICON_X - 75 * SCALE
DASH_LENGTH = 12 * SCALE
GAP_LENGTH = 8 * SCALE
ARROW_WIDTH = 3 * SCALE
ARROWHEAD_SIZE = 14 * SCALE


def draw_dashed_arrow(draw, x1, y, x2, color, width, dash, gap, head_size):
    """Draw a horizontal dashed line with an arrowhead."""
    # Dashed line
    x = x1
    while x < x2 - head_size:
        end = min(x + dash, x2 - head_size)
        draw.line([(x, y), (end, y)], fill=color, width=width)
        x = end + gap

    # Arrowhead (filled triangle)
    tip_x = x2
    draw.polygon(
        [
            (tip_x, y),
            (tip_x - head_size, y - head_size // 2),
            (tip_x - head_size, y + head_size // 2),
        ],
        fill=color,
    )


def generate_background(output_path, logo_path=None, story_path=None):
    """Generate the DMG background image."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Place logo in upper area if available
    if logo_path and os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA")
        # Scale logo to fit nicely in upper portion (max 200pt tall)
        max_logo_height = 200 * SCALE
        if logo.height > max_logo_height:
            ratio = max_logo_height / logo.height
            logo = logo.resize(
                (int(logo.width * ratio), max_logo_height),
                Image.LANCZOS,
            )
        # Center horizontally, place in upper area
        logo_x = (WIDTH - logo.width) // 2
        logo_y = 20 * SCALE
        img.paste(logo, (logo_x, logo_y), logo)

    # Draw dashed arrow from app position to Applications position
    draw_dashed_arrow(
        draw,
        ARROW_START_X,
        ARROW_Y,
        ARROW_END_X,
        ACCENT_COLOR,
        ARROW_WIDTH,
        DASH_LENGTH,
        GAP_LENGTH,
        ARROWHEAD_SIZE,
    )

    # "Drag to install" text centered above arrow
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16 * SCALE)
    except (OSError, IOError):
        font = ImageFont.load_default()

    text = "Drag to install"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = (APP_ICON_X + APPS_ICON_X - text_w) // 2
    text_y = ARROW_Y - 30 * SCALE
    draw.text((text_x, text_y), text, fill=TEXT_COLOR, font=font)

    # Place story guide image at the bottom
    if story_path and os.path.exists(story_path):
        story = Image.open(story_path).convert("RGBA")
        # Scale story to fit width with 20pt margin on each side
        story_max_width = WIDTH - 40 * SCALE
        ratio = story_max_width / story.width
        story = story.resize(
            (int(story.width * ratio), int(story.height * ratio)),
            Image.LANCZOS,
        )
        # Center horizontally, place below icons
        story_top = ICON_Y + 100 * SCALE
        story_x = (WIDTH - story.width) // 2
        story_y = story_top
        img.paste(story, (story_x, story_y), story)

    img.save(output_path, "PNG", dpi=(RETINA_DPI, RETINA_DPI))
    print(f"DMG background saved to {output_path} ({img.width}x{img.height} @ {RETINA_DPI} DPI)")


def main():
    parser = argparse.ArgumentParser(description="Generate OnionPress DMG background")
    parser.add_argument("output", help="Output PNG path")
    parser.add_argument("--logo", help="Path to OnionPress logo PNG")
    parser.add_argument("--story", help="Path to installation story guide PNG")
    args = parser.parse_args()
    generate_background(args.output, args.logo, args.story)


if __name__ == "__main__":
    main()
