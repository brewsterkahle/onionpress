"""Browser detection for OnionPress on a Mac.

What is installed and which extension last talked to the daemon: the
menubar's menu title and first-run waits, and launcher_ops.open_onion
(`onionpress open onion`, issue #527), which opens the site.
"""

import json
import os
import subprocess
import time

TOR_BROWSER_PATH = "/Applications/Tor Browser.app"
BRAVE_BROWSER_PATH = "/Applications/Brave Browser.app"

# Browsers we trust for open -a / osascript activate
ALLOWED_BROWSERS = {"Firefox", "Google Chrome", "Brave Browser", "Microsoft Edge", "Safari"}

# Browsers that support the OnionPress extension
EXTENSION_BROWSERS = {
    "Firefox": "/Applications/Firefox.app",
    "Google Chrome": "/Applications/Google Chrome.app",
    "Brave Browser": "/Applications/Brave Browser.app",
    "Microsoft Edge": "/Applications/Microsoft Edge.app",
}


def is_tor_browser_installed():
    """Check if Tor Browser is installed in /Applications."""
    return os.path.isdir(TOR_BROWSER_PATH)


def is_brave_installed():
    """Check if Brave Browser is installed in /Applications."""
    return os.path.isdir(BRAVE_BROWSER_PATH)


# ~/.onionpress/extension-connected says which browser's OnionPress
# extension was last heard from, and when: one JSON object,
# {"timestamp": <unix seconds>, "browser": <a name in ALLOWED_BROWSERS>}.
# Two writers: the onion proxy's /status (the extension polls it every few
# seconds) and the native messaging host. Both must write this format: the
# native host wrote a bare integer, which the readers here could not parse,
# so the menubar never detected the extension (issue #318). The browser
# starts the native host outside the app's Python, so it keeps its own copy
# of the writer; tests/test_browser_extension.py ties the two together.
EXTENSION_MARKER = "extension-connected"


def write_extension_marker(app_support, browser, allowed=ALLOWED_BROWSERS):
    """Record that *browser*'s extension is connected now.

    *browser* comes from the extension (a request header or a native
    message), so only a name in *allowed* is written. Written to a temp file
    and renamed, so a reader never sees half a file. Returns True if written.
    """
    if browser not in allowed:
        return False
    marker = os.path.join(app_support, EXTENSION_MARKER)
    tmp = f"{marker}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps({"timestamp": int(time.time()), "browser": browser}))
        os.replace(tmp, marker)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _read_extension_marker(app_support, allowed=ALLOWED_BROWSERS):
    """(timestamp, browser) from the marker, or None when there is none or it
    is not what write_extension_marker writes (an older native host's bare
    integer names no browser, so it does not count)."""
    marker = os.path.join(app_support, EXTENSION_MARKER)
    try:
        with open(marker, "r") as f:
            data = json.loads(f.read(4096).strip())
        timestamp = data["timestamp"]
        browser = data.get("browser")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    if browser not in allowed:
        return None
    return timestamp, browser


def extension_connected_recently(app_support, allowed=ALLOWED_BROWSERS):
    """Check if a browser extension is actively connected.

    Returns the browser app name (e.g. "Firefox") if connected in the
    last 10 seconds, or None.
    """
    entry = _read_extension_marker(app_support, allowed)
    if entry and (time.time() - entry[0]) < 10:
        return entry[1]
    return None


def detect_best_browser(app_support):
    """Detect the best available browser for .onion sites.

    Returns (browser_type, browser_name) where browser_type is one of:
    'tor_browser', 'extension', 'brave', or None.
    """
    if is_tor_browser_installed():
        return ('tor_browser', 'Tor Browser')

    ext = extension_connected_recently(app_support)
    if ext:
        return ('extension', ext)

    if is_brave_installed():
        return ('brave', 'Brave Browser')

    return (None, None)


def browser_menu_title(app_support):
    """Return the appropriate menu title based on available browsers."""
    btype, bname = detect_best_browser(app_support)
    if btype == 'tor_browser':
        return "Open in Tor Browser"
    if btype == 'extension':
        return f"Open in {bname}"
    if btype == 'brave':
        return "Open in Brave Browser"
    return "Open in Browser"


def installed_extension_browsers():
    """Return list of installed browsers that support the OnionPress extension."""
    return [name for name, path in EXTENSION_BROWSERS.items()
            if os.path.exists(path)]


def wait_for_app_install(app_path, executable_subpath, timeout=600,
                         check_interval=3, cancel_check=None):
    """Poll for an app to appear in /Applications.

    Args:
        app_path: Full path to the .app bundle.
        executable_subpath: Relative path to the main executable inside the bundle.
        timeout: Max seconds to wait.
        check_interval: Seconds between checks.
        cancel_check: Optional callable returning True to abort early.

    Returns True if app was detected, False on timeout/cancel.
    """
    elapsed = 0
    while elapsed < timeout:
        if cancel_check and cancel_check():
            return False
        time.sleep(check_interval)
        elapsed += check_interval

        if not os.path.isdir(app_path):
            continue

        real_path = os.path.realpath(app_path)
        if not real_path.startswith("/Applications/"):
            continue

        if not os.path.exists(os.path.join(app_path, executable_subpath)):
            continue

        return True

    return False


def wait_for_extension_active(app_support, timeout=30):
    """Wait for the browser extension to register as active.

    Returns True if extension became active within timeout.
    """
    for _ in range(timeout):
        entry = _read_extension_marker(app_support)
        if entry and (time.time() - entry[0]) < 5:
            return True
        time.sleep(1)
    return False
