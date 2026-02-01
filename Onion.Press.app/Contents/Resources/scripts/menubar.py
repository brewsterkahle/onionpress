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
import urllib.request
import plistlib
import sys

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

        # Icon paths
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Initialize with icon instead of text (empty string, not None)
        super(OnionPressApp, self).__init__("", icon=self.icon_stopped, quit_button=None)

        # Get version from Info.plist
        self.version = self.get_version()

        # Set up bundled binaries environment
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"

        # State
        self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
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
            rumps.separator,
            rumps.MenuItem("View Logs", callback=self.view_logs),
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

        # Start status checker
        self.start_status_checker()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

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

    def check_wordpress_health(self):
        """Check if WordPress is actually responding to requests"""
        try:
            req = urllib.request.Request('http://localhost:8080')
            with urllib.request.urlopen(req, timeout=3) as response:
                content = response.read().decode('utf-8', errors='ignore')
                # Check for database errors or WordPress not ready
                if 'Error establishing a database connection' in content:
                    return False
                if 'Database connection error' in content:
                    return False
                # If we get here and got a response, WordPress is responding
                # Either it's the install page or actual WordPress content
                return True
        except:
            return False

    def check_tor_reachability(self):
        """Check if the onion address is actually reachable over the Tor network"""
        try:
            if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
                return False

            # Use docker to run a Tor client and test connectivity
            docker_cmd = [
                "docker", "run", "--rm", "--network", "onionpress-network",
                "alpine", "sh", "-c",
                f"timeout 15 sh -c 'apk add --no-cache tor curl >/dev/null 2>&1 && "
                f"(tor >/dev/null 2>&1 &) && "
                f"while ! nc -z localhost 9050 2>/dev/null; do sleep 0.5; done && "
                f"curl -s -m 10 --socks5-hostname localhost:9050 http://{self.onion_address} >/dev/null 2>&1'"
            ]

            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                timeout=20
            )

            return result.returncode == 0
        except subprocess.TimeoutExpired:
            print("Tor reachability check timed out")
            return False
        except Exception as e:
            print(f"Error checking Tor reachability: {e}")
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

                # Check if WordPress is actually ready AND the onion address is reachable over Tor
                wordpress_ready = self.check_wordpress_health()
                tor_reachable = self.check_tor_reachability()

                self.is_ready = wordpress_ready and tor_reachable
            else:
                self.onion_address = "Not running"
                self.is_ready = False

            # Update menu
            self.update_menu()

        except Exception as e:
            print(f"Error checking status: {e}")
        finally:
            self.checking = False

    def update_menu(self):
        """Update menu items based on current state"""
        if self.is_running and self.is_ready:
            # Fully operational
            self.icon = self.icon_running
            self.menu["Starting..."].title = f"Address: {self.onion_address}"
            self.menu["Start"].set_callback(None)
            self.menu["Stop"].set_callback(self.stop_service)
        elif self.is_running and not self.is_ready:
            # Containers running but WordPress not ready yet
            self.icon = self.icon_starting
            self.menu["Starting..."].title = "Status: Starting up, please wait..."
            self.menu["Start"].set_callback(None)
            self.menu["Stop"].set_callback(self.stop_service)
        else:
            # Stopped
            self.icon = self.icon_stopped
            self.menu["Starting..."].title = "Status: Stopped"
            self.menu["Start"].set_callback(self.start_service)
            self.menu["Stop"].set_callback(None)

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

    @rumps.clicked("Start")
    def start_service(self, _):
        """Start the WordPress + Tor service"""
        self.menu["Starting..."].title = "Status: Starting..."

        def start():
            subprocess.run([self.launcher_script, "start"])
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
        """Export Tor private key as BIP39 mnemonic words"""
        if not self.is_running:
            rumps.alert(
                title="Service Not Running",
                message="Please start the service first before exporting the private key."
            )
            return

        try:
            # Get the mnemonic
            mnemonic = key_manager.export_key_as_mnemonic()
            word_count = len(mnemonic.split())

            # Format for display with line breaks every 6 words
            words = mnemonic.split()
            formatted_lines = []
            for i in range(0, len(words), 6):
                formatted_lines.append(' '.join(words[i:i+6]))
            formatted_mnemonic = '\n'.join(formatted_lines)

            # Show the mnemonic with warning
            message = f"""⚠️ IMPORTANT: Keep these words safe and private!

These {word_count} words represent your private key and onion address. Anyone with these words can restore your exact onion address.

{formatted_mnemonic}

The words have been copied to your clipboard.

Store them in a safe place - you can use them to restore your onion address on a new installation."""

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
            message="Paste your BIP39 mnemonic words below (47 words):",
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

    @rumps.clicked("Check for Updates...")
    def check_for_updates(self, _):
        """Check GitHub for newer versions"""
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
                    response = rumps.alert(
                        title="Update Available",
                        message=f"A new version of onion.press is available!\n\nCurrent: v{current_version}\nLatest: v{latest_version}\n\nWould you like to download it?",
                        ok="Download Update",
                        cancel="Later"
                    )
                    if response == 1:  # OK clicked
                        release_url = data.get('html_url', 'https://github.com/brewsterkahle/onion.press/releases/latest')
                        subprocess.run(["open", release_url])
                else:
                    rumps.alert(
                        title="No Updates Available",
                        message=f"You're running the latest version (v{current_version})"
                    )
        except Exception as e:
            rumps.alert(
                title="Update Check Failed",
                message=f"Could not check for updates.\n\nPlease visit:\nhttps://github.com/brewsterkahle/onion.press/releases"
            )

    @rumps.clicked("About Onion.Press")
    def show_about(self, _):
        """Show about dialog"""
        about_text = f"""Onion.Press v{self.version}

Easy-to-install WordPress with Tor Hidden Service for macOS

Features:
• Tor Hidden Service with vanity addresses (op2*)
• Internet Archive Wayback Machine integration
• Bundled container runtime (no Docker needed)
• Privacy-first design

Created by Brewster Kahle
License: AGPL v3

GitHub: github.com/brewsterkahle/onion.press"""

        rumps.alert(title="About Onion.Press", message=about_text)

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Guide user through uninstallation"""
        response = rumps.alert(
            title="Uninstall Onion.Press",
            message="This will guide you through removing Onion.Press from your system.\n\nYour WordPress data and onion address will be permanently deleted.\n\nContinue?",
            ok="Continue",
            cancel="Cancel"
        )

        if response == 1:  # OK clicked
            # Stop services first
            subprocess.run([self.launcher_script, "stop"], capture_output=True)

            # Show uninstall instructions
            instructions = """To complete uninstallation:

1. Quit Onion.Press (it will quit automatically)
2. Move Onion.Press.app to Trash
3. Open Terminal and run these commands:

   # Remove data directory
   rm -rf ~/.onion.press

   # Remove Docker volumes
   docker volume rm onionpress-tor-keys
   docker volume rm onionpress-wordpress-data
   docker volume rm onionpress-db-data

These commands have been copied to your clipboard."""

            # Copy commands to clipboard
            commands = """rm -rf ~/.onion.press
docker volume rm onionpress-tor-keys onionpress-wordpress-data onionpress-db-data"""
            subprocess.run(["pbcopy"], input=commands.encode(), check=True)

            rumps.alert(title="Uninstall Instructions", message=instructions)

            # Quit the app
            rumps.quit_application()

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        response = rumps.alert(
            title="Quit Onion.Press?",
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
