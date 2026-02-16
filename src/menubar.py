#!/usr/bin/env python3
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
from datetime import datetime
import AppKit
import signal
import socket
import atexit

# Add scripts directory to path for imports
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

import key_manager
import onion_proxy
import install_native_messaging
import setup_window


def parse_version(version_str):
    """Parse a version string like '2.10.3' into a tuple of ints for comparison."""
    try:
        return tuple(int(x) for x in version_str.split('.'))
    except (ValueError, AttributeError):
        return (0,)


class OnionPressApp(rumps.App):
    def __init__(self):
        # Get paths first (fast - no I/O)
        self.app_support = os.path.expanduser("~/.onionpress")
        self.script_dir = os.path.dirname(os.path.realpath(__file__))

        # Single-instance safety net via PID file
        self.pid_file = os.path.join(self.app_support, "menubar.pid")
        os.makedirs(self.app_support, exist_ok=True)
        if os.path.exists(self.pid_file):
            try:
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
        self.log_file = os.path.join(self.app_support, "onionpress.log")

        # Initialize rumps WITHOUT icon first (fastest possible)
        super(OnionPressApp, self).__init__("", quit_button=None)

        # Show launch splash IMMEDIATELY before any I/O
        self.launch_splash = None
        self.show_launch_splash()

        # Now load icon files (this does I/O but splash is already showing)
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Set the stopped icon
        self.icon = self.icon_stopped

        # Set version to placeholder (will be updated in background)
        self.version = "2.2.83"

        # Set up environment variables (fast - no I/O)
        docker_config_dir = os.path.join(self.app_support, "docker-config")
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        os.environ["DOCKER_CONFIG"] = docker_config_dir

        # Do slow I/O operations in background after icon appears
        def background_init():
            # Append to existing log file (continuous log across sessions)
            with open(self.log_file, 'a') as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === New session starting ===\n")
                f.write(f"{'=' * 60}\n")

            # Debug logging
            with open(self.log_file, 'a') as f:
                f.write(f"DEBUG: frozen={getattr(sys, 'frozen', False)}\n")
                f.write(f"DEBUG: resources_dir={self.resources_dir}\n")
                f.write(f"DEBUG: bin_dir={self.bin_dir}\n")
                f.write(f"DEBUG: launcher_script={self.launcher_script}\n")
                f.write(f"DEBUG: icon_stopped exists={os.path.exists(self.icon_stopped)}\n")
                f.write(f"DEBUG: icon_stopped path={self.icon_stopped}\n")
                f.write(f"DEBUG: rumps initialized successfully\n")

            # Create Docker config without credential store (avoids docker-credential-osxkeychain errors)
            os.makedirs(docker_config_dir, exist_ok=True)
            docker_config_file = os.path.join(docker_config_dir, "config.json")
            if not os.path.exists(docker_config_file):
                with open(docker_config_file, 'w') as f:
                    f.write('{\n\t"auths": {},\n\t"currentContext": "colima"\n}\n')

            # Install docker-compose plugin: prefer bundled, fall back to system
            cli_plugins_dir = os.path.join(docker_config_dir, "cli-plugins")
            os.makedirs(cli_plugins_dir, exist_ok=True)
            compose_plugin_dest = os.path.join(cli_plugins_dir, "docker-compose")
            bundled_compose = os.path.join(self.bin_dir, "docker-compose")
            system_compose = os.path.expanduser("~/.docker/cli-plugins/docker-compose")
            if os.path.isfile(bundled_compose) and not os.path.exists(compose_plugin_dest):
                try:
                    os.symlink(bundled_compose, compose_plugin_dest)
                except Exception:
                    pass
            elif os.path.islink(system_compose) and not os.path.exists(compose_plugin_dest):
                try:
                    os.symlink(system_compose, compose_plugin_dest)
                except Exception:
                    pass

            # Get actual version from Info.plist
            self.version = self.get_version()

            # Log version information at startup
            self.log_version_info()

            # Update browser menu title after checking filesystem
            self.update_browser_menu_title()

            # Install native messaging manifests for browser extension support
            try:
                install_native_messaging.install(log_func=self.log)
            except Exception as e:
                self.log(f"Native messaging install failed: {e}")

        # Start background initialization
        threading.Thread(target=background_init, daemon=True).start()

        # State — load cached onion address from previous run if available
        cached_addr_file = os.path.join(self.app_support, "onion_address")
        try:
            with open(cached_addr_file) as f:
                cached = f.read().strip()
            if cached and cached.endswith('.onion'):
                self.onion_address = cached
            else:
                self.onion_address = "Starting..."
        except (OSError, IOError):
            self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
        self.checking = False
        self._checking_lock = threading.Lock()  # Protect self.checking from race conditions
        self.web_log_process = None  # Background process for web logs
        self.web_log_file_handle = None  # File handle for web log capture
        self.last_status_logged = None  # Track last logged status to avoid spam
        self.auto_opened_browser = False  # Track if we've auto-opened browser this session
        self.setup_dialog_showing = False  # Track if setup dialog is currently showing
        self.setup_alert = None  # Reference to NSAlert for programmatic dismissal
        self.monitoring_tor_install = False  # Track if we're monitoring for Tor Browser installation
        self.caffeinate_process = None  # Process handle for caffeinate to prevent sleep
        self.proxy_server = None  # Onion proxy HTTP server instance
        self.proxy_thread = None  # Thread running the proxy server
        self._wp_installed = None  # None = unknown, True/False = checked
        self._setup_page_opened = False  # Track if we've opened the setup page
        self._port_conflict = False  # True if ports are in use by another instance
        self._has_internet = True          # Host-level internet connectivity
        self._last_bootstrap_pct = 0       # Last observed Tor bootstrap percentage
        self._bootstrap_stall_count = 0    # Consecutive checks with no bootstrap progress
        self._yellow_since = None          # Timestamp when entered yellow state
        self._was_ready = False            # Were we ever ready this session?
        self.healthcheck_address = None    # Healthcheck .onion address
        self.relay_messages = []           # Messages received from OnionRelay
        self._relay_alert_shown = False    # Whether we've shown the relay alert icon

        # Menu items
        # Store reference to browser menu item so we can update its title
        self.browser_menu_item = rumps.MenuItem("Open in Tor Browser", callback=self.open_tor_browser)
        self.relay_alert_item = rumps.MenuItem("Relay Alerts", callback=self.view_relay_alerts)

        self.menu = [
            rumps.MenuItem("Starting...", callback=None),
            rumps.separator,
            rumps.MenuItem("Copy Onion Address", callback=self.copy_address),
            self.browser_menu_item,
            self.relay_alert_item,
            rumps.separator,
            rumps.MenuItem("Start", callback=self.start_service),
            rumps.MenuItem("Stop", callback=self.stop_service),
            rumps.MenuItem("Restart", callback=self.restart_service),
            rumps.separator,
            rumps.MenuItem("View Logs", callback=self.view_logs),
            rumps.MenuItem("View Web Usage Log", callback=self.view_web_log),
            rumps.MenuItem("Settings...", callback=self.open_settings),
            rumps.separator,
            rumps.MenuItem("Export Private Key...", callback=self.export_key),
            rumps.MenuItem("Import Private Key...", callback=self.import_key),
            rumps.separator,
            rumps.MenuItem("Check for Updates...", callback=self.check_for_updates),
            rumps.MenuItem("About OnionPress", callback=self.show_about),
            rumps.MenuItem("Uninstall...", callback=self.uninstall),
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Ensure Docker is available
        threading.Thread(target=self.ensure_docker_available, daemon=True).start()

        # Start status checker
        self.start_status_checker()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

    def show_launch_splash(self):
        """Show non-blocking launch splash with logo - no I/O blocking"""
        def show():
            try:
                # Create window (no I/O) - taller for buttons and time estimate
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

                # Add "Launching..." text (no I/O)
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

                window.setContentView_(content_view)
                window.makeKeyAndOrderFront_(None)

                self.launch_splash = window
                self.launch_splash_time_field = time_field  # Store reference for updates

                # Log splash creation
                try:
                    with open(self.log_file, 'a') as f:
                        f.write(f"DEBUG: Launch splash created and shown\n")
                except Exception:
                    pass

                # Add logo in background (I/O happens after window shows)
                def add_logo():
                    icon_path = os.path.join(self.resources_dir, "app-icon.png")
                    if os.path.exists(icon_path):
                        image_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(110, 180, 100, 100))
                        image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                        if image:
                            image_view.setImage_(image)
                            content_view.addSubview_(image_view)
                threading.Thread(target=add_logo, daemon=True).start()

            except Exception as e:
                pass  # Don't log yet, log file not ready

        # Show on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(show)

    def dismiss_launch_splash(self):
        """Dismiss the launch splash window"""
        def dismiss():
            if self.launch_splash:
                try:
                    self.log("Dismissing launch splash")
                    self.launch_splash.orderOut_(None)
                    self.launch_splash.close()
                    self.launch_splash = None
                except Exception as e:
                    self.log(f"Error dismissing launch splash: {e}")

        # Dismiss on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(dismiss)

    def openLogFile_(self, sender):
        """Action handler for View Log button — open in Console.app for live tailing"""
        try:
            subprocess.run(["open", "-a", "Console", self.log_file], check=False)
        except Exception as e:
            self.log(f"Error opening log file: {e}")

    def dismissSplashButton_(self, sender):
        """Action handler for Dismiss button"""
        self.dismiss_launch_splash()

    def log(self, message):
        """Write log message to onionpress.log file"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"[{timestamp}] {message}\n"
            fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as f:
                f.write(log_message)
        except Exception as e:
            print(f"Error writing to log: {e}")

    def start_caffeinate(self):
        """Start caffeinate to prevent Mac from sleeping while service runs"""
        # Check if already running
        if self.caffeinate_process is not None:
            try:
                # Check if process is still alive
                if self.caffeinate_process.poll() is None:
                    return  # Already running
            except Exception:
                pass

        # Check config setting
        prevent_sleep = self.read_config_value("PREVENT_SLEEP", "yes").lower()
        if prevent_sleep != "yes":
            self.log("Sleep prevention disabled in config")
            return

        try:
            # Start caffeinate in background
            # -s: prevent system sleep when on AC power (battery power allows normal sleep)
            self.caffeinate_process = subprocess.Popen(
                ["caffeinate", "-s"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self.log(f"Started caffeinate (PID {self.caffeinate_process.pid}) - Mac will stay awake while plugged in")
        except Exception as e:
            self.log(f"Failed to start caffeinate: {e}")

    def stop_caffeinate(self):
        """Stop caffeinate to allow Mac to sleep normally"""
        if self.caffeinate_process is not None:
            try:
                self.caffeinate_process.terminate()
                self.caffeinate_process.wait(timeout=2)
                self.log("Stopped caffeinate - Mac can sleep normally")
            except Exception as e:
                # Force kill if terminate doesn't work
                try:
                    self.caffeinate_process.kill()
                    self.log("Force killed caffeinate process")
                except Exception:
                    pass
            finally:
                self.caffeinate_process = None

    def start_onion_proxy(self):
        """Start the local .onion proxy server in a background thread."""
        if self.proxy_server is not None:
            return  # already running

        docker_bin = os.path.join(self.bin_dir, "docker")
        docker_env = os.environ.copy()
        docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

        # Install the PHP proxy script into the WordPress container
        php_script = os.path.join(self.script_dir, "onion-forward.php")
        if not os.path.exists(php_script):
            # Fallback: check parent resources dir
            php_script = os.path.join(self.parent_resources_dir, "onion-forward.php")
        onion_proxy.install_php_proxy(docker_bin, docker_env, php_script, log_func=self.log)

        def run_proxy():
            try:
                server = onion_proxy.ThreadingHTTPServer(
                    ("127.0.0.1", onion_proxy.PROXY_PORT),
                    onion_proxy.OnionProxyHandler
                )
                server.docker_bin = docker_bin
                server.docker_env = docker_env
                server.onion_address = self.onion_address
                server.healthcheck_address = self.healthcheck_address
                server.version = self.version
                server.data_dir = self.app_support
                server.log_func = self.log
                server.launcher_script = self.launcher_script
                self.proxy_server = server
                self.log(f"Onion proxy listening on http://127.0.0.1:{onion_proxy.PROXY_PORT}")
                server.serve_forever()
            except Exception as e:
                self.log(f"Onion proxy failed to start: {e}")
                self.proxy_server = None

        self.proxy_thread = threading.Thread(target=run_proxy, daemon=True)
        self.proxy_thread.start()

    def stop_onion_proxy(self):
        """Stop the local .onion proxy server."""
        if self.proxy_server is not None:
            try:
                self.proxy_server.shutdown()
                self.log("Onion proxy stopped")
            except Exception as e:
                self.log(f"Error stopping onion proxy: {e}")
            finally:
                self.proxy_server = None
                self.proxy_thread = None

    def check_wp_installed(self):
        """Check if WordPress core is installed via wp-cli."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-wordpress",
                 "wp", "core", "is-installed", "--allow-root"],
                env=env, capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

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

            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(run_on_main)

            # Wait for result (with timeout)
            max_wait = 300  # 5 minutes
            waited = 0
            while result_container[0] is None and waited < max_wait:
                time.sleep(0.1)
                waited += 0.1

            return result_container[0]

    def log_version_info(self):
        """Log version information for all components at startup"""
        self.log("=" * 60)
        self.log(f"OnionPress v{self.version} starting up")
        self.startup_time = time.time()
        self.log("=" * 60)

        # macOS version
        try:
            result = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            macos_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
            self.log(f"macOS version: {macos_version}")
        except Exception:
            pass

        # Colima version
        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            if os.path.exists(colima_bin):
                result = subprocess.run([colima_bin, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                colima_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Colima version: {colima_version}")
        except Exception:
            pass

        # Docker version
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            if os.path.exists(docker_bin):
                result = subprocess.run([docker_bin, "--version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                docker_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
                self.log(f"Docker version: {docker_version}")
        except Exception:
            pass

        # Docker Compose version
        try:
            compose_bin = os.path.join(self.bin_dir, "docker-compose")
            if os.path.exists(compose_bin):
                result = subprocess.run([compose_bin, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                compose_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Docker Compose version: {compose_version}")
        except Exception:
            pass

        self.log("=" * 60)

    def _web_log_reader_thread(self, process, raw_path, filtered_path):
        """Read docker logs and write to both raw and filtered log files"""
        try:
            with open(raw_path, 'a') as raw_f, open(filtered_path, 'a') as filtered_f:
                for line in process.stdout:
                    raw_f.write(line)
                    raw_f.flush()
                    if "OnionPress-HealthCheck" not in line:
                        filtered_f.write(line)
                        filtered_f.flush()
        except Exception:
            pass

    def start_web_log_capture(self):
        """Start capturing WordPress logs to a file"""
        if self.web_log_process is not None:
            return  # Already running

        try:
            web_log_file = os.path.join(self.app_support, "wordpress-access.log")
            visitors_log_file = os.path.join(self.app_support, "wordpress-visitors.log")
            docker_bin = os.path.join(self.bin_dir, "docker")

            # Start docker logs process in background, capture stdout as text
            self.web_log_process = subprocess.Popen(
                [docker_bin, "logs", "-f", "--tail", "100", "onionpress-wordpress"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                env={
                    "DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"
                }
            )

            # Start reader thread that splits logs into raw + filtered files
            self.web_log_thread = threading.Thread(
                target=self._web_log_reader_thread,
                args=(self.web_log_process, web_log_file, visitors_log_file),
                daemon=True
            )
            self.web_log_thread.start()

            print(f"Started web log capture to {web_log_file}")
        except Exception as e:
            print(f"Error starting web log capture: {e}")
            self.web_log_process = None

    def stop_web_log_capture(self):
        """Stop capturing WordPress logs"""
        if self.web_log_process is not None:
            try:
                self.web_log_process.terminate()
                self.web_log_process.wait(timeout=5)
            except Exception:
                try:
                    self.web_log_process.kill()
                except Exception:
                    pass
            self.web_log_process = None
            # Wait for reader thread to finish
            if hasattr(self, 'web_log_thread') and self.web_log_thread:
                self.web_log_thread.join(timeout=3)
                self.web_log_thread = None
            print("Stopped web log capture")

    def ensure_docker_available(self):
        """Ensure bundled Colima is running (no-op during first-time setup as launcher handles it)"""
        try:
            # During first-time setup, the launcher script handles Colima initialization
            # So we just check if it's ready, but don't try to start it ourselves
            colima_bin = os.path.join(self.bin_dir, "colima")
            if not os.path.exists(colima_bin):
                self.log("ERROR: Bundled Colima not found")
                return

            # Check if running
            result = subprocess.run([colima_bin, "status"], capture_output=True, timeout=5)

            if result.returncode == 0:
                # Verify docker accessible
                docker_check = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                if docker_check.returncode == 0:
                    self.log("Bundled Colima is running")
                    return

            # Don't try to start Colima here - the launcher script handles initialization
            # This avoids conflicts during first-time setup
            self.log("Colima not running yet (launcher may still be initializing)")

        except Exception as e:
            self.log(f"Error checking Colima: {e}")

    def check_port_conflict(self):
        """Check if required ports are already in use by another process."""
        ports = [8080, 9050, onion_proxy.PROXY_PORT]
        in_use = []
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.bind(('127.0.0.1', port))
                s.close()
            except OSError:
                in_use.append(port)
        return in_use

    def auto_start(self):
        """Automatically start the service when the app launches"""
        time.sleep(1)  # Brief delay

        # Wait for Colima to be ready (important for first-time setup)
        self.log("Waiting for container runtime to be ready...")
        docker_bin = os.path.join(self.bin_dir, "docker")
        colima_initialized = os.path.join(self.colima_home, ".initialized")

        # Wait up to 3 minutes for Colima initialization
        max_wait = 180  # 3 minutes
        waited = 0
        while waited < max_wait:
            # Check if Colima is initialized and docker is responding
            if os.path.exists(colima_initialized):
                try:
                    result = subprocess.run(
                        [docker_bin, "info"],
                        capture_output=True,
                        timeout=5,
                        env=os.environ.copy()
                    )
                    if result.returncode == 0:
                        self.log("Container runtime is ready")
                        break
                except Exception:
                    pass

            time.sleep(3)
            waited += 3

        if waited >= max_wait:
            self.log("WARNING: Container runtime not ready after 3 minutes")

        # Check for port conflicts (another user's OnionPress or other process)
        # Only flag a conflict if ports are busy AND our own containers aren't running
        in_use = self.check_port_conflict()
        if in_use:
            # Check if our containers are already running (normal restart case)
            try:
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                result = subprocess.run(
                    [docker_bin, "ps", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=5, env=env
                )
                our_containers = result.stdout.strip()
            except Exception:
                our_containers = ""

            if "onionpress-" not in our_containers:
                ports_str = ', '.join(str(p) for p in in_use)
                self.log(f"Port conflict detected: ports {ports_str} already in use by another process")
                self._port_conflict = True
                rumps.alert(
                    title="OnionPress Cannot Start",
                    message=f"Port(s) {ports_str} already in use.\n\n"
                            "Another instance of OnionPress may be running, "
                            "possibly under a different user account.\n\n"
                            "Only one OnionPress can run at a time on this Mac."
                )
                self.menu["Starting..."].title = "Status: Port conflict"
                return

        # Check if UPDATE_ON_LAUNCH is enabled
        config_file = os.path.join(self.app_support, "config")
        update_on_launch = False
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    for line in f:
                        if line.startswith('UPDATE_ON_LAUNCH='):
                            value = line.split('=', 1)[1].strip().lower()
                            update_on_launch = (value == 'yes')
                            break
            except Exception:
                pass

        if update_on_launch:
            self.log("UPDATE_ON_LAUNCH enabled - checking for Docker image updates...")
            self.update_docker_images(show_notifications=False)

        self.start_service(None)


    def add_login_item(self):
        """Add app to login items - prompts user to add manually"""
        try:
            # Open System Settings to Login Items
            # Modern macOS doesn't allow programmatic login item addition without prompts
            rumps.alert(
                title="Enable Launch on Login",
                message="Please add OnionPress to Login Items:\n\n1. System Settings will open\n2. Go to General → Login Items\n3. Click the + button\n4. Select OnionPress.app from Applications\n\nNote: You can also disable this setting in the config file.",
                ok="Open System Settings"
            )

            # Open System Settings to Login Items
            subprocess.run(["open", "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"])

            self.log("User prompted to add login item manually")
            return True
        except Exception as e:
            self.log(f"Error prompting login item addition: {e}")
            return False

    def remove_login_item(self):
        """Remove app from login items - prompts user to remove manually"""
        try:
            # Open System Settings to Login Items
            rumps.alert(
                title="Disable Launch on Login",
                message="Please remove OnionPress from Login Items:\n\n1. System Settings will open\n2. Go to General → Login Items\n3. Select OnionPress\n4. Click the - button to remove it",
                ok="Open System Settings"
            )

            # Open System Settings to Login Items
            subprocess.run(["open", "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"])

            self.log("User prompted to remove login item manually")
            return True
        except Exception as e:
            self.log(f"Error prompting login item removal: {e}")
            return False


    def run_command(self, command):
        """Run a command and return output"""
        try:
            result = subprocess.run(
                [self.launcher_script, command],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=60
            )
            return result.stdout.strip()
        except Exception as e:
            print(f"Error running command {command}: {e}")
            return None

    def check_wordpress_health(self, log_result=True):
        """Check if WordPress is actually responding to requests"""
        try:
            if log_result:
                self.log("Checking local access: http://localhost:8080")
            # Use curl instead of urllib to avoid "local network" permission prompt
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", "-H", "User-Agent: OnionPress-HealthCheck", "http://localhost:8080"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5
            )
            if result.returncode == 0:
                content = result.stdout
                # Check for database errors or WordPress not ready
                if 'Error establishing a database connection' in content:
                    if log_result:
                        self.log("✗ Local access: Database connection error")
                    return False
                if 'Database connection error' in content:
                    if log_result:
                        self.log("✗ Local access: Database connection error")
                    return False
                # If we get here and got a response, WordPress is responding
                # Either it's the install page or actual WordPress content
                if log_result:
                    self.log("✓ Local access: WordPress responding")
                return True
            else:
                if log_result:
                    self.log(f"✗ Local access: Connection failed (curl exit code {result.returncode})")
                return False
        except Exception as e:
            if log_result:
                self.log(f"✗ Local access: Connection failed ({str(e)})")
            return False

    def check_tor_reachability(self, log_result=True):
        """Check if the .onion service is properly configured and published"""
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            return False

        try:
            if log_result:
                self.log(f"Checking Tor onion service status for: {self.onion_address}")

            docker_bin = os.path.join(self.bin_dir, "docker")

            # Set up environment for docker commands
            docker_env = os.environ.copy()
            docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # Check 1: Verify hostname file exists and matches
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
                env=docker_env
            )

            if result.returncode != 0:
                if log_result:
                    self.log(f"✗ Onion service hostname file not found")
                return False

            hostname = result.stdout.strip()
            if hostname != self.onion_address:
                if log_result:
                    self.log(f"✗ Hostname mismatch: {hostname} != {self.onion_address}")
                return False

            # Check 2: Verify Tor has bootstrapped to 100%
            result = subprocess.run(
                [docker_bin, "logs", "--tail", "100", "onionpress-tor"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
                env=docker_env
            )

            if "Bootstrapped 100% (done)" not in result.stdout:
                if log_result:
                    self.log(f"✗ Tor not fully bootstrapped yet")
                return False

            # Check 3: Verify no critical errors in recent logs
            if "ERROR" in result.stdout or "failed to publish" in result.stdout.lower():
                if log_result:
                    self.log(f"✗ Tor errors detected in logs")
                return False

            # Check 4: Verify WordPress is reachable from Tor container
            # (SOCKS proxy at 127.0.0.1:9050 doesn't work through Colima VM
            # port forwarding, so we test the actual path: tor -> wordpress
            # over the Docker network using docker exec + wget)
            probe_result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "wget", "-q", "-O", "/dev/null", "--timeout=5",
                 "-U", "OnionPress-HealthCheck",
                 "http://wordpress:80/"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
                env=docker_env
            )
            if probe_result.returncode != 0:
                if log_result:
                    self.log(f"✗ WordPress not reachable from Tor container")
                return False

            # Check 5: Verify onion service is actually reachable through Tor network
            # This catches the case where Tor is bootstrapped but service descriptors
            # haven't propagated yet (common after fresh install with new key)
            probe_result = subprocess.run(
                [docker_bin, "exec", "onionpress-wordpress",
                 "curl", "-s", "--socks5-hostname", "onionpress-tor:9050",
                 "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}",
                 "-H", "User-Agent: OnionPress-HealthCheck",
                 f"http://{self.onion_address}/"],
                capture_output=True,
                text=True,
                timeout=15,
                env=docker_env
            )
            if probe_result.returncode != 0 or probe_result.stdout.strip() not in ["200", "301", "302", "303"]:
                if log_result:
                    self.log(f"✗ Onion service not yet reachable through Tor network")
                return False

            if log_result:
                self.log(f"✓ Onion service verified: {self.onion_address}")

            return True

        except Exception as e:
            if log_result:
                self.log(f"✗ Tor status check failed: {str(e)}")
            return False

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
        """Handle SIGTERM/SIGINT — clean up PID file and exit"""
        self._remove_pid_file()
        os._exit(0)

    def handle_reopen(self):
        """Handle reopen signal from launcher (user double-clicked app while running)"""
        self.log("Reopen signal received")
        if self.is_running and self.is_ready:
            self.log("Service is ready — opening browser")
            self.open_tor_browser(None)
        elif not self.is_running:
            self.log("Service not running — starting service")
            self.start_service(None)

    def check_internet_connectivity(self):
        """Check if host has internet connectivity.
        Uses curl subprocess to avoid macOS 'local network' permission prompt
        that Python's socket module triggers."""
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://1.1.1.1/"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def _parse_bootstrap_percentage(self):
        """Parse Tor bootstrap percentage from recent container logs.
        Returns highest percentage found (0-100), or 0 if not parseable."""
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", "50", "onionpress-tor"],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout + result.stderr
            best = 0
            for line in output.splitlines():
                idx = line.find("Bootstrapped ")
                if idx >= 0:
                    rest = line[idx + len("Bootstrapped "):]
                    pct_str = ""
                    for ch in rest:
                        if ch.isdigit():
                            pct_str += ch
                        else:
                            break
                    if pct_str:
                        val = int(pct_str)
                        if val > best:
                            best = val
            return best
        except Exception:
            return 0

    @property
    def display_state(self):
        """Compute the display state from current variables.
        Returns one of: 'stopped', 'available', 'offline', 'stuck', 'starting'."""
        if not self.is_running:
            return "stopped"
        if self.is_ready:
            return "available"
        if not self._has_internet:
            return "offline"
        # Check for stuck: bootstrap stalled 2min+ (24 checks at 5s) or yellow 5min+
        if self._bootstrap_stall_count >= 24:
            return "stuck"
        if self._yellow_since and (time.time() - self._yellow_since) > 300:
            return "stuck"
        return "starting"

    def check_status(self):
        """Check if containers are running and get onion address"""
        if self._port_conflict:
            return
        with self._checking_lock:
            if self.checking:
                return
            self.checking = True

        try:
            # Check for reopen signal from launcher
            reopen_file = os.path.join(self.app_support, ".reopen")
            if os.path.exists(reopen_file):
                try:
                    os.remove(reopen_file)
                except OSError:
                    pass
                self.handle_reopen()

            # Check if containers are running
            status_json = self.run_command("status")

            if status_json and status_json != "[]":
                try:
                    status = json.loads(status_json)
                    self.is_running = len(status) > 0 and all(
                        s.get("State", "").lower() == "running" for s in status
                    )
                except Exception:
                    self.is_running = False
            else:
                self.is_running = False

            # Get onion address if running
            if self.is_running:
                addr = self.run_command("address")
                if addr and addr != "Generating...":
                    self.onion_address = addr.strip()
                    # Cache address locally for instant availability on next launch
                    try:
                        with open(os.path.join(self.app_support, "onion_address"), 'w') as f:
                            f.write(self.onion_address)
                    except OSError:
                        pass
                else:
                    self.onion_address = "Generating address..."

                # Check internet connectivity
                had_internet = self._has_internet
                self._has_internet = self.check_internet_connectivity()
                if not self._has_internet and had_internet:
                    self.log("Internet connectivity lost")
                elif self._has_internet and not had_internet:
                    self.log("Internet connectivity restored")

                if not self._has_internet:
                    # No internet — skip expensive WordPress/Tor checks
                    if self.is_ready:
                        self.log("Going offline — no internet connection")
                    self.is_ready = False
                    # Track yellow/starting state
                    if self._yellow_since is None:
                        self._yellow_since = time.time()
                else:
                    # Internet available — do full health checks
                    # Determine if we should do detailed checks and logging
                    current_status = (self.is_running, self.onion_address)
                    should_log = (current_status != self.last_status_logged) or not self.is_ready

                    # Check if WordPress is ready and Tor is reachable
                    wordpress_ready = self.check_wordpress_health(log_result=should_log)
                    tor_reachable = self.check_tor_reachability(log_result=should_log)

                    previous_ready = self.is_ready
                    ready_now = wordpress_ready and tor_reachable

                    if ready_now and not previous_ready:
                        self.is_ready = True
                        self._was_ready = True
                        self._bootstrap_stall_count = 0
                        self._yellow_since = None
                        elapsed = int(time.time() - self.startup_time)
                        self.log(f"✓ System fully operational (launched in {elapsed}s)")
                        self.last_status_logged = current_status

                        # Dismiss setup dialog if it's showing
                        self.dismiss_setup_dialog()

                        # Auto-open Tor Browser on first ready (if installed)
                        if not self.auto_opened_browser:
                            self.auto_opened_browser = True
                            self.auto_open_browser()

                        # Force menu update (changes icon to purple)
                        self.update_menu()

                        # Dismiss splash AFTER icon turns purple
                        self.dismiss_launch_splash()
                    elif ready_now:
                        # Already was ready, keep it ready
                        self.is_ready = True
                        self._bootstrap_stall_count = 0
                        self._yellow_since = None
                        self.last_status_logged = current_status
                    elif previous_ready and not ready_now:
                        # Was ready, now failing — go to reconnecting state
                        self.is_ready = False
                        self._yellow_since = time.time()
                        self._bootstrap_stall_count = 0
                        self.log("Service became unreachable — reconnecting")
                    else:
                        # Not ready yet — track bootstrap progress for stuck detection
                        pct = self._parse_bootstrap_percentage()
                        if pct > self._last_bootstrap_pct:
                            self._last_bootstrap_pct = pct
                            self._bootstrap_stall_count = 0
                        else:
                            self._bootstrap_stall_count += 1
                        if self._yellow_since is None:
                            self._yellow_since = time.time()

                # Start web log capture if not already running
                if self.web_log_process is None:
                    threading.Thread(target=self.start_web_log_capture, daemon=True).start()

                # Start caffeinate if not already running (prevents sleep while service runs)
                if self.caffeinate_process is None or self.caffeinate_process.poll() is not None:
                    self.start_caffeinate()

                # Start onion proxy if not already running
                if self.proxy_server is None:
                    self.start_onion_proxy()
                elif self.proxy_server:
                    # Update onion address and readiness on existing proxy
                    self.proxy_server.onion_address = self.onion_address
                    self.proxy_server.healthcheck_address = self.healthcheck_address
                    self.proxy_server.tor_ready = self.is_ready

                # Read healthcheck address if not yet known
                if self.healthcheck_address is None and self.is_ready:
                    self.read_healthcheck_address()

                # Poll for relay messages from healthcheck service
                if self.is_ready:
                    self.poll_relay_messages()

                # Check if WordPress setup is needed (first-run guard)
                if self._wp_installed is not True and self.proxy_server:
                    wp_installed = self.check_wp_installed()
                    if wp_installed:
                        was_waiting = (self._wp_installed is False)
                        self._wp_installed = True
                        if was_waiting:
                            # Setup just completed — start Tor
                            self.log("Setup complete — starting Tor")
                            threading.Thread(
                                target=lambda: subprocess.run([self.launcher_script, "start-tor"]),
                                daemon=True
                            ).start()
                    elif not self._setup_page_opened:
                        # WordPress container is running but not installed — open setup page
                        self._wp_installed = False
                        self._setup_page_opened = True
                        self.log("WordPress not installed — opening setup page")
                        # Dismiss dialogs before opening browser
                        self.dismiss_setup_dialog()
                        self.dismiss_launch_splash()
                        subprocess.run(["open", f"http://localhost:{onion_proxy.PROXY_PORT}/setup"])
            else:
                # Log when stopping
                if self.is_running or self.is_ready:
                    self.log("Service stopped")
                    self.last_status_logged = None

                    # Only dismiss setup dialog when actually stopping (not during startup)
                    self.dismiss_setup_dialog()

                # Keep cached address visible even when stopped — it's still valid
                if not self.onion_address or self.onion_address in ["Starting...", "Generating address..."]:
                    self.onion_address = "Not running"
                self.is_ready = False
                self.auto_opened_browser = False  # Reset for next start
                self._wp_installed = None  # Reset for next start
                self._setup_page_opened = False
                self._was_ready = False
                self._last_bootstrap_pct = 0
                self._bootstrap_stall_count = 0
                self._yellow_since = None
                self.healthcheck_address = None
                self.relay_messages = []
                self._relay_alert_shown = False

                # Stop web log capture if running
                if self.web_log_process is not None:
                    self.stop_web_log_capture()

                # Stop caffeinate to allow Mac to sleep
                self.stop_caffeinate()

            # Update menu
            self.update_menu()

        except Exception as e:
            print(f"Error checking status: {e}")
        finally:
            self.checking = False

    def update_menu(self):
        """Update menu items based on current state - thread-safe"""
        # Dispatch UI updates to main thread to avoid AppKit threading violations
        def do_update():
            state = self.display_state

            # Relay alert indicator: show "!" next to icon when messages exist
            if self.relay_messages:
                self.title = "!"
                count = len(self.relay_messages)
                self.relay_alert_item.title = f"Relay Alerts ({count})"
                self.relay_alert_item.set_callback(self.view_relay_alerts)
            else:
                self.title = ""
                self.relay_alert_item.title = "Relay Alerts"
                self.relay_alert_item.set_callback(None)

            if state == "available":
                self.icon = self.icon_running
                self.menu["Starting..."].title = f"Address: {self.onion_address}"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.update_browser_menu_title()
            elif state == "starting":
                self.icon = self.icon_starting
                pct = self._last_bootstrap_pct
                if pct > 0:
                    self.menu["Starting..."].title = f"Status: Connecting to Tor ({pct}%)..."
                else:
                    self.menu["Starting..."].title = "Status: Starting up, please wait..."
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
            elif state == "offline":
                self.icon = self.icon_stopped
                self.menu["Starting..."].title = "Status: Offline — no internet connection"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
            elif state == "stuck":
                self.icon = self.icon_stopped
                self.menu["Starting..."].title = "Status: Stuck — try Restart"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
            else:
                # Stopped
                self.icon = self.icon_stopped
                if self.onion_address and self.onion_address.endswith('.onion'):
                    self.menu["Starting..."].title = f"Stopped — {self.onion_address}"
                else:
                    self.menu["Starting..."].title = "Status: Stopped"
                self.menu["Start"].set_callback(self.start_service)
                self.menu["Stop"].set_callback(None)
                self.menu["Restart"].set_callback(None)

        # Execute on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(do_update)

    def read_healthcheck_address(self):
        """Read the healthcheck .onion address from the tor container."""
        try:
            # First try the cached file written by the launcher
            hc_file = os.path.join(self.app_support, "healthcheck-address")
            if os.path.exists(hc_file):
                with open(hc_file) as f:
                    addr = f.read().strip()
                if addr and addr.endswith('.onion'):
                    self.healthcheck_address = addr
                    self.log(f"Healthcheck address: {addr}")
                    return

            # Fall back to reading from container
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
                capture_output=True, text=True, timeout=10, env=env
            )
            if result.returncode == 0:
                addr = result.stdout.strip()
                if addr and addr.endswith('.onion'):
                    self.healthcheck_address = addr
                    # Cache for next time
                    try:
                        with open(hc_file, 'w') as f:
                            f.write(addr)
                    except OSError:
                        pass
                    self.log(f"Healthcheck address: {addr}")
        except Exception as e:
            self.log(f"Failed to read healthcheck address: {e}")

    def poll_relay_messages(self):
        """Poll for messages from the OnionRelay via the healthcheck service."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # List message files in the container
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "ls", "/var/lib/tor/healthcheck-messages/"],
                capture_output=True, text=True, timeout=10, env=env
            )
            if result.returncode != 0 or not result.stdout.strip():
                # No messages — clear any existing alerts
                if self.relay_messages:
                    self.relay_messages = []
                    self._relay_alert_shown = False
                return

            files = result.stdout.strip().split('\n')
            json_files = [f for f in files if f.endswith('.json')]
            if not json_files:
                if self.relay_messages:
                    self.relay_messages = []
                    self._relay_alert_shown = False
                return

            # Read all message files
            messages = []
            for fname in json_files:
                try:
                    r = subprocess.run(
                        [docker_bin, "exec", "onionpress-tor",
                         "cat", f"/var/lib/tor/healthcheck-messages/{fname}"],
                        capture_output=True, text=True, timeout=5, env=env
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        msg = json.loads(r.stdout.strip())
                        messages.append(msg)
                except Exception:
                    continue

            if messages and messages != self.relay_messages:
                self.relay_messages = messages
                if not self._relay_alert_shown:
                    self._relay_alert_shown = True
                    self.log(f"Received {len(messages)} message(s) from OnionRelay")
                    # Show notification for the latest message
                    latest = messages[-1]
                    msg_type = latest.get("type", "unknown")
                    msg_text = latest.get("message", "New message from OnionRelay")
                    rumps.notification(
                        title="OnionPress Alert",
                        subtitle=msg_type.replace("_", " ").title(),
                        message=msg_text
                    )
        except Exception as e:
            # Don't spam logs — relay polling failures are expected when container is starting
            pass

    def view_relay_alerts(self, _):
        """Show relay alert messages and offer to dismiss them."""
        if not self.relay_messages:
            rumps.alert("No relay alerts.")
            return

        # Build summary of all messages
        lines = []
        for msg in self.relay_messages:
            msg_type = msg.get("type", "unknown").replace("_", " ").title()
            msg_text = msg.get("message", "")
            lines.append(f"[{msg_type}] {msg_text}")
        summary = "\n".join(lines)

        response = rumps.alert(
            title=f"Relay Alerts ({len(self.relay_messages)})",
            message=summary,
            ok="Dismiss All",
            cancel="Close"
        )

        if response == 1:  # "Dismiss All" clicked
            self.log("Dismissing relay alerts")
            self.relay_messages = []
            self._relay_alert_shown = False
            # Delete message files from container
            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
                subprocess.run(
                    [docker_bin, "exec", "onionpress-tor",
                     "sh", "-c", "rm -f /var/lib/tor/healthcheck-messages/*.json"],
                    capture_output=True, timeout=10, env=env
                )
            except Exception:
                pass
            self.update_menu()

    def start_status_checker(self):
        """Start background thread to check status periodically"""
        def checker():
            while True:
                if self._port_conflict:
                    time.sleep(30)
                    continue
                self.check_status()
                # Adaptive polling based on display state
                state = self.display_state
                if state == "available":
                    time.sleep(30)  # Check every 30 seconds when operational
                elif state == "offline":
                    time.sleep(10)  # Check every 10 seconds when offline (detect recovery)
                else:
                    time.sleep(5)   # Check every 5 seconds during startup/stuck

        thread = threading.Thread(target=checker, daemon=True)
        thread.start()

    @rumps.clicked("Copy Onion Address")
    def copy_address(self, _):
        """Copy onion address to clipboard"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            subprocess.run(
                ["pbcopy"],
                input=self.onion_address.encode(),
                check=True
            )
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def monitor_tor_browser_install(self):
        """Monitor for Tor Browser installation and offer to open site when detected"""
        if self.monitoring_tor_install:
            return  # Already monitoring

        self.monitoring_tor_install = True
        self.log("Starting Tor Browser installation monitor")

        def check_for_tor():
            tor_browser_path = "/Applications/Tor Browser.app"
            timeout = 600  # 10 minutes
            check_interval = 3  # Check every 3 seconds
            elapsed = 0

            while elapsed < timeout and self.monitoring_tor_install:
                time.sleep(check_interval)
                elapsed += check_interval

                # Verify the app is in /Applications and is a proper app bundle
                if os.path.exists(tor_browser_path) and os.path.isdir(tor_browser_path):
                    # Check it's actually in /Applications (not on a volume)
                    real_path = os.path.realpath(tor_browser_path)
                    if not real_path.startswith("/Applications/"):
                        continue  # It's a symlink or on a volume, keep waiting

                    # Verify it's a proper app bundle with executable
                    executable_path = os.path.join(tor_browser_path, "Contents", "MacOS", "firefox")
                    if not os.path.exists(executable_path):
                        continue  # Not fully installed yet

                    self.log("Tor Browser detected in Applications!")
                    self.monitoring_tor_install = False

                    # Dismiss setup dialog before showing browser ready dialog
                    self.dismiss_setup_dialog()

                    # Show dialog asking if they want to open the site
                    address = self.onion_address
                    try:
                        button_index = self.show_native_alert(
                            title="OnionPress",
                            message=f"Tor Browser is now installed!\n\nWould you like to open your site?\n\n{address}",
                            buttons=["Open Site", "Later"],
                            default_button=0,
                            style="informational"
                        )

                        if button_index == 0:  # Open Site
                            url = f"http://{address}"
                            # Use full path to ensure we open the one in Applications
                            subprocess.run(["open", "-a", tor_browser_path, url])
                            self.log(f"Opened site in Tor Browser: {url}")
                    except Exception as e:
                        self.log(f"Error showing Tor Browser ready dialog: {e}")
                    return

            # Timeout reached
            self.monitoring_tor_install = False
            self.log("Tor Browser installation monitor timed out")

        threading.Thread(target=check_for_tor, daemon=True).start()

    def monitor_brave_install(self):
        """Monitor for Brave Browser installation and offer to open site when detected"""
        if self.monitoring_tor_install:  # Reuse the same flag since we only monitor one at a time
            return  # Already monitoring

        self.monitoring_tor_install = True
        self.log("Starting Brave Browser installation monitor")

        def check_for_brave():
            brave_browser_path = "/Applications/Brave Browser.app"
            timeout = 600  # 10 minutes
            check_interval = 3  # Check every 3 seconds
            elapsed = 0

            while elapsed < timeout and self.monitoring_tor_install:
                time.sleep(check_interval)
                elapsed += check_interval

                # Verify the app is in /Applications and is a proper app bundle
                if os.path.exists(brave_browser_path) and os.path.isdir(brave_browser_path):
                    # Check it's actually in /Applications (not on a volume)
                    real_path = os.path.realpath(brave_browser_path)
                    if not real_path.startswith("/Applications/"):
                        continue  # It's a symlink or on a volume, keep waiting

                    # Verify it's a proper app bundle with executable
                    executable_path = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                    if not os.path.exists(executable_path):
                        continue  # Not fully installed yet

                    self.log("Brave Browser detected in Applications!")
                    self.monitoring_tor_install = False

                    # Dismiss setup dialog before showing browser ready dialog
                    self.dismiss_setup_dialog()

                    # Show dialog asking if they want to open the site
                    address = self.onion_address
                    try:
                        button_index = self.show_native_alert(
                            title="OnionPress",
                            message=f"Brave Browser is now installed!\n\nWould you like to open your site?\n\n{address}",
                            buttons=["Open Site", "Later"],
                            default_button=0,
                            style="informational"
                        )

                        if button_index == 0:  # Open Site
                            url = f"http://{address}"
                            # Launch Brave in Tor mode using executable with --tor flag
                            brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                            subprocess.run([brave_executable, "--tor", url])
                            self.log(f"Opened site in Brave Browser (Tor mode): {url}")
                    except Exception as e:
                        self.log(f"Error showing Brave Browser ready dialog: {e}")
                    return

            # Timeout reached
            self.monitoring_tor_install = False
            self.log("Brave Browser installation monitor timed out")

        threading.Thread(target=check_for_brave, daemon=True).start()

    def extension_connected_recently(self):
        """Check if a browser extension was active within the last 24 hours.

        Returns the browser app name (e.g. "Firefox") if connected recently,
        or None if not.
        """
        marker = os.path.join(self.app_support, "extension-connected")
        try:
            if os.path.exists(marker):
                with open(marker, 'r') as f:
                    data = json.loads(f.read().strip())
                if (time.time() - data["timestamp"]) < 86400:
                    return data.get("browser")
        except Exception:
            pass
        return None

    def update_browser_menu_title(self):
        """Update the browser menu item title based on which browser is available"""
        tor_browser_path = "/Applications/Tor Browser.app"
        brave_browser_path = "/Applications/Brave Browser.app"

        ext_browser = self.extension_connected_recently()
        if ext_browser:
            self.browser_menu_item.title = f"Open in {ext_browser}"
        elif os.path.exists(brave_browser_path):
            self.browser_menu_item.title = "Open in Brave Browser"
        elif os.path.exists(tor_browser_path):
            self.browser_menu_item.title = "Open in Tor Browser"
        else:
            self.browser_menu_item.title = "Open in Browser"

    def open_tor_browser(self, _):
        """Open the onion address in the best available browser"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"
            brave_browser_path = "/Applications/Brave Browser.app"
            url = f"http://{self.onion_address}"

            ext_browser = self.extension_connected_recently()
            if ext_browser:
                subprocess.run(["open", "-a", ext_browser, url])
                self.log(f"Opened {url} in {ext_browser} (extension)")
            elif os.path.exists(brave_browser_path):
                brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                subprocess.run([brave_executable, "--tor", url])
                self.log(f"Opened {url} in Brave Browser (Tor mode)")
            elif os.path.exists(tor_browser_path):
                subprocess.run(["open", "-a", "Tor Browser", url])
                self.log(f"Opened {url} in Tor Browser")
            else:
                self.show_browser_install_dialog()
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def show_browser_install_dialog(self):
        """Show dialog with browser options based on what's installed."""
        # Detect which extension-compatible browsers are installed
        extension_browsers = {
            "Firefox": "/Applications/Firefox.app",
            "Google Chrome": "/Applications/Google Chrome.app",
            "Brave Browser": "/Applications/Brave Browser.app",
            "Microsoft Edge": "/Applications/Microsoft Edge.app",
        }
        installed = [name for name, path in extension_browsers.items()
                     if os.path.exists(path)]

        address = self.onion_address or ""
        try:
            if installed:
                # Has a compatible browser — suggest installing the extension
                browsers_str = ", ".join(installed)
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"Your site is ready!\n\n{address}\n\nInstall the OnionPress extension for {browsers_str} to browse .onion sites.\n\nOr download Tor Browser for a dedicated solution.",
                    buttons=["Install Extension", "Download Tor Browser", "Later"],
                    default_button=0,
                    cancel_button=2,
                    style="informational"
                )
                if button_index == 0:
                    subprocess.run(["open", "https://github.com/brewsterkahle/onionpress/releases/latest"])
                elif button_index == 1:
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    self.monitor_tor_browser_install()
            else:
                # Safari-only user — don't mention extension
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"Your site is ready!\n\n{address}\n\nTo visit .onion sites, download Tor Browser or Brave Browser (both are free).",
                    buttons=["Download Tor Browser", "Download Brave Browser", "Later"],
                    default_button=0,
                    cancel_button=2,
                    style="informational"
                )
                if button_index == 0:
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    self.monitor_tor_browser_install()
                elif button_index == 1:
                    subprocess.run(["open", "https://brave.com/download/"])
                    self.monitor_brave_install()
        except Exception as e:
            self.log(f"Browser dialog failed: {e}")

    def auto_open_browser(self):
        """Automatically open a browser when service becomes ready"""
        # Wait for Tor circuits to stabilize before opening browser
        self.log("Waiting 10s for Tor circuits to stabilize before opening browser...")
        time.sleep(10)
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"
            brave_browser_path = "/Applications/Brave Browser.app"
            url = f"http://{self.onion_address}"

            # Wait up to 5 more seconds for a browser extension to register
            ext_browser = None
            for i in range(5):
                ext_browser = self.extension_connected_recently()
                if ext_browser:
                    break
                self.log(f"Waiting for extension registration... ({i+1}/5)")
                time.sleep(1)
            if ext_browser:
                # Open in the specific browser that has the extension
                self.log(f"Auto-opening {ext_browser} (extension detected): {url}")
                subprocess.run(["open", "-a", ext_browser, url])
            elif os.path.exists(brave_browser_path):
                self.log(f"Auto-opening Brave Browser (Tor mode): {url}")
                brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                subprocess.run([brave_executable, "--tor", url])
            elif os.path.exists(tor_browser_path):
                self.log(f"Auto-opening Tor Browser: {url}")
                subprocess.run(["open", "-a", "Tor Browser", url])
            else:
                self.log("No Tor-capable browser found - showing options dialog")
                self.dismiss_setup_dialog()
                self.dismiss_launch_splash()
                self.show_browser_install_dialog()

    @rumps.clicked("Start")
    def start_service(self, _):
        """Start the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Starting..."

        def start():
            # Check if this is first run (no docker images yet)
            first_run = False
            try:
                result = subprocess.run(
                    ["docker", "images", "--format", "{{.Repository}}"],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=5
                )
                images = result.stdout.strip().split('\n')
                # First run if we don't have wordpress/mysql/tor images
                if not any('wordpress' in img for img in images):
                    first_run = True
            except Exception:
                pass

            # First run: show welcome screen and wait for user to click Continue
            if first_run:
                self.log("First run detected - showing welcome screen")
                self.setup_dialog_showing = True

                def on_continue():
                    self.log("User confirmed setup - starting installation")
                    threading.Thread(target=self._run_first_time_setup, daemon=True).start()

                def on_cancel():
                    self.log("User cancelled setup")
                    self.menu["Starting..."].title = "Status: Stopped"
                    setup_window.close_setup_progress()
                    self.setup_dialog_showing = False

                setup_window.show_welcome_screen(on_continue=on_continue, on_cancel=on_cancel)
                return

            # Not first run: start the service normally
            subprocess.run([self.launcher_script, "start"])

            # Poll until WordPress is responding (replaces fixed sleep)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                if self.check_wordpress_health(log_result=False):
                    self.log(f"WordPress responding after {waited}s")
                    break
                time.sleep(2)
                waited += 2

            self.check_status()

            # Start caffeinate to prevent sleep while service runs
            self.start_caffeinate()

        threading.Thread(target=start, daemon=True).start()

    def _run_first_time_setup(self):
        """Run first-time setup with progress window: launcher, pull images, then wait for ready."""
        progress_window = setup_window.get_setup_window()
        # Window already transitioned to progress view by Continue button

        try:
            # Step 0: System check
            progress_window.set_step(0, "in_progress")
            progress_window.set_status("Checking system requirements")
            progress_window.add_log("CHECKING MACOS VERSION...", "progress")
            time.sleep(0.3)
            try:
                ver = subprocess.run(
                    ["sw_vers", "-productVersion"],
                    capture_output=True, text=True, timeout=5
                )
                progress_window.add_log(f"MACOS {ver.stdout.strip()} DETECTED", "ok")
            except Exception:
                progress_window.add_log("MACOS DETECTED", "ok")
            progress_window.add_log("PYTHON 3 FOUND", "ok")
            time.sleep(0.3)
            progress_window.complete_step(0)

            # Step 1: Runtime init
            progress_window.set_step(1, "in_progress")
            progress_window.set_status("Initializing container runtime")
            progress_window.add_log("STARTING COLIMA VM...", "progress")
            subprocess.run([self.launcher_script, "start"])
            time.sleep(1)
            progress_window.add_log("VM INITIALIZED", "ok")
            progress_window.complete_step(1)

            # Step 2: Download images
            progress_window.set_step(2, "in_progress")
            progress_window.set_status("Downloading container images")
            progress_window.add_log("FETCHING CONTAINER IMAGES...", "progress")
        except Exception as e:
            self.log(f"Error in _run_first_time_setup: {e}")
            progress_window.add_log(f"ERROR: {e}", "error")

        docker_dir = os.path.join(self.parent_resources_dir, "docker")
        try:
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            secrets_file = os.path.join(self.app_support, "secrets")
            if os.path.exists(secrets_file):
                with open(secrets_file, 'r') as sf:
                    for line in sf:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, val = line.split('=', 1)
                            env[key] = val.strip("'")
            docker_bin = os.path.join(self.bin_dir, "docker")
            docker_log = os.path.join(self.app_support, "docker-pull.log")

            def pull_and_start():
                with open(docker_log, 'w') as log_file:
                    result = subprocess.run(
                        [docker_bin, "compose", "up", "-d", "wordpress", "db"],
                        cwd=docker_dir,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        timeout=600,
                        env=env
                    )
                self.log(f"Docker compose up completed with exit code: {result.returncode}")
                if result.returncode == 0:
                    progress_window.set_status("Generating vanity onion address")
                    progress_window.set_detail("Finding address...")
                    self.show_notification("Containers started", "WordPress is starting...")

            threading.Thread(target=pull_and_start, daemon=True).start()
            self.monitor_image_downloads(progress_window)
        except Exception as e:
            self.log(f"Error starting containers: {e}")
            progress_window.set_status(f"Error: {e}")

        # Poll until WordPress is responding
        max_wait = 60
        waited = 0
        while waited < max_wait:
            if self.check_wordpress_health(log_result=False):
                self.log(f"WordPress responding after {waited}s")
                break
            time.sleep(2)
            waited += 2

        self.check_status()
        self.start_caffeinate()

        # Complete setup window when service is ready
        def wait_for_ready():
            for _ in range(120):
                time.sleep(1)
                if self.is_ready and self.onion_address and self.onion_address not in ("Starting...", "Not running", "Generating address..."):
                    progress_window.set_step(3, "in_progress")
                    progress_window.add_log(f"ADDRESS: {self.onion_address[:25]}...", "ok")
                    progress_window.complete_step(3)
                    progress_window.set_step(4, "in_progress")
                    progress_window.set_status("Starting services")
                    progress_window.add_log("ALL CONTAINERS RUNNING", "ok")
                    progress_window.complete_step(4)
                    progress_window.set_step(5, "in_progress")
                    progress_window.set_status("Finalizing setup")
                    progress_window.add_log("VERIFYING TOR CIRCUIT...", "progress")
                    progress_window.add_log("ONION SERVICE PUBLISHED", "ok")
                    progress_window.complete_step(5)
                    progress_window.set_modem_active(False)
                    progress_window.show_completion(self.onion_address)
                    time.sleep(4)
                    setup_window.close_setup_progress()
                    self.setup_dialog_showing = False
                    break

        threading.Thread(target=wait_for_ready, daemon=True).start()

    @rumps.clicked("Stop")
    def stop_service(self, _):
        """Stop the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Stopping..."

        def stop():
            subprocess.run([self.launcher_script, "stop"])
            time.sleep(1)
            self.check_status()

            # Stop background processes
            self.stop_web_log_capture()
            self.stop_caffeinate()
            self.stop_onion_proxy()

        threading.Thread(target=stop, daemon=True).start()

    @rumps.clicked("Restart")
    def restart_service(self, _):
        """Restart the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Restarting..."
        self.icon = self.icon_starting  # Change icon to indicate restarting

        def restart():
            # Mark as not ready during restart
            self.is_ready = False
            self.is_running = False
            self._was_ready = False
            self._last_bootstrap_pct = 0
            self._bootstrap_stall_count = 0
            self._yellow_since = None

            # Run restart command
            subprocess.run([self.launcher_script, "restart"])

            # Poll until WordPress is responding (replaces fixed sleep)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                if self.check_wordpress_health(log_result=False):
                    self.log(f"WordPress responding after restart ({waited}s)")
                    break
                time.sleep(2)
                waited += 2

            # Check status after restart
            self.check_status()

        threading.Thread(target=restart, daemon=True).start()

    @rumps.clicked("View Logs")
    def view_logs(self, _):
        """Open logs in Console.app"""
        log_file = os.path.join(self.app_support, "onionpress.log")
        if os.path.exists(log_file):
            subprocess.run(["open", "-a", "Console", log_file])
        else:
            rumps.alert("No logs available yet")

    @rumps.clicked("View Web Usage Log")
    def view_web_log(self, _):
        """Open WordPress access log in Console.app"""
        if not self.is_running:
            rumps.alert("Service not running. Please start the service first.")
            return

        web_log_file = os.path.join(self.app_support, "wordpress-visitors.log")

        # Ensure the log file exists
        if not os.path.exists(web_log_file):
            # Create it and wait a moment for logs to populate
            open(web_log_file, 'a').close()
            time.sleep(1)

        # Open in Console.app (filtered log excludes health check pings)
        subprocess.run(["open", "-a", "Console", web_log_file])

    def get_version(self):
        """Get version from Info.plist"""
        try:
            with open(self.info_plist, 'rb') as f:
                plist = plistlib.load(f)
                return plist.get('CFBundleShortVersionString', 'Unknown')
        except Exception:
            return 'Unknown'

    def read_config_value(self, key, default=""):
        """Read a value from the config file"""
        config_file = os.path.join(self.app_support, "config")
        if not os.path.exists(config_file):
            return default
        try:
            with open(config_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{key}="):
                        return line.split('=', 1)[1]
        except Exception:
            pass
        return default

    def write_config_value(self, key, value):
        """Write a value to the config file"""
        config_file = os.path.join(self.app_support, "config")

        # Create default config if it doesn't exist
        if not os.path.exists(config_file):
            config_template = os.path.join(self.resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                subprocess.run(["cp", config_template, config_file])

        # Read all lines
        lines = []
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                lines = f.readlines()

        # Update or add the key
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                found = True
                break

        if not found:
            lines.append(f"{key}={value}\n")

        # Write back
        with open(config_file, 'w') as f:
            f.writelines(lines)

    @rumps.clicked("Settings...")
    def open_settings(self, _):
        """Open config file in default text editor"""
        config_file = os.path.join(self.app_support, "config")

        # Create default config if it doesn't exist
        if not os.path.exists(config_file):
            config_template = os.path.join(self.parent_resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                subprocess.run(["cp", config_template, config_file])

        if os.path.exists(config_file):
            # Show helpful dialog first
            rumps.alert(
                title="Opening Settings",
                message="Edit the config file that opens.\n\nWhen you're done:\n1. Save the file (⌘S)\n2. Close the editor\n3. Restart OnionPress from the menu bar\n\nYour changes will then take effect.",
                ok="Open Config File"
            )
            # Open in TextEdit and bring it to the front
            subprocess.run(["open", "-a", "TextEdit", config_file])
            self.log("Opened config file in text editor")
        else:
            rumps.alert("Settings file not found")

    def show_export_dialog(self, base64_key, mnemonic):
        """Show export dialog with base64 key and recovery words option."""
        # NSAlert dialogs lack an Edit menu, so Cmd+C doesn't work.
        # Install a local event monitor to intercept Cmd+C and copy the key.
        current_text = [base64_key]  # mutable so the handler can see updates
        def handle_copy(event):
            if (event.modifierFlags() & AppKit.NSEventModifierFlagCommand and
                    event.charactersIgnoringModifiers() == 'c'):
                subprocess.run(["pbcopy"], input=current_text[0].encode(), check=True)
                return None  # consume the event (no beep)
            return event
        monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, handle_copy)

        while True:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Private Key Backup")
            alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            # Set app icon
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Build accessory view with key text
            container = AppKit.NSView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, 420, 120))

            # Base64 key text field (selectable, monospaced, read-only)
            text_field = AppKit.NSTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(10, 30, 400, 72))
            text_field.setStringValue_(base64_key)
            text_field.setEditable_(False)
            text_field.setSelectable_(True)
            text_field.setBordered_(True)
            text_field.setBezeled_(True)
            text_field.setFont_(
                AppKit.NSFont.monospacedSystemFontOfSize_weight_(11, AppKit.NSFontWeightRegular))
            text_field.setLineBreakMode_(AppKit.NSLineBreakByCharWrapping)
            text_field.setUsesSingleLineMode_(False)
            container.addSubview_(text_field)

            # Security warning
            warning = AppKit.NSTextField.labelWithString_(
                "DO NOT share this key with anyone you don't trust.")
            warning.setFrame_(AppKit.NSMakeRect(10, 5, 400, 18))
            warning.setAlignment_(AppKit.NSTextAlignmentCenter)
            warning.setTextColor_(AppKit.NSColor.systemRedColor())
            warning.setFont_(AppKit.NSFont.boldSystemFontOfSize_(12))
            container.addSubview_(warning)

            alert.setAccessoryView_(container)

            # Buttons: Done (default), Copy Key, Show Recovery Words
            alert.addButtonWithTitle_("Done").setKeyEquivalent_("\r")
            alert.addButtonWithTitle_("Copy Key")
            alert.addButtonWithTitle_("Show Recovery Words")

            response = alert.runModal()
            button_index = response - 1000

            if button_index == 0:  # Done
                break
            elif button_index == 1:  # Copy Key
                subprocess.run(["pbcopy"], input=base64_key.encode(), check=True)
                continue  # re-show dialog
            elif button_index == 2:  # Show Recovery Words
                current_text[0] = mnemonic  # Cmd+C copies words while this dialog is up
                # Format mnemonic for display
                words = mnemonic.split()
                word_count = len([w for w in words if w != '|'])
                formatted_lines = []
                for i in range(0, len(words), 6):
                    formatted_lines.append(' '.join(words[i:i+6]))
                formatted_mnemonic = '\n'.join(formatted_lines)

                bip_alert = AppKit.NSAlert.alloc().init()
                bip_alert.setMessageText_("Recovery Words (BIP39)")
                bip_alert.setInformativeText_(
                    f"These {word_count} words are an alternative backup of your key.\n\n"
                    f"{formatted_mnemonic}\n\n"
                    "Store them in a safe place.")
                bip_alert.addButtonWithTitle_("Done").setKeyEquivalent_("\r")
                bip_alert.addButtonWithTitle_("Back")
                bip_alert.addButtonWithTitle_("Copy Words")
                if os.path.exists(icon_path):
                    bip_icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                    if bip_icon:
                        bip_alert.setIcon_(bip_icon)
                bip_response = bip_alert.runModal() - 1000
                if bip_response == 2:  # Copy Words
                    subprocess.run(["pbcopy"], input=mnemonic.encode(), check=True)
                    continue  # re-show recovery words
                elif bip_response == 1:  # Back
                    current_text[0] = base64_key  # restore Cmd+C to copy key
                    continue  # re-show main dialog
                else:  # Done
                    break

        AppKit.NSEvent.removeMonitor_(monitor)

    @rumps.clicked("Export Private Key...")
    def export_key(self, _):
        """Export Tor private key as base64 with BIP39 recovery words"""
        if not self.is_running:
            rumps.alert(
                title="Service Not Running",
                message="Please start the service first before exporting the private key."
            )
            return

        # Show security warning dialog first
        button_index = self.show_native_alert(
            title="Export Private Key",
            message="SECURITY WARNING\n\nYou are about to export your private key. This key controls your onion address and website identity.\n\nANYONE with this key can:\n\u2022 Impersonate your onion address\n\u2022 Take over your site identity\n\u2022 Restore your address on another computer\n\nOnly export if you understand the security implications.\n\nContinue with export?",
            buttons=["Cancel", "I Understand - Continue"],
            default_button=0,
            cancel_button=0,
            style="warning"
        )

        if button_index != 1:
            return

        # Second confirmation
        button_index = self.show_native_alert(
            title="Final Confirmation",
            message="This is your final confirmation.\n\nAre you absolutely sure you want to export your private key?",
            buttons=["Cancel", "Yes, Export Key"],
            default_button=0,
            cancel_button=0,
            style="warning"
        )

        if button_index != 1:
            return

        try:
            self.log("Export: extracting private key...")
            # Extract key once, generate both formats
            key_bytes = key_manager.extract_private_key()
            base64_key = key_manager.bytes_to_base64_key(key_bytes)
            mnemonic = key_manager.bytes_to_mnemonic(key_bytes)
            self.log("Export: key extracted, showing export dialog")

            # Show the export dialog
            self.show_export_dialog(base64_key, mnemonic)
            self.log("Export: dialog closed")

        except Exception as e:
            self.log(f"Export failed: {e}")
            rumps.alert(
                title="Export Failed",
                message=f"Could not export private key:\n\n{str(e)}"
            )

    @rumps.clicked("Import Private Key...")
    def import_key(self, _):
        """Import Tor private key from base64 key or BIP39 mnemonic words"""
        # Warning dialog
        response = rumps.alert(
            title="Import Private Key",
            message="WARNING: Importing a private key will replace your current onion address!\n\nYour WordPress site will be accessible at a different .onion address after import.\n\nMake sure you have backed up your current key first.\n\nContinue?",
            ok="Continue",
            cancel="Cancel"
        )

        if response != 1:  # Cancel clicked
            return

        # Get key from user (accepts either format)
        window = rumps.Window(
            title="Import Private Key",
            message="Paste your key below.\n\nAccepted formats:\n\u2022 Base64 key (onionpress:2:XXXX-XXXX-...)\n\u2022 BIP39 recovery words (48 words separated by |)",
            default_text="",
            ok="Import",
            cancel="Cancel",
            dimensions=(400, 100)
        )

        response = window.run()

        if not response.clicked:  # Cancel
            return

        key_text = response.text.strip()

        if not key_text:
            rumps.alert("No key provided")
            return

        # Normalize whitespace: for base64 keys, collapse all whitespace
        # (text field may wrap and insert newlines); for BIP39, normalize to single spaces
        if key_text.startswith('onionpress:'):
            key_text = ''.join(key_text.split())
        else:
            key_text = ' '.join(key_text.split())

        # Validate the key format before starting the long operation
        self.log("Import: validating key...")
        try:
            key_bytes, coordinator = key_manager.import_key(key_text)
        except Exception as e:
            self.log(f"Import validation failed: {e}")
            rumps.alert(
                title="Import Failed",
                message=f"Could not import private key:\n\n{str(e)}"
            )
            return

        if coordinator:
            self.log(f"Key imported with coordinator: {coordinator}")

        # Show progress notification and do the work in a background thread
        rumps.notification(
            title="Importing Key...",
            subtitle="This takes about a minute.",
            message="Writing key and restarting service."
        )

        def do_import():
            try:
                # Write the new key while container is still running
                # (docker cp requires the container to exist)
                self.log("Import: writing new key...")
                key_manager.write_private_key(key_bytes)

                # Restart the service to pick up the new key
                self.log("Import: restarting service...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True)
                time.sleep(2)
                subprocess.run([self.launcher_script, "start"], capture_output=True)

                self.log("Import: complete")
                rumps.notification(
                    title="Import Successful",
                    subtitle="",
                    message="Your private key has been imported. The service is restarting with your new onion address."
                )

                # Trigger status check
                time.sleep(2)
                self.check_status()

            except Exception as e:
                self.log(f"Import failed: {e}")
                rumps.notification(
                    title="Import Failed",
                    subtitle="",
                    message=f"Could not import private key: {str(e)}\n\nYour original key has not been changed."
                )

        threading.Thread(target=do_import, daemon=True).start()

    def update_docker_images(self, show_notifications=True):
        """Update Docker images (WordPress, MariaDB, Tor)"""
        try:
            self.log("Checking for Docker image updates...")

            docker_bin = os.path.join(self.bin_dir, "docker")
            docker_compose_file = os.path.join(self.parent_resources_dir, "docker", "docker-compose.yml")

            # Set up environment
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # Pull latest images
            self.log("Pulling latest Docker images...")
            result = subprocess.run(
                [docker_bin, "compose", "-f", docker_compose_file, "pull"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300,  # 5 minute timeout
                env=env
            )

            if result.returncode == 0:
                self.log("Docker images updated successfully")
                if "Downloaded" in result.stdout or "Pulled" in result.stdout:
                    return True
                else:
                    return False
            else:
                self.log(f"Failed to update Docker images: {result.stderr}")
                return False

        except Exception as e:
            self.log(f"Error updating Docker images: {e}")
            return False

    @rumps.clicked("Check for Updates...")
    def check_for_updates(self, _):
        """Check GitHub for newer versions and update Docker images"""
        # Check for app updates
        app_update_available = False
        try:
            # Fetch latest release from GitHub using curl to avoid permission prompts
            url = "https://api.github.com/repos/brewsterkahle/onionpress/releases/latest"
            result = subprocess.run(
                ["curl", "-s", "-H", "User-Agent: onionpress", "--max-time", "10", url],
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=15
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                latest_version = data.get('tag_name', '').lstrip('v')
                current_version = self.version

                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    app_update_available = True
                    response = rumps.alert(
                        title="App Update Available",
                        message=f"A new version of onionpress is available!\n\nCurrent: v{current_version}\nLatest: v{latest_version}\n\nWould you like to download it?",
                        ok="Download Update",
                        cancel="Later"
                    )
                    if response == 1:  # OK clicked
                        release_url = data.get('html_url', 'https://github.com/brewsterkahle/onionpress/releases/latest')
                        subprocess.run(["open", release_url])
        except Exception as e:
            self.log(f"Update check failed: {e}")
            import traceback
            self.log(traceback.format_exc())
            rumps.alert(
                title="Update Check Failed",
                message=f"Could not check for app updates.\n\nPlease visit:\nhttps://github.com/brewsterkahle/onionpress/releases"
            )

        # Check for Docker image updates
        threading.Thread(target=self._check_docker_updates_async, args=(app_update_available,), daemon=True).start()

    def _check_docker_updates_async(self, app_update_available):
        """Check for Docker updates in background thread"""
        images_updated = self.update_docker_images(show_notifications=True)

        # Show final summary if no app update was available
        if not app_update_available and not images_updated:
            rumps.alert(
                title="No Updates Available",
                message=f"You're running the latest version (v{self.version})\nAll container images are up to date."
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
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(create_and_show)
            self.log("Setup dialog shown (native NSAlert)")
        except Exception as e:
            self.log(f"Error showing setup dialog: {e}")
            self.setup_dialog_showing = False
            self.log("Setup dialog fallback - dialog failed to show")

    def dismiss_setup_dialog(self):
        """Dismiss the setup dialog if it's showing (native alert or setup progress window)"""
        if self.setup_dialog_showing:
            self.setup_dialog_showing = False
            self.log("Setup dialog marked for dismissal")
            try:
                setup_window.hide_setup_progress()
            except Exception:
                pass
            try:
                if self.setup_alert:
                    AppKit.NSApp.abortModal()
                    self.log("Setup dialog dismissed programmatically")
            except Exception as e:
                self.log(f"Error dismissing setup dialog: {e}")

    def monitor_image_downloads(self, progress_window=None):
        """Monitor Docker image downloads and show progress notifications.
        If progress_window is provided (first-time setup), update its log and progress bar.
        """
        images_to_check = {
            'wordpress': False,
            'mariadb': False,
            'tor': False
        }

        self.log("Monitoring image downloads...")

        # Check for images every 3 seconds for up to 10 minutes
        for i in range(200):
            try:
                if progress_window:
                    estimated_progress = min(0.9, i / 60)
                    progress_window.set_progress(estimated_progress, "DOWNLOADING")

                result = subprocess.run(
                    ["docker", "images", "--format", "{{.Repository}}"],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=5
                )
                current_images = result.stdout.strip().split('\n')

                for image_name in images_to_check:
                    if not images_to_check[image_name]:
                        if any(image_name in img for img in current_images):
                            images_to_check[image_name] = True
                            self.log(f"Image downloaded: {image_name}")
                            if progress_window:
                                if image_name == 'wordpress':
                                    progress_window.add_log("WORDPRESS IMAGE READY", "ok")
                                    progress_window.set_progress(0.4, "WORDPRESS OK")
                                elif image_name == 'mariadb':
                                    progress_window.add_log("MARIADB IMAGE READY", "ok")
                                    progress_window.set_progress(0.7, "MARIADB OK")
                                elif image_name == 'tor':
                                    progress_window.add_log("TOR IMAGE READY", "ok")
                                    progress_window.set_progress(0.95, "TOR OK")

                # When using progress window, step 2 completes when wordpress + mariadb are ready (first compose only pulls those)
                all_needed = images_to_check['wordpress'] and images_to_check['mariadb']
                if progress_window and all_needed:
                    self.log("Required images ready (wordpress + mariadb)")
                    progress_window.set_progress(1.0, "COMPLETE")
                    progress_window.add_log("ALL IMAGES DOWNLOADED", "ok")
                    progress_window.complete_step(2)
                    progress_window.set_status("Generating vanity onion address")
                    progress_window.add_log("GENERATING VANITY PREFIX...", "progress")
                    break
                if all(images_to_check.values()):
                    self.log("All images downloaded")
                    break

            except Exception as e:
                self.log(f"Error checking images: {e}")

            time.sleep(3)

    @rumps.clicked("About OnionPress")
    def show_about(self, _):
        """Show about dialog"""
        about_text = f"""OnionPress v{self.version}

Run your own website from your Mac. Just Works. Free, forever.
WordPress + Tor Onion Service

Features:
• Tor Onion Service with vanity addresses (op2*)
• Requires visitors to use Tor or Brave browsers
• Internet Archive Wayback Machine integration
• Bundled container runtime (no Docker needed)
• Privacy-first design
• Free and open source

Created by Brewster Kahle
License: AGPL v3"""

        github_url = "https://github.com/brewsterkahle/onionpress"
        link_label = "GitHub: github.com/brewsterkahle/onionpress"

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

            # Create clickable GitHub link as accessory view
            link_field = AppKit.NSTextField.labelWithString_("")
            link_field.setSelectable_(True)
            link_field.setAllowsEditingTextAttributes_(True)
            link_field.setBordered_(False)
            link_field.setDrawsBackground_(False)

            # Build attributed string with clickable link
            attr_str = AppKit.NSMutableAttributedString.alloc().initWithString_(link_label)
            url = AppKit.NSURL.URLWithString_(github_url)
            link_range = AppKit.NSMakeRange(len("GitHub: "), len(link_label) - len("GitHub: "))
            attr_str.addAttribute_value_range_(AppKit.NSLinkAttributeName, url, link_range)
            font = AppKit.NSFont.systemFontOfSize_(AppKit.NSFont.smallSystemFontSize())
            full_range = AppKit.NSMakeRange(0, len(link_label))
            attr_str.addAttribute_value_range_(AppKit.NSFontAttributeName, font, full_range)

            link_field.setAttributedStringValue_(attr_str)
            link_field.sizeToFit()
            alert.setAccessoryView_(link_field)

            alert.runModal()

        if AppKit.NSThread.isMainThread():
            show_dialog()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(show_dialog)

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Uninstall OnionPress with mandatory key backup prompt"""
        # Step 1: Show critical warning about key loss (native NSAlert - no permissions)
        button_index = self.show_native_alert(
            title="Uninstall Warning",
            message="⚠️ CRITICAL WARNING ⚠️\n\nUninstalling will PERMANENTLY DELETE:\n• Your onion address and private key\n• All WordPress content and data\n• Database and configuration\n\nYOUR ONION ADDRESS CANNOT BE RECOVERED unless you have a backup of your private key.\n\nDo you want to backup your private key before uninstalling?",
            buttons=["Cancel", "No, Delete Everything", "Yes, Backup First"],
            default_button=2,
            cancel_button=0,
            style="critical"
        )

        if button_index == 0:  # Cancel
            return

        if button_index == 2:  # Yes, Backup First
            # User wants to backup key first - call export function
                self.log("User chose to backup key before uninstall")
                if self.is_running:
                    self.export_key(None)
                else:
                    rumps.alert(
                        title="Service Not Running",
                        message="Cannot export key while service is stopped.\n\nPlease start the service first, then try uninstall again to backup your key."
                    )
                    return

                # After export, ask again if they want to continue with uninstall
                button_index = self.show_native_alert(
                    title="Confirm Uninstall",
                    message="Key backup complete.\n\nProceed with uninstall?\n\nThis will permanently delete all data.",
                    buttons=["Cancel", "Proceed with Uninstall"],
                    default_button=0,
                    cancel_button=0,
                    style="warning"
                )

                if button_index != 1:  # User didn't click "Proceed"
                    return

        # Step 2: Final confirmation with explicit acknowledgment
        # Use rumps.Window for text input (no osascript, no permissions needed)
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

        # Check if user clicked OK and typed "DELETE" (case insensitive)
        if response.clicked != 1:  # User clicked Cancel
            self.log("Uninstall cancelled - user clicked Cancel")
            return

        user_input = response.text.strip().upper() if response.text else ""
        if user_input != "DELETE":
            self.log(f"Uninstall cancelled - user input was: '{response.text.strip()}' (expected 'DELETE')")
            rumps.alert(
                title="Uninstall Cancelled",
                message=f"Uninstall cancelled. Type 'DELETE' to confirm.\n\n(You typed: '{response.text.strip()}')"
            )
            return

        # User confirmed uninstall - run in background thread to avoid beach ball
        def do_uninstall():
            try:
                # First, stop any ongoing setup processes
                self.log("Uninstall: Stopping any ongoing processes...")
                # Stop any ongoing browser monitoring
                self.monitoring_tor_install = False
                self.dismiss_setup_dialog()

                # Stop the service (this will cancel any startup in progress)
                self.log("Uninstall: Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                self.stop_web_log_capture()
                self.stop_onion_proxy()
                self.stop_caffeinate()

                # Delete Colima VM (cleaner than pkill, properly removes VM)
                # Only affects OnionPress instance, not system Colima
                self.log("Uninstall: Deleting Colima VM...")
                colima_bin = os.path.join(self.bin_dir, "colima")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "delete", "-f"], capture_output=True, timeout=60, env=env)
                # Note: Docker volumes lived inside the Colima VM and are deleted with it

                # Step 3: Remove data directory (but keep it until after we show dialog)
                self.log("Uninstall: Preparing to remove data directory...")
                import shutil
                data_dir_exists = os.path.exists(self.app_support)

                # Step 4: Remove data directory
                if data_dir_exists:
                    shutil.rmtree(self.app_support)
                    self.log("Uninstall: Data directory removed successfully")

                # Step 5: Show final dialog and quit
                # Use show_native_alert which already handles main thread
                self.show_native_alert(
                    title="Uninstall Complete",
                    message="OnionPress has been uninstalled.\n\nFinal step: Move OnionPress.app to the Trash.\n\nClick OK to quit.",
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

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        self.log("="*60)
        self.log("QUIT BUTTON CLICKED - v2.2.83 RUNNING")
        self.log("="*60)

        # Stop monitoring immediately
        self.monitoring_tor_install = False
        self.dismiss_setup_dialog()
        self.stop_web_log_capture()

        # Update menu to show we're quitting (must be on main thread)
        def update_and_cleanup():
            self.menu["Starting..."].title = "Stopping services..."
            self.icon = self.icon_starting

            # Small delay to ensure UI updates
            time.sleep(0.5)

            # Now run cleanup
            try:
                self.log("Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                self.log("Services stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: Stop command timed out")
            except Exception as e:
                self.log(f"Warning: Stop failed: {e}")

            # Stop caffeinate to allow Mac to sleep
            self.stop_caffeinate()

            # Stop onion proxy
            self.stop_onion_proxy()

            try:
                colima_bin = os.path.join(self.bin_dir, "colima")
                self.log("Stopping Colima VM...")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "stop"], capture_output=True, timeout=60, env=env)
                self.log("Colima stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: Colima stop timed out")
            except Exception as e:
                self.log(f"Warning: Colima stop failed: {e}")

            # Remove PID file
            self._remove_pid_file()

            # Now quit
            rumps.quit_application()

        # Start cleanup thread
        threading.Thread(target=update_and_cleanup, daemon=True).start()

if __name__ == "__main__":
    OnionPressApp().run()
