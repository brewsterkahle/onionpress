#!/usr/bin/env python3
"""
onion.press Menu Bar Application
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

# Add scripts directory to path for imports
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

import key_manager

class OnionPressApp(rumps.App):
    def __init__(self):
        # Get paths first
        self.app_support = os.path.expanduser("~/.onion.press")
        self.script_dir = os.path.dirname(os.path.realpath(__file__))

        # When running as py2app bundle, __file__ is in Contents/Resources/
        # so we need to use that as resources_dir, not the parent
        if getattr(sys, 'frozen', False):
            # Running as py2app bundle
            # __file__ is like: .../MenubarApp/Contents/Resources/menubar.py (in zip)
            # MenubarApp is nested inside Onion.Press.app
            # Structure: Onion.Press.app/Contents/Resources/MenubarApp/Contents/Resources/menubar.py
            menubar_resources_dir = os.path.join(os.environ.get('RESOURCEPATH', ''))
            if not menubar_resources_dir:
                # Fallback: get from bundle structure
                bundle_contents = os.path.dirname(os.path.dirname(self.script_dir))
                menubar_resources_dir = os.path.join(bundle_contents, 'Resources')

            # Keep menubar resources for icons
            self.resources_dir = menubar_resources_dir

            # Navigate to parent Onion.Press.app bundle for launcher script and bin dir
            # MenubarApp/Contents/Resources -> MenubarApp/Contents -> MenubarApp -> Onion.Press.app/Resources -> Onion.Press.app/Contents
            menubar_contents = os.path.dirname(menubar_resources_dir)  # MenubarApp/Contents
            menubar_app = os.path.dirname(menubar_contents)  # MenubarApp
            parent_resources = os.path.dirname(menubar_app)  # Onion.Press.app/Contents/Resources
            self.contents_dir = os.path.dirname(parent_resources)  # Onion.Press.app/Contents
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onion.press")
            self.bin_dir = os.path.join(parent_resources, "bin")
        else:
            # Running as regular Python script
            self.resources_dir = os.path.dirname(self.script_dir)
            self.contents_dir = os.path.dirname(self.resources_dir)
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onion.press")
            self.bin_dir = os.path.join(self.resources_dir, "bin")
        self.colima_home = os.path.join(self.app_support, "colima")
        self.info_plist = os.path.join(self.contents_dir, "Info.plist")
        self.log_file = os.path.join(self.app_support, "onion.press.log")

        # Rotate log file on startup to avoid confusion with old sessions
        if os.path.exists(self.log_file):
            # Keep only the last session as backup
            backup_log = os.path.join(self.app_support, "onion.press.log.prev")
            try:
                import shutil
                shutil.move(self.log_file, backup_log)
            except OSError:
                pass  # If rotation fails, just append

        # Icon paths
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Debug logging
        with open(self.log_file, 'a') as f:
            f.write(f"DEBUG: frozen={getattr(sys, 'frozen', False)}\n")
            f.write(f"DEBUG: resources_dir={self.resources_dir}\n")
            f.write(f"DEBUG: bin_dir={self.bin_dir}\n")
            f.write(f"DEBUG: launcher_script={self.launcher_script}\n")
            f.write(f"DEBUG: icon_stopped exists={os.path.exists(self.icon_stopped)}\n")
            f.write(f"DEBUG: icon_stopped path={self.icon_stopped}\n")
            f.write(f"DEBUG: About to initialize rumps with icon\n")

        # Initialize with icon instead of text (empty string, not None)
        try:
            super(OnionPressApp, self).__init__("", icon=self.icon_stopped, quit_button=None)
            with open(self.log_file, 'a') as f:
                f.write(f"DEBUG: rumps initialized successfully\n")
        except Exception as e:
            with open(self.log_file, 'a') as f:
                f.write(f"ERROR: rumps init failed: {e}\n")
            raise

        # Get version from Info.plist
        self.version = self.get_version()

        # Create Docker config without credential store (avoids docker-credential-osxkeychain errors)
        docker_config_dir = os.path.join(self.app_support, "docker-config")
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
            except:
                pass
        elif os.path.islink(system_compose) and not os.path.exists(compose_plugin_dest):
            try:
                os.symlink(system_compose, compose_plugin_dest)
            except:
                pass

        # Set up bundled binaries environment
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        os.environ["DOCKER_CONFIG"] = docker_config_dir

        # Log version information at startup
        self.log_version_info()

        # State
        self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
        self.checking = False
        self.web_log_process = None  # Background process for web logs
        self.web_log_file_handle = None  # File handle for web log capture
        self.last_status_logged = None  # Track last logged status to avoid spam
        self.auto_opened_browser = False  # Track if we've auto-opened browser this session
        self.setup_dialog_showing = False  # Track if setup dialog is currently showing
        self.monitoring_tor_install = False  # Track if we're monitoring for Tor Browser installation

        # Menu items
        self.menu = [
            rumps.MenuItem("Starting...", callback=None),
            rumps.separator,
            rumps.MenuItem("Copy Onion Address", callback=self.copy_address),
            rumps.MenuItem("Open in Tor Browser", callback=self.open_tor_browser),
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
            rumps.MenuItem("About Onion.Press", callback=self.show_about),
            rumps.MenuItem("Uninstall...", callback=self.uninstall),
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Ensure Docker is available
        threading.Thread(target=self.ensure_docker_available, daemon=True).start()

        # Sync launch on login setting
        threading.Thread(target=self.sync_launch_on_login, daemon=True).start()

        # Start status checker
        self.start_status_checker()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

    def log(self, message):
        """Write log message to onion.press.log file"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"[{timestamp}] {message}\n"
            fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as f:
                f.write(log_message)
        except Exception as e:
            print(f"Error writing to log: {e}")

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
        self.log(f"Onion.Press v{self.version} starting up")
        self.log("=" * 60)

        # macOS version
        try:
            result = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5)
            macos_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
            self.log(f"macOS version: {macos_version}")
        except:
            pass

        # Colima version
        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            if os.path.exists(colima_bin):
                result = subprocess.run([colima_bin, "version"], capture_output=True, text=True, timeout=5)
                colima_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Colima version: {colima_version}")
        except:
            pass

        # Docker version
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            if os.path.exists(docker_bin):
                result = subprocess.run([docker_bin, "--version"], capture_output=True, text=True, timeout=5)
                docker_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
                self.log(f"Docker version: {docker_version}")
        except:
            pass

        # Docker Compose version
        try:
            compose_bin = os.path.join(self.bin_dir, "docker-compose")
            if os.path.exists(compose_bin):
                result = subprocess.run([compose_bin, "version"], capture_output=True, text=True, timeout=5)
                compose_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Docker Compose version: {compose_version}")
        except:
            pass

        self.log("=" * 60)

    def request_applescript_permission(self):
        """No longer needed - we use native APIs for all operations.

        - Dialogs: Native NSAlert and rumps.Window (no osascript)
        - Login items: Native LaunchServices API (no osascript)
        - All functionality works without System Events permission
        """
        # Skip permission check - no longer needed
        pass

    def start_web_log_capture(self):
        """Start capturing WordPress logs to a file"""
        if self.web_log_process is not None:
            return  # Already running

        try:
            web_log_file = os.path.join(self.app_support, "wordpress-access.log")
            docker_bin = os.path.join(self.bin_dir, "docker")

            # Open log file for writing (kept as instance var so we can close it later)
            self.web_log_file_handle = open(web_log_file, 'a')

            # Start docker logs process in background
            self.web_log_process = subprocess.Popen(
                [docker_bin, "logs", "-f", "--tail", "100", "onionpress-wordpress"],
                stdout=self.web_log_file_handle,
                stderr=subprocess.STDOUT,
                env={
                    "DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"
                }
            )
            print(f"Started web log capture to {web_log_file}")
        except Exception as e:
            print(f"Error starting web log capture: {e}")
            if hasattr(self, 'web_log_file_handle') and self.web_log_file_handle:
                self.web_log_file_handle.close()
                self.web_log_file_handle = None
            self.web_log_process = None

    def stop_web_log_capture(self):
        """Stop capturing WordPress logs"""
        if self.web_log_process is not None:
            try:
                self.web_log_process.terminate()
                self.web_log_process.wait(timeout=5)
            except:
                try:
                    self.web_log_process.kill()
                except:
                    pass
            self.web_log_process = None
            # Close the log file handle to avoid leaking it
            if hasattr(self, 'web_log_file_handle') and self.web_log_file_handle:
                try:
                    self.web_log_file_handle.close()
                except:
                    pass
                self.web_log_file_handle = None
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
                except:
                    pass

            time.sleep(3)
            waited += 3

        if waited >= max_wait:
            self.log("WARNING: Container runtime not ready after 3 minutes")

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
            except:
                pass

        if update_on_launch:
            self.log("UPDATE_ON_LAUNCH enabled - checking for Docker image updates...")
            self.update_docker_images(show_notifications=False)

        self.start_service(None)

    def is_login_item(self):
        """Check if app is currently in login items (no permissions needed)"""
        try:
            # Use native LaunchServices API - no osascript, no permissions
            from LaunchServices import LSSharedFileListCreate, LSSharedFileListCopySnapshot, \
                kLSSharedFileListSessionLoginItems, LSSharedFileListItemCopyResolvedURL

            login_items_ref = LSSharedFileListCreate(None, kLSSharedFileListSessionLoginItems, None)
            if login_items_ref:
                snapshot = LSSharedFileListCopySnapshot(login_items_ref, None)
                if snapshot:
                    for item in snapshot[0]:
                        item_url = LSSharedFileListItemCopyResolvedURL(item, 0, None)
                        if item_url and item_url[0]:
                            path = item_url[0].path()
                            if 'Onion.Press' in path or 'onion.press' in path:
                                return True
            return False
        except Exception as e:
            self.log(f"Error checking login items: {e}")
            return False

    def add_login_item(self):
        """Add app to login items (no permissions needed)"""
        try:
            # Use native LaunchServices API - no osascript, no permissions
            from LaunchServices import LSSharedFileListCreate, LSSharedFileListInsertItemURL, \
                kLSSharedFileListSessionLoginItems, kLSSharedFileListItemBeforeFirst
            from CoreFoundation import CFURLCreateWithFileSystemPath, kCFURLPOSIXPathStyle

            app_path = os.path.dirname(os.path.dirname(os.path.dirname(self.script_dir)))

            login_items_ref = LSSharedFileListCreate(None, kLSSharedFileListSessionLoginItems, None)
            if login_items_ref:
                url = CFURLCreateWithFileSystemPath(None, app_path, kCFURLPOSIXPathStyle, True)
                if url:
                    LSSharedFileListInsertItemURL(
                        login_items_ref,
                        kLSSharedFileListItemBeforeFirst,
                        None, None, url, None, None
                    )
                    self.log("Added to login items")
                    return True
            return False
        except Exception as e:
            self.log(f"Error adding login item: {e}")
            return False

    def remove_login_item(self):
        """Remove app from login items (no permissions needed)"""
        try:
            # Use native LaunchServices API - no osascript, no permissions
            from LaunchServices import LSSharedFileListCreate, LSSharedFileListCopySnapshot, \
                kLSSharedFileListSessionLoginItems, LSSharedFileListItemCopyResolvedURL, \
                LSSharedFileListItemRemove

            login_items_ref = LSSharedFileListCreate(None, kLSSharedFileListSessionLoginItems, None)
            if login_items_ref:
                snapshot = LSSharedFileListCopySnapshot(login_items_ref, None)
                if snapshot:
                    for item in snapshot[0]:
                        item_url = LSSharedFileListItemCopyResolvedURL(item, 0, None)
                        if item_url and item_url[0]:
                            path = item_url[0].path()
                            if 'Onion.Press' in path or 'onion.press' in path:
                                LSSharedFileListItemRemove(login_items_ref, item)
                                self.log("Removed from login items")
                                return True
            return False
        except Exception as e:
            self.log(f"Error removing login item: {e}")
            return False

    def sync_launch_on_login(self):
        """Sync LAUNCH_ON_LOGIN config with macOS login items"""
        time.sleep(2)  # Brief delay to let app initialize

        try:
            # Read config
            config_file = os.path.join(self.app_support, "config")
            launch_on_login_enabled = False

            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r') as f:
                        for line in f:
                            if line.startswith('LAUNCH_ON_LOGIN='):
                                value = line.split('=', 1)[1].strip().lower()
                                launch_on_login_enabled = (value == 'yes')
                                break
                except:
                    pass

            # Check current system state
            is_currently_login_item = self.is_login_item()

            # Sync if needed
            if launch_on_login_enabled and not is_currently_login_item:
                self.log("LAUNCH_ON_LOGIN=yes but not in login items - adding...")
                self.add_login_item()
            elif not launch_on_login_enabled and is_currently_login_item:
                self.log("LAUNCH_ON_LOGIN=no but in login items - removing...")
                self.remove_login_item()
            else:
                self.log(f"Launch on login state synced (enabled={launch_on_login_enabled})")

        except Exception as e:
            self.log(f"Error syncing launch on login: {e}")

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
                ["curl", "-s", "--max-time", "3", "http://localhost:8080"],
                capture_output=True,
                text=True,
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
                timeout=5,
                env=docker_env
            )

            if result.returncode != 0:
                if log_result:
                    self.log(f"✗ Hidden service hostname file not found")
                return False

            hostname = result.stdout.strip()
            if hostname != self.onion_address:
                if log_result:
                    self.log(f"✗ Hostname mismatch: {hostname} != {self.onion_address}")
                return False

            # Check 2: Verify Tor has bootstrapped to 100%
            result = subprocess.run(
                [docker_bin, "logs", "--tail", "50", "onionpress-tor"],
                capture_output=True,
                text=True,
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

            # All checks passed - onion service should be reachable
            if log_result:
                self.log(f"✓ Onion service published and ready: {self.onion_address}")
            return True

        except Exception as e:
            if log_result:
                self.log(f"✗ Tor status check failed: {str(e)}")
            return False

    def check_status(self):
        """Check if containers are running and get onion address"""
        if self.checking:
            return

        self.checking = True
        try:
            # Check if containers are running
            status_json = self.run_command("status")

            if status_json and status_json != "[]":
                try:
                    status = json.loads(status_json)
                    self.is_running = len(status) > 0 and all(
                        s.get("State", "").lower() == "running" for s in status
                    )
                except:
                    self.is_running = False
            else:
                self.is_running = False

            # Get onion address if running
            if self.is_running:
                addr = self.run_command("address")
                if addr and addr != "Generating...":
                    self.onion_address = addr.strip()
                else:
                    self.onion_address = "Generating address..."

                # Determine if we should do detailed checks and logging
                # Only do detailed checks if we're not ready yet or status changed
                current_status = (self.is_running, self.onion_address)
                should_log = (current_status != self.last_status_logged) or not self.is_ready

                # Check if WordPress is ready and Tor is reachable
                wordpress_ready = self.check_wordpress_health(log_result=should_log)

                # Check Tor reachability immediately - if it works in Tor Browser, show as ready
                tor_reachable = self.check_tor_reachability(log_result=should_log)

                previous_ready = self.is_ready
                ready_now = wordpress_ready and tor_reachable

                # If just became ready, wait 10 seconds before showing as ready
                # This gives the Tor network time to fully propagate the onion service
                if ready_now and not previous_ready:
                    self.log("✓ System checks passed - waiting 10 seconds for Tor network propagation...")
                    self.last_status_logged = current_status

                    def delayed_ready():
                        time.sleep(10)
                        self.is_ready = True
                        self.log("✓ System fully operational - reducing check frequency")

                        # Dismiss setup dialog if it's showing
                        self.dismiss_setup_dialog()

                        # Auto-open Tor Browser on first ready (if installed)
                        if not self.auto_opened_browser:
                            self.auto_opened_browser = True
                            self.auto_open_browser()

                        # Force menu update
                        self.update_menu()

                    threading.Thread(target=delayed_ready, daemon=True).start()
                elif ready_now:
                    # Already was ready, keep it ready
                    self.is_ready = True

                # Start web log capture if not already running
                if self.web_log_process is None:
                    threading.Thread(target=self.start_web_log_capture, daemon=True).start()
            else:
                # Log when stopping
                if self.is_running or self.is_ready:
                    self.log("Service stopped")
                    self.last_status_logged = None

                    # Only dismiss setup dialog when actually stopping (not during startup)
                    self.dismiss_setup_dialog()

                self.onion_address = "Not running"
                self.is_ready = False
                self.auto_opened_browser = False  # Reset for next start

                # Stop web log capture if running
                if self.web_log_process is not None:
                    self.stop_web_log_capture()

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
            if self.is_running and self.is_ready:
                # Fully operational
                self.icon = self.icon_running
                self.menu["Starting..."].title = f"Address: {self.onion_address}"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
            elif self.is_running and not self.is_ready:
                # Containers running but WordPress not ready yet
                self.icon = self.icon_starting
                self.menu["Starting..."].title = "Status: Starting up, please wait..."
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
            else:
                # Stopped
                self.icon = self.icon_stopped
                self.menu["Starting..."].title = "Status: Stopped"
                self.menu["Start"].set_callback(self.start_service)
                self.menu["Stop"].set_callback(None)
                self.menu["Restart"].set_callback(None)

        # Execute on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(do_update)

    def start_status_checker(self):
        """Start background thread to check status periodically"""
        def checker():
            while True:
                self.check_status()
                # Check frequently when starting up, slowly when ready
                if self.is_ready:
                    time.sleep(30)  # Check every 30 seconds when operational
                else:
                    time.sleep(5)   # Check every 5 seconds during startup

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
            rumps.notification(
                title="Onion.Press",
                subtitle="Address Copied",
                message=f"Copied {self.onion_address} to clipboard"
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
                            title="Onion.Press",
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
                            title="Onion.Press",
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

    @rumps.clicked("Open in Tor Browser")
    def open_tor_browser(self, _):
        """Open the onion address in Tor Browser or Brave Browser"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"
            brave_browser_path = "/Applications/Brave Browser.app"
            url = f"http://{self.onion_address}"

            if os.path.exists(tor_browser_path):
                # Prefer Tor Browser if available
                subprocess.run(["open", "-a", "Tor Browser", url])
                self.log(f"Opened {url} in Tor Browser")
            elif os.path.exists(brave_browser_path):
                # Fallback to Brave Browser with Tor support
                # Launch Brave with --tor flag to open in Private Window with Tor
                brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                subprocess.run([brave_executable, "--tor", url])
                self.log(f"Opened {url} in Brave Browser (Tor mode)")
                # Show notification to let user know Brave is being used
                rumps.notification(
                    title="Onion.Press",
                    subtitle="Opening in Brave Browser",
                    message="Opening in Private Window with Tor"
                )
            else:
                # Neither browser is installed - offer download options
                response = rumps.alert(
                    title="Tor-Compatible Browser Not Found",
                    message="Neither Tor Browser nor Brave Browser is installed.\n\nWould you like to download one?",
                    ok="Download Tor Browser",
                    cancel="Cancel",
                    other="Download Brave Browser"
                )
                if response == 1:  # Tor Browser
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    # Start monitoring for Tor Browser installation
                    self.monitor_tor_browser_install()
                elif response == 0:  # Brave Browser (other button)
                    subprocess.run(["open", "https://brave.com/download/"])
                    # Start monitoring for Brave Browser installation
                    self.monitor_brave_install()
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def auto_open_browser(self):
        """Automatically open Tor Browser or Brave Browser when service becomes ready"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"
            brave_browser_path = "/Applications/Brave Browser.app"
            url = f"http://{self.onion_address}"

            if os.path.exists(tor_browser_path):
                # Prefer Tor Browser if available
                self.log(f"Auto-opening Tor Browser: {url}")
                subprocess.run(["open", "-a", "Tor Browser", url])
            elif os.path.exists(brave_browser_path):
                # Fallback to Brave Browser with Tor support
                self.log(f"Auto-opening Brave Browser (Tor mode): {url}")
                brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                subprocess.run([brave_executable, "--tor", url])
                # Show notification to let user know Brave is being used
                rumps.notification(
                    title="Onion.Press",
                    subtitle="Opening in Brave Browser",
                    message="Opening in Private Window with Tor"
                )
            else:
                self.log("Neither Tor Browser nor Brave Browser installed - showing download dialog")
                # Dismiss setup dialog before showing browser download dialog
                self.dismiss_setup_dialog()
                address = self.onion_address
                try:
                    button_index = self.show_native_alert(
                        title="Onion.Press",
                        message=f"Your site is ready!\n\n{address}\n\nTo visit your site, you need Tor Browser or Brave Browser.\n\nWould you like to download one now?",
                        buttons=["Download Tor Browser", "Download Brave Browser", "Later"],
                        default_button=0,
                        cancel_button=2,
                        style="informational"
                    )
                    if button_index == 0:  # Download Tor Browser
                        subprocess.run(["open", "https://www.torproject.org/download/"])
                        # Start monitoring for Tor Browser installation
                        self.monitor_tor_browser_install()
                    elif button_index == 1:  # Download Brave Browser
                        subprocess.run(["open", "https://brave.com/download/"])
                        # Start monitoring for Brave Browser installation
                        self.monitor_brave_install()
                except Exception as e:
                    self.log(f"Download dialog failed: {e}")

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
                    timeout=5
                )
                images = result.stdout.strip().split('\n')
                # First run if we don't have wordpress/mysql/tor images
                if not any('wordpress' in img for img in images):
                    first_run = True
                    self.log("First run detected - opening Console to show progress")
                    # Open Console.app with the log file so user can see what's happening
                    try:
                        subprocess.run(["open", "-a", "Console", self.log_file], capture_output=True)
                        self.log("Console.app opened to show setup progress")
                    except Exception as e:
                        self.log(f"Failed to open Console.app: {e}")
                    # Show persistent setup dialog
                    self.show_setup_dialog()
            except:
                pass

            # Start the service
            subprocess.run([self.launcher_script, "start"])

            # If first run, start containers directly (which will pull images automatically)
            if first_run:
                self.log("Starting containers (will download images automatically)...")
                docker_dir = os.path.join(self.resources_dir, "docker")
                try:
                    # Run docker compose up which will automatically pull missing images
                    env = os.environ.copy()
                    env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                    # Load database passwords from secrets file into env
                    secrets_file = os.path.join(self.app_support, "secrets")
                    if os.path.exists(secrets_file):
                        with open(secrets_file, 'r') as sf:
                            for line in sf:
                                line = line.strip()
                                if line and not line.startswith('#') and '=' in line:
                                    key, val = line.split('=', 1)
                                    # Strip surrounding single quotes
                                    env[key] = val.strip("'")
                    # Use the bundled docker binary
                    docker_bin = os.path.join(self.bin_dir, "docker")
                    # Run in separate thread so it doesn't block
                    def pull_and_start():
                        # Don't capture output - let it stream to log file
                        docker_log = os.path.join(self.app_support, "docker-pull.log")
                        with open(docker_log, 'w') as log_file:
                            result = subprocess.run(
                                [docker_bin, "compose", "up", "-d"],
                                cwd=docker_dir,
                                stdout=log_file,
                                stderr=subprocess.STDOUT,
                                timeout=600,  # 10 minute timeout for image downloads
                                env=env
                            )
                        self.log(f"Docker compose up completed with exit code: {result.returncode}")
                        if result.returncode == 0:
                            self.show_notification("Containers started!", "WordPress is starting...")

                    threading.Thread(target=pull_and_start, daemon=True).start()
                    # Monitor for completion
                    self.monitor_image_downloads()
                except Exception as e:
                    self.log(f"Error starting containers: {e}")

            time.sleep(2)
            self.check_status()

        threading.Thread(target=start, daemon=True).start()

    @rumps.clicked("Stop")
    def stop_service(self, _):
        """Stop the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Stopping..."

        def stop():
            subprocess.run([self.launcher_script, "stop"])
            time.sleep(1)
            self.check_status()

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

            # Run restart command
            subprocess.run([self.launcher_script, "restart"])
            time.sleep(3)

            # Check status after restart
            self.check_status()

        threading.Thread(target=restart, daemon=True).start()

    @rumps.clicked("View Logs")
    def view_logs(self, _):
        """Open logs in Console.app"""
        log_file = os.path.join(self.app_support, "onion.press.log")
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

        web_log_file = os.path.join(self.app_support, "wordpress-access.log")

        # Ensure the log file exists
        if not os.path.exists(web_log_file):
            # Create it and wait a moment for logs to populate
            open(web_log_file, 'a').close()
            time.sleep(1)

        # Open in Console.app
        subprocess.run(["open", "-a", "Console", web_log_file])

    def get_version(self):
        """Get version from Info.plist"""
        try:
            with open(self.info_plist, 'rb') as f:
                plist = plistlib.load(f)
                return plist.get('CFBundleShortVersionString', 'Unknown')
        except:
            return 'Unknown'

    @rumps.clicked("Settings...")
    def open_settings(self, _):
        """Open config file in default text editor"""
        config_file = os.path.join(self.app_support, "config")

        # Create default config if it doesn't exist
        if not os.path.exists(config_file):
            config_template = os.path.join(self.resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                subprocess.run(["cp", config_template, config_file])

        if os.path.exists(config_file):
            subprocess.run(["open", "-t", config_file])
        else:
            rumps.alert("Settings file not found")

    @rumps.clicked("Export Private Key...")
    def export_key(self, _):
        """Export Tor private key as BIP39 mnemonic words with authentication"""
        if not self.is_running:
            rumps.alert(
                title="Service Not Running",
                message="Please start the service first before exporting the private key."
            )
            return

        # Show security warning dialog first (native NSAlert - no permissions needed)
        button_index = self.show_native_alert(
            title="Export Private Key",
            message="⚠️ SECURITY WARNING ⚠️\n\nYou are about to export your private key. This key controls your onion address and website identity.\n\nANYONE with these backup words can:\n• Impersonate your onion address\n• Take over your site identity\n• Restore your address on another computer\n\nOnly export if you understand the security implications.\n\nContinue with export?",
            buttons=["Cancel", "I Understand - Continue"],
            default_button=0,
            cancel_button=0,
            style="warning"
        )

        if button_index != 1:  # User didn't click "I Understand - Continue"
            return

        # Second confirmation for extra safety (no admin privileges needed)
        button_index = self.show_native_alert(
            title="Final Confirmation",
            message="This is your final confirmation.\n\nAre you absolutely sure you want to export your private key?",
            buttons=["Cancel", "Yes, Export Key"],
            default_button=0,
            cancel_button=0,
            style="warning"
        )

        if button_index != 1:  # User didn't click "Yes, Export Key"
            return

        try:
            # Get the mnemonic
            mnemonic = key_manager.export_key_as_mnemonic()

            # Count actual words (excluding separator)
            word_count = len([w for w in mnemonic.split() if w != '|'])

            # Format for display with line breaks every 6 words
            words = mnemonic.split()
            formatted_lines = []
            for i in range(0, len(words), 6):
                formatted_lines.append(' '.join(words[i:i+6]))
            formatted_mnemonic = '\n'.join(formatted_lines)

            # Show the mnemonic with warning
            message = f"""⚠️ IMPORTANT: Keep these words safe and private!

These {word_count} words represent your private key and onion address. Anyone with these words can restore your exact onion address and impersonate your site.

{formatted_mnemonic}

The words have been copied to your clipboard.

Store them in a safe place - you can use them to restore your onion address on a new installation.

DO NOT share these words with anyone."""

            subprocess.run(["pbcopy"], input=mnemonic.encode(), check=True)

            rumps.alert(
                title="Private Key Backup",
                message=message
            )

        except Exception as e:
            rumps.alert(
                title="Export Failed",
                message=f"Could not export private key:\n\n{str(e)}"
            )

    @rumps.clicked("Import Private Key...")
    def import_key(self, _):
        """Import Tor private key from BIP39 mnemonic words"""
        # Warning dialog
        response = rumps.alert(
            title="Import Private Key",
            message="⚠️ WARNING: Importing a private key will replace your current onion address!\n\nYour WordPress site will be accessible at a different .onion address after import.\n\nMake sure you have backed up your current key first.\n\nContinue?",
            ok="Continue",
            cancel="Cancel"
        )

        if response != 1:  # Cancel clicked
            return

        # Get mnemonic from user
        window = rumps.Window(
            title="Import Private Key",
            message="Paste your BIP39 mnemonic words below (48 words, two 24-word mnemonics separated by |):",
            default_text="",
            ok="Import",
            cancel="Cancel",
            dimensions=(400, 100)
        )

        response = window.run()

        if not response.clicked:  # Cancel
            return

        mnemonic = response.text.strip()

        if not mnemonic:
            rumps.alert("No mnemonic provided")
            return

        # Validate word count
        word_count = len(mnemonic.split())
        if word_count != 47:
            rumps.alert(
                title="Invalid Mnemonic",
                message=f"Expected 47 words, got {word_count} words.\n\nPlease check your mnemonic and try again."
            )
            return

        # Try to import
        try:
            # Convert mnemonic to key bytes
            key_bytes = key_manager.import_key_from_mnemonic(mnemonic)

            # Stop the service first
            subprocess.run([self.launcher_script, "stop"], capture_output=True)
            time.sleep(2)

            # Write the new key
            key_manager.write_private_key(key_bytes)

            # Restart the service
            time.sleep(3)
            subprocess.run([self.launcher_script, "start"], capture_output=True)

            rumps.alert(
                title="Import Successful",
                message="Your private key has been imported successfully!\n\nThe service is restarting with your new onion address.\n\nPlease wait a moment for the address to appear in the menu."
            )

            # Trigger status check
            time.sleep(2)
            self.check_status()

        except Exception as e:
            rumps.alert(
                title="Import Failed",
                message=f"Could not import private key:\n\n{str(e)}\n\nYour original key has not been changed."
            )

    def update_docker_images(self, show_notifications=True):
        """Update Docker images (WordPress, MariaDB, Tor)"""
        try:
            if show_notifications:
                self.log("Checking for Docker image updates...")
                rumps.notification(
                    title="Onion.Press",
                    subtitle="Checking for Updates",
                    message="Checking for updated container images..."
                )

            docker_bin = os.path.join(self.bin_dir, "docker")
            docker_compose_file = os.path.join(self.resources_dir, "docker", "docker-compose.yml")

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
                timeout=300,  # 5 minute timeout
                env=env
            )

            if result.returncode == 0:
                self.log("Docker images updated successfully")

                # Check if any images were actually updated by comparing output
                if "Downloaded" in result.stdout or "Pulled" in result.stdout:
                    if show_notifications:
                        rumps.notification(
                            title="Onion.Press",
                            subtitle="Updates Downloaded",
                            message="Container images updated. Restart the service to apply updates."
                        )
                    return True
                else:
                    if show_notifications:
                        rumps.notification(
                            title="Onion.Press",
                            subtitle="Already Up to Date",
                            message="All container images are current."
                        )
                    return False
            else:
                self.log(f"Failed to update Docker images: {result.stderr}")
                if show_notifications:
                    rumps.notification(
                        title="Onion.Press",
                        subtitle="Update Failed",
                        message="Could not update container images. Check logs for details."
                    )
                return False

        except Exception as e:
            self.log(f"Error updating Docker images: {e}")
            if show_notifications:
                rumps.notification(
                    title="Onion.Press",
                    subtitle="Update Error",
                    message=f"Error updating containers: {str(e)}"
                )
            return False

    @rumps.clicked("Check for Updates...")
    def check_for_updates(self, _):
        """Check GitHub for newer versions and update Docker images"""
        # Check for app updates
        app_update_available = False
        try:
            # Fetch latest release from GitHub using curl to avoid permission prompts
            url = "https://api.github.com/repos/brewsterkahle/onion.press/releases/latest"
            result = subprocess.run(
                ["curl", "-s", "-H", "User-Agent: onion.press", "--max-time", "10", url],
                capture_output=True,
                text=True,
                timeout=15
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                latest_version = data.get('tag_name', '').lstrip('v')
                current_version = self.version

                if latest_version and latest_version > current_version:
                    app_update_available = True
                    response = rumps.alert(
                        title="App Update Available",
                        message=f"A new version of onion.press is available!\n\nCurrent: v{current_version}\nLatest: v{latest_version}\n\nWould you like to download it?",
                        ok="Download Update",
                        cancel="Later"
                    )
                    if response == 1:  # OK clicked
                        release_url = data.get('html_url', 'https://github.com/brewsterkahle/onion.press/releases/latest')
                        subprocess.run(["open", release_url])
        except Exception as e:
            rumps.alert(
                title="Update Check Failed",
                message=f"Could not check for app updates.\n\nPlease visit:\nhttps://github.com/brewsterkahle/onion.press/releases"
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

    def show_notification(self, message, subtitle=""):
        """Show a macOS notification (thread-safe via main queue dispatch)"""
        def _notify():
            try:
                rumps.notification(
                    title="Onion.Press Setup",
                    subtitle=subtitle,
                    message=message
                )
            except:
                pass
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_notify)

    def show_setup_dialog(self):
        """Show a persistent setup dialog during first run that stays until service is ready"""
        try:
            # Dismiss any existing dialog first
            self.dismiss_setup_dialog()

            # Show native dialog in background thread so it doesn't block
            def show_and_monitor():
                try:
                    # Show native alert dialog (no osascript = no permission prompts)
                    button_index = self.show_native_alert(
                        title="Onion.Press Setup",
                        message="Setting up Onion.Press for first use...\n\n• Downloading container images\n• Configuring Tor hidden service\n• Starting WordPress\n\nThis may take 2-5 minutes depending on your internet speed.\n\nConsole.app has been opened so you can watch the progress.\n\nThis window will close automatically when your site is ready.",
                        buttons=["Dismiss", "Cancel Setup"],
                        default_button=0,
                        cancel_button=1,
                        style="informational"
                    )

                    # If user clicked Cancel Setup (button index 1)
                    if button_index == 1:
                        self.log("User cancelled setup - stopping services")
                        subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                        self.setup_dialog_showing = False
                    elif button_index == 0:
                        self.log("User dismissed setup dialog")
                        self.setup_dialog_showing = False
                except Exception as e:
                    self.log(f"Error in setup dialog: {e}")
                    self.setup_dialog_showing = False

            self.setup_dialog_showing = True
            threading.Thread(target=show_and_monitor, daemon=True).start()
            self.log("Setup dialog shown (native NSAlert)")
        except Exception as e:
            self.log(f"Error showing setup dialog: {e}")
            self.setup_dialog_showing = False
            # Fallback to notification if dialog fails
            try:
                self.show_notification("Setting up onion.press for first use...",
                                     "Console.app opened to show progress. Downloading images (2-5 min)")
            except:
                pass

    def dismiss_setup_dialog(self):
        """Dismiss the setup dialog if it's showing"""
        if self.setup_dialog_showing:
            self.setup_dialog_showing = False
            self.log("Setup dialog marked for dismissal")
            # Note: Native NSAlert dialogs close when user clicks a button
            # We can't programmatically force-close them, but they auto-close on completion

    def monitor_image_downloads(self):
        """Monitor Docker image downloads and show progress notifications"""
        images_to_check = {
            'wordpress': False,
            'mariadb': False,
            'tor': False
        }

        self.log("Monitoring image downloads...")

        # Check for images every 3 seconds for up to 10 minutes
        for i in range(200):
            try:
                result = subprocess.run(
                    ["docker", "images", "--format", "{{.Repository}}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                current_images = result.stdout.strip().split('\n')

                # Check each image
                for image_name in images_to_check:
                    if not images_to_check[image_name]:
                        if any(image_name in img for img in current_images):
                            images_to_check[image_name] = True
                            if image_name == 'wordpress':
                                self.show_notification("Downloaded WordPress container")
                            elif image_name == 'mariadb':
                                self.show_notification("Downloaded database container")
                            elif image_name == 'tor':
                                self.show_notification("Downloaded Tor container")
                            self.log(f"Image downloaded: {image_name}")

                # If all images are downloaded, we're done
                if all(images_to_check.values()):
                    self.show_notification("All containers downloaded!", "Starting services...")
                    self.log("All images downloaded")
                    break

            except Exception as e:
                self.log(f"Error checking images: {e}")
                pass

            time.sleep(3)

    @rumps.clicked("About Onion.Press")
    def show_about(self, _):
        """Show about dialog"""
        about_text = f"""Onion.Press v{self.version}

Easy and free self-hosted web server for macOS
WordPress + Tor Onion Service

Features:
• Tor Onion Service with vanity addresses (op2*)
• Requires visitors to use Tor or Brave browsers
• Internet Archive Wayback Machine integration
• Bundled container runtime (no Docker needed)
• Privacy-first design
• Free and open source

Created by Brewster Kahle
License: AGPL v3

GitHub: github.com/brewsterkahle/onion.press"""

        # Use native NSAlert - no permissions needed, shows custom icon
        self.show_native_alert(
            title="About Onion.Press",
            message=about_text,
            buttons=["OK"],
            default_button=0,
            style="informational"
        )

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Uninstall Onion.Press with mandatory key backup prompt"""
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

                # Delete Colima VM (cleaner than pkill, properly removes VM)
                # Only affects Onion.Press instance, not system Colima
                self.log("Uninstall: Deleting Colima VM...")
                colima_bin = os.path.join(self.bin_dir, "colima")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "delete", "-f"], capture_output=True, timeout=60, env=env)

                # Remove Docker volumes
                self.log("Uninstall: Removing Docker volumes...")
                docker_bin = os.path.join(self.bin_dir, "docker")
                result = subprocess.run(
                    [docker_bin, "volume", "rm",
                     "onionpress-tor-keys",
                     "onionpress-wordpress-data",
                     "onionpress-db-data"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={"DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"}
                )

                if result.returncode == 0:
                    self.log("Uninstall: Docker volumes removed successfully")
                else:
                    self.log(f"Uninstall: Docker volume removal failed: {result.stderr}")

                # Step 3: Remove data directory (but keep it until after we show dialog)
                self.log("Uninstall: Preparing to remove data directory...")
                import shutil
                data_dir_exists = os.path.exists(self.app_support)

                # Step 4: Show final instructions BEFORE removing data dir
                # (so log file still exists if needed)
                # Use native alert (no osascript = no permissions needed)
                rumps.alert(
                    title="Uninstall Complete",
                    message="Onion.Press has been uninstalled.\n\nFinal step: Move Onion.Press.app to the Trash.\n\nClick OK to quit.",
                    ok="OK"
                )

                # Now remove data directory
                if data_dir_exists:
                    shutil.rmtree(self.app_support)
                    print("Uninstall: Data directory removed successfully")

            except Exception as e:
                rumps.alert(
                    title="Uninstall Error",
                    message=f"An error occurred during uninstall:\n\n{str(e)}\n\nYou may need to manually remove:\n• ~/.onion.press directory\n• Docker volumes (if they exist)"
                )

            # Quit the app
            rumps.quit_application()

        # Run uninstall in background thread to avoid blocking UI
        threading.Thread(target=do_uninstall, daemon=True).start()

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        self.log("="*60)
        self.log("QUIT BUTTON CLICKED - v2.2.31 RUNNING")
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

            # Now quit
            rumps.quit_application()

        # Start cleanup thread
        threading.Thread(target=update_and_cleanup, daemon=True).start()

if __name__ == "__main__":
    OnionPressApp().run()
