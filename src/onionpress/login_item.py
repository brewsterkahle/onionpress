"""The macOS login item: a LaunchAgent that opens OnionPress.app at login.

One implementation for the menubar (add_login_item / remove_login_item)
and `onionpress config set LAUNCH_ON_LOGIN` (config.apply_side_effects),
so the plist is the same whoever writes it. Nothing here runs on Linux
(START_ON_BOOT is systemd's).
"""

import os
import plistlib
import subprocess

LAUNCHAGENT_LABEL = "com.onionpress.launcher"


def launchagent_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHAGENT_LABEL}.plist")


def is_installed(path: str = "") -> bool:
    return os.path.exists(path or launchagent_path())


def install(app_bundle: str, log, path: str = "") -> bool:
    """Write the LaunchAgent plist that opens *app_bundle* (OnionPress.app)
    at login. Without a bundle path nothing is written: `open -a` of a
    wrong path would open nothing at every login."""
    path = path or launchagent_path()
    if not app_bundle or not os.path.isdir(app_bundle):
        log("LaunchAgent not installed: no app bundle found "
            f"({app_bundle or 'none'})")
        return False
    try:
        plist = {
            "Label": LAUNCHAGENT_LABEL,
            "ProgramArguments": ["open", "-a", app_bundle],
            "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua",
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        log("LaunchAgent installed for login auto-start")
        return True
    except Exception as e:
        log(f"Error installing LaunchAgent: {e}")
        return False


def remove(log, path: str = "", run=None) -> bool:
    """Unload and delete the LaunchAgent plist (nothing to do is success)."""
    path = path or launchagent_path()
    run = run or subprocess.run
    try:
        if os.path.exists(path):
            # Unload first (ignore errors if not loaded; no launchctl at
            # all, as off a Mac, is the same as not loaded)
            try:
                run(["launchctl", "bootout", f"gui/{os.getuid()}", path],
                    capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            os.remove(path)
        log("LaunchAgent removed")
        return True
    except Exception as e:
        log(f"Error removing LaunchAgent: {e}")
        return False
