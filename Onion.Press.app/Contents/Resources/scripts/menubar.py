#!/usr/bin/env python3
"""
onion.press Menu Bar Application
Provides a simple menu bar interface to control the WordPress + Tor onion service
"""

# Set activation policy BEFORE importing rumps to prevent dock icon
import AppKit
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

import rumps
import subprocess
import os
import threading
import time
import json
import urllib.request
import plistlib
import sys
from datetime import datetime

# Add scripts directory to path for imports
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

import key_manager

class OnionPressApp(rumps.App):
    def __init__(self):
        # Get paths first
        self.app_support = os.path.expanduser("~/.onion.press")
        self.script_dir = os.path.dirname(os.path.realpath(__file__))
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
            except:
                pass  # If rotation fails, just append

        # Icon paths
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Initialize with icon instead of text (empty string, not None)
        super(OnionPressApp, self).__init__("", icon=self.icon_stopped, quit_button=None)

        # Get version from Info.plist
        self.version = self.get_version()

        # Create Docker config without credential store (avoids docker-credential-osxkeychain errors)
        docker_config_dir = os.path.join(self.app_support, "docker-config")
        os.makedirs(docker_config_dir, exist_ok=True)
        docker_config_file = os.path.join(docker_config_dir, "config.json")
        if not os.path.exists(docker_config_file):
            with open(docker_config_file, 'w') as f:
                f.write('{\n\t"auths": {},\n\t"currentContext": "colima"\n}\n')

        # Create symlink to docker-compose plugin if it exists in default location
        cli_plugins_dir = os.path.join(docker_config_dir, "cli-plugins")
        os.makedirs(cli_plugins_dir, exist_ok=True)
        compose_plugin_src = os.path.expanduser("~/.docker/cli-plugins/docker-compose")
        compose_plugin_dest = os.path.join(cli_plugins_dir, "docker-compose")
        if os.path.islink(compose_plugin_src) and not os.path.exists(compose_plugin_dest):
            try:
                os.symlink(compose_plugin_src, compose_plugin_dest)
            except:
                pass  # Ignore errors if symlink creation fails

        # Set up bundled binaries environment
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        os.environ["DOCKER_CONFIG"] = docker_config_dir

        # State
        self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
        self.checking = False
        self.web_log_process = None  # Background process for web logs
        self.last_status_logged = None  # Track last logged status to avoid spam
        self.auto_opened_browser = False  # Track if we've auto-opened browser this session
        self.setup_dialog_process = None  # Track setup dialog process to dismiss it later

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
            with open(self.log_file, 'a') as f:
                f.write(log_message)
        except Exception as e:
            print(f"Error writing to log: {e}")

    def start_web_log_capture(self):
        """Start capturing WordPress logs to a file"""
        if self.web_log_process is not None:
            return  # Already running

        try:
            web_log_file = os.path.join(self.app_support, "wordpress-access.log")
            docker_bin = os.path.join(self.bin_dir, "docker")

            # Open log file for writing
            log_file_handle = open(web_log_file, 'a')

            # Start docker logs process in background
            self.web_log_process = subprocess.Popen(
                [docker_bin, "logs", "-f", "--tail", "100", "onionpress-wordpress"],
                stdout=log_file_handle,
                stderr=subprocess.STDOUT,
                env={
                    "DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"
                }
            )
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
            except:
                try:
                    self.web_log_process.kill()
                except:
                    pass
            self.web_log_process = None
            print("Stopped web log capture")

    def ensure_docker_available(self):
        """Ensure bundled Colima is running"""
        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            if not os.path.exists(colima_bin):
                print("ERROR: Bundled Colima not found")
                return

            # Check if running
            result = subprocess.run([colima_bin, "status"], capture_output=True, timeout=5)

            if result.returncode == 0:
                # Verify docker accessible
                docker_check = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                if docker_check.returncode == 0:
                    print("Bundled Colima is running")
                    return

            # Start Colima
            print("Starting bundled Colima...")
            subprocess.run([colima_bin, "start"], capture_output=True, timeout=120)

            # Wait for docker
            for i in range(30):
                time.sleep(1)
                try:
                    result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                    if result.returncode == 0:
                        print("Colima started successfully")
                        return
                except:
                    continue

            print("Warning: Colima started but docker not available yet")

        except Exception as e:
            print(f"Error with Colima: {e}")

    def auto_start(self):
        """Automatically start the service when the app launches"""
        time.sleep(1)  # Brief delay

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
        """Check if app is currently in login items"""
        try:
            app_path = os.path.dirname(os.path.dirname(os.path.dirname(self.script_dir)))

            # Use osascript to check login items
            script = f'''
tell application "System Events"
    get the name of every login item
end tell
'''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                login_items = result.stdout.strip()
                # Check if Onion.Press or onion.press is in the list
                return "Onion.Press" in login_items or "onion.press" in login_items

            return False
        except Exception as e:
            self.log(f"Error checking login items: {e}")
            return False

    def add_login_item(self):
        """Add app to login items"""
        try:
            app_path = os.path.dirname(os.path.dirname(os.path.dirname(self.script_dir)))

            script = f'''
tell application "System Events"
    make new login item at end with properties {{path:"{app_path}", hidden:false}}
end tell
'''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                self.log("Added to login items")
                return True
            else:
                self.log(f"Failed to add to login items: {result.stderr}")
                return False
        except Exception as e:
            self.log(f"Error adding login item: {e}")
            return False

    def remove_login_item(self):
        """Remove app from login items"""
        try:
            script = '''
tell application "System Events"
    delete (every login item whose name is "Onion.Press" or name is "onion.press")
end tell
'''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                self.log("Removed from login items")
                return True
            else:
                self.log(f"Failed to remove from login items: {result.stderr}")
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
            req = urllib.request.Request('http://localhost:8080')
            with urllib.request.urlopen(req, timeout=3) as response:
                content = response.read().decode('utf-8', errors='ignore')
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
            docker_env["DOCKER_CONFIG"] = os.path.join(os.path.expanduser("~/.onion.press"), "docker-config")

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

    @rumps.clicked("Open in Tor Browser")
    def open_tor_browser(self, _):
        """Open the onion address in Tor Browser"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"

            if os.path.exists(tor_browser_path):
                url = f"http://{self.onion_address}"
                subprocess.run(["open", "-a", "Tor Browser", url])
            else:
                response = rumps.alert(
                    title="Tor Browser Not Found",
                    message="Tor Browser is not installed. Would you like to download it?",
                    ok="Download Tor Browser",
                    cancel="Cancel"
                )
                if response == 1:  # OK clicked
                    subprocess.run(["open", "https://www.torproject.org/download/"])
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def auto_open_browser(self):
        """Automatically open Tor Browser when service becomes ready"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"

            if os.path.exists(tor_browser_path):
                url = f"http://{self.onion_address}"
                self.log(f"Auto-opening Tor Browser: {url}")
                subprocess.run(["open", "-a", "Tor Browser", url])
            else:
                self.log("Tor Browser not installed - showing download notification")
                rumps.notification(
                    title="Tor Browser Suggested",
                    subtitle="Download Tor or Brave Browser to access your site",
                    message=f"Your site is ready at {self.onion_address}"
                )

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
                    self.log("First run detected - will show setup dialog and monitor image downloads")
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

        # Show security warning dialog first
        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        try:
            result = subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    set userChoice to button returned of (display dialog "⚠️ SECURITY WARNING ⚠️

You are about to export your private key. This key controls your onion address and website identity.

ANYONE with these backup words can:
• Impersonate your onion address
• Take over your site identity
• Restore your address on another computer

Only export if you understand the security implications.

Continue with export?" buttons {{"Cancel", "I Understand - Continue"}} default button "Cancel" cancel button "Cancel" with icon POSIX file "{icon_path}" with title "Export Private Key")
    return userChoice
end tell
'''], capture_output=True, text=True, timeout=60)

            if result.returncode != 0 or "Continue" not in result.stdout:
                return  # User cancelled
        except Exception as e:
            self.log(f"Warning dialog failed: {e}")
            return

        # Require macOS password authentication
        try:
            auth_result = subprocess.run(["osascript", "-e", f'''
do shell script "echo 'Authentication successful'" with administrator privileges
'''], capture_output=True, text=True, timeout=60)

            if auth_result.returncode != 0:
                rumps.alert(
                    title="Authentication Failed",
                    message="macOS password authentication is required to export private keys."
                )
                return
        except Exception as e:
            self.log(f"Authentication failed: {e}")
            rumps.alert(
                title="Authentication Failed",
                message="Could not authenticate. Private key export cancelled."
            )
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
            # Fetch latest release from GitHub
            url = "https://api.github.com/repos/brewsterkahle/onion.press/releases/latest"
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'onion.press')

            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read())
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
        """Show a macOS notification"""
        try:
            rumps.notification(
                title="Onion.Press Setup",
                subtitle=subtitle,
                message=message
            )
        except:
            pass

    def show_setup_dialog(self):
        """Show a persistent setup dialog during first run that stays until service is ready"""
        try:
            # Dismiss any existing dialog first
            self.dismiss_setup_dialog()

            # Show dialog with "Dismiss" and "Cancel Setup" buttons
            # User can dismiss to hide the dialog, or cancel to stop setup
            icon_path = os.path.join(self.resources_dir, "app-icon.png")

            script = f'''
tell application "System Events"
    activate
    display dialog "Setting up onion.press for first use...

Downloading container images (2-5 minutes)

This window will close automatically when your site is ready." buttons {{"Cancel Setup", "Dismiss"}} default button "Dismiss" cancel button "Cancel Setup" with icon POSIX file "{icon_path}" with title "onion.press Setup" giving up after 1800
end tell
'''

            # Start the dialog process
            self.setup_dialog_process = subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )

            # Monitor if user clicks Cancel Setup
            def monitor_result():
                try:
                    stdout, _ = self.setup_dialog_process.communicate(timeout=1800)
                    # If user clicked Cancel Setup, stop setup
                    # (returncode 1 = cancel button clicked)
                    result = self.setup_dialog_process.returncode
                    if result == 1:
                        self.log("User cancelled setup - stopping services")
                        subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                    # If user clicked Dismiss (returncode 0) or timeout, just continue
                except:
                    pass  # Dialog was dismissed programmatically or timed out

            threading.Thread(target=monitor_result, daemon=True).start()
            self.log("Setup dialog shown via osascript")
        except Exception as e:
            self.log(f"Error showing setup dialog via osascript (possibly permissions): {e}")
            # Fallback to notification if osascript fails
            try:
                self.show_notification("Setting up onion.press for first use...",
                                     "Downloading container images (2-5 minutes)")
            except:
                pass

    def dismiss_setup_dialog(self):
        """Dismiss the setup dialog if it's showing"""
        if self.setup_dialog_process is not None:
            try:
                # Try to close the dialog window directly via AppleScript
                subprocess.run([
                    "osascript", "-e",
                    '''tell application "System Events"
                        tell process "osascript"
                            if exists window "onion.press Setup" then
                                click button 1 of window "onion.press Setup"
                            end if
                        end tell
                    end tell'''
                ], timeout=2, capture_output=True)

                # Then terminate the process
                self.setup_dialog_process.terminate()
                self.setup_dialog_process.wait(timeout=2)
                self.log("Setup dialog dismissed")
            except:
                try:
                    self.setup_dialog_process.kill()
                except:
                    pass
            self.setup_dialog_process = None

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

        # Use osascript to show dialog with custom icon
        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        try:
            subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    display dialog "{about_text}" buttons {{"OK"}} default button "OK" with icon POSIX file "{icon_path}" with title "About Onion.Press"
end tell
'''], timeout=60)
        except Exception as e:
            # Fallback to rumps if osascript fails
            self.log(f"About dialog osascript failed: {e}")
            rumps.alert(title="About Onion.Press", message=about_text)

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Uninstall Onion.Press with mandatory key backup prompt"""
        icon_path = os.path.join(self.resources_dir, "app-icon.png")

        # Step 1: Show critical warning about key loss
        try:
            result = subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    set userChoice to button returned of (display dialog "⚠️ CRITICAL WARNING ⚠️

Uninstalling will PERMANENTLY DELETE:
• Your onion address and private key
• All WordPress content and data
• Database and configuration

YOUR ONION ADDRESS CANNOT BE RECOVERED unless you have a backup of your private key.

Do you want to backup your private key before uninstalling?" buttons {{"Cancel", "No, Delete Everything", "Yes, Backup First"}} default button "Yes, Backup First" cancel button "Cancel" with icon POSIX file "{icon_path}" with title "Uninstall Warning")
    return userChoice
end tell
'''], capture_output=True, text=True, timeout=60)

            self.log(f"Key backup prompt result: {result.stdout}")

            if result.returncode != 0 or "Cancel" in result.stdout:
                return  # User cancelled

            if "Yes, Backup First" in result.stdout:
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
                result2 = subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    set userChoice to button returned of (display dialog "Key backup complete.

Proceed with uninstall?

This will permanently delete all data." buttons {{"Cancel", "Proceed with Uninstall"}} default button "Cancel" cancel button "Cancel" with icon POSIX file "{icon_path}" with title "Confirm Uninstall")
    return userChoice
end tell
'''], capture_output=True, text=True, timeout=60)

                if result2.returncode != 0 or "Proceed" not in result2.stdout:
                    return  # User cancelled after backup

        except Exception as e:
            # Fallback to rumps if osascript fails
            self.log(f"Warning dialog osascript failed: {e}")
            response = rumps.alert(
                title="Uninstall Warning",
                message="⚠️ WARNING: Uninstalling will permanently delete your onion address and all data.\n\nBackup your private key first?\n\nClick 'Backup' to export your key, or 'Continue' to proceed without backup.",
                ok="Continue Without Backup",
                cancel="Cancel"
            )
            if response != 1:
                return

        # Step 2: Final confirmation with explicit acknowledgment
        try:
            result = subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    set userChoice to button returned of (display dialog "FINAL CONFIRMATION

Type 'DELETE' below to confirm permanent deletion of all data:
" default answer "" buttons {{"Cancel", "Confirm Deletion"}} default button "Cancel" cancel button "Cancel" with icon POSIX file "{icon_path}" with title "Confirm Uninstall")
    set userText to text returned of result
    return userText
end tell
'''], capture_output=True, text=True, timeout=60)

            self.log(f"Final confirmation result: {result.stdout}")

            # Check if user typed "DELETE" (case insensitive)
            if result.returncode != 0 or "DELETE" not in result.stdout.upper():
                rumps.alert(
                    title="Uninstall Cancelled",
                    message="Uninstall cancelled. Type 'DELETE' to confirm."
                )
                return

        except Exception as e:
            self.log(f"Final confirmation failed: {e}")
            # Fallback: just require one more click
            response = rumps.alert(
                title="Final Confirmation",
                message="Are you absolutely sure you want to delete everything?",
                ok="Yes, Delete Everything",
                cancel="Cancel"
            )
            if response != 1:
                return

        # User confirmed uninstall - run in background thread to avoid beach ball
        def do_uninstall():
            try:
                # First, stop any ongoing setup processes
                self.log("Uninstall: Stopping any ongoing processes...")
                self.dismiss_setup_dialog()

                # Stop the service (this will cancel any startup in progress)
                self.log("Uninstall: Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)

                # Kill all colima/lima processes (including orphaned ones)
                self.log("Uninstall: Killing all colima/lima processes...")
                subprocess.run(["pkill", "-f", "colima daemon"], capture_output=True, timeout=10)
                subprocess.run(["pkill", "-f", "limactl hostagent"], capture_output=True, timeout=10)
                subprocess.run(["pkill", "-f", "limactl usernet"], capture_output=True, timeout=10)
                subprocess.run(["pkill", "-f", "ssh.*colima.*ssh.sock"], capture_output=True, timeout=10)

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
                # Try osascript first for nice icon, fall back to rumps.alert if it fails
                try:
                    result = subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    display dialog "Onion.Press has been uninstalled.

Final step: Move Onion.Press.app to the Trash.

Click OK to quit." buttons {{"OK"}} default button "OK" with icon POSIX file "{icon_path}" with title "Uninstall Complete"
end tell
'''], timeout=60, capture_output=True)
                    if result.returncode != 0:
                        raise Exception("osascript failed")
                except:
                    # Fallback to rumps.alert if osascript fails (permissions, etc)
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

            # Quit Console.app if it's running (so it releases the log file)
            subprocess.run(["osascript", "-e", 'quit app "Console"'], capture_output=True)
            # Quit the app
            rumps.quit_application()

        # Run uninstall in background thread to avoid blocking UI
        threading.Thread(target=do_uninstall, daemon=True).start()

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        # Use osascript to show dialog with custom icon
        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        try:
            result = subprocess.run(["osascript", "-e", f'''
tell application "System Events"
    activate
    set userChoice to button returned of (display dialog "This will stop the WordPress service. Are you sure?" buttons {{"Cancel", "Quit"}} default button "Cancel" cancel button "Cancel" with icon POSIX file "{icon_path}" with title "Quit Onion.Press?")
    return userChoice
end tell
'''], capture_output=True, text=True, timeout=60)

            # Check if user clicked Quit
            if result.returncode != 0 or "Quit" not in result.stdout:
                return  # User cancelled
        except Exception as e:
            # Fallback to rumps if osascript fails
            self.log(f"Quit dialog osascript failed: {e}")
            response = rumps.alert(
                title="Quit Onion.Press?",
                message="This will stop the WordPress service. Are you sure?",
                ok="Quit",
                cancel="Cancel"
            )
            if response != 1:  # OK not clicked
                return

        # User confirmed quit
        # Dismiss setup dialog if showing
        self.dismiss_setup_dialog()
        # Stop web log capture
        self.stop_web_log_capture()
        # Stop services before quitting (with timeout to prevent hanging)
        try:
            subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            self.log("Warning: Stop command timed out, forcing quit anyway")
        # Quit Console.app if it's running (so it releases the log file)
        subprocess.run(["osascript", "-e", 'quit app "Console"'], capture_output=True)
        rumps.quit_application()

if __name__ == "__main__":
    OnionPressApp().run()
