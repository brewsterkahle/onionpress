#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OnionPress: Copyright (C) 2026 Internet Archive and OnionPress contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
onionpress Menu Bar Application
Provides a simple menu bar interface to control the WordPress + Tor onion service
"""

import rumps
import subprocess
import os
import threading
import time
import json
import plistlib
import sys
from datetime import datetime, timezone
import AppKit
import signal
import socket
import atexit
import re

# Add scripts directory to path for imports
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

from onionpress import install_native_messaging
from onionpress import onionheaven
from onionpress import key_manager
from onionpress import login_item
from onionpress import daemon as op_daemon
try:
    from onionpress import setup_window
except ImportError:
    setup_window = None
from onionpress.platform import resolve_paths
from onionpress import config as op_config
from onionpress import confirm as op_confirm
from onionpress import containers
from onionpress.ui_helpers import (
    HelpButtonTarget as _HelpButtonTarget,
    parse_version,
    main_thread as _main_thread,
    set_main_thread_logger as _set_main_thread_logger,
    BackupProgressWindow as _BackupProgressWindow,
    LogViewerActions as _LogViewerActions,
    LogViewerWindow as _LogViewerWindow,
    ensure_edit_menu as _ensure_edit_menu,
)
from onionpress import browser as op_browser
from onionpress.log_rotation import RotatingLog, current_log_path
from onionpress import redact
from onionpress import wp_files
from onionpress import launcher_ops
from onionpress import auto_login
from onionpress import splash_tips



class OnionPressApp(rumps.App):
    # The daemon (onionpress.daemon, `onionpress daemon` under launchd) runs
    # every loop; this process shows its state file and sends commands
    # through the launcher. How often the state is read and the menu
    # repainted; a state older than daemon.STALE_AFTER_SECONDS, or from a
    # dead pid, reads as "reconnecting", never as "stopped".
    VIEW_TICK_SECONDS = 2
    DAEMON_START_RETRY_SECONDS = 30

    def __init__(self):
        # Get paths first (fast - no I/O)
        self.app_support = os.path.expanduser("~/.onionpress")
        self.startup_time = time.time()
        self.script_dir = os.path.dirname(os.path.realpath(__file__))

        # Single-instance safety net via PID file
        self.pid_file = os.path.join(self.app_support, "menubar.pid")
        os.makedirs(self.app_support, exist_ok=True)
        if os.path.exists(self.pid_file):
            try:
                # If PID file is older than system boot, it's from a previous
                # boot session — definitely stale (PID may have been recycled)
                import subprocess as _sp
                _boot = _sp.run(['sysctl', '-n', 'kern.boottime'],
                                capture_output=True, text=True, timeout=5)
                # Format: "{ sec = 1717000000, usec = 0 } Sun ..."
                _boot_sec = int(_boot.stdout.split('sec = ')[1].split(',')[0])
                _pid_mtime = os.path.getmtime(self.pid_file)
                if _pid_mtime < _boot_sec:
                    raise ProcessLookupError("PID file from previous boot")

                with open(self.pid_file) as f:
                    old_pid = int(f.read().strip())
                # Check if that PID is still alive
                os.kill(old_pid, 0)
                # Process is alive — signal reopen and exit
                reopen_file = os.path.join(self.app_support, ".reopen")
                with open(reopen_file, 'w') as f:
                    f.write(str(os.getpid()))
                sys.exit(0)
            except (ProcessLookupError, ValueError, OSError):
                # Stale PID file — continue launching
                pass
        # Write our PID
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))
        # Register cleanup for normal exit
        atexit.register(self._remove_pid_file)
        # Register signal handlers for clean removal on SIGTERM/SIGINT
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # When running as py2app bundle, __file__ is in Contents/Resources/
        # so we need to use that as resources_dir, not the parent
        if getattr(sys, 'frozen', False):
            # Running as py2app bundle
            # __file__ is like: .../MenubarApp/Contents/Resources/menubar.py (in zip)
            # MenubarApp is nested inside OnionPress.app
            # Structure: OnionPress.app/Contents/Resources/MenubarApp/Contents/Resources/menubar.py
            menubar_resources_dir = os.path.join(os.environ.get('RESOURCEPATH', ''))
            if not menubar_resources_dir:
                # Fallback: get from bundle structure
                bundle_contents = os.path.dirname(os.path.dirname(self.script_dir))
                menubar_resources_dir = os.path.join(bundle_contents, 'Resources')

            # Keep menubar resources for icons
            self.resources_dir = menubar_resources_dir

            # Navigate to parent OnionPress.app bundle for launcher script and bin dir
            # MenubarApp/Contents/Resources -> MenubarApp/Contents -> MenubarApp -> OnionPress.app/Resources -> OnionPress.app/Contents
            menubar_contents = os.path.dirname(menubar_resources_dir)  # MenubarApp/Contents
            menubar_app = os.path.dirname(menubar_contents)  # MenubarApp
            parent_resources = os.path.dirname(menubar_app)  # OnionPress.app/Contents/Resources
            self.parent_resources_dir = parent_resources  # Store for accessing docker/ and other parent resources
            self.contents_dir = os.path.dirname(parent_resources)  # OnionPress.app/Contents
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onionpress")
            self.bin_dir = os.path.join(parent_resources, "bin")
        else:
            # Running as regular Python script
            self.resources_dir = os.path.dirname(self.script_dir)
            self.parent_resources_dir = self.resources_dir  # Same as resources_dir when not bundled
            self.contents_dir = os.path.dirname(self.resources_dir)
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onionpress")
            self.bin_dir = os.path.join(self.resources_dir, "bin")
        self.colima_home = os.path.join(self.app_support, "colima")
        self.info_plist = os.path.join(self.contents_dir, "Info.plist")
        self.logs_dir = os.path.join(self.app_support, "logs")
        # This process's own log (menubar-YYYY-MM-DD-NNN.log). onionpress.log
        # is the daemon's, which runs the loops; View Logs opens both.
        self._menubar_log = RotatingLog(
            self.logs_dir, "menubar",
            scrub_fn=redact.make_scrub_fn(self.app_support, scrub_ips=False))
        self.log_file = self._menubar_log.current_path()
        self.config_file = os.path.join(self.app_support, "config")

        if getattr(sys, 'frozen', False):
            _app_bundle = os.path.dirname(self.contents_dir)
        else:
            _app_bundle = None
        self._paths = resolve_paths(data_dir=self.app_support, app_bundle=_app_bundle)

        # Initialize rumps WITHOUT icon first (fastest possible)
        super(OnionPressApp, self).__init__("", quit_button=None, template=False)

        # LSUIElement apps have no visible menu bar, but ⌘-key equivalents
        # (⌘V paste, ⌘C copy, …) are still dispatched through the main menu —
        # without this, pasting a password into the setup window just beeps.
        _ensure_edit_menu()

        # Detect first-run early so we can show the right window.
        # Use the .setup_complete marker (written when a setup completes:
        # setup_logic.mark_setup_complete from the setup window, the
        # launcher's `onionpress setup`, and the daemon once the site is
        # operational) instead of secrets (which the launcher recreates
        # before Python starts).
        self._is_first_run = False
        setup_marker = os.path.join(self.app_support, ".setup_complete")
        if not os.path.exists(setup_marker):
            self._is_first_run = True
        # Check FORCE_SETUP_WINDOW in config
        config_file = os.path.join(self.app_support, "config")
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.strip() == "FORCE_SETUP_WINDOW=yes":
                            self._is_first_run = True
                            break
            except Exception:
                pass

        # Always show splash first — setup window replaces it from auto_start()
        self.launch_splash = None
        self.launch_splash_time_field = None
        self.launch_splash_tip_field = None
        self.launch_splash_tip_timer = None
        self._launch_splash_tip_tick = 0
        self.show_launch_splash()

        # Now load icon files (this does I/O but splash is already showing)
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Set the stopped icon
        self.icon = self.icon_stopped

        # Set version to placeholder (will be updated in background)
        self.version = "2.5.6"

        # The environment of this process's children: the launcher (which
        # sets its own), auto_login's token mint and the first-run setup's
        # wp-cli calls run the bundled docker against this install's VM.
        docker_config_dir = os.path.join(self.app_support, "docker-config")
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        # Colima honours COLIMA_HOME only when the directory exists (else it
        # uses ~/.colima, socket forward included): make it before the
        # first colima call below, on a fresh install too.
        os.makedirs(os.environ["LIMA_HOME"], exist_ok=True)
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        os.environ["DOCKER_CONFIG"] = docker_config_dir

        # The daemon's state: what this process shows. Ports come from it
        # (it resolves the multi-user offset at its start: the running
        # stack's published port, else the first free offset); until it has
        # written one, the environment's offset or the last state's.
        self.state = {}
        self._state_fresh = False
        self._daemon_seen = False
        self._daemon_missing_since = None
        self._daemon_start_attempted_at = 0.0
        self._last_painted = None
        self._asked_question = None
        self._asking_question = False
        self._relaunch_handled = None
        self._collision_handled = None
        self._settings_error_shown = ""
        self._port_conflict_shown = False
        last_state = op_daemon.read_state(self.app_support) or {}
        _offset = os.environ.get("ONIONPRESS_PORT_OFFSET")
        if _offset is None:
            _offset = (last_state.get("ports") or {}).get("offset", 0)
        self._apply_ports(op_config.PortConfig.from_offset(int(_offset or 0)))

        # Do slow I/O operations in background after icon appears
        def background_init():
            # Hook up the UI crash logger so main_thread() exceptions
            # get written to the menubar log
            _set_main_thread_logger(self.log)

            self.log("=" * 60)
            self.log("=== New session starting (menubar) ===")
            self.log("=" * 60)
            self.log(f"DEBUG: frozen={getattr(sys, 'frozen', False)}")
            self.log(f"DEBUG: resources_dir={self.resources_dir}")
            self.log(f"DEBUG: bin_dir={self.bin_dir}")
            self.log(f"DEBUG: launcher_script={self.launcher_script}")
            self.log(f"DEBUG: icon_stopped exists={os.path.exists(self.icon_stopped)}")

            # Get actual version from Info.plist
            self.version = self.get_version()
            self.log(f"Version: {self.version}")

            # Multi-user: ensure app bundle is group-writable so either user can update
            self._fix_app_bundle_permissions()

            # Update browser menu title after checking filesystem
            self.update_browser_menu_title()

            # Install native messaging manifests for browser extension support
            try:
                install_native_messaging.install(log_func=self.log)
            except Exception as e:
                self.log(f"Native messaging install failed: {e}")

            # Sync login item LaunchAgent with config
            launch_on_login = self.read_config_value("LAUNCH_ON_LOGIN", "yes")
            if launch_on_login == "yes" and not self._is_login_item_installed():
                self.add_login_item()
            elif launch_on_login != "yes" and self._is_login_item_installed():
                self.remove_login_item()

            # Check if Cloudflare Tunnel is configured
            if self.read_config_value("CLOUDFLARE_TUNNEL_TOKEN"):
                self.cloudflare_tunnel_enabled = True
                self.log("Cloudflare Tunnel configured")
                # Detect host-level cloudflared that would conflict with container
                self._check_host_cloudflared()

        # Start background initialization
        threading.Thread(target=background_init, daemon=True).start()

        # State — load cached onion address from previous run if available
        cached_addr_file = os.path.join(self.app_support, "onion_address")
        self._had_cached_address = False  # True if a previous session's address was found
        try:
            with open(cached_addr_file) as f:
                cached = f.read().strip()
            if key_manager.is_valid_onion_address(cached):
                self.onion_address = cached
                self._had_cached_address = True
            else:
                self.onion_address = "Starting..."
        except (OSError, IOError):
            self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
        self.last_status_logged = None
        # Only auto-open browser on first-ever run; on restarts the user
        # already knows their address so opening the browser is unwanted.
        # On reinstall, _is_first_run is True even if a key was imported and
        # wrote a cached address before Python started — always open then.
        self.auto_opened_browser = self._had_cached_address and not self._is_first_run
        self.setup_dialog_showing = False  # Track if setup dialog is currently showing
        self.setup_alert = None  # Reference to NSAlert for programmatic dismissal
        self.monitoring_tor_install = False  # Track if we're monitoring for Tor Browser installation
        self._wp_installed = None  # None = unknown, True/False = checked (the daemon's)
        self._last_bootstrap_pct = 0       # Last observed Tor bootstrap percentage
        self.healthcheck_address = None    # Healthcheck .onion address
        self.onionheaven_messages = []          # Messages received from OnionHeaven (the daemon's)
        # True if this instance is the OnionHeaven hub. Identity, so it is
        # known with the address (cached one here, live one from the daemon).
        self.is_onionheaven = onionheaven.is_onionheaven_instance(self.onion_address)
        self.cloudflare_tunnel_enabled = False  # True when CLOUDFLARE_TUNNEL_TOKEN is set
        self._quitting = False                 # True once quit cleanup has started
        self._stopping = False                 # True while Stop button is in progress
        self._start_failed_rc = None

        # Menu items
        # Store reference to browser menu item so we can update its title
        self.browser_menu_item = rumps.MenuItem("Open in Tor Browser", callback=self.open_tor_browser)
        self.local_site_item = rumps.MenuItem("Open Local Site", callback=self.open_local_site)
        self.onionheaven_alert_item = rumps.MenuItem("OnionHeaven Alerts", callback=self.view_onionheaven_alerts)
        self._onionheaven_alert_in_menu = False
        self.clearnet_status_item = rumps.MenuItem("", callback=None)

        self.menu = [
            rumps.MenuItem("Starting...", callback=None),
            rumps.separator,
            rumps.MenuItem("Copy Onion Address", callback=self.copy_address),
            self.browser_menu_item,
            self.local_site_item,
            rumps.separator,
            rumps.MenuItem("Start", callback=self.start_service),
            rumps.MenuItem("Stop", callback=self.stop_service),
            rumps.MenuItem("Restart", callback=self.restart_service),
            rumps.separator,
            rumps.MenuItem("View Logs", callback=self.view_logs),
            rumps.MenuItem("View Web Usage Log", callback=self.view_web_log),
            rumps.MenuItem("Settings...", callback=self.open_settings),
            rumps.separator,
            rumps.MenuItem("Backup...", callback=self.backup),
            rumps.MenuItem("Restore...", callback=self.restore),
            rumps.separator,
            rumps.MenuItem("Check for Updates...", callback=self.check_for_updates),
            rumps.MenuItem("About OnionPress", callback=self.show_about),
            rumps.MenuItem("Uninstall...", callback=self.uninstall),
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Apple Event quit (osascript, the launcher's SIGTERM) runs the
        # same teardown as the Quit item. Sleep and wake are the daemon's.
        self._register_terminate_notification()

        # Listen for reopen notification from Swift launcher wrapper
        self._register_reopen_notification()

        # The view: read the daemon's state, repaint, react to its
        # transitions, start the daemon when there is none.
        self.start_view_loop()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

    def log(self, message):
        """Write a log message to this process's menubar log"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"[{timestamp}] {message}\n"
            self._menubar_log.write(log_message)
            self.log_file = self._menubar_log.current_path()
        except Exception as e:
            print(f"Error writing to log: {e}")

    def read_config_value(self, key, default=""):
        """Read a value from the config file."""
        return op_config.read_value(self.config_file, key, default)

    # ── The view of the daemon's state ───────────────────────────────

    def _apply_ports(self, pc):
        self.port_offset = pc.offset
        self.wp_port = pc.wp_port
        self.socks_port = pc.socks_port
        self.proxy_port = pc.proxy_port
        os.environ["ONIONPRESS_PORT_OFFSET"] = str(pc.offset)
        os.environ["ONIONPRESS_WP_PORT"] = str(self.wp_port)
        os.environ["ONIONPRESS_SOCKS_PORT"] = str(self.socks_port)
        os.environ["ONIONPRESS_PROXY_PORT"] = str(self.proxy_port)

    @property
    def display_state(self):
        """'stopped', 'available', 'offline', 'stalled', 'stuck',
        'starting' from the daemon's state, or 'reconnecting' when there
        is no fresh state (the daemon is starting, restarting under
        launchd's KeepAlive, or gone)."""
        if self._quitting:
            return "stopped"
        if not self._state_fresh:
            return "reconnecting"
        return op_daemon.display_state_of(self.state, time.time())

    def start_view_loop(self):
        """Background thread: the daemon's state file every
        VIEW_TICK_SECONDS. No docker, no Tor, no hub: a file read."""
        def loop():
            while not self._quitting:
                try:
                    self._view_tick()
                except Exception as e:
                    self.log(f"view: {e}")
                time.sleep(self.VIEW_TICK_SECONDS)
        threading.Thread(target=loop, daemon=True, name="view").start()

    def _view_tick(self):
        now = time.time()
        # Reopen signal from the launcher (user double-clicked the app)
        reopen_file = os.path.join(self.app_support, ".reopen")
        if os.path.exists(reopen_file):
            try:
                os.remove(reopen_file)
            except OSError:
                pass
            self.handle_reopen()

        state = op_daemon.read_state(self.app_support)
        fresh = op_daemon.state_is_fresh(state, now)
        was_fresh = self._state_fresh
        self._state_fresh = fresh
        if fresh:
            if not self._daemon_seen:
                self.log(f"Daemon state found (pid {state.get('pid')}, v{state.get('version')})")
            if self._daemon_missing_since is not None and self._daemon_seen:
                self.log(f"Daemon back after {int(now - self._daemon_missing_since)}s")
            self._daemon_seen = True
            self._daemon_missing_since = None
            self._apply_state(state)
        else:
            if self._daemon_missing_since is None:
                self._daemon_missing_since = now
                if was_fresh:
                    self.log("Daemon state is stale — reconnecting")
            self.ensure_daemon_running(now)

        painted = (self.display_state, self.onion_address, len(self.onionheaven_messages),
                   self.cloudflare_tunnel_enabled, self._last_bootstrap_pct, self.wp_port,
                   self._start_failed_rc, self.read_config_value("ONIONNAME", "").strip())
        if painted != self._last_painted or now - getattr(self, "_last_paint_ts", 0) > 30:
            self._last_painted = painted
            self._last_paint_ts = now
            self.update_menu()

    def ensure_daemon_running(self, now=None):
        """Start the daemon's launchd agent when it is not loaded or has
        no process (at most once per DAEMON_START_RETRY_SECONDS). The
        launcher's `start` does the same, so a headless start has it too."""
        now = time.time() if now is None else now
        if self._quitting or now - self._daemon_start_attempted_at < self.DAEMON_START_RETRY_SECONDS:
            return
        self._daemon_start_attempted_at = now
        if op_daemon.daemon_pid(self.app_support):
            return      # alive, its first state is on its way
        loaded, pid = op_daemon.agent_loaded_pid()
        if pid:
            return      # launchd has it (restarting after a crash: KeepAlive)
        self.log("Daemon not running — starting it (onionpress daemon start)")
        try:
            subprocess.run([self.launcher_script, "daemon", "start"],
                           capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"Could not start the daemon: {e}")

    def _apply_state(self, state):
        """Copy what the menu shows out of the daemon's state and react to
        what changed since the last tick."""
        was_running, was_ready = self.is_running, self.is_ready
        self.is_running = bool(state.get("is_running"))
        self.is_ready = bool(state.get("is_ready"))
        if self.is_running:
            self._start_failed_rc = None
        addr = state.get("onion_address") or ""
        if key_manager.is_valid_onion_address(addr):
            self.onion_address = addr
            self.is_onionheaven = bool(state.get("is_onionheaven")) or onionheaven.is_onionheaven_instance(addr)
        elif state.get("address_placeholder") and not key_manager.is_valid_onion_address(self.onion_address):
            self.onion_address = state["address_placeholder"]
        self.healthcheck_address = state.get("healthcheck_address") or None
        self._last_bootstrap_pct = int(state.get("bootstrap_pct") or 0)
        self.cloudflare_tunnel_enabled = bool(state.get("cloudflare_tunnel_enabled"))
        self.onionheaven_messages = list(state.get("onionheaven_messages") or [])
        self._wp_installed = state.get("wp_installed")
        ports = state.get("ports") or {}
        if ports.get("offset") is not None and int(ports["offset"]) != self.port_offset:
            self.log(f"Ports: offset {ports['offset']} from the daemon (was {self.port_offset})")
            self._apply_ports(op_config.PortConfig.from_offset(int(ports["offset"])))

        if self.is_running and not self.is_ready and not self._quitting:
            pct = self._last_bootstrap_pct
            if pct >= 100:
                self.update_splash_status("Waiting for onion service to become reachable...")
            elif pct > 0:
                self.update_splash_status(f"Tor bootstrap: {pct}%")

        if self.is_ready and not was_ready:
            self._on_ready()
        if was_running and not self.is_running:
            # Only dismiss the setup dialog when actually stopping (not during startup)
            self.dismiss_setup_dialog()

        conflict = state.get("port_conflict") or []
        if conflict and not self.is_running and not self._port_conflict_shown:
            self._port_conflict_shown = True
            ports_str = ', '.join(str(p) for p in conflict)
            self.log(f"Port conflict detected: ports {ports_str} already in use by another process")
            _main_thread(lambda: rumps.alert(
                title="OnionPress Cannot Start",
                message=f"Port(s) {ports_str} already in use.\n\n"
                        "Another process is using these ports.\n\n"
                        "Close the conflicting application and try again."))
        elif not conflict:
            self._port_conflict_shown = False

        question = state.get("pending_question")
        if question and question != self._asked_question and not self._asking_question:
            self._asked_question = question
            self._asking_question = True
            threading.Thread(target=self._answer_settings_page_question,
                             args=(question,), daemon=True).start()
        elif not question:
            self._asked_question = None

        relaunch = state.get("relaunch")
        if relaunch and relaunch != self._relaunch_handled:
            self._relaunch_handled = relaunch
            self.log(f"Daemon asks for a relaunch (update to v{relaunch.get('version') or '?'})")
            self._relaunch_app(os.path.dirname(self.contents_dir))

        collision = state.get("onionname_collision")
        if collision and collision != self._collision_handled:
            self._collision_handled = collision
            threading.Thread(target=self._resolve_onionname_collision,
                             args=(collision,), daemon=True).start()

        error = state.get("settings_page_error") or ""
        if error and error != self._settings_error_shown:
            self._settings_error_shown = error
            self.show_native_alert(
                "Settings Page Requests Not Handled",
                "OnionPress could not handle the WordPress Settings page's "
                f"requests:\n\n{error}\n\nSee the OnionPress log.",
                style="warning")

    def _on_ready(self):
        """The daemon says the onion service is reachable: the setup
        window's last steps, the browser on a first run, the splash."""
        self.log(f"✓ System fully operational (launched in {int(time.time() - self.startup_time)}s)")
        self.dismiss_setup_dialog()
        if setup_window and setup_window._setup_window is not None:
            sw = setup_window._setup_window
            if sw.window:
                sw.set_step(5)
                sw.add_log("Onion service reachable through Tor")
                sw.complete_step(5)
                sw.set_status("Starting heartbeat...")
                sw.add_log("Starting heartbeat...")
                sw.complete_step(6)
                sw.set_status("Opening tor-enabled browser...")
                sw.add_log("Opening tor-enabled browser...")
        if not self.auto_opened_browser:
            self.auto_opened_browser = True
            self.log(f"DEBUG: Spawning auto_open_browser thread, onion_address={self.onion_address!r}")
            threading.Thread(target=self.auto_open_browser, daemon=True).start()
        if setup_window and setup_window._setup_window is not None:
            sw = setup_window._setup_window
            if sw.window:
                sw.complete_step(7)
                sw.show_completion(self.onion_address)
                def _close_setup():
                    time.sleep(10)
                    setup_window.close_setup_progress()
                threading.Thread(target=_close_setup, daemon=True).start()
        self.update_menu()
        # Dismiss splash AFTER icon turns purple
        self.dismiss_launch_splash()

    def _answer_settings_page_question(self, question):
        """A Settings-page request the daemon's handle-action stopped at
        (a key import, a restore of another address): asked here in a
        native dialog, answered with `handle-action --confirmed <what>`
        or `--cancel` (the pending-action protocol, issue #366)."""
        try:
            answered_yes = self._ask_settings_page_question(question)
            args = ["--confirmed", question["confirm"]] if answered_yes else ["--cancel"]
            try:
                r = subprocess.run(
                    [self.launcher_script, "handle-action", "--json", *args],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                self.log(f"Settings page: could not run onionpress handle-action: {e}")
                return
            result = op_confirm.last_json_line(r.stdout)
            if r.returncode != 0 or result is None:
                detail = (r.stderr or r.stdout).strip()[-400:]
                self.log(f"Settings page: onionpress handle-action failed (rc {r.returncode}): {detail}")
            if result and result.get("relaunch"):
                self._relaunch_app(os.path.dirname(self.contents_dir))
        except Exception as e:
            self.log(f"Settings page: {e}")
        finally:
            self._asking_question = False

    def _resolve_onionname_collision(self, collision):
        """The daemon's onionname retry hit a collision: ask for another
        name, record it with `config set ONIONNAME`, and have the daemon
        try again (.onionname-retry)."""
        name = collision.get("name") or ""
        new_name = self._prompt_onionname_collision(name, collision.get("suggestions") or [])
        if not new_name:
            self.log("onionname: collision left unresolved (no new name chosen)")
            return
        try:
            from onionpress.onionnames_client import validate_name
            if not validate_name(new_name)[0]:
                self.log(f"onionname: {new_name!r} is not a valid onionname; not changed")
                return
        except ImportError:
            pass
        self._rename_wp_admin(name, new_name)
        self.write_config_value("ONIONNAME", new_name)
        self.write_config_value("ONIONNAME_REGISTERED", "no")
        try:
            with open(os.path.join(self.app_support, op_daemon.ONIONNAME_RETRY_MARKER), "w") as f:
                f.write(new_name)
        except OSError:
            pass
        self.log(f"onionname: '{name}' -> '{new_name}', registration retried by the daemon")

    def _wait_for_state(self, predicate, timeout):
        """Wait up to *timeout* s for the daemon's state to satisfy
        *predicate*; True when it did."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._state_fresh and predicate(self.state):
                return True
            time.sleep(2)
        return False

    def show_launch_splash(self):
        """Show launch splash with logo synchronously (called from __init__ on main thread)"""
        try:
            # Create window - taller for buttons and time estimate
            window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                AppKit.NSMakeRect(0, 0, 320, 300),
                AppKit.NSWindowStyleMaskTitled,  # No close button - dismisses automatically when ready
                AppKit.NSBackingStoreBuffered,
                False
            )
            window.setTitle_("OnionPress")
            window.setLevel_(AppKit.NSFloatingWindowLevel)
            window.center()
            window.setReleasedWhenClosed_(False)  # Keep window object alive
            window.setHidesOnDeactivate_(False)  # Stay visible when clicking other windows

            # Create content view
            content_view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 300))

            # Add "Launching..." text
            text_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(60, 120, 200, 30))
            text_field.setStringValue_("Launching OnionPress...")
            text_field.setBezeled_(False)
            text_field.setDrawsBackground_(False)
            text_field.setEditable_(False)
            text_field.setSelectable_(False)
            text_field.setAlignment_(AppKit.NSTextAlignmentCenter)
            font = AppKit.NSFont.systemFontOfSize_(16)
            text_field.setFont_(font)
            content_view.addSubview_(text_field)

            # Add estimated time text
            time_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(40, 90, 240, 20))
            time_field.setStringValue_("Estimated time: ~3 minutes")
            time_field.setBezeled_(False)
            time_field.setDrawsBackground_(False)
            time_field.setEditable_(False)
            time_field.setSelectable_(False)
            time_field.setAlignment_(AppKit.NSTextAlignmentCenter)
            time_field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            small_font = AppKit.NSFont.systemFontOfSize_(12)
            time_field.setFont_(small_font)
            content_view.addSubview_(time_field)

            # Rotating "what you can do" tip under the status line (#166).
            # Two wrapped lines of small secondary text in the gap between the
            # status line (y=90) and the buttons (y=52).
            tip_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(20, 56, 280, 32))
            tip_field.setStringValue_(splash_tips.tip_at(0))
            tip_field.setBezeled_(False)
            tip_field.setDrawsBackground_(False)
            tip_field.setEditable_(False)
            tip_field.setSelectable_(False)
            tip_field.setAlignment_(AppKit.NSTextAlignmentCenter)
            tip_field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            tip_field.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            tip_field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            tip_field.cell().setWraps_(True)
            content_view.addSubview_(tip_field)

            # Add View Log button
            view_log_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(20, 20, 130, 32))
            view_log_button.setTitle_("View Log")
            view_log_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            view_log_button.setTarget_(self)
            view_log_button.setAction_("openLogFile:")
            content_view.addSubview_(view_log_button)

            # Add Dismiss button
            dismiss_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(170, 20, 130, 32))
            dismiss_button.setTitle_("Dismiss")
            dismiss_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            dismiss_button.setTarget_(self)
            dismiss_button.setAction_("dismissSplashButton:")
            content_view.addSubview_(dismiss_button)

            # Add logo (fast local PNG load)
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                image_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(110, 180, 100, 100))
                image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if image:
                    image_view.setImage_(image)
                    content_view.addSubview_(image_view)

            window.setContentView_(content_view)
            window.makeKeyAndOrderFront_(None)

            self.launch_splash = window
            self.launch_splash_time_field = time_field  # Store reference for updates
            self.launch_splash_tip_field = tip_field
            self._launch_splash_tip_tick = 0
            self.start_splash_tip_rotation()

            # Log splash creation
            try:
                self.log("DEBUG: Launch splash created and shown")
            except Exception:
                pass

        except Exception as e:
            pass  # Don't crash on splash failure

    def dismiss_launch_splash(self):
        """Dismiss the launch splash window"""
        def dismiss():
            self.stop_splash_tip_rotation()
            if self.launch_splash:
                try:
                    self.log("Dismissing launch splash")
                    self.launch_splash.orderOut_(None)
                    self.launch_splash.close()
                    self.launch_splash = None
                except Exception as e:
                    self.log(f"Error dismissing launch splash: {e}")

        # Dismiss on main thread
        _main_thread(dismiss)

    def update_splash_status(self, message):
        """Update the launch splash status text from any thread."""
        def _update():
            if self.launch_splash_time_field:
                self.launch_splash_time_field.setStringValue_(message)
        _main_thread(_update)

    def start_splash_tip_rotation(self):
        """Cycle the splash's tip label while the splash is up (#166).

        Called on the main thread from show_launch_splash(). The NSTimer is
        scheduled on the main run loop in the common modes so it keeps
        firing while a menu is open; which tip shows is decided by the pure
        ``onionpress.splash_tips.tip_at`` so the order is unit-testable.
        The timer is invalidated by stop_splash_tip_rotation(), which every
        splash-dismiss path goes through.
        """
        try:
            def _advance(_timer):
                try:
                    if not self.launch_splash or not self.launch_splash_tip_field:
                        self.stop_splash_tip_rotation()
                        return
                    self._launch_splash_tip_tick += 1
                    self.launch_splash_tip_field.setStringValue_(
                        splash_tips.tip_at(self._launch_splash_tip_tick))
                except Exception as e:
                    self.log(f"Splash tip rotation error: {e}")

            timer = AppKit.NSTimer.timerWithTimeInterval_repeats_block_(
                splash_tips.TIP_INTERVAL_SECONDS, True, _advance)
            AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
            self.launch_splash_tip_timer = timer
        except Exception as e:
            # Tips are a nicety; the splash must still show without them.
            self.log(f"Could not start splash tip rotation: {e}")

    def stop_splash_tip_rotation(self):
        """Stop the splash tip timer (main thread; idempotent)."""
        timer = self.launch_splash_tip_timer
        self.launch_splash_tip_timer = None
        if timer is not None:
            try:
                timer.invalidate()
            except Exception as e:
                self.log(f"Error stopping splash tip rotation: {e}")

    def openLogFile_(self, sender):
        """Action handler for View Log button — open in built-in log viewer"""
        try:
            _LogViewerWindow.show_for_file(self.log_file, "OnionPress Log")
        except Exception as e:
            self.log(f"Error opening log file: {e}")

    def dismissSplashButton_(self, sender):
        """Action handler for Dismiss button"""
        self.dismiss_launch_splash()





    def show_native_alert(self, title, message, buttons=["OK"], default_button=0, cancel_button=None, style="informational"):
        """Show a native macOS alert dialog using AppKit (no permission prompts, shows custom icon)

        Args:
            title: Dialog title
            message: Dialog message text
            buttons: List of button labels (default: ["OK"])
            default_button: Index of default button (default: 0)
            cancel_button: Index of cancel button or None (default: None)
            style: "informational", "warning", or "critical" (default: "informational")

        Returns:
            Index of clicked button (0-based), or None if dialog dismissed
        """
        def show_dialog():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)

            # Set alert style
            if style == "warning":
                alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            elif style == "critical":
                alert.setAlertStyle_(AppKit.NSAlertStyleCritical)
            else:
                alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            # Add buttons (first button is default)
            for i, button_text in enumerate(buttons):
                btn = alert.addButtonWithTitle_(button_text)
                if i == default_button:
                    btn.setKeyEquivalent_("\r")  # Return key
                elif cancel_button is not None and i == cancel_button:
                    btn.setKeyEquivalent_("\x1b")  # Escape key

            # Set app icon if available
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Show modal dialog and get response
            response = alert.runModal()

            # Convert response to button index
            # NSAlertFirstButtonReturn = 1000, second = 1001, etc.
            button_index = response - 1000
            return button_index if button_index >= 0 else None

        # Must run on main thread
        # Check if we're already on the main thread to avoid deadlock
        if AppKit.NSThread.isMainThread():
            # Already on main thread, run directly
            return show_dialog()
        else:
            # Not on main thread, dispatch to main thread and wait
            result_container = [None]
            def run_on_main():
                result_container[0] = show_dialog()

            _main_thread(run_on_main)

            # Wait for result (with timeout)
            max_wait = 300  # 5 minutes
            waited = 0
            while result_container[0] is None and waited < max_wait:
                time.sleep(0.1)
                waited += 0.1

            return result_container[0]





    # --- Container log capture (supervised) -----------------------------
    #
    # A single supervisor thread periodically reconciles the desired set
    # of captures (from docker-compose + dynamically-discovered takeover
    # workers) with the set of live capture workers. Each worker has its
    # own reattach loop: if ``docker logs -f`` exits (container restart,
    # daemon hiccup, log-stream break), the worker re-launches with
    # ``--since <last-seen>`` so we don't lose the window between
    # exit and reattach. A vanished container lets the worker drain and
    # exit; if the container later returns, the next supervisor sweep
    # starts a fresh worker.











    def auto_start(self):
        """Automatically start the service when the app launches"""
        time.sleep(1)  # Brief delay

        # Wait for the container runtime, as the daemon reports it (it
        # waits for Colima the launcher initializes on a first run).
        self.log("Waiting for container runtime to be ready...")
        msg = "Preparing your site..."
        self.update_splash_status(msg)
        if self._is_first_run and setup_window and setup_window._setup_window:
            setup_window._setup_window.set_status(msg)
            setup_window._setup_window.add_log(msg)

        # Wait up to 3 minutes for Colima initialization
        if self._wait_for_state(lambda s: s.get("runtime_ready"), 180):
            self.log("Container runtime is ready")
        else:
            self.log("WARNING: Container runtime not ready after 3 minutes")
            # The splash otherwise stays on "Preparing your site..." through
            # the launcher's own 180s Docker wait that follows.
            self.update_splash_status("Container runtime did not start in 3 minutes — see View Logs")

        # A port conflict (another user's OnionPress or other process) is
        # the daemon's finding: it reports it only while our containers
        # are not running, and _apply_state shows the dialog.
        if self._state_fresh and (self.state.get("port_conflict") or []) and not self.is_running:
            time.sleep(10)      # a previous instance may still be releasing them
        if self._state_fresh and (self.state.get("port_conflict") or []) and not self.is_running:
            self.menu["Starting..."].title = "Status: Port conflict"
            return

        # UPDATE_ON_LAUNCH is the launcher's: its `start` runs update_images()
        # once, before the containers. The menubar used to run its own
        # `update-images` here first, so a `yes` cost two registry passes
        # per launch (issue #36); one is the launcher's.

        # First run: show welcome screen, wait for user to click "Set Up",
        # then start_service runs in the callback
        if self._is_first_run and setup_window:
            self.dismiss_launch_splash()
            sw = setup_window.get_setup_window()
            sw.set_on_setup(lambda: self._first_run_after_welcome())
            setup_window.show_welcome_screen()
            return  # Don't call start_service — the callback will

        self.start_service(None)


    # The login item is onionpress.login_item's, shared with
    # `onionpress config set LAUNCH_ON_LOGIN` (config.apply_side_effects).
    LAUNCHAGENT_LABEL = login_item.LAUNCHAGENT_LABEL
    LAUNCHAGENT_PATH = login_item.launchagent_path()

    def _is_login_item_installed(self):
        """Check if LaunchAgent plist exists"""
        return login_item.is_installed()

    def add_login_item(self):
        """Install LaunchAgent plist for auto-start on login"""
        return login_item.install(os.path.dirname(self.contents_dir), self.log)

    def remove_login_item(self):
        """Remove LaunchAgent plist"""
        return login_item.remove(self.log)


    def run_command(self, command):
        """Run a command and return output"""
        try:
            result = subprocess.run(
                [self.launcher_script, command],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=60
            )
            return result.stdout.strip()
        except Exception as e:
            # Went to stdout (launcher.log) before; a 60s timeout here is what
            # makes a wedged Docker look like "Stopped", so it belongs in our log.
            self.log(f"Error running command {command}: {e}")
            return None


    def _remove_pid_file(self):
        """Remove PID file on exit"""
        try:
            if os.path.exists(self.pid_file):
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                if pid == os.getpid():
                    os.remove(self.pid_file)
        except Exception:
            pass

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT — trigger graceful quit (same as Quit button)"""
        self.log(f"Received signal {signum}, initiating graceful shutdown...")
        _main_thread(lambda: self.quit_app(None))







    def _check_host_cloudflared(self):
        """Warn if a host-level cloudflared is running outside Docker.

        When OnionPress runs cloudflared inside a container, a second
        host-level cloudflared (e.g. installed via Homebrew) using the
        same tunnel token causes intermittent 502 errors — the host
        connector can't resolve the Docker-internal 'wordpress' hostname.
        """
        try:
            result = subprocess.run(
                ["pgrep", "-u", "root", "-f", "cloudflared.*tunnel.*run"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                self.log("WARNING: Host-level cloudflared detected — this causes intermittent 502 errors")
                _main_thread(lambda: self.show_native_alert(
                    "Host cloudflared Conflicts with OnionPress",
                    "A system-level cloudflared service is running on this Mac "
                    "(likely installed via Homebrew).\n\n"
                    "OnionPress runs its own cloudflared inside Docker. Having both "
                    "causes intermittent 502 errors — about half of visitors will "
                    "see an error page.\n\n"
                    "To fix, run in Terminal:\n"
                    "  sudo cloudflared service uninstall\n"
                    "  brew uninstall cloudflared\n\n"
                    "Then restart OnionPress.",
                    style="warning"
                ))
        except Exception as e:
            self.log(f"Host cloudflared check failed: {e}")




    @staticmethod
    def _short_onion(addr):
        """Truncate a 56-char .onion so the menu doesn't stretch.
        Full address is one click away via Copy Onion Address."""
        if addr and len(addr) > 16 and addr.endswith(".onion"):
            return f"{addr[:8]}…{addr[-6:]}"
        return addr






    def _register_reopen_notification(self):
        """Listen for distributed notification from Swift launcher wrapper.

        This fires immediately when the user double-clicks the app,
        instead of waiting for the next 30-second check_status poll.
        """
        dnc = AppKit.NSDistributedNotificationCenter.defaultCenter()
        dnc.addObserverForName_object_queue_usingBlock_(
            "press.onion.app.reopen",
            None,
            AppKit.NSOperationQueue.mainQueue(),
            lambda _: self.handle_reopen())
        self.log("Registered for reopen distributed notification")













    @property
    def local_url(self):
        """The local URL for accessing WordPress."""
        return f"http://localhost:{self.wp_port}"

    def _onion_url(self):
        """Return the full onion URL including /onionname if set."""
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            return None
        name = self.read_config_value("ONIONNAME", "")
        if name:
            return f"http://{self.onion_address}/{name}/"
        return f"http://{self.onion_address}/"

    @rumps.clicked("Copy Onion Address")
    def copy_address(self, _):
        """Copy onion address to clipboard"""
        url = self._onion_url()
        if url:
            subprocess.run(
                ["pbcopy"],
                input=url.encode(),
                check=True
            )
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def _generate_login_url(self, base_url):
        """Return base_url with a one-time auto-login token, or None.

        The token reaches WordPress on stdin (auto_login.mint) and the URL
        must reach the browser through op_browser.open_url, never through a
        command line: ps shows every user's argv, and whoever redeems the
        token first is the admin. Log base_url, never the result.
        """
        return auto_login.login_url(
            base_url, docker_bin=os.path.join(self.bin_dir, "docker"),
            log_func=self.log)

    def open_local_site(self, _):
        """Open the local WordPress site in the default browser"""
        name = self.read_config_value("ONIONNAME", "")
        local_base = f"{self.local_url}/{name}/" if name else self.local_url
        op_browser.open_url(self._generate_login_url(local_base) or local_base,
                            log_func=self.log)
        self.log(f"Opened local site: {local_base}")

    def monitor_tor_browser_install(self):
        """Monitor for Tor Browser installation and offer to open site when detected."""
        self._monitor_browser_install(
            "Tor Browser",
            op_browser.TOR_BROWSER_PATH,
            "Contents/MacOS/firefox",
            lambda url: op_browser.open_url(
                self._generate_login_url(url) or url, op_browser.TOR_BROWSER_PATH,
                self.log),
        )

    def monitor_brave_install(self):
        """Monitor for Brave Browser installation and offer to open site when detected."""
        self._monitor_browser_install(
            "Brave Browser",
            op_browser.BRAVE_BROWSER_PATH,
            "Contents/MacOS/Brave Browser",
            # No auto-login token: Brave's Tor window takes the URL only on
            # its command line, which every local user can read.
            lambda url: subprocess.run([
                os.path.join(op_browser.BRAVE_BROWSER_PATH, "Contents", "MacOS", "Brave Browser"),
                "--tor", url]),
        )

    def _monitor_browser_install(self, name, app_path, executable_subpath, open_func):
        """Generic browser install monitor."""
        if self.monitoring_tor_install:
            return
        self.monitoring_tor_install = True
        self.log(f"Starting {name} installation monitor")

        def _check():
            found = op_browser.wait_for_app_install(
                app_path, executable_subpath,
                cancel_check=lambda: not self.monitoring_tor_install,
            )
            self.monitoring_tor_install = False
            if not found:
                self.log(f"{name} installation monitor timed out")
                return
            self.log(f"{name} detected in Applications!")
            self.dismiss_setup_dialog()
            url = self._onion_url()
            if not url:
                url = f"http://{self.onion_address}/"
            try:
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"{name} is now installed!\n\nWould you like to open your site?\n\n{url}",
                    buttons=["Open Site", "Later"],
                    default_button=0,
                    style="informational"
                )
                if button_index == 0:
                    open_func(url)
                    self.log(f"Opened site in {name}: {url}")
            except Exception as e:
                self.log(f"Error showing {name} ready dialog: {e}")

        threading.Thread(target=_check, daemon=True).start()

    # Browsers we trust for open -a / osascript activate
    ALLOWED_BROWSERS = op_browser.ALLOWED_BROWSERS

    def extension_connected_recently(self):
        """Check if a browser extension is actively connected right now."""
        return op_browser.extension_connected_recently(self.app_support)

    def update_browser_menu_title(self):
        """Update the browser menu item title based on which browser is available."""
        self.browser_menu_item.title = op_browser.browser_menu_title(self.app_support)

    def open_tor_browser(self, _):
        """Open the onion address in the best available browser."""
        base = self._onion_url()
        if base:
            if not op_browser.open_onion_url(base, self.app_support, self.log,
                                             add_login=self._generate_login_url):
                self.show_browser_install_dialog()
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def show_browser_install_dialog(self):
        """Show dialog offering Tor Browser download."""
        address = self.onion_address or ""
        try:
            button_index = self.show_native_alert(
                title="OnionPress",
                message=f"Your site is ready!\n\n{address}\n\nTo visit .onion sites, download Tor Browser (free).",
                buttons=["Download Tor Browser", "Later"],
                default_button=0,
                cancel_button=1,
                style="informational"
            )
            if button_index == 0:
                subprocess.run(["open", "https://www.torproject.org/download/"])
                self.monitor_tor_browser_install()
        except Exception as e:
            self.log(f"Browser dialog failed: {e}")

    def auto_open_browser(self):
        """Automatically open a browser when service becomes ready"""
        try:
            self._auto_open_browser_inner()
        except Exception as e:
            self.log(f"ERROR in auto_open_browser: {e}")
            import traceback
            self.log(traceback.format_exc())

    def _auto_open_browser_inner(self):
        """Inner implementation of auto_open_browser"""
        # Wait until the onion service is actually reachable before opening
        # the browser. Poll via docker exec into the tor container (the same
        # path the launcher uses) instead of a fixed sleep.
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            self.log(f"auto_open_browser: skipping, onion_address={self.onion_address!r}")
            return

        self.log("Waiting for onion service to become reachable before opening browser...")

        # The daemon's dual probe is the measure of reachability
        if self._wait_for_state(lambda s: s.get("is_ready"), 90):
            self.log("Onion service reachable")
        else:
            self.log("WARNING: Onion service not reachable after 90s, opening browser anyway")

        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            # The auto-login token goes only to browsers that get the URL
            # through LaunchServices (op_browser.open_url); logs get url.
            url = self._onion_url() or f"http://{self.onion_address}/"

            if op_browser.is_tor_browser_installed():
                self.log(f"Auto-opening Tor Browser: {url}")
                op_browser.open_url(self._generate_login_url(url) or url,
                                    op_browser.TOR_BROWSER_PATH, self.log)
            else:
                # Wait for the onion proxy (the daemon's) to start
                self._wait_for_state(lambda s: s.get("proxy_running"), 15)

                # Wait up to 5 seconds for a browser extension to register
                ext_browser = None
                for i in range(5):
                    ext_browser = self.extension_connected_recently()
                    if ext_browser:
                        break
                    self.log(f"Waiting for extension registration... ({i+1}/5)")
                    time.sleep(1)
                if ext_browser:
                    self.log(f"Auto-opening {ext_browser} (extension detected): {url}")
                    # Open browser first so extension can set up SOCKS routing
                    subprocess.run(["open", "-a", ext_browser])
                    if op_browser.wait_for_extension_active(self.app_support):
                        self.log("Extension active, opening .onion URL")
                    else:
                        self.log("Extension did not poll within 30s, opening .onion URL anyway")
                    op_browser.open_url(self._generate_login_url(url) or url,
                                        ext_browser, self.log)
                    subprocess.run(["osascript", "-e", f'tell application "{ext_browser}" to activate'])
                elif op_browser.is_brave_installed():
                    # Without a token: see op_browser.open_onion_url.
                    self.log(f"Auto-opening Brave Browser (Tor mode): {url}")
                    brave_exe = os.path.join(op_browser.BRAVE_BROWSER_PATH, "Contents", "MacOS", "Brave Browser")
                    subprocess.run([brave_exe, "--tor", url])
                else:
                    self.log("No Tor-capable browser found - showing options dialog")
                    self.dismiss_setup_dialog()
                    self.dismiss_launch_splash()
                    self.show_browser_install_dialog()

    def check_address_prefix_change(self):
        """No-op: the vanity prefix is chosen once at install (welcome
        screen) and never changed on the fly. The old behaviour — detect a
        config ADDRESS_PREFIX that no longer matched the live address and
        regenerate the onion identity on startup — was removed (#256 phase
        4b): it shared the churny stop -> delete key volume -> regenerate
        path and risked clobbering the address. Kept as a stub so the
        startup/restart call sites are unchanged; always proceeds."""
        return True

    def start_service(self, _):
        """Start the WordPress + Tor service"""
        self._stopping = False  # Clear in case Stop was hit previously
        self.menu["Starting..."].title = "Status: Starting..."

        def start():
            # Check if this is first run (uses same marker as __init__)
            first_run = self._is_first_run

            # First run: setup window is already showing (from __init__), just run setup
            if first_run:
                self.log("First run detected - starting installation")
                self.dismiss_launch_splash()  # In case splash was shown instead
                if setup_window:
                    sw = setup_window.get_setup_window()
                    sw.add_log("First-time setup starting...")
                threading.Thread(target=self._run_first_time_setup, daemon=True).start()
                return

            # Not first run: check if address prefix changed before starting
            if not self.check_address_prefix_change():
                self.log("Start aborted due to address prefix issue")
                self.menu["Starting..."].title = "Status: Stopped"
                return

            # Start the service normally (the launcher starts the daemon
            # too when it is not running)
            self.update_splash_status("Starting your site...")
            self._start_failed_rc = None
            rc = subprocess.run([self.launcher_script, "start"], stdin=subprocess.DEVNULL).returncode
            if rc != 0:
                # Every hard stop in the launcher (Docker never answered, key
                # volume missing, compose up failed three times) exits 1 with
                # an ERROR line in ~/.onionpress/onionpress.log. Nothing read
                # the rc before: the menu just said "Stopped" with no reason.
                self._start_failed_rc = rc
                self.log(f"onionpress start failed (rc={rc}) — see ~/.onionpress/onionpress.log for the ERROR line")
                self.update_splash_status("Start failed — see View Logs")

            # The daemon notices the stack, resyncs the ports, arms
            # onionheaven's HSFETCH timer and starts caffeinate; wait for
            # its word that the containers are up.
            self.update_splash_status("Starting your site...")
            if self._wait_for_state(lambda s: s.get("is_running"), 60):
                self.log("Containers running")
            self.update_menu()

        threading.Thread(target=start, daemon=True).start()

    def _first_run_after_welcome(self):
        """Called after the user clicks Set Up (new site) or Restore from backup.
        Runs on a background thread (from setup_window callback)."""
        sw = setup_window.get_setup_window() if setup_window else None

        # Restore-from-backup path: stage the backup host-side (extract + seed
        # the onion key + apply config overrides + write the .install-from-backup
        # marker), then start — the launcher's marker hook imports the DB/content
        # into the fresh containers. No new site, no vanity generation, no
        # key-swap churn; the original op2… address comes back from the backup.
        if sw and getattr(sw, 'restore_mode', False):
            def _rlog(m):
                self.log(m)
                try:
                    if sw:
                        sw.add_log(m)
                except Exception:
                    pass
            # `onionpress prepare-restore` (cli.cmd_prepare_restore) stages
            # it; the password goes in the environment, never on argv. A
            # hub's sealed registry and keys are not opened on this path
            # (no recovery key; restored afterwards with the key, on its
            # own): the CLI's note says so.
            _rlog("Restore from backup: preparing…")
            rc, ans, out, err = self._run_launcher_json(
                ["prepare-restore", sw.restore_zip, "--json"],
                env={launcher_ops.BACKUP_PASSWORD_ENV: sw.restore_password},
                on_line=_rlog)
            if rc != 0 or not ans or not ans.get("ok"):
                failure = (ans or {}).get("error") or self._launcher_failure(
                    out, err, "see the OnionPress log")
                self.log(f"Restore from backup: prepare failed: {failure}")
                try:
                    if sw:
                        sw.set_status("Restore failed — check the log for details")
                except Exception:
                    pass
                return
            if ans.get("note"):
                _rlog(ans["note"])
            _rlog("Restore from backup: staged — starting install…")
            self.start_service(None)
            return

        # New-site path
        if sw and getattr(sw, 'address_prefix', None):
            # Vanity prefix chosen on the welcome screen — generated once at
            # first-run start (no on-the-fly change needed later).
            # Chosen just now, before any address exists: no question.
            self.write_config_value("ADDRESS_PREFIX", sw.address_prefix,
                                    confirmed=["prefix-change"])
            self.log(f"Address prefix set to '{sw.address_prefix}' from welcome screen")
        if sw and hasattr(sw, 'share_analytics'):
            self.write_config_value("SHARE_ANALYTICS_WITH_ONIONHOME", sw.share_analytics)
            self.log(f"Analytics sharing set to {sw.share_analytics} from welcome screen")
            # (The daemon uploads at once when it takes the change marker.)
        self.start_service(None)

    # ── Onionname registration ───────────────────────────────────────────
    #
    # The onionname is registered with OnionHome BEFORE WordPress is
    # installed — that way the WP admin username is always a confirmed
    # onionname and we never need a rename later. If OnionHome is
    # unreachable we proceed with the user's chosen name and retry on every
    # subsequent launch (see _retry_pending_onionname).

    def _read_onion_address(self):
        """Return the local wordpress .onion address, or None if not yet written."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor", "cat",
                 "/var/lib/tor/hidden_service/wordpress/hostname"],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=10,
            )
            addr = result.stdout.strip()
            if key_manager.is_valid_onion_address(addr):
                return addr
        except Exception as e:
            self.log(f"onionname: can't read onion address: {e}")
        return None

    def _save_onionname(self, name, onionaddress, registered):
        """Persist onionname state to ~/.onionpress/config."""
        self.write_config_value("ONIONNAME", name)
        self.write_config_value("ONIONNAME_ADDRESS", onionaddress or "")
        self.write_config_value(
            "ONIONNAME_REGISTERED", "yes" if registered else "no"
        )

    def _get_admin_username(self):
        """Return the WordPress admin user's login.

        Preference order:
        1. ONIONNAME from ~/.onionpress/config (fast path, no subprocess).
        2. First administrator returned by wp user list in the running
           WordPress container — for installs whose config predates
           name-sync populating ONIONNAME, or where it was wiped.
        3. "admin" as a last-resort fallback.

        Caches a successful container query back to the config so future
        calls hit the fast path.
        """
        name = self.read_config_value("ONIONNAME", "").strip()
        if name:
            return name
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            result = subprocess.run(
                [docker_bin] + wp_files.wp_cli_args(
                    "user", "list", "--role=administrator",
                    "--field=user_login"),
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    login = line.strip()
                    if login:
                        self.write_config_value("ONIONNAME", login)
                        return login
        except Exception as e:
            self.log(f"_get_admin_username: container query failed: {e}")
        return "admin"

    def _prompt_onionname_collision(self, current_name, suggestions):
        """Modal alert — returns chosen new name or None if canceled.

        MUST be called from a background thread; the AppKit modal runs on
        the main thread and we block here until the user dismisses it.
        """
        result = {"name": None}
        done = threading.Event()

        def _show():
            try:
                alert = AppKit.NSAlert.alloc().init()
                alert.setMessageText_(
                    f"The onionname '{current_name}' is already taken"
                )
                info = "Pick a different onionname."
                if suggestions:
                    info += "\n\nSuggestions: " + ", ".join(suggestions)
                alert.setInformativeText_(info)
                alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

                field = AppKit.NSTextField.alloc().initWithFrame_(
                    AppKit.NSMakeRect(0, 0, 320, 24)
                )
                field.setStringValue_(suggestions[0] if suggestions else "")
                field.setPlaceholderString_("your-onionname")
                alert.setAccessoryView_(field)

                alert.addButtonWithTitle_("Use this name")
                alert.addButtonWithTitle_("Skip for now")

                response = alert.runModal()
                if response == AppKit.NSAlertFirstButtonReturn:
                    result["name"] = field.stringValue().strip()
            finally:
                done.set()

        try:
            from onionpress.ui_helpers import main_thread
            main_thread(_show)
        except ImportError:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_show)

        # 5-minute timeout — user may be AFK mid-install. After that we give
        # up and continue unregistered; they can fix it from Settings later.
        if not done.wait(timeout=300):
            self.log("onionname: collision dialog timed out")
            return None
        return result["name"] or None


    def _register_onionname_during_setup(self, sw):
        """Try to register sw.admin_user with OnionHome.

        On collision: prompt the user to pick a new name, update sw.admin_user,
        retry. On unreachable / other failure: mark pending and let the
        next-launch retry path handle it. Must not raise — failures here
        should never block the install.
        """
        try:
            from onionpress.onionnames_registrar import Registrar
            from onionpress.onionnames_client import validate_name
        except Exception as e:
            self.log(f"onionname: cannot import registrar: {e}")
            return

        onionaddress = self._read_onion_address()
        if not onionaddress:
            self.log("onionname: no onion address yet, skipping register")
            # Still save the preference so the retry path has something to
            # register on the next launch.
            self._save_onionname(sw.admin_user, "", registered=False)
            return

        docker_bin = os.path.join(self.bin_dir, "docker")
        reg = Registrar(docker_bin=docker_bin, log=self.log)

        max_attempts = 5
        name = sw.admin_user
        for attempt in range(max_attempts):
            if sw:
                sw.set_status(f"Reserving onionname '{name}'...")
                sw.add_log(f"Registering '{name}' with OnionHome...")
            self.log(f"onionname: register attempt {attempt + 1}: "
                     f"{name} -> {onionaddress}")
            result = reg.register(name, onionaddress)

            if result.ok:
                self.log(f"onionname: registered '{name}'")
                sw.admin_user = name
                self._save_onionname(name, onionaddress, registered=True)
                if sw:
                    sw.add_log(f"Onionname '{name}' registered")
                return

            if result.status == "collision":
                self.log(f"onionname: '{name}' taken; prompting user")
                suggestions = result.suggestions or []
                new_name = self._prompt_onionname_collision(name, suggestions)
                if not new_name:
                    break
                ok, _ = validate_name(new_name)
                if not ok:
                    # Keep the current (valid) name and ask again: this name
                    # goes on to the WordPress install and the config file.
                    self.log(f"onionname: user-entered {new_name!r} invalid, "
                             "retrying with same prompt")
                    continue
                name = new_name
                continue

            # Anything else is a non-collision failure — give up and let the
            # retry-on-launch path handle it.
            reason = result.reason or result.status
            self.log(f"onionname: register failed ({result.status}: {reason})")
            break

        sw.admin_user = name
        self._save_onionname(name, onionaddress, registered=False)
        if sw:
            sw.add_log(
                "Onionname not registered with OnionHome yet — "
                "will retry on next launch."
            )

    def _rename_wp_admin(self, old_name, new_name):
        """Rename the WordPress admin user via WP-CLI (direct DB update)."""
        try:
            from onionpress.onionnames_client import validate_name
        except ImportError:
            return
        # Safety: both names must pass validation (alphanumeric + ._- only)
        if not validate_name(old_name)[0] or not validate_name(new_name)[0]:
            self.log("onionname: rename aborted — name failed validation")
            return
        docker_bin = os.path.join(self.bin_dir, "docker")
        result = subprocess.run(
            [docker_bin] + wp_files.wp_cli_args(
                "db", "query",
                f"UPDATE wp_users SET user_login='{new_name}', "
                f"user_nicename='{new_name}' WHERE user_login='{old_name}'"),
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=15
        )
        if result.returncode == 0:
            self.log(f"WordPress admin renamed: '{old_name}' -> '{new_name}'")
        else:
            self.log(f"WordPress admin rename failed: {result.stderr.strip()}")

    def _wp_core_install(self, sw):
        """Delegate to setup_logic.install_fresh_wordpress() (shared with Linux).

        Was a 70-line inline wp-cli sequence; now a thin wrapper that
        bundles the SetupWindow's fields and points install_fresh_
        wordpress at the Mac launcher for the provision-post-install
        bash step. The two platforms share one install path now.
        """
        if not sw or not sw.admin_pass:
            return
        try:
            from onionpress.setup_logic import install_fresh_wordpress
            docker_bin = os.path.join(self.bin_dir, "docker")
            addr_result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor", "cat",
                 "/var/lib/tor/hidden_service/wordpress/hostname"],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=10
            )
            onion_addr = addr_result.stdout.strip() or "localhost"

            def _log(msg):
                self.log(msg)
                if sw:
                    sw.add_log(msg)

            if sw:
                sw.set_status("Configuring WordPress...")
                sw.add_log(f"Installing WordPress as '{sw.admin_user}'...")
            ok = install_fresh_wordpress(
                site_title=sw.site_title,
                onionname=sw.admin_user,
                password=sw.admin_pass,
                language=getattr(sw, "language", "en_US") or "en_US",
                onion_addr=onion_addr,
                docker_bin=docker_bin,
                launcher_bin=self.launcher_script,
                log_func=_log,
            )
            if sw:
                sw.add_log("WordPress installed and configured" if ok else
                           "WordPress setup did not complete (see above)")
        except subprocess.SubprocessError as e:
            # Not {e}: a subprocess error's message quotes the whole argv.
            self.log(f"wp core install error: {type(e).__name__}")
        except Exception as e:
            self.log(f"wp core install error: {e}")

    def _run_first_time_setup(self):
        """Run first-time setup: launcher start with concurrent progress monitoring.

        The launcher 'start' command does everything (start Colima, pull images,
        generate vanity address, docker compose up, wait for services).  We run
        it in a background thread and poll for milestones concurrently so the
        setup window shows real-time progress instead of a single long wait.
        """
        sw = setup_window.get_setup_window() if setup_window else None

        # Step 0: System check — verify bundled binaries exist
        if sw:
            sw.set_step(0)
            sw.set_status("Checking system requirements...")
            sw.add_log("Checking system requirements...")
        self.log("Checking system requirements...")
        missing = []
        for binary in ("docker", "colima", "limactl"):
            if not os.path.exists(os.path.join(self.bin_dir, binary)):
                missing.append(binary)
        if missing:
            msg = f"Missing binaries: {', '.join(missing)}"
            self.log(f"System check failed: {msg}")
            if sw:
                sw.set_status(msg)
                sw.add_log(f"ERROR: {msg}")
            return
        if sw:
            sw.set_progress(1 / 8)
            sw.complete_step(0)
            sw.add_log("System check passed")

        # Launch the launcher script in the background — it does steps 1-4
        if sw:
            sw.set_status("Preparing your site...")
            sw.add_log("Starting Colima VM...")
        self.log("Starting Colima VM and containers...")

        launcher_done = threading.Event()
        launcher_failed = [False]

        def run_launcher():
            try:
                result = subprocess.run(
                    [self.launcher_script, "start"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace'
                )
                if result.returncode != 0:
                    # capture_output swallowed the launcher's stderr, including
                    # the tracebacks of its onionpress.cli steps; keep the tail.
                    tail = (result.stderr or result.stdout or "").strip()[-4000:]
                    self.log(f"Launcher exited with rc={result.returncode}")
                    if tail:
                        self.log("Launcher output (tail):\n" + tail)
                    # Before WordPress has answered, a non-zero exit is the
                    # launcher's own hard stop (Docker never ready, key volume,
                    # compose up): fail now instead of sitting on "Downloading
                    # components..." until the 10-minute timeout. After step 4
                    # it is a late, non-fatal step, recorded by the log above.
                    try:
                        wordpress_up = step4_done
                    except NameError:
                        wordpress_up = False
                    if not wordpress_up:
                        launcher_failed[0] = True
            except Exception as e:
                launcher_failed[0] = True
                self.log(f"Error in _run_first_time_setup: {e}")
            finally:
                launcher_done.set()

        threading.Thread(target=run_launcher, daemon=True).start()

        # Poll for milestones while the launcher runs (the daemon's state:
        # the runtime, the images it sees, the address, WordPress)
        step1_done = False   # Container runtime
        step2_done = False   # Images downloaded
        step3_done = False   # .onion address generated
        step4_done = False   # WordPress responding
        images_found = {'wordpress': False, 'mariadb': False, 'tor': False}
        total_images = len(images_found)
        setup_start = time.time()
        setup_timeout = 600  # 10 minute max

        while not launcher_done.is_set() or not step4_done:
            # Check for timeout
            if time.time() - setup_start > setup_timeout:
                if sw:
                    sw.set_status("Setup timed out — check log for details")
                    sw.add_log("ERROR: Setup timed out after 10 minutes")
                self.log("First-time setup timed out after 10 minutes")
                break

            # Check for launcher failure
            if launcher_failed[0]:
                if sw:
                    sw.set_status("Setup failed — check log for details")
                    sw.add_log("ERROR: Launcher script failed — see ~/.onionpress/onionpress.log")
                self.log("First-time setup failed")
                break

            state = self.state if self._state_fresh else {}

            # Step 1: Container runtime ready?
            if not step1_done and state.get("runtime_ready"):
                step1_done = True
                if sw:
                    sw.set_progress(2 / 8)
                    sw.complete_step(1)
                    # No "ready" log line — the step row in the checklist
                    # already got its ✓ via complete_step(1).
                    sw.set_status("Downloading components...")

            # Step 2: Docker images downloaded?
            if step1_done and not step2_done:
                present = state.get("images_present") or {}
                for name in images_found:
                    if not images_found[name] and present.get(name):
                        images_found[name] = True
                        done = sum(images_found.values())
                        self.log(f"Image downloaded: {name}")
                        if sw:
                            sw.set_progress(
                                (2 + done / total_images) / 8,
                                f"Downloading images ({done}/{total_images})"
                            )
                            sw.add_log(f"Image downloaded: {name}")
                if all(images_found.values()):
                    step2_done = True
                    self.log("All images downloaded")
                    if sw:
                        sw.complete_step(2)
                        sw.add_log("All images downloaded")
                        sw.set_status("Generating .onion address...")

            # Step 3: .onion address generated?
            if step2_done and not step3_done:
                addr = state.get("onion_address") or ""
                if key_manager.is_valid_onion_address(addr):
                    step3_done = True
                    if sw:
                        sw.complete_step(3)
                        sw.set_progress(4 / 8)
                        sw.add_log(f"Address: {addr[:30]}...")
                        sw.set_status("Starting WordPress + Tor...")

            # Step 4: WordPress responding?
            if step3_done and not step4_done:
                if state.get("wordpress_up"):
                    step4_done = True
                    self.log("WordPress responding")
                    if sw:
                        sw.add_log("WordPress responding")
                    # Install WordPress with credentials from setup window.
                    # Register the onionname FIRST so the WP admin username is
                    # always a confirmed, OnionHome-blessed onionname — the
                    # registrar may mutate sw.admin_user if the user hit a
                    # collision and picked a new name.
                    if sw and getattr(sw, 'restore_mode', False):
                        # Restore-from-backup: the launcher's install-from-backup
                        # marker hook imports the backup's DB (admin user +
                        # content come from the backup), so DON'T fresh-install
                        # WordPress or register a new onionname over it.
                        self.log("Restore from backup: skipping fresh WordPress install")
                    elif sw and sw.admin_pass:
                        try:
                            self._register_onionname_during_setup(sw)
                        except Exception as e:
                            self.log(f"onionname: register path errored: {e}")
                        self._wp_core_install(sw)
                    if sw:
                        sw.complete_step(4)
                        sw.set_progress(5 / 8)
                        sw.set_status("Publishing onion address to Tor network (may take 5-10 min)...")

            # Between steps 4 and 5, feed bootstrap % into setup window
            # so it doesn't look frozen during descriptor propagation
            if step4_done and sw and sw.window:
                pct = self._last_bootstrap_pct
                elapsed = int(time.time() - setup_start)
                mins, secs = divmod(elapsed, 60)
                if pct < 100:
                    sw.set_status(f"Tor bootstrap: {pct}% ({mins}m {secs:02d}s)")
                else:
                    sw.set_status(f"Publishing onion address to Tor network (may take 5-10 min)... {mins}m {secs:02d}s")

            time.sleep(3)

        # If launcher succeeded but we missed some steps (e.g. fast cached restart),
        # mark them complete
        if not launcher_failed[0] and sw:
            if not step1_done:
                sw.complete_step(1)
            if not step2_done:
                sw.complete_step(2)
            if not step3_done:
                sw.complete_step(3)
            if not step4_done:
                sw.complete_step(4)
            sw.set_progress(5 / 8)
            sw.set_status("Publishing onion address to Tor network (may take 5-10 min)...")

        # The daemon arms onionheaven's HSFETCH timer, starts caffeinate
        # and reports readiness (the setup window's last steps follow in
        # _on_ready).
        self.update_menu()




    @rumps.clicked("View Web Usage Log")
    def view_web_log(self, _):
        """Open WordPress access log in built-in log viewer"""
        if not self.is_running:
            rumps.alert("Service not running. Please start the service first.")
            return

        # The daemon's capture (wordpress-visitors-*.log)
        web_log_file = current_log_path(self.logs_dir, "wordpress-visitors")
        if not web_log_file or not os.path.exists(web_log_file):
            rumps.alert("No web usage log yet. The OnionPress service writes it once "
                        "the site is running.")
            return

        # Open in built-in log viewer (filtered log excludes health check pings)
        _LogViewerWindow.show_for_file(web_log_file, "OnionPress Web Usage Log")

    def get_version(self):
        """Get version from Info.plist"""
        try:
            with open(self.info_plist, 'rb') as f:
                plist = plistlib.load(f)
                return plist.get('CFBundleShortVersionString', 'Unknown')
        except Exception:
            return 'Unknown'

    def write_config_value(self, key, value, confirmed=()):
        """Change one setting through `onionpress config set` (issue #365):
        the CLI is the one writer of ~/.onionpress/config, with the key's
        check, the log line, the login item, and the change marker. A
        secret goes on stdin, never on argv. Returns True when the CLI
        accepted it; what changed is then applied by the daemon
        (caffeinate, status.json in the volume), from the marker.
        A question the CLI stops at (onionpress.confirm) is asked here
        unless *confirmed* already names it (the welcome screen's prefix:
        the person just chose it).
        """
        if not os.path.exists(self.config_file):
            config_template = os.path.join(self.resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                import shutil
                shutil.copy2(config_template, self.config_file)
        argv = [self.launcher_script, "config", "set", key]
        stdin = None
        if key in op_config.SECRET_CONFIG_KEYS and value:
            argv.append("-")
            stdin = f"{value}\n"
        else:
            argv.append(value)
        argv.append("--json")
        confirmed = list(confirmed)
        while True:
            try:
                result = subprocess.run(
                    argv + [f for w in confirmed for f in ("--confirmed", w)],
                    input=stdin, capture_output=True, text=True,
                    **({} if stdin is not None else {"stdin": subprocess.DEVNULL}),
                    encoding="utf-8", errors="replace", timeout=120)
            except (OSError, subprocess.SubprocessError) as e:
                self.log(f"config set {key} failed to run: {e}")
                return False
            answer = op_confirm.last_json_line(result.stdout) or {}
            # A question the CLI stops at (an ADDRESS_PREFIX the live
            # address does not start with: "prefix-change"), asked here;
            # the run itself is refused (exit 3) until it is answered.
            if result.returncode != 0 and answer.get("confirm"):
                if not self._ask_cli_question(answer["confirm"], answer, action="Change"):
                    self.log(f"config set {key}: not confirmed ({answer['confirm']})")
                    return False
                confirmed.append(answer["confirm"])
                continue
            break
        if result.returncode != 0:
            # The CLI prints no secret value; a refusal names the key and
            # what it must be.
            self.log(f"config set {key} refused (rc {result.returncode}): "
                     f"{answer.get('error') or (result.stderr or result.stdout).strip()}")
            return False
        # What changed is applied by the daemon (caffeinate, status.json),
        # which takes the marker the CLI left on its next tick.
        return True


    @rumps.clicked("Settings...")
    def open_settings(self, _):
        """Show GUI settings dialog."""
        from onionpress.settings_ui import show_settings_dialog

        # Create default config if it doesn't exist
        if not os.path.exists(self.config_file):
            config_template = os.path.join(self.parent_resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                import shutil
                shutil.copy2(config_template, self.config_file)

        icon_path = os.path.join(self.resources_dir, "app-icon.png")

        # Every change goes through `onionpress config set`
        # (write_config_value); its side effects follow in
        # apply_config_changes.
        show_settings_dialog(
            config_path=self.config_file,
            icon_path=icon_path,
            launcher_script=self.launcher_script,
            log_func=self.log,
            callbacks={'write_config': self.write_config_value},
        )

    def _run_launcher_json(self, args, env=None, input=None, timeout=None, on_line=None):
        """Run `onionpress <args>` and read the JSON object the CLI prints
        last (onionpress.confirm). Returns (rc, answer or None, stdout,
        stderr). *env* is merged into the environment: a password or key
        goes there, never on argv. With *on_line*, every line the CLI logs
        on stderr is handed over as it arrives (a progress window), its
        time stamp removed. The CLI gets no terminal: stdin is /dev/null
        unless *input* is given."""
        full_env = {**os.environ, **(env or {})}
        argv = [self.launcher_script, *args]
        try:
            if on_line is None:
                r = subprocess.run(
                    argv, env=full_env, input=input,
                    **({} if input is not None else {"stdin": subprocess.DEVNULL}),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout)
                return r.returncode, op_confirm.last_json_line(r.stdout), r.stdout, r.stderr
            proc = subprocess.Popen(
                argv, env=full_env,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            if input is not None:
                proc.stdin.write(input)
                proc.stdin.close()
            err_lines = []

            def drain():
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    err_lines.append(line)
                    try:
                        on_line(launcher_ops.strip_log_stamp(line))
                    except Exception:
                        pass
            t = threading.Thread(target=drain, daemon=True)
            t.start()
            out = proc.stdout.read()
            rc = proc.wait(timeout=timeout)
            t.join(5)
            return rc, op_confirm.last_json_line(out), out, "\n".join(err_lines)
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"onionpress {' '.join(args[:2])}: could not run: {e}")
            return -1, None, "", str(e)

    @staticmethod
    def _launcher_failure(stdout, stderr, default):
        """What a failed `onionpress` run said: its last ERROR line, else
        its last line, else *default*."""
        lines = [ln.strip() for ln in (stderr or "").splitlines() + (stdout or "").splitlines()
                 if ln.strip()]
        errors = [ln for ln in lines if ln.startswith("ERROR")]
        if errors:
            return launcher_ops.strip_log_stamp(errors[-1])[len("ERROR:"):].strip() or default
        return launcher_ops.strip_log_stamp(lines[-1]) if lines else default

    @rumps.clicked("Backup...")
    def backup(self, _, on_complete=None):
        """Create a full backup through `onionpress backup` (cli.cmd_backup:
        saved where the owner chooses, never handed to WordPress, so with an
        OnionHeaven hub's registry and registrants' keys, sealed to its
        recovery key when one is set up, the whole config and the site's
        onion service key). The dialogs are this process's; the CLI
        verifies the credentials and names the file (backup --check --json),
        then makes the backup, its log lines shown as progress. The
        password goes in the environment, never on argv.

        on_complete, if given, is called with True after a successful backup
        (once the user dismisses the "Done" alert) or False on any cancel /
        failure. The uninstall "backup first" flow uses it to proceed only
        after a successful backup; the menu item passes none (unchanged)."""
        # Show credentials dialog using AppKit accessory view
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Backup OnionPress")
        alert.setInformativeText_(
            "Enter your WordPress administrator credentials.\n"
            "The password will be used to encrypt the backup.")

        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        # Build accessory view with username and password fields
        container = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 70))

        user_label = AppKit.NSTextField.labelWithString_("Username:")
        user_label.setFrame_(AppKit.NSMakeRect(0, 48, 80, 18))
        container.addSubview_(user_label)

        user_field = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(85, 44, 210, 24))
        user_field.setStringValue_(self._get_admin_username())
        container.addSubview_(user_field)

        pass_label = AppKit.NSTextField.labelWithString_("Password:")
        pass_label.setFrame_(AppKit.NSMakeRect(0, 18, 80, 18))
        container.addSubview_(pass_label)

        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(85, 14, 210, 24))
        container.addSubview_(pass_field)

        alert.setAccessoryView_(container)
        alert.addButtonWithTitle_("Backup").setKeyEquivalent_("\r")
        alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")

        # Make username field first responder
        alert.window().setInitialFirstResponder_(user_field)
        user_field.setNextKeyView_(pass_field)

        response = alert.runModal()
        if response != 1000:  # Not "Backup"
            if on_complete:
                on_complete(False)
            return

        username = user_field.stringValue().strip()
        password = pass_field.stringValue()

        if not username or not password:
            rumps.alert(title="Missing Credentials",
                        message="Both username and password are required.")
            if on_complete:
                on_complete(False)
            return

        # The CLI verifies the credentials and names the file after the
        # address the live key derives to (the cached one can lag a key
        # import or a restore).
        self.log("Backup: verifying credentials...")
        password_env = {launcher_ops.BACKUP_PASSWORD_ENV: password}
        rc, ans, out, err = self._run_launcher_json(
            ["backup", "--check", "--json", "--user", username], env=password_env,
            timeout=120)
        if not ans or not ans.get("ok"):
            failure = (ans or {}).get("error") or self._launcher_failure(
                out, err, "Could not verify the credentials")
            self.log(f"Backup: credential verification failed: {failure}")
            rumps.alert(title="Verification Failed", message=failure)
            if on_complete:
                on_complete(False)
            return

        # Show NSSavePanel for output location
        panel = AppKit.NSSavePanel.savePanel()
        panel.setTitle_("Save Backup")
        panel.setNameFieldStringValue_(ans.get("filename") or "OnionPress-backup.zip")
        backups_dir = os.path.expanduser("~/OnionPress/backups")
        os.makedirs(backups_dir, exist_ok=True)
        panel.setDirectoryURL_(
            AppKit.NSURL.fileURLWithPath_(backups_dir))
        panel.setAllowedContentTypes_([
            AppKit.UTType.typeWithFilenameExtension_("zip")])

        if panel.runModal() != 1:  # NSModalResponseOK
            if on_complete:
                on_complete(False)
            return

        output_path = panel.URL().path()

        # Show progress window (stored on self to prevent garbage collection)
        self._progress_window = _BackupProgressWindow("Backing Up OnionPress")
        self._progress_window.show()

        def do_backup():
            pw = self._progress_window

            def log_and_update(msg):
                self.log(msg)
                display = msg.replace("Backup: ", "") if msg.startswith("Backup: ") else msg
                _main_thread(lambda: pw.update(display))

            rc, ans, out, err = self._run_launcher_json(
                ["backup", "--json", "-o", output_path, "--user", username],
                env=password_env, on_line=log_and_update)
            if rc != 0 or not ans or not ans.get("ok"):
                failure = (ans or {}).get("error") or self._launcher_failure(
                    out, err, "see the OnionPress log")
                self.log(f"Backup failed: {failure}")
                _main_thread(lambda: pw.finish(
                    f"Backup failed: {failure}",
                    on_done=(lambda: on_complete(False)) if on_complete else None))
                return
            size_mb = (ans.get("bytes") or 0) / (1024 * 1024)
            msg = f"Backup saved to {os.path.basename(output_path)} ({size_mb:.1f} MB)"
            # What the CLI says every time: a hub without a recovery key,
            # a hub or OnionHome check that did not answer (what this
            # backup may lack).
            for note in ans.get("notes") or []:
                msg += "\n\n" + note
            # Route completion through finish's on_done so on_complete(True)
            # fires on the main thread AFTER the user dismisses the Done
            # alert (not from this daemon thread, and not before the alert).
            _main_thread(lambda: pw.finish(
                msg, on_done=(lambda: on_complete(True)) if on_complete else None))

        threading.Thread(target=do_backup, daemon=True).start()

    def _ask_hub_recovery_key(self, facts, zip_path, env):
        """For a backup whose OnionHeaven hub data is sealed (the CLI's
        "sealed-hub-data-left-out" question): the recovery key's private
        half as typed, "" to restore without the hub data, or None to
        cancel the restore. A typed key is checked by the CLI against the
        backup (restore --check with the key in the environment: by its
        key id) and asked for again when it is not this backup's."""
        kid = facts.get("recovery_key_id") or "unknown"
        while True:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Restore the Hub's Registrants' Keys?")
            alert.setInformativeText_(
                f"This backup holds an OnionHeaven hub's registry and its "
                f"registrants' onion service keys, sealed to the hub's recovery "
                f"key (key id {kid}). Paste that key to restore them.\n\n"
                f"Without it, everything else is restored: registrants that are "
                f"online send their keys again within a minute; sites that are "
                f"offline cannot be taken over until they come back.")
            key_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, 380, 24))
            alert.setAccessoryView_(key_field)
            alert.addButtonWithTitle_("Restore With Key").setKeyEquivalent_("\r")
            alert.addButtonWithTitle_("Restore Without")
            alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")
            alert.window().setInitialFirstResponder_(key_field)
            response = alert.runModal()
            if response == 1001:    # Restore Without
                self.log("Restore: the hub's sealed data will not be restored "
                         "(no recovery key given)")
                return ""
            if response != 1000:    # Cancel
                return None
            text = key_field.stringValue().strip()
            rc, ans, out, err = self._run_launcher_json(
                ["restore", zip_path, "--check", "--json"],
                env={**env, launcher_ops.HUB_RECOVERY_KEY_ENV: text}, timeout=300)
            if ans and ans.get("ok"):
                return text
            rumps.alert(title="Recovery Key Not Accepted",
                        message=(ans or {}).get("error") or self._launcher_failure(
                            out, err, "The key could not be checked."))

    @rumps.clicked("Restore...")
    def restore(self, _):
        """Restore OnionPress from a backup zip through `onionpress restore`
        (install-from-backup: cli.cmd_restore tears the install down and
        rebuilds it from the backup). The CLI is asked first what the
        backup is (restore --check --json): the password it needs, the
        facts for the confirmation dialog, and whether a hub's sealed data
        needs the recovery key. The dialogs are this process's; the
        password and the key go in the environment, never on argv."""
        # File picker for .zip
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setTitle_("Select OnionPress Backup")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedContentTypes_([
            AppKit.UTType.typeWithFilenameExtension_("zip")])

        if panel.runModal() != 1:  # NSModalResponseOK
            return

        zip_path = panel.URL().path()

        # What the CLI needs first: the password, its prompt worded with
        # the login the file's name carries (for the prompt only; the name
        # is the file's, not the backup's).
        rc, ans, out, err = self._run_launcher_json(
            ["restore", zip_path, "--check", "--json"], timeout=120)
        backup_user = (ans or {}).get("backup_user") if (ans or {}).get("ask") == "password" else None

        # Prompt for password
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Enter Backup Password")
        if backup_user:
            alert.setInformativeText_(
                f"Enter the password of '{backup_user}' that was used "
                f"when this backup was created.")
        else:
            alert.setInformativeText_(
                "Enter the password that was used when this backup was created.")

        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 24))
        alert.setAccessoryView_(pass_field)
        alert.addButtonWithTitle_("Continue").setKeyEquivalent_("\r")
        alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")
        alert.window().setInitialFirstResponder_(pass_field)

        response = alert.runModal()
        if response != 1000:
            return

        password = pass_field.stringValue()
        if not password:
            rumps.alert(title="No Password", message="A password is required.")
            return

        # The backup's facts, read by the CLI (a wrong password or an
        # invalid backup is its error).
        restore_env = {launcher_ops.BACKUP_PASSWORD_ENV: password}
        rc, facts, out, err = self._run_launcher_json(
            ["restore", zip_path, "--check", "--json"], env=restore_env, timeout=300)
        if not facts or facts.get("error") or not (facts.get("ok") or facts.get("confirm")):
            failure = (facts or {}).get("error") or self._launcher_failure(
                out, err, "Could not read backup")
            self.log(f"Restore: failed to read backup metadata: {failure}")
            rumps.alert(title="Invalid Backup", message=failure)
            return

        # Show confirmation with backup details (the CLI validated the
        # address and username and sanitized the rest).
        addr = facts.get('onion_address') or 'unknown'
        date = facts.get('backup_date') or 'unknown'
        user = facts.get('username') or 'unknown'
        ver = facts.get('onionpress_version') or 'unknown'
        # A Settings page backup holds no key: the restore keeps this
        # site's address (the restore refuses one whose metadata says so
        # but which holds a key: backup.staged_onion_key_address).
        if facts.get('excludes_onion_key') is True:
            addr += " (its key is not in this backup)"
            warning = ("WARNING: This will overwrite your current site and "
                       "database. This backup was made from the Settings "
                       "page and holds no onion address key, so this site "
                       "keeps its onion address. This cannot be undone.")
        else:
            warning = ("WARNING: This will overwrite your current site, "
                       "database, and onion address. This cannot be undone.")

        button_index = self.show_native_alert(
            title="Confirm Restore",
            message=(
                f"You are about to restore from this backup:\n\n"
                f"Onion address: {addr}\n"
                f"Backup date: {date}\n"
                f"Username: {user}\n"
                f"OnionPress version: {ver}\n\n"
                f"{warning}"),
            buttons=["Cancel", "Restore"],
            default_button=0,
            cancel_button=0,
            style="critical"
        )

        if button_index != 1:
            return

        # An OnionHeaven hub's backup: its registry and registrants' keys
        # are sealed to the hub's recovery key, which the operator keeps
        # off this machine. The CLI stops at that question
        # ("sealed-hub-data-left-out"); asked here and checked against the
        # backup before anything is restored; without it the rest is
        # restored, with --confirmed sealed-hub-data-left-out.
        confirmed_flags = []
        if facts.get("confirm") == "sealed-hub-data-left-out":
            hub_recovery_key = self._ask_hub_recovery_key(facts, zip_path, restore_env)
            if hub_recovery_key is None:
                return
            if hub_recovery_key:
                restore_env[launcher_ops.HUB_RECOVERY_KEY_ENV] = hub_recovery_key
            else:
                confirmed_flags = ["--confirmed", "sealed-hub-data-left-out"]
        elif facts.get("confirm"):
            self.log(f"Restore: unexpected question '{facts.get('confirm')}' (cancelled)")
            return

        # Show progress window (stored on self to prevent garbage collection)
        self._progress_window = _BackupProgressWindow("Restoring OnionPress")
        self._progress_window.show()

        def do_restore():
            pw = self._progress_window
            try:
                def log_and_update(msg):
                    self.log(msg)
                    display = msg.replace("Restore: ", "") if msg.startswith("Restore: ") else msg
                    _main_thread(lambda: pw.update(display))

                # install-from-backup: the launcher's `restore` tears down +
                # rebuilds the install directly from the backup (seeded key
                # + imported DB/content).
                log_and_update("Rebuilding from backup (install-from-backup)…")
                # The password goes in the environment: argv is readable by
                # every local user (ps), the environment only by this one.
                # So does a hub's recovery key (the CLI removes it from its
                # environment before any docker child could inherit it).
                # A failure shows the launcher's last lines, the staging
                # folder kept for the next start and the log's path (#323).
                hub_key_env = {k: v for k, v in restore_env.items()
                               if k == launcher_ops.HUB_RECOVERY_KEY_ENV}
                ok, failure, restore_stderr, _out, answer = launcher_ops.run_launcher_restore(
                    self.launcher_script, zip_path, password,
                    data_dir=self.app_support,
                    log_path=os.path.join(self.app_support, "onionpress.log"),
                    extra_env=hub_key_env,
                    extra_args=["--json", *confirmed_flags],
                    # No time limit (issue #317): a fixed 1800 s ended a
                    # large site's restore halfway and left the launcher's
                    # steps running; those steps carry their own limits,
                    # sized to the backup, and fail it when hit.
                    timeout=None)
                if not ok:
                    self.log(f"Restore: launcher restore failed: {failure}")
                    _main_thread(lambda: pw.finish(failure,
                                                   title="Restore Failed"))
                    return
                answer = answer or {}

                restored_addr = self._adopt_restored_address()
                if not restored_addr:
                    # Shown only; update_status picks the address up from Tor.
                    restored_addr = answer.get("address") or addr
                self.log(f"Restore complete (install-from-backup): {restored_addr}")

                # Build summary of what was restored and what will happen
                notes = [f"Onion address: {restored_addr}"]

                # The restore asked OnionHeaven to delete its copy of the
                # replaced key (onionpress.cli restore); say what a hub may
                # still hold of a key this install no longer has.
                left = answer.get("left_on_hubs") or []
                if left:
                    notes.append(onionheaven.describe_remaining(left))

                # Check if onionheaven mode was restored
                onionheaven_addr = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
                if restored_addr == onionheaven_addr:
                    cur_mem = self.read_config_value("VM_MEMORY", "2")
                    try:
                        cur_mem_int = int(cur_mem)
                    except ValueError:
                        cur_mem_int = 1
                    if cur_mem_int < 5:
                        notes.append("OnionHeaven detected — VM memory will increase to 5 GB on relaunch.")
                    else:
                        notes.append(f"OnionHeaven detected — VM memory: {cur_mem} GB.")

                # A hub's sealed registry and registrants' keys: whether they
                # came back (the CLI's answer says).
                if facts.get("onionheaven_data") == "sealed":
                    if answer.get("hub_data") == "restored":
                        notes.append("OnionHeaven registry and registrants' keys: restored.")
                    elif answer.get("hub_data_note"):
                        notes.append(answer["hub_data_note"])
                    else:
                        notes.append("OnionHeaven registry and registrants' keys: not restored.")

                summary = "Site restored successfully.\n\n" + "\n".join(notes)
                _main_thread(lambda: pw.finish(summary))
            except Exception as e:
                self.log(f"Restore failed: {e}")
                _main_thread(lambda: pw.finish(f"Restore failed: {e}"))

        threading.Thread(target=do_restore, daemon=True).start()

    def _adopt_restored_address(self):
        """The address a restore derived from the backup's key, from the
        on-disk cache it wrote — not metadata.onion_address, which is the
        backup author's to fill in — or None. A valid one becomes
        self.onion_address, so the heartbeat ships the matching
        content_address."""
        try:
            with open(os.path.join(self.app_support, "onion_address")) as f:
                restored_addr = f.read().strip()
        except OSError:
            return None
        if not key_manager.is_valid_onion_address(restored_addr):
            return None
        self.onion_address = restored_addr
        return restored_addr


    def update_docker_images(self, show_notifications=True):
        """Pull the stack's container images through `onionpress
        update-images` (cli.cmd_update_images: never for locally built
        images, a prune of dangling images after a fresh download, #230).
        True when something was downloaded."""
        rc, ans, out, err = self._run_launcher_json(["update-images", "--json"], timeout=900)
        if not ans:
            self.log(f"Error updating Docker images: {self._launcher_failure(out, err, 'no answer')}")
            return False
        if ans.get("local_images"):
            self.log("Skipping image update: running locally built images")
            if show_notifications:
                self.show_native_alert(
                    "Running Locally Built Images",
                    "Image updates are skipped because ONIONPRESS_TOR_IMAGE "
                    "or ONIONPRESS_WORDPRESS_IMAGE points at a locally built "
                    "image.\nUnset it to resume updates from the registry.",
                )
            return False
        if ans.get("error"):
            self.log(f"Failed to update Docker images: {ans['error']}")
            return False
        return bool(ans.get("pulled"))

    def _fix_app_bundle_permissions(self):
        """Make app bundle group-writable so any admin user can update it.

        On multi-user Macs, /Applications/OnionPress.app is owned by whoever
        installed it.  The other user can't replace the bundle on update.
        Fix: set group to 'admin' and add group-write, which is safe because
        only admin-group users can write, and all interactive macOS users are
        typically in admin.
        """
        try:
            app_path = os.path.dirname(self.contents_dir)  # /Applications/OnionPress.app
            if not app_path.startswith("/Applications"):
                return  # Only fix apps in /Applications

            import stat
            st = os.stat(app_path)
            # Check if group-writable already
            if st.st_mode & stat.S_IWGRP:
                return

            import grp
            try:
                admin_gid = grp.getgrnam("admin").gr_gid
            except KeyError:
                return  # No admin group (unusual)

            # Only attempt if we own the app bundle
            if st.st_uid != os.getuid():
                return

            self.log("Multi-user: fixing app bundle permissions (adding group-write for admin)")
            subprocess.run(
                ["chmod", "-R", "g+w", app_path],
                capture_output=True, timeout=30
            )
            subprocess.run(
                ["chgrp", "-R", "admin", app_path],
                capture_output=True, timeout=30
            )
            self.log("Multi-user: app bundle permissions updated")
        except Exception as e:
            self.log(f"Multi-user: could not fix app bundle permissions: {e}")

    @rumps.clicked("Check for Updates...")
    def check_for_updates(self, _):
        """Check GitHub for newer versions (`onionpress update --check
        --json`: cli.check_for_update_report, with the other macOS users
        running OnionPress) and update Docker images. The dialogs are this
        process's."""
        app_update_available = False
        rc, ans, out, err = self._run_launcher_json(["update", "--check", "--json"], timeout=120)
        if not ans or ans.get("error"):
            self.log(f"Update check failed: {(ans or {}).get('error') or self._launcher_failure(out, err, 'no answer')}")
            rumps.alert(
                title="Update Check Failed",
                message=f"Could not check for app updates.\n\nPlease visit:\nhttps://github.com/brewsterkahle/onionpress/releases"
            )
        elif ans.get("update_available"):
            app_update_available = True
            latest_version = ans.get("latest") or "?"

            # Multi-user check: are other users running OnionPress?
            others = ans.get("other_instances") or []
            if others:
                our_offset = int(os.environ.get("ONIONPRESS_PORT_OFFSET", "0"))

                if our_offset != 0:
                    # We're not the primary user — tell them to switch
                    primary_user = others[0]["user"]
                    self.show_native_alert(
                        "Update Available",
                        f"OnionPress v{latest_version} is available.\n\n"
                        f"To keep port assignments stable, please install "
                        f"the update from the primary account "
                        f"(\"{primary_user}\", port 8080).\n\n"
                        f"1. Quit OnionPress from this account first\n"
                        f"2. Log in as {primary_user}\n"
                        f"3. Click Check for Updates from their OnionPress menubar",
                    )
                    return
                else:
                    # We're primary but others are still running
                    active_offsets = ans.get("active_offsets") or []
                    user_lines = []
                    for o in others:
                        # Find the offset that isn't ours (0)
                        other_offsets = [off for off in active_offsets if off != 0]
                        port = 8080 + (other_offsets[0] if other_offsets else 10000)
                        user_lines.append(f"  \u2022 {o['user']} (port {port})")
                    user_list = "\n".join(user_lines)

                    self.show_native_alert(
                        "Update Available \u2014 Other Users Running",
                        f"OnionPress v{latest_version} is available.\n\n"
                        f"Other users are also running OnionPress:\n"
                        f"{user_list}\n\n"
                        f"To update safely:\n"
                        f"1. Ask each user to Quit OnionPress from their menubar\n"
                        f"2. Come back here and click Check for Updates again\n"
                        f"3. After the update, each user can relaunch OnionPress\n\n"
                        f"Their onion sites will be briefly offline during the update.",
                    )
                    return

            # Single user (or primary and others have quit) — proceed
            response = self.show_native_alert(
                "App Update Available",
                f"A new version of OnionPress is available!\n\nCurrent: v{self.version}\nLatest: v{latest_version}\n\nInstall will download and replace the app, then restart.",
                buttons=["Install Update", "Later"],
                cancel_button=1
            )
            if response == 0:  # Install Update clicked
                threading.Thread(
                    target=self._install_update,
                    args=(latest_version,),
                    daemon=True
                ).start()
                return  # Skip Docker update check — we're about to restart

        # Check for Docker image updates
        threading.Thread(target=self._check_docker_updates_async, args=(app_update_available,), daemon=True).start()

    def _install_update(self, latest_version):
        """`onionpress update --yes --json`: the CLI downloads the DMG and
        swaps the bundle in (the updater's staged copy and rename; the swap
        is its last step, since the CLI itself runs out of
        the bundle being replaced), then this process asks to relaunch."""
        install_path = os.path.dirname(self.contents_dir)

        # No notification toasts — the download-progress splash and the
        # "Update Installed — Restart OnionPress?" modal below already
        # tell the user what's happening. Using rumps.notification()
        # would import Foundation's NSUserNotificationCenter, which on
        # newer macOS can trigger an unsolicited "Would you like to
        # allow notifications?" prompt the very first time the app
        # launches — confusing during setup.
        rc, ans, out, err = self._run_launcher_json(["update", "--yes", "--json"], on_line=self.log)
        if rc == 0 and ans and ans.get("ok"):
            installed = ans.get("version") or latest_version
            # Prompt user to restart. Button text is explicitly
            # "Restart OnionPress" (not "Restart Now") so it doesn't
            # read as "restart the Mac" in the modal.
            response = self.show_native_alert(
                "Update Installed",
                f"OnionPress v{installed} has been installed.\n\nRestart OnionPress to use the new version.\n\nThis will briefly stop and restart all containers to pick up any changes. Your onion address stays the same.",
                buttons=["Restart OnionPress", "Later"]
            )
            if response == 0:  # Restart OnionPress
                self._relaunch_app(install_path)
            return

        failure = (ans or {}).get("error") or self._launcher_failure(out, err, "see the OnionPress log")
        self.log(f"Auto-update failed: {failure}")
        if ans and ans.get("permission_denied"):
            self.show_native_alert(
                "Update Failed \u2014 Permission Denied", failure, style="warning")
            return
        self.show_native_alert(
            "Update Failed",
            f"Could not install the update automatically.\n\n{failure}\n\nYou can update manually from:\nhttps://github.com/brewsterkahle/onionpress/releases",
            style="warning"
        )

    def _relaunch_app(self, app_path):
        """Full quit (stop containers + VM) then launch the new app"""
        self.log(f"Auto-update: relaunching from {app_path}")

        # Spawn a background process that waits for us to exit, then relaunches
        pid = os.getpid()
        relaunch_script = f'''
            while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
            sleep 1
            open "{app_path}"
        '''
        subprocess.Popen(
            ["bash", "-c", relaunch_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Skip OnionHeaven /offline — we're coming right back up
        self._updating = True

        # Full quit — stops containers, VM, and relaunches with new images
        self.quit_app(None)

    def _check_docker_updates_async(self, app_update_available):
        """Check for Docker updates in background thread"""
        images_updated = self.update_docker_images(show_notifications=True)

        # Show final summary if no app update was available. Not when running
        # locally built images — update_docker_images() has already said so,
        # and "all container images are up to date" would be a lie.
        if (not app_update_available and not images_updated
                and not containers.using_local_images(self._paths.config_file)):
            version = self.version
            self.show_native_alert(
                "No Updates Available",
                f"You're running the latest version (v{version})\nAll container images are up to date."
            )

    def show_setup_dialog(self):
        """Show a persistent setup dialog during first run that stays until service is ready"""
        try:
            # Dismiss any existing dialog first
            self.dismiss_setup_dialog()

            # Create and show dialog on main thread, storing reference for programmatic dismissal
            def create_and_show():
                try:
                    alert = AppKit.NSAlert.alloc().init()
                    alert.setMessageText_("OnionPress Setup")
                    alert.setInformativeText_("Setting up OnionPress for first use...\n\n• Downloading container images\n• Configuring Tor onion service\n• Starting WordPress\n\nThis may take 2-5 minutes depending on your internet speed.\n\nThis window will close automatically to set up your WordPress.")
                    alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

                    btn_dismiss = alert.addButtonWithTitle_("Dismiss")
                    btn_dismiss.setKeyEquivalent_("\r")
                    btn_cancel = alert.addButtonWithTitle_("Cancel Setup")
                    btn_cancel.setKeyEquivalent_("\x1b")

                    # Set app icon
                    icon_path = os.path.join(self.resources_dir, "app-icon.png")
                    if os.path.exists(icon_path):
                        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                        if icon:
                            alert.setIcon_(icon)

                    # Store reference so dismiss_setup_dialog can close it
                    self.setup_alert = alert

                    # runModal blocks until button click or abortModal
                    response = alert.runModal()

                    # Close the alert window
                    alert.window().close()
                    self.setup_alert = None

                    # NSModalResponseAbort = -1001 (from abortModal call)
                    if response == AppKit.NSModalResponseAbort:
                        self.log("Setup dialog auto-dismissed (service ready)")
                    else:
                        button_index = response - 1000
                        if button_index == 1:
                            self.log("User cancelled setup - stopping services")
                            subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                        elif button_index == 0:
                            self.log("User dismissed setup dialog")

                    self.setup_dialog_showing = False
                except Exception as e:
                    self.log(f"Error in setup dialog: {e}")
                    self.setup_dialog_showing = False
                    self.setup_alert = None

            self.setup_dialog_showing = True
            _main_thread(create_and_show)
            self.log("Setup dialog shown (native NSAlert)")
        except Exception as e:
            self.log(f"Error showing setup dialog: {e}")
            self.setup_dialog_showing = False
            self.log("Setup dialog fallback - dialog failed to show")

    def dismiss_setup_dialog(self):
        """Dismiss the setup dialog if it's showing (native NSAlert)"""
        if self.setup_dialog_showing:
            self.setup_dialog_showing = False
            self.log("Setup dialog marked for dismissal")
            try:
                if self.setup_alert:
                    AppKit.NSApp.abortModal()
                    self.log("Setup dialog dismissed programmatically")
            except Exception as e:
                self.log(f"Error dismissing setup dialog: {e}")


    @rumps.clicked("About OnionPress")
    def show_about(self, _):
        """Show about dialog"""
        about_text = f"""OnionPress v{self.version}

Run your own website from your Mac. Just Works. Free, forever.
WordPress + Tor Onion Service

Features:
• Full WordPress that you own and run
• Internet Archive's Wayback Machine integration
• Tor Onion Service with an address you own, forever
• Requires visitors to use Tor or Brave browsers
• Privacy-first design
• Free and open source

Created by the Internet Archive
License: AGPL v3 or later"""

        web_url = "https://onionpress.org"
        link_label = "onionpress.org"

        def show_dialog():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("About OnionPress")
            alert.setInformativeText_(about_text)
            alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            btn = alert.addButtonWithTitle_("OK")
            btn.setKeyEquivalent_("\r")

            # Set app icon if available
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Clickable website link as accessory view
            link_field = AppKit.NSTextField.labelWithString_("")
            link_field.setSelectable_(True)
            link_field.setAllowsEditingTextAttributes_(True)
            link_field.setBordered_(False)
            link_field.setDrawsBackground_(False)

            # Build attributed string with clickable link
            attr_str = AppKit.NSMutableAttributedString.alloc().initWithString_(link_label)
            url = AppKit.NSURL.URLWithString_(web_url)
            full_range = AppKit.NSMakeRange(0, len(link_label))
            attr_str.addAttribute_value_range_(AppKit.NSLinkAttributeName, url, full_range)
            font = AppKit.NSFont.systemFontOfSize_(AppKit.NSFont.smallSystemFontSize())
            attr_str.addAttribute_value_range_(AppKit.NSFontAttributeName, font, full_range)

            link_field.setAttributedStringValue_(attr_str)
            link_field.sizeToFit()
            alert.setAccessoryView_(link_field)

            alert.runModal()

        if AppKit.NSThread.isMainThread():
            show_dialog()
        else:
            _main_thread(show_dialog)

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Uninstall OnionPress with mandatory backup prompt"""
        # Step 1: Show critical warning about data loss (native NSAlert - no permissions)
        button_index = self.show_native_alert(
            title="Uninstall Warning",
            message="CRITICAL WARNING\n\nUninstalling will PERMANENTLY DELETE:\n\u2022 Your onion address and private key\n\u2022 All WordPress content and data\n\u2022 Database and configuration\n\nYour site CANNOT BE RECOVERED unless you have a backup.\n\nDo you want to create a backup before uninstalling?",
            buttons=["Cancel", "No, Delete Everything", "Yes, Backup First"],
            default_button=2,
            cancel_button=0,
            style="critical"
        )

        if button_index == 0:  # Cancel
            return

        if button_index == 2:  # Yes, Backup First
            self.log("User chose to backup before uninstall")
            if not self.is_running:
                rumps.alert(
                    title="Service Not Running",
                    message="Cannot create a backup while service is stopped.\n\nPlease start the service first, then try uninstall again."
                )
                return
            # Back up FIRST, then proceed ONLY after it completes successfully.
            # The teardown must never run concurrently with the backup — that
            # would corrupt the backup mid-write and then delete everything (#258).
            self.backup(None, on_complete=lambda ok: (
                self._proceed_with_uninstall() if ok
                else self.log("Uninstall aborted — backup did not complete")))
            return

        # button_index == 1: "No, Delete Everything" — go straight to the final
        # DELETE confirmation (no backup; no running-service requirement).
        self._proceed_with_uninstall()

    def _proceed_with_uninstall(self):
        """The questions, then the teardown. Invoked directly for "Delete
        Everything", or as the backup on_complete callback (success only)
        so the destructive work never races an in-progress backup (#258).

        `onionpress uninstall --check --json` says what to ask
        (onionpress.confirm): "hub-holds-key" when an OnionHeaven hub may
        hold a copy of the key and OnionPress is not running to ask it to
        let go, then "uninstall" (the typed DELETE); each is answered here
        and given back as --confirmed. The run itself is the CLI's
        (onionpress.uninstall): the hubs, the stop, the Colima VM, the
        login item, ~/.onionpress."""
        confirmed = []
        while True:
            flags = [f for what in confirmed for f in ("--confirmed", what)]
            rc, ans, out, err = self._run_launcher_json(
                ["uninstall", "--check", "--json", *flags], timeout=300)
            if not ans or ans.get("error"):
                failure = (ans or {}).get("error") or self._launcher_failure(
                    out, err, "see the OnionPress log")
                self.log(f"Uninstall: check failed: {failure}")
                self.show_native_alert(title="Uninstall Error",
                                       message=f"OnionPress could not check what to ask:\n\n{failure}",
                                       buttons=["OK"], style="warning")
                return
            what = ans.get("confirm")
            if not what:
                break
            if not self._ask_cli_question(what, ans, action="Uninstall"):
                self.log(f"Uninstall cancelled at the '{what}' question")
                return
            confirmed.append(what)
        flags = [f for what in confirmed for f in ("--confirmed", what)]

        # User confirmed uninstall - run in background thread to avoid beach ball
        def do_uninstall():
            try:
                # First, stop any ongoing setup processes
                self.log("Uninstall: Stopping any ongoing processes...")
                # Stop any ongoing browser monitoring
                self.monitoring_tor_install = False
                self.dismiss_setup_dialog()

                # The CLI stops the daemon (its heartbeat holds the key)
                # before the /unregister and the stop (onionpress.uninstall).
                rc, ans, out, err = self._run_launcher_json(
                    ["uninstall", "--json", *flags], on_line=self.log)


                if rc != 0 or not ans or not ans.get("ok"):
                    failure = (ans or {}).get("error") or self._launcher_failure(
                        out, err, "see the OnionPress log")
                    raise RuntimeError(failure)

                hub_notice = onionheaven.describe_remaining(ans.get("remaining") or [])
                if hub_notice:
                    self.log(f"Uninstall: {hub_notice}")
                    hub_notice = ("\n\n" + hub_notice + " Ask the hub's "
                                  "operator to remove it if you need it gone.")
                # Use show_native_alert which already handles main thread
                self.show_native_alert(
                    title="Uninstall Complete",
                    message=("OnionPress has been uninstalled." + hub_notice +
                             "\n\nFinal step: Move OnionPress.app to the Trash.\n\nClick OK to quit."),
                    buttons=["OK"]
                )
                rumps.quit_application()

            except Exception as e:
                # Show error and quit
                self.show_native_alert(
                    title="Uninstall Error",
                    message=f"An error occurred during uninstall:\n\n{str(e)}\n\nYou may need to manually remove:\n• ~/.onionpress directory\n• Docker volumes (if they exist)",
                    buttons=["OK"]
                )
                rumps.quit_application()

        # Run uninstall in background thread to avoid blocking UI
        threading.Thread(target=do_uninstall, daemon=True).start()

    def _ask_cli_question(self, what, facts, action="Continue"):
        """A question the CLI stopped at (onionpress.confirm: {"confirm":
        what, ...facts}), asked of the person at this Mac in a native
        dialog; True only for the explicit yes. Cancel is the default, and
        no answer (show_native_alert gives up after 5 minutes) is Cancel.
        *action* names the command for the yes button ("Uninstall",
        "Reset", "Change")."""
        if what == "address-change":
            return self._ask_to_replace_address(
                facts.get("current") or "", facts.get("new") or "",
                "replace this site's onion service key",
                "OnionPress keeps a copy of the current key in "
                "~/.onionpress/replaced-keys/, from which it can be imported "
                "again.",
                "Replace")
        if what == "hub-holds-key":
            listed = "\n".join(f"• {h.get('onionheaven_address')} (key of {h.get('content_address')})"
                               for h in facts.get("hubs") or [])
            choice = self.show_native_alert(
                title="OnionHeaven Holds Your Key",
                message=("These OnionHeaven hubs may hold a copy of your onion "
                         f"service private key:\n\n{listed}\n\n"
                         f"OnionPress asks them to delete it when you {action.lower()}, "
                         "but only while it is running. Start OnionPress and "
                         f"{action.lower()} again, or {action.lower()} anyway and the hub "
                         "keeps the key."),
                buttons=["Cancel", f"{action} Anyway"],
                default_button=0,
                cancel_button=0,
                style="critical"
            )
            return choice == 1
        if what == "uninstall":
            # Final confirmation with explicit acknowledgment. rumps.Window
            # for text input (no osascript, no permissions needed)
            window = rumps.Window(
                message="FINAL CONFIRMATION\n\nType 'DELETE' below to confirm permanent deletion of all data:",
                title="Confirm Uninstall",
                default_text="",
                ok="Confirm Deletion",
                cancel="Cancel",
                dimensions=(320, 24)
            )
            response = window.run()
            self.log(f"Final confirmation: button={response.clicked}, text='{response.text}'")
            if response.clicked != 1:  # User clicked Cancel
                self.log("Uninstall cancelled - user clicked Cancel")
                return False
            user_input = response.text.strip().upper() if response.text else ""
            if user_input != "DELETE":
                self.log(f"Uninstall cancelled - user input was: '{response.text.strip()}' (expected 'DELETE')")
                rumps.alert(
                    title="Uninstall Cancelled",
                    message=f"Uninstall cancelled. Type 'DELETE' to confirm.\n\n(You typed: '{response.text.strip()}')"
                )
                return False
            return True
        if what == "reset":
            choice = self.show_native_alert(
                title="Reset OnionPress?",
                message=("This will ERASE everything: WordPress, database, Tor state, "
                         "onion address keys, config, and secrets.\n\n"
                         "To preserve your data, make a backup first (Backup... in "
                         "the menu). This cannot be undone."),
                buttons=["Cancel", "Reset"], default_button=0, cancel_button=0,
                style="critical")
            return choice == 1
        if what == "prefix-change":
            choice = self.show_native_alert(
                title="Change the Address Prefix?",
                message=(f"The address prefix would change from "
                         f"'{facts.get('current') or ''}' to '{facts.get('new') or ''}', but "
                         f"this site's onion address stays\n{facts.get('address') or ''}\n"
                         "(the prefix is used only when a new address is made: a reset, "
                         "or a new install)."),
                buttons=["Cancel", "Change Prefix"], default_button=0, cancel_button=0,
                style="warning")
            return choice == 1
        self.log(f"Unknown question from the CLI: '{what}' (cancelled)")
        return False

    # ── Settings Page Support ─────────────────────────────────────






    def _ask_settings_page_question(self, result):
        """The question `onionpress handle-action` stopped at, asked of the
        person at this Mac; True only for an explicit Replace/Restore.
        Today one question: "address-change" (handle_action.py), for a key
        import or a restore of another address from the page."""
        what = result.get("confirm")
        current = result.get("current") or ""
        new_address = result.get("new") or ""
        if what != "address-change":
            self.log(f"Settings page: unknown question '{what}' (cancelled)")
            return False
        if result.get("action") == "restore-backup":
            confirmed = self._ask_to_replace_address(
                current, new_address,
                "restore a backup of another onion address, which replaces this "
                "site's onion service key and its content",
                "The current site's content is replaced by the backup's.",
                "Restore")
            self.log(f"Settings page: restore of {new_address} "
                     f"{'confirmed' if confirmed else 'not confirmed'} at this Mac")
            return confirmed
        confirmed = self._ask_cli_question("address-change", result)
        self.log(f"Settings page: key import of {new_address} "
                 f"{'confirmed' if confirmed else 'not confirmed'} at this Mac")
        return confirmed

    def _ask_to_replace_address(self, current, new_address, request, note, button):
        """Ask the person at this Mac whether the Settings page may replace
        this site's onion address *current* with *new_address*; True only
        for an explicit *button* click. Cancel is the default, and no answer
        (show_native_alert gives up after 5 minutes) is Cancel too.

        Anything running in WordPress can write the page's requests, so a
        key import or a restore of another address from there changes the
        address only after this dialog (`onionpress handle-action` stops
        at the question; _ask_settings_page_question). *request* finishes
        "asked to ..."; *note* says what is kept.
        """
        choice = self.show_native_alert(
            "Replace this site's onion address?",
            f"The OnionPress Settings page in WordPress asked to {request}.\n\n"
            f"Current address:\n{current or '(not known yet)'}\n\n"
            f"New address:\n{new_address}\n\n"
            "Visitors and links using the current address will stop reaching "
            f"this site. {note}\n\n"
            f"{button} it only if you asked for this yourself.",
            buttons=["Cancel", button], default_button=0, cancel_button=0,
            style="critical")
        return choice == 1

    # ── Wayback Queue ──────────────────────────────────────────────


    def handle_reopen(self):
        """Handle reopen signal from launcher (user double-clicked app while running)"""
        self.log("Reopen signal received")
        if self.is_running and self.is_ready:
            self.log("Service is ready — opening browser")
            self.open_tor_browser(None)
        elif not self.is_running:
            self.log("Service not running — starting service")
            self.start_service(None)

    def update_menu(self):
        """Repaint the menu from the daemon's state (display_state)."""
        def do_update():
            state = self.display_state

            # OnionHeaven alert indicator: show "!" next to icon when messages exist.
            # Track insertion with a flag because the item's title changes with the
            # alert count, which invalidates any `title in self.menu` lookup and
            # would otherwise re-insert an already-attached NSMenuItem (raises
            # NSInternalInconsistencyException).
            if self.onionheaven_messages:
                self.title = "!"
                count = len(self.onionheaven_messages)
                self.onionheaven_alert_item.title = f"OnionHeaven Alerts ({count})"
                self.onionheaven_alert_item.set_callback(self.view_onionheaven_alerts)
                if not self._onionheaven_alert_in_menu:
                    self.menu.insert_after("Copy Onion Address", self.onionheaven_alert_item)
                    self._onionheaven_alert_in_menu = True
            else:
                self.title = ""
                if self._onionheaven_alert_in_menu:
                    for key in list(self.menu.keys()):
                        if isinstance(key, str) and key.startswith("OnionHeaven Alerts"):
                            del self.menu[key]
                    self._onionheaven_alert_in_menu = False

            # Show/hide clearnet status based on tunnel config and state
            show_clearnet = (state == "available" and self.cloudflare_tunnel_enabled)
            if show_clearnet:
                self.clearnet_status_item.title = "Clearnet: Active (via Cloudflare)"
                self.clearnet_status_item.set_callback(None)
                if self.clearnet_status_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self.clearnet_status_item)
            else:
                if "Clearnet: Active (via Cloudflare)" in self.menu:
                    del self.menu["Clearnet: Active (via Cloudflare)"]

            if self._quitting:
                return  # Don't update icon/menu during shutdown

            if state == "available":
                self.icon = self.icon_running
                onionname = self.read_config_value("ONIONNAME", "").strip()
                short = self._short_onion(self.onion_address)
                if onionname:
                    self.menu["Starting..."].title = f"{onionname}@{short}"
                else:
                    self.menu["Starting..."].title = f"Address: {short}"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.menu["Backup..."].set_callback(self.backup)
                self.menu["Restore..."].set_callback(self.restore)
                self.update_browser_menu_title()
                # Purple: browser opens .onion, local site available as secondary
                self.browser_menu_item.set_callback(self.open_tor_browser)
                self.local_site_item.title = f"Open Local Site ({self.local_url})"
                self.local_site_item.set_callback(self.open_local_site)
            elif state in ("starting", "offline", "stuck", "stalled", "reconnecting"):
                if state == "starting":
                    self.icon = self.icon_starting
                    pct = self._last_bootstrap_pct
                    if pct > 0:
                        self.menu["Starting..."].title = f"Status: Connecting to Tor ({pct}%)..."
                    else:
                        self.menu["Starting..."].title = "Status: Starting up, please wait..."
                elif state == "reconnecting":
                    # No fresh state from the daemon: it is starting, or
                    # launchd is restarting it. Never "Stopped": the site
                    # is most likely still up.
                    self.icon = self.icon_starting
                    if self._daemon_seen:
                        self.menu["Starting..."].title = "Status: Reconnecting to the OnionPress service..."
                    else:
                        self.menu["Starting..."].title = "Status: Starting up, please wait..."
                elif state == "offline":
                    self.icon = self.icon_stopped
                    self.menu["Starting..."].title = "Status: Offline — no internet connection"
                elif state == "stalled":
                    self.icon = self.icon_starting
                    self.menu["Starting..."].title = "Status: Health check stalled (system busy)"
                else:  # stuck
                    self.icon = self.icon_starting
                    # Not "try Restart": the app is already retrying (tor
                    # restart on a stalled bootstrap, descriptor republish
                    # on a wedge), and a restart throws that away (#321).
                    self.menu["Starting..."].title = "Status: Slow to connect — still trying..."
                self.menu["Start"].set_callback(self.start_service if state == "reconnecting" else None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.menu["Backup..."].set_callback(self.backup)
                self.menu["Restore..."].set_callback(None)
                # Gray/Yellow: browser opens local site since .onion isn't reachable
                self.browser_menu_item.title = f"Open Local Site ({self.local_url})"
                self.browser_menu_item.set_callback(self.open_local_site)
                self.local_site_item.title = ""
                self.local_site_item.set_callback(None)
            else:
                # Stopped
                self.icon = self.icon_stopped
                if self._start_failed_rc is not None:
                    self.menu["Starting..."].title = "Status: Start failed — see View Logs"
                elif self.onion_address and self.onion_address.endswith('.onion'):
                    self.menu["Starting..."].title = f"Stopped — {self._short_onion(self.onion_address)}"
                else:
                    self.menu["Starting..."].title = "Status: Stopped"
                self.menu["Start"].set_callback(self.start_service)
                self.menu["Stop"].set_callback(None)
                self.menu["Restart"].set_callback(None)
                self.menu["Backup..."].set_callback(None)
                self.menu["Restore..."].set_callback(None)
                # Stopped: disable browser items
                self.browser_menu_item.set_callback(None)
                self.local_site_item.title = ""
                self.local_site_item.set_callback(None)

        # Execute on main thread
        _main_thread(do_update)

    def view_onionheaven_alerts(self, _):
        """Show OnionHeaven alert messages and offer to dismiss them."""
        if not self.onionheaven_messages:
            rumps.alert("No OnionHeaven alerts.")
            return

        # Build summary of all messages
        lines = []
        for msg in self.onionheaven_messages:
            msg_type = str(msg.get("type", "unknown")).replace("_", " ").title()
            msg_text = msg.get("message", "")
            lines.append(f"[{msg_type}] {msg_text}")
        summary = "\n".join(lines)

        response = rumps.alert(
            title=f"OnionHeaven Alerts ({len(self.onionheaven_messages)})",
            message=summary,
            ok="Dismiss All",
            cancel="Close"
        )

        if response == 1:  # "Dismiss All" clicked
            self.log("Dismissing OnionHeaven alerts")
            self.onionheaven_messages = []
            # The daemon deletes the message files from the tor container
            # on its next tick (.dismiss-onionheaven-alerts).
            try:
                with open(os.path.join(self.app_support, op_daemon.DISMISS_ALERTS_MARKER), "w") as f:
                    f.write(str(time.time()))
            except OSError as e:
                self.log(f"Could not ask the daemon to dismiss the alerts: {e}")
            self.update_menu()

    def _register_terminate_notification(self):
        """App termination (osascript quit, Apple Event, the launcher's
        SIGTERM) runs the same teardown as the Quit item, synchronously
        before the app exits."""
        def wrapper(notification):
            try:
                if not getattr(self, '_quitting', False):
                    self._handle_terminate()
            except Exception as e:
                try:
                    self.log(f"WARNING: terminate callback error: {e}")
                except Exception:
                    pass
        AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationWillTerminateNotification,
            None,
            None,  # Deliver on posting thread (main thread)
            wrapper)
        self.log("Registered for app termination notification")

    def _quit_keeps_site_running(self):
        """QUIT_KEEPS_SITE_RUNNING=yes in ~/.onionpress/config: Quit closes
        only this menu bar app; the daemon keeps the site running."""
        return self.read_config_value("QUIT_KEEPS_SITE_RUNNING", "no").strip().lower() == "yes"

    def _perform_quit_cleanup(self):
        """Take the site down: `onionpress quit --leave-menubar` stops the
        daemon (which tells the hub /offline first), the containers and
        the Colima VM and reaps lima's usernet daemon; this process only
        removes its pid file. With QUIT_KEEPS_SITE_RUNNING=yes nothing
        but this process ends. Shared by the Quit item, Apple Event quit
        and SIGTERM."""
        if self._quit_keeps_site_running():
            self.log("Quit: QUIT_KEEPS_SITE_RUNNING=yes — the site and the daemon keep running")
        else:
            args = [self.launcher_script, "quit", "--leave-menubar"]
            if getattr(self, '_updating', False):
                args.append("--no-offline")    # coming right back
            try:
                self.log("Stopping OnionPress (daemon, containers, VM)...")
                r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=300, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    self.log(f"Warning: onionpress quit exited {r.returncode}: "
                             f"{(r.stderr or r.stdout).strip()[-300:]}")
                else:
                    self.log("OnionPress stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: onionpress quit timed out")
            except Exception as e:
                self.log(f"Warning: onionpress quit failed: {e}")
        self._remove_pid_file()

    def _handle_terminate(self):
        """Handle app termination (osascript quit, Apple Event, etc.).
        Runs synchronously before the app exits to ensure proper cleanup."""
        if self._quitting:
            return  # Already cleaning up via Quit button
        self._quitting = True
        self.log("="*60)
        self.log("APP TERMINATING (Apple Event / osascript quit)")
        self.log("="*60)
        self._perform_quit_cleanup()
        self.log("Cleanup complete")

    @rumps.clicked("Stop")
    def stop_service(self, _):
        """Stop the WordPress + Tor service (`onionpress stop`: the daemon
        tells the hub /offline first; the daemon itself keeps running)."""
        self._stopping = True
        self.menu["Starting..."].title = "Status: Stopping..."
        self.menu["Stop"].set_callback(None)  # Disable immediately to prevent double-click

        def stop():
            try:
                subprocess.run([self.launcher_script, "stop"], stdin=subprocess.DEVNULL)
                self._wait_for_state(lambda s: not s.get("is_running"), 30)
            finally:
                self._stopping = False
                self.update_menu()

        threading.Thread(target=stop, daemon=True).start()

    @rumps.clicked("Restart")
    def restart_service(self, _):
        """Restart the WordPress + Tor service"""
        self._stopping = False  # Clear in case Stop was hit previously
        self.menu["Starting..."].title = "Status: Restarting..."
        self.icon = self.icon_starting  # Change icon to indicate restarting

        def restart():
            self.auto_opened_browser = False  # Re-open browser after restart

            # Check if address prefix changed before restarting
            if not self.check_address_prefix_change():
                self.log("Restart aborted due to address prefix issue")
                self.menu["Starting..."].title = "Status: Stopped"
                self.icon = self.icon_stopped
                return

            # Run restart command
            subprocess.run([self.launcher_script, "restart"], stdin=subprocess.DEVNULL)
            if self._wait_for_state(lambda s: s.get("is_running"), 60):
                self.log("Containers running after restart")
            self.update_menu()

        threading.Thread(target=restart, daemon=True).start()

    @rumps.clicked("View Logs")
    def view_logs(self, _):
        """Open logs in built-in log viewer"""
        # The daemon's log (the loops), this process's, and the launcher's
        # flat file (every ERROR from start_containers lives there).
        daemon_log = current_log_path(self.logs_dir, "onionpress")
        launcher_log = os.path.join(self.app_support, "onionpress.log")
        opened = False
        if daemon_log and os.path.exists(daemon_log):
            _LogViewerWindow.show_for_file(daemon_log, "OnionPress Log")
            opened = True
        if os.path.exists(launcher_log):
            _LogViewerWindow.show_for_file(launcher_log, "Launcher Log (onionpress start)")
            opened = True
        if os.path.exists(self.log_file):
            _LogViewerWindow.show_for_file(self.log_file, "Menu Bar Log")
            opened = True
        if not opened:
            rumps.alert("No logs available yet")

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        self.log("="*60)
        self.log(f"QUIT BUTTON CLICKED - v{self.version} RUNNING")
        self.log("="*60)
        self._quitting = True  # Prevent _handle_terminate from running again

        # Stop monitoring immediately
        self.monitoring_tor_install = False
        self.dismiss_setup_dialog()

        # Close any open log viewer windows
        _LogViewerWindow.close_all()

        # Show stopped icon and status during shutdown — stays visible until
        # all services are actually stopped (prevents port conflicts on relaunch)
        def show_stopping():
            self.menu["Starting..."].title = "Quitting..."
            self.icon = self.icon_stopped
        _main_thread(show_stopping)

        def cleanup_and_quit():
            # Small delay to ensure UI updates
            time.sleep(0.5)
            self._perform_quit_cleanup()
            self.log("Cleanup complete, exiting")
            # Now quit (must dispatch to main thread)
            _main_thread(rumps.quit_application)

        # Non-daemon thread so the app stays alive until cleanup finishes
        threading.Thread(target=cleanup_and_quit, daemon=False).start()

if __name__ == "__main__":
    OnionPressApp().run()
