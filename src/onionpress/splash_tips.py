"""Rotating "what you can do" tips for the launch splash (issue #166).

First launch takes a few minutes (Colima boot, image pulls, Tor bootstrap).
The splash's status line says what OnionPress is doing right now; these tips
sit under it and tell a new author what they will be able to do once it is
up, so the wait doubles as the introduction nothing else gives them.

This module is pure Python on purpose: the splash itself is AppKit and can
only be exercised by launching the app, but the copy and the rotation order
are testable without it (``tests/test_splash_tips.py``). ``src/menubar.py``
owns the NSTimer that calls :func:`tip_at` every :data:`TIP_INTERVAL_SECONDS`.
"""

# Keep each tip accurate to the menubar menu titles and the paths it names
# (``Open in Tor Browser``, ``Copy Onion Address``, ``Backup...``,
# ``~/OnionPress/backups``, ``~/OnionPress/Creations/My Creations``, and the
# OnionPress -> Following page in the site's admin). The splash is 320px wide
# and the tip label wraps to two lines, so stay under ~90 characters.
SPLASH_TIPS = (
    "OnionPress lives in your menu bar. Click the onion icon "
    "to open your site or back it up.",
    "Write your first post: choose Open in Tor Browser "
    "from the menu bar, then click + New.",
    "Your backups and My Creations folder live in ~/OnionPress, "
    "in your home folder.",
    "Follow friends by onionname: in your site's admin, "
    "open OnionPress → Following.",
    "Share your address: Copy Onion Address in the menu bar "
    "puts it on the clipboard.",
)

# How long each tip stays on screen before the next one.
TIP_INTERVAL_SECONDS = 6.0

# Longest tip the splash's two-line label can show without clipping.
TIP_MAX_CHARS = 90


def tip_at(tick, tips=SPLASH_TIPS):
    """Return the tip to show on the ``tick``-th timer fire (0 = at launch).

    Cycles through ``tips`` in order and wraps around, so the caller only has
    to keep a counter. An empty ``tips`` sequence yields ``""`` so the splash
    never crashes over copy.
    """
    if not tips:
        return ""
    return tips[tick % len(tips)]
