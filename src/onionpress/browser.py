"""Browser detection, selection, and launching for OnionPress.

Consolidates browser detection logic that was duplicated across
open_tor_browser, _auto_open_browser_inner, and show_browser_install_dialog.
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


def extension_connected_recently(app_support, allowed=ALLOWED_BROWSERS):
    """Check if a browser extension is actively connected.

    Returns the browser app name (e.g. "Firefox") if connected in the
    last 10 seconds, or None.
    """
    marker = os.path.join(app_support, "extension-connected")
    try:
        if os.path.exists(marker):
            with open(marker, 'r') as f:
                data = json.loads(f.read().strip())
            if (time.time() - data["timestamp"]) < 10:
                browser = data.get("browser")
                if browser in allowed:
                    return browser
    except Exception:
        pass
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


# Bundle identifiers for browsers that are not at /Applications/<name>.app.
_BUNDLE_IDS = {
    "Tor Browser": "org.torproject.torbrowser",
    "Firefox": "org.mozilla.firefox",
    "Google Chrome": "com.google.Chrome",
    "Brave Browser": "com.brave.Browser",
    "Microsoft Edge": "com.microsoft.edgemac",
    "Safari": "com.apple.Safari",
}


def _app_url(AppKit, app):
    """NSURL of *app* (an .app path, or a name like "Tor Browser"), or None."""
    if app.endswith(".app"):
        candidates = [app]
    else:
        candidates = [os.path.join(d, f"{app}.app") for d in
                      ("/Applications", os.path.expanduser("~/Applications"))]
    for path in candidates:
        if os.path.isdir(path):
            return AppKit.NSURL.fileURLWithPath_(path)
    bundle_id = _BUNDLE_IDS.get(app)
    if bundle_id:
        return AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle_id)
    return None


def open_url(url, app=None, log_func=None):
    """Open *url* in *app* (an .app path or a name such as "Tor Browser"), or
    in the default browser, the way `open [-a app] url` does: through
    LaunchServices. But from this process: `open` would carry the URL in its
    argv, which `ps` shows to every user of the Mac, and an auto-login URL
    holds a one-time admin token (auto_login.py). LaunchServices hands the
    URL to the browser as an Apple Event, never on a command line.

    Runs on the main thread (dispatched there when called from another), so
    it returns before the browser is asked; failures are logged.
    """
    import AppKit

    def _open():
        try:
            ns_url = AppKit.NSURL.URLWithString_(url)
            ws = AppKit.NSWorkspace.sharedWorkspace()
            app_url = _app_url(AppKit, app) if app else None
            if ns_url is None or (app and app_url is None):
                ok = False
            elif app_url is None:
                ok = bool(ws.openURL_(ns_url))
            else:
                ws.openURLs_withApplicationAtURL_configuration_completionHandler_(
                    [ns_url], app_url,
                    AppKit.NSWorkspaceOpenConfiguration.configuration(), None)
                ok = True
        except Exception as e:  # never let it escape into the run loop
            ok = False
            if log_func:
                log_func(f"Opening {app or 'the browser'} failed: {type(e).__name__}")
            return
        if not ok and log_func:
            log_func(f"Could not open {app or 'the default browser'}")

    if AppKit.NSThread.isMainThread():
        _open()
    else:
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_open)


def open_onion_url(url, app_support, log_func, add_login=None):
    """Open a .onion URL in the best available browser.

    *add_login(url)* returns *url* with a one-time auto-login token, or None.
    It is called only for browsers that get the URL through LaunchServices
    (open_url). Brave's Tor window is reachable only through Brave's command
    line, where every local user could read the token, so Brave gets *url*
    as it is: the site opens, not logged in. Logs never carry the token.

    Returns True if a browser was found and opened, False if no browser available.
    """
    btype, bname = detect_best_browser(app_support)

    if btype == 'brave':
        brave_exe = os.path.join(BRAVE_BROWSER_PATH, "Contents", "MacOS", "Brave Browser")
        subprocess.run([brave_exe, "--tor", url])
        log_func(f"Opened {url} in Brave Browser (Tor mode)")
        return True
    if btype in ('extension', 'tor_browser'):
        app = TOR_BROWSER_PATH if btype == 'tor_browser' else bname
        open_url((add_login(url) if add_login else None) or url, app, log_func)
        log_func(f"Opened {url} in {bname}" + (" (extension)" if btype == 'extension' else ""))
        return True

    return False


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
    marker = os.path.join(app_support, "extension-connected")
    for _ in range(timeout):
        try:
            with open(marker, 'r') as f:
                data = json.loads(f.read().strip())
            if (time.time() - data["timestamp"]) < 5:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False
