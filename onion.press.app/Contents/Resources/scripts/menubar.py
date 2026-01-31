#!/usr/bin/env python3
"""
onion.press Menu Bar Application
Provides a simple menu bar interface to control the WordPress + Tor service
"""

import rumps
import subprocess
import os
import threading
import time
import json

class OnionPressApp(rumps.App):
    def __init__(self):
        super(OnionPressApp, self).__init__("OP")

        # Get paths
        self.app_support = os.path.expanduser("~/.onion.press")
        self.script_dir = os.path.dirname(os.path.realpath(__file__))
        self.resources_dir = os.path.dirname(self.script_dir)
        self.contents_dir = os.path.dirname(self.resources_dir)
        self.macos_dir = os.path.join(self.contents_dir, "MacOS")
        self.launcher_script = os.path.join(self.macos_dir, "onion.press")

        # State
        self.onion_address = "Starting..."
        self.is_running = False
        self.checking = False

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
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Ensure Docker is available
        threading.Thread(target=self.ensure_docker_available, daemon=True).start()

        # Start status checker
        self.start_status_checker()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

    def ensure_docker_available(self):
        """Ensure Docker is running, start Docker Desktop or OrbStack if needed"""
        try:
            # Check if Docker is accessible
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                # Docker is already running
                return
        except:
            pass

        # Docker not accessible, try to start a container runtime
        # Try Docker Desktop first (free for personal use)
        docker_apps = [
            ("Docker", "Docker Desktop"),
            ("OrbStack", "OrbStack")
        ]

        for app_name, display_name in docker_apps:
            if os.path.exists(f"/Applications/{app_name}.app"):
                print(f"Docker not accessible, launching {display_name}...")
                try:
                    subprocess.run(["open", "-a", app_name], check=False)

                    # Wait for Docker to become available (up to 30 seconds)
                    for i in range(30):
                        time.sleep(1)
                        try:
                            result = subprocess.run(
                                ["docker", "info"],
                                capture_output=True,
                                timeout=5
                            )
                            if result.returncode == 0:
                                print(f"Docker is now available via {display_name}")
                                return
                        except:
                            continue

                    print(f"Warning: Docker still not available after launching {display_name}")
                except Exception as e:
                    print(f"Error launching {display_name}: {e}")
                    continue

        print("Warning: No Docker runtime found. Please install Docker Desktop or OrbStack.")

    def auto_start(self):
        """Automatically start the service when the app launches"""
        time.sleep(1)  # Brief delay
        self.start_service(None)

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
            else:
                self.onion_address = "Not running"

            # Update menu
            self.update_menu()

        except Exception as e:
            print(f"Error checking status: {e}")
        finally:
            self.checking = False

    def update_menu(self):
        """Update menu items based on current state"""
        if self.is_running:
            self.title = "OP ●"  # Running indicator
            self.menu["Starting..."].title = f"Address: {self.onion_address}"
            self.menu["Start"].set_callback(None)
            self.menu["Stop"].set_callback(self.stop_service)
            self.menu["Restart"].set_callback(self.restart_service)
        else:
            self.title = "OP ○"  # Stopped indicator
            self.menu["Starting..."].title = "Status: Stopped"
            self.menu["Start"].set_callback(self.start_service)
            self.menu["Stop"].set_callback(None)
            self.menu["Restart"].set_callback(None)

    def start_status_checker(self):
        """Start background thread to check status periodically"""
        def checker():
            while True:
                self.check_status()
                time.sleep(5)  # Check every 5 seconds

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
                title="onion.press",
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

    @rumps.clicked("Start")
    def start_service(self, _):
        """Start the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Starting..."
        self.title = "OP ..."

        def start():
            subprocess.run([self.launcher_script, "start"])
            time.sleep(2)
            self.check_status()

        threading.Thread(target=start, daemon=True).start()

    @rumps.clicked("Stop")
    def stop_service(self, _):
        """Stop the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Stopping..."
        self.title = "OP ..."

        def stop():
            subprocess.run([self.launcher_script, "stop"])
            time.sleep(1)
            self.check_status()

        threading.Thread(target=stop, daemon=True).start()

    @rumps.clicked("Restart")
    def restart_service(self, _):
        """Restart the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Restarting..."
        self.title = "OP ..."

        def restart():
            subprocess.run([self.launcher_script, "restart"])
            time.sleep(2)
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

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        response = rumps.alert(
            title="Quit onion.press?",
            message="This will stop the WordPress service. Are you sure?",
            ok="Quit",
            cancel="Cancel"
        )
        if response == 1:  # OK clicked
            # Stop services before quitting
            subprocess.run([self.launcher_script, "stop"])
            rumps.quit_application()

if __name__ == "__main__":
    OnionPressApp().run()
