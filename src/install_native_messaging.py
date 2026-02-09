#!/usr/bin/env python3
"""
OnionPress Native Messaging Installer

Registers the native messaging host manifest for Chrome, Firefox, and Brave
so the OnionPress browser extension can communicate with the local system.

Called by OnionPress on first launch.
"""

import json
import os
import stat

HOST_NAME = "press.onion.onionpress"
EXTENSION_ID = "press.onion.onionpress"  # Firefox extension ID

# Chrome extension ID will be determined after publishing to Chrome Web Store.
# During development, use the unpacked extension ID shown in chrome://extensions.
# This placeholder is updated at install time if a real ID is known.
CHROME_EXTENSION_ORIGIN = "chrome-extension://*/"

# Directories where each browser looks for native messaging host manifests (macOS)
BROWSER_DIRS = {
    "chrome": os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    ),
    "brave": os.path.expanduser(
        "~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"
    ),
    "firefox": os.path.expanduser(
        "~/Library/Application Support/Mozilla/NativeMessagingHosts"
    ),
}


def _host_script_path():
    """Return the path to the native messaging host script inside OnionPress.app."""
    # When installed: OnionPress.app/Contents/Resources/native-messaging-host.py
    app_path = "/Applications/OnionPress.app/Contents/Resources/native-messaging-host.py"
    if os.path.exists(app_path):
        return app_path
    # Fallback for development: use src/ relative to this script
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_messaging_host.py")


def _chrome_manifest(host_path, chrome_extension_id=None):
    """Build a Chrome/Brave native messaging host manifest."""
    origin = f"chrome-extension://{chrome_extension_id}/" if chrome_extension_id else CHROME_EXTENSION_ORIGIN
    return {
        "name": HOST_NAME,
        "description": "OnionPress – browse .onion sites in any browser",
        "path": host_path,
        "type": "stdio",
        "allowed_origins": [origin],
    }


def _firefox_manifest(host_path):
    """Build a Firefox native messaging host manifest."""
    return {
        "name": HOST_NAME,
        "description": "OnionPress – browse .onion sites in any browser",
        "path": host_path,
        "type": "stdio",
        "allowed_extensions": [f"{EXTENSION_ID}@onionpress"],
    }


def install(chrome_extension_id=None, log_func=None):
    """Install native messaging manifests for all supported browsers.

    Args:
        chrome_extension_id: Optional Chrome Web Store extension ID.
        log_func: Optional logging function (e.g., app.log).

    Returns:
        List of browser names that were successfully registered.
    """
    host_path = _host_script_path()
    installed = []

    # Ensure the host script is executable
    try:
        st = os.stat(host_path)
        os.chmod(host_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass

    for browser, directory in BROWSER_DIRS.items():
        try:
            os.makedirs(directory, exist_ok=True)
            manifest_path = os.path.join(directory, f"{HOST_NAME}.json")

            if browser == "firefox":
                manifest = _firefox_manifest(host_path)
            else:
                manifest = _chrome_manifest(host_path, chrome_extension_id)

            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2)

            installed.append(browser)
            if log_func:
                log_func(f"Installed native messaging manifest for {browser}: {manifest_path}")
        except Exception as e:
            if log_func:
                log_func(f"Failed to install native messaging for {browser}: {e}")

    return installed


def uninstall(log_func=None):
    """Remove native messaging manifests for all browsers."""
    for browser, directory in BROWSER_DIRS.items():
        manifest_path = os.path.join(directory, f"{HOST_NAME}.json")
        try:
            if os.path.exists(manifest_path):
                os.remove(manifest_path)
                if log_func:
                    log_func(f"Removed native messaging manifest for {browser}")
        except Exception as e:
            if log_func:
                log_func(f"Failed to remove native messaging for {browser}: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall(log_func=print)
    else:
        result = install(log_func=print)
        print(f"Installed for: {', '.join(result) if result else 'none'}")
