"""OnionPress Settings Dialog.

Extracted from menubar.py — builds and runs the native macOS settings form,
validates input, confirms changes, and writes back to config.
"""

import json
import os
import re
import subprocess

import AppKit
import rumps

from . import config as op_config
from .ui_helpers import HelpButtonTarget


# -- Settings help text (from config-template comments) --
SETTINGS_HELP = {
    "ADDRESS_PREFIX": (
        "Onion Address Prefix\n\n"
        "Customise the beginning of your .onion address.\n"
        "Default: \"op2\" (generates addresses like op2xxxxxxxxxxxxx.onion)\n\n"
        "Only base32 characters allowed (a-z, 2-7). Numbers 0, 1, 8, 9 are not valid.\n"
        "2 to 5 characters. Longer prefixes take exponentially longer to generate:\n"
        "  2 chars: < 1 second\n"
        "  3 chars: < 1 second\n"
        "  4 chars: 5-30 seconds\n"
        "  5 chars: 10-30 minutes"
    ),
    "VM_MEMORY": (
        "Virtual Machine Memory (GB)\n\n"
        "RAM allocated to the Linux VM that runs WordPress, Tor, and MariaDB.\n"
        "1 GB is sufficient for normal use. Increase if you run many plugins "
        "or experience out-of-memory issues.\n\n"
        "Requires restart to take effect."
    ),
    "VM_CPU": (
        "Virtual Machine CPUs\n\n"
        "Number of CPU cores allocated to the Linux VM.\n"
        "2 is sufficient for normal use. OnionHeaven mode automatically "
        "sets this to 3/4 of your Mac's cores.\n\n"
        "Requires restart to take effect."
    ),
    "PREVENT_SLEEP": (
        "Sleep Prevention Mode\n\n"
        "Controls whether OnionPress keeps your Mac awake.\n\n"
        "Normal: Mac sleeps as usual. Your site goes offline when sleeping.\n\n"
        "On AC Power: Stay awake when plugged in, sleep on battery. "
        "Good balance of uptime and battery life.\n\n"
        "Never: Mac never idle-sleeps while OnionPress runs. "
        "Best for always-on servers (OnionHeaven machines).\n\n"
        "Display sleep is not affected \u2014 the screen can still turn off."
    ),
    "LAUNCH_ON_LOGIN": (
        "Launch on Login\n\n"
        "Automatically start OnionPress when you log in to macOS.\n"
        "Installs a LaunchAgent that runs OnionPress at login."
    ),
    "UPDATE_ON_LAUNCH": (
        "Update Docker Images on Launch\n\n"
        "Automatically check for updated WordPress, MariaDB, and Tor container "
        "images when the app launches. Ensures you have the latest security patches."
    ),
    "INSTALL_IA_PLUGIN": (
        "Internet Archive Wayback Machine Link Fixer Plugin\n\n"
        "Automatically installs and activates the IA Link Fixer plugin, which:\n"
        "  - Scans posts for outbound links\n"
        "  - Creates archived versions in the Wayback Machine\n"
        "  - Redirects to archived versions when links break\n"
        "  - Archives your own posts on every update"
    ),
    "REGISTER_WITH_ONIONHEAVEN": (
        "Register with OnionHeaven\n\n"
        "Registers your site with OnionHeaven so it can redirect page "
        "requests to the Wayback Machine as a fallback when your Mac is offline."
    ),
    "SHARE_ANALYTICS_WITH_ONIONHOME": (
        "Share Analytics with OnionHome\n\n"
        "When enabled, OnionPress will periodically upload completed log files "
        "to the OnionHome hub for remote debugging. Logs are scrubbed of your "
        "home directory path before upload.\n\n"
        "Enable to help the OnionPress project diagnose issues."
    ),
    "ONIONHEAVEN_ADDRESS": (
        "OnionHeaven Hub Address\n\n"
        "The .onion address of the OnionHeaven hub your site registers with. "
        "When your site goes offline, the hub redirects visitors to the "
        "Wayback Machine copy.\n\n"
        "Default: oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
    ),
    "CLOUDFLARE_TUNNEL_TOKEN": (
        "Cloudflare Tunnel (Clearnet Access)\n\n"
        "Expose your WordPress site on the regular internet via Cloudflare Tunnel.\n\n"
        "PRIVACY NOTE: This reveals your Mac's IP address to Cloudflare. "
        "Your site is no longer anonymous.\n\n"
        "Setup:\n"
        "1. Create a free Cloudflare account and add your domain\n"
        "2. Go to Zero Trust > Networks > Tunnels > Create a tunnel\n"
        "3. Set the tunnel service to http://wordpress:80\n"
        "4. Copy the tunnel token and paste it below\n"
        "5. Restart OnionPress\n\n"
        "IMPORTANT: Do NOT install cloudflared on your Mac (e.g. via "
        "Homebrew). OnionPress runs cloudflared automatically inside "
        "Docker. A host-level install causes intermittent 502 errors."
    ),
}

# Consequence text shown in the hazards confirmation dialog
SETTINGS_CONSEQUENCES = {
    "ADDRESS_PREFIX": (
        "Your current .onion address will stop working. A new address "
        "will be generated. Existing links or bookmarks will break."
    ),
    "VM_MEMORY": (
        "The VM will be resized on next restart. "
        "Brief downtime expected while the VM restarts."
    ),
    "VM_CPU": (
        "The VM will be resized on next restart. "
        "Brief downtime expected while the VM restarts."
    ),
    "PREVENT_SLEEP": {
        "normal": "Mac will sleep normally. Site goes offline when sleeping.",
        "on-battery": "Mac stays awake on AC power. Sleeps normally on battery.",
        "never": "Mac will never idle-sleep while OnionPress runs. Best for always-on servers.",
    },
    "LAUNCH_ON_LOGIN": {
        "yes": "OnionPress will start automatically on login.",
        "no": "OnionPress will no longer auto-start.",
    },
    "UPDATE_ON_LAUNCH": {
        "yes": "Docker images will update automatically on launch.",
        "no": "Automatic updates disabled.",
    },
    "INSTALL_IA_PLUGIN": {
        "yes": "Internet Archive plugin will be installed.",
        "no": "Internet Archive plugin will not be auto-installed.",
    },
    "REGISTER_WITH_ONIONHEAVEN": {
        "yes": "Site will register with OnionHeaven.",
        "no": "OnionHeaven registration disabled. Wayback fallback won't work.",
    },
    "SHARE_ANALYTICS_WITH_ONIONHOME": {
        "yes": "Diagnostic logs will be shared with OnionHome.",
        "no": "Log sharing disabled.",
    },
    "CLOUDFLARE_TUNNEL_TOKEN": {
        "set": (
            "Your site will be exposed on the clearnet via Cloudflare. "
            "Your Mac's IP will be visible to Cloudflare."
        ),
        "cleared": "Clearnet access will be disabled.",
    },
}

# Settings that require a restart to take effect.
# All others are applied immediately (read from config on next cycle).
_NEEDS_RESTART = {
    "ADDRESS_PREFIX", "VM_MEMORY", "VM_CPU",
    "CLOUDFLARE_TUNNEL_TOKEN",
}

# Human-readable labels for each setting key
_LABELS = {
    "ADDRESS_PREFIX": "Address Prefix",
    "VM_MEMORY": "VM Memory (GB)",
    "VM_CPU": "VM CPUs",
    "PREVENT_SLEEP": "Sleep Prevention",
    "LAUNCH_ON_LOGIN": "Launch on Login",
    "UPDATE_ON_LAUNCH": "Update Docker on Launch",
    "INSTALL_IA_PLUGIN": "Install IA Plugin",
    "REGISTER_WITH_ONIONHEAVEN": "Register with OnionHeaven",
    "ONIONHEAVEN_ADDRESS": "OnionHeaven Hub",
    "CLOUDFLARE_TUNNEL_TOKEN": "Cloudflare Token",
    "SHARE_ANALYTICS_WITH_ONIONHOME": "Share Analytics with OnionHome",
}

# Settings form definition: (key, default)
SETTINGS_KEYS = [
    ("ADDRESS_PREFIX", "op2"),
    ("VM_MEMORY", "1"),
    ("VM_CPU", "2"),
    ("PREVENT_SLEEP", "normal"),
    ("LAUNCH_ON_LOGIN", "yes"),
    ("UPDATE_ON_LAUNCH", "yes"),
    ("INSTALL_IA_PLUGIN", "yes"),
    ("REGISTER_WITH_ONIONHEAVEN", "yes"),
    ("SHARE_ANALYTICS_WITH_ONIONHOME", "no"),
    ("ONIONHEAVEN_ADDRESS", "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"),
    ("CLOUDFLARE_TUNNEL_TOKEN", ""),
]


def _icon_alert(title, message, icon_path):
    """Show an alert with the OnionPress icon."""
    a = AppKit.NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(message)
    if icon_path and os.path.exists(icon_path):
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
        if img:
            a.setIcon_(img)
    a.runModal()


def _normalize_sleep(val):
    """Normalize legacy PREVENT_SLEEP values."""
    val = val.lower()
    if val == "yes":
        return "on-battery"
    if val == "no":
        return "normal"
    if val not in ("normal", "on-battery", "never"):
        return "normal"
    return val


def show_settings_dialog(config_path, icon_path, launcher_script, log_func, callbacks):
    """Show the OnionPress settings dialog.

    Args:
        config_path: Path to the config file (~/.onionpress/config).
        icon_path: Path to app-icon.png for dialogs.
        launcher_script: Path to the onionpress launcher script (for OH validation).
        log_func: Callable for logging messages.
        callbacks: Dict with keys:
            'write_config': func(key, value)
            'restart_caffeinate': func()
            'add_login_item': func()
            'remove_login_item': func()
    """
    if not os.path.exists(config_path):
        rumps.alert("Settings file not found")
        return

    # Read current values
    old_values = {}
    for key, default in SETTINGS_KEYS:
        old_values[key] = op_config.read_value(config_path, key, default)
    old_values["PREVENT_SLEEP"] = _normalize_sleep(old_values.get("PREVENT_SLEEP", "normal"))

    # Layout constants
    field_w = 300
    row_h = 30
    label_w = 170
    input_x = 175
    input_w = 100
    help_x = 280
    help_w = 25
    container_h = 13 * row_h + 10

    # Create help button target (shared across dialog rebuilds)
    help_target = HelpButtonTarget.alloc().init()
    help_keys = [k for k, _ in SETTINGS_KEYS]
    help_target._help_texts = {i: SETTINGS_HELP[k] for i, k in enumerate(help_keys)}
    help_target._icon_path = icon_path
    # Store reference to prevent GC during modal
    show_settings_dialog._help_target = help_target

    form_values = dict(old_values)

    while True:
        # -- Build settings form dialog --
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("OnionPress Settings")
        alert.setInformativeText_("Change settings below. Click (?) for help on any setting.")

        if icon_path and os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        container = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, field_w, container_h))

        fields = {}
        tag_counter = [0]

        def _make_help_btn(y):
            btn = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(help_x, y, help_w, 24))
            btn.setBezelStyle_(9)  # NSBezelStyleHelpButton
            btn.setTag_(tag_counter[0])
            btn.setTarget_(help_target)
            btn.setAction_(help_target.helpClicked_)
            container.addSubview_(btn)
            tag_counter[0] += 1

        def add_text_row(y, label_text, key, value):
            label = AppKit.NSTextField.labelWithString_(label_text)
            label.setFrame_(AppKit.NSMakeRect(0, y + 3, label_w, 18))
            container.addSubview_(label)
            field = AppKit.NSTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(input_x, y, input_w, 24))
            field.setStringValue_(str(value))
            container.addSubview_(field)
            fields[key] = field
            _make_help_btn(y)
            return field

        def add_check_row(y, label_text, key, value):
            cb = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, y, help_x - 5, 24))
            cb.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb.setTitle_(label_text)
            if value.lower() == "yes":
                cb.setState_(AppKit.NSControlStateValueOn)
            else:
                cb.setState_(AppKit.NSControlStateValueOff)
            container.addSubview_(cb)
            fields[key] = cb
            _make_help_btn(y)
            return cb

        def add_popup_row(y, label_text, key, value, options):
            label = AppKit.NSTextField.labelWithString_(label_text)
            label.setFrame_(AppKit.NSMakeRect(0, y + 3, label_w, 18))
            container.addSubview_(label)
            popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                AppKit.NSMakeRect(input_x, y, input_w, 24), False)
            for title, val in options:
                popup.addItemWithTitle_(title)
            for i, (_, val) in enumerate(options):
                if val == value:
                    popup.selectItemAtIndex_(i)
                    break
            container.addSubview_(popup)
            fields[key] = popup
            _make_help_btn(y)
            return popup

        y = container_h - row_h
        # The vanity prefix is chosen once at install (welcome screen) and can't
        # be changed on the fly (#256 phase 4b) — show it read-only.
        prefix_field = add_text_row(y, "Address prefix (fixed):", "ADDRESS_PREFIX", form_values["ADDRESS_PREFIX"])
        prefix_field.setEditable_(False)
        y -= row_h
        add_text_row(y, "VM Memory (GB):", "VM_MEMORY", form_values["VM_MEMORY"])
        y -= row_h
        add_text_row(y, "VM CPUs:", "VM_CPU", form_values["VM_CPU"])
        y -= row_h
        sleep_val = _normalize_sleep(form_values["PREVENT_SLEEP"])
        add_popup_row(y, "Sleep Prevention:", "PREVENT_SLEEP", sleep_val, [
            ("Normal", "normal"),
            ("On AC Power", "on-battery"),
            ("Never Sleep", "never"),
        ])
        y -= row_h
        add_check_row(y, "Launch on Login", "LAUNCH_ON_LOGIN", form_values["LAUNCH_ON_LOGIN"])
        y -= row_h
        add_check_row(y, "Update Docker on Launch", "UPDATE_ON_LAUNCH", form_values["UPDATE_ON_LAUNCH"])
        y -= row_h
        add_check_row(y, "Install IA Plugin", "INSTALL_IA_PLUGIN", form_values["INSTALL_IA_PLUGIN"])
        y -= row_h
        add_check_row(y, "Register with OnionHeaven (advanced)", "REGISTER_WITH_ONIONHEAVEN", form_values["REGISTER_WITH_ONIONHEAVEN"])
        y -= row_h
        add_check_row(y, "Share diagnostic logs with OnionHome", "SHARE_ANALYTICS_WITH_ONIONHOME", form_values["SHARE_ANALYTICS_WITH_ONIONHOME"])
        y -= row_h
        oh_addr_field = add_text_row(y, "OnionHeaven Hub (advanced):", "ONIONHEAVEN_ADDRESS", form_values["ONIONHEAVEN_ADDRESS"])
        oh_addr_field.setPlaceholderString_("oheavenfhb...onion")
        oh_addr_field.setFrame_(AppKit.NSMakeRect(input_x, oh_addr_field.frame().origin.y, input_w, 24))
        y -= row_h
        cf_field = add_text_row(y, "Cloudflare Token (optional):", "CLOUDFLARE_TUNNEL_TOKEN", form_values["CLOUDFLARE_TUNNEL_TOKEN"])
        cf_field.setPlaceholderString_("paste tunnel token")
        cf_field.setFrame_(AppKit.NSMakeRect(input_x, cf_field.frame().origin.y, input_w, 24))

        alert.setAccessoryView_(container)

        save_btn = alert.addButtonWithTitle_("Save")
        cancel_btn = alert.addButtonWithTitle_("Cancel")
        save_btn.setKeyEquivalent_("\r")
        cancel_btn.setKeyEquivalent_("\033")  # Escape

        alert.window().setInitialFirstResponder_(prefix_field)

        response = alert.runModal()
        if response != 1000:  # Not "Save"
            return

        # -- Collect new values from form --
        new_values = {}
        sleep_options_map = ["normal", "on-battery", "never"]
        for key in [k for k, _ in SETTINGS_KEYS]:
            widget = fields[key]
            if key == "PREVENT_SLEEP":
                idx = widget.indexOfSelectedItem()
                new_values[key] = sleep_options_map[idx] if 0 <= idx < len(sleep_options_map) else "normal"
            elif key in ("LAUNCH_ON_LOGIN", "UPDATE_ON_LAUNCH",
                         "INSTALL_IA_PLUGIN", "REGISTER_WITH_ONIONHEAVEN",
                         "SHARE_ANALYTICS_WITH_ONIONHOME"):
                new_values[key] = "yes" if widget.state() == AppKit.NSControlStateValueOn else "no"
            else:
                new_values[key] = widget.stringValue().strip()

        # -- Validate prefix --
        #
        # Uses config.validate_address_prefix() rather than re-implementing
        # the rules. The duplicate that used to live here drifted from it:
        # it never explained which digits are invalid in base32 (0, 1, 8, 9),
        # never mentioned that a too-long prefix takes hours or days rather
        # than merely being rejected, and threw away the corrected prefix the
        # validator offers. Keeping one implementation is also what stops the
        # five entry points for this value disagreeing again.
        prefix = new_values["ADDRESS_PREFIX"]
        prefix_ok, prefix_error, prefix_suggestion = \
            op_config.validate_address_prefix(prefix)
        if not prefix_ok:
            if prefix_suggestion:
                prefix_error += f'\n\nSuggested: "{prefix_suggestion}"'
            _icon_alert("Invalid Address Prefix", prefix_error, icon_path)
            form_values = new_values
            # Offer the correction rather than silently reverting to the old
            # value — the user came here to change it.
            form_values["ADDRESS_PREFIX"] = (
                prefix_suggestion or old_values["ADDRESS_PREFIX"])
            continue

        # -- Validate VM memory --
        try:
            mem = int(new_values["VM_MEMORY"])
            if mem < 1:
                raise ValueError
        except ValueError:
            _icon_alert("Invalid VM Memory",
                   "VM memory must be a whole number of at least 1 GB.", icon_path)
            form_values = new_values
            form_values["VM_MEMORY"] = old_values["VM_MEMORY"]
            continue

        # -- Validate VM CPUs --
        try:
            cpus = int(new_values["VM_CPU"])
            if cpus < 1:
                raise ValueError
        except ValueError:
            _icon_alert("Invalid VM CPUs",
                   "VM CPUs must be a whole number of at least 1.", icon_path)
            form_values = new_values
            form_values["VM_CPU"] = old_values["VM_CPU"]
            continue

        # -- Validate OnionHeaven address --
        oh_addr = new_values.get("ONIONHEAVEN_ADDRESS", "").strip()
        if oh_addr and oh_addr != old_values.get("ONIONHEAVEN_ADDRESS", ""):
            log_func(f"Validating OnionHeaven address: {oh_addr}")
            try:
                result = subprocess.run(
                    [launcher_script, "validate-oh-address", oh_addr],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=90
                )
                vr = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
                status = vr.get("status", "")
                message = vr.get("message", "")

                if status == "invalid_format" or status == "self":
                    _icon_alert("Invalid OnionHeaven Address", message, icon_path)
                    form_values = new_values
                    form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
                    continue
                elif status == "site":
                    resp = rumps.alert(
                        title="Not an OnionHeaven Hub",
                        message=f"{message}\n\nUse it anyway?",
                        ok="Use Anyway",
                        cancel="Cancel"
                    )
                    if resp != 1:
                        form_values = new_values
                        form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
                        continue
                    log_func(f"User accepted non-hub address: {oh_addr}")
                elif status == "unreachable":
                    resp = rumps.alert(
                        title="Address Unreachable",
                        message=f"{message}\n\nUse it anyway?",
                        ok="Use Anyway",
                        cancel="Cancel"
                    )
                    if resp != 1:
                        form_values = new_values
                        form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
                        continue
                    log_func(f"User accepted unreachable address: {oh_addr}")
                else:
                    log_func(f"OnionHeaven hub validated: {oh_addr}")
            except Exception as e:
                log_func(f"OnionHeaven address validation error: {e}")

        # Validation passed
        break

    # -- Find changed settings --
    changes = []
    for key, _ in SETTINGS_KEYS:
        if new_values[key] != old_values[key]:
            changes.append(key)

    if not changes:
        _icon_alert("Settings", "No changes.", icon_path)
        return

    # -- Dialog 2: Hazards confirmation --
    change_lines = []
    for key in changes:
        old_v = old_values[key]
        new_v = new_values[key]
        label = _LABELS.get(key, key)

        disp_old = old_v if len(old_v) <= 20 else old_v[:17] + "..."
        disp_new = new_v if len(new_v) <= 20 else new_v[:17] + "..."
        if not disp_old:
            disp_old = "(empty)"
        if not disp_new:
            disp_new = "(empty)"

        line = f"- {label}: {disp_old} \u2192 {disp_new}"

        cons = SETTINGS_CONSEQUENCES.get(key)
        if isinstance(cons, dict):
            if key == "CLOUDFLARE_TUNNEL_TOKEN":
                cons_text = cons.get("set") if new_v else cons.get("cleared")
            else:
                cons_text = cons.get(new_v, "")
        elif isinstance(cons, str):
            cons_text = cons
        else:
            cons_text = ""

        if cons_text:
            line += f"\n  \u26a0 {cons_text}"
        change_lines.append(line)

    hazard_msg = "The following settings will change:\n\n" + "\n\n".join(change_lines)

    hazard = AppKit.NSAlert.alloc().init()
    hazard.setMessageText_("Confirm Changes")
    hazard.setInformativeText_(hazard_msg)

    if icon_path and os.path.exists(icon_path):
        icon2 = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
        if icon2:
            hazard.setIcon_(icon2)

    apply_btn = hazard.addButtonWithTitle_("Apply")
    cancel2 = hazard.addButtonWithTitle_("Cancel")
    cancel2.setKeyEquivalent_("\r")
    apply_btn.setKeyEquivalent_("")

    resp2 = hazard.runModal()
    if resp2 != 1000:  # Not "Apply"
        return

    # -- Write changed values --
    write_config = callbacks.get('write_config')
    for key in changes:
        if write_config:
            write_config(key, new_values[key])

    # Apply sleep mode change immediately (no restart needed)
    if "PREVENT_SLEEP" in changes:
        restart_caffeinate = callbacks.get('restart_caffeinate')
        if restart_caffeinate:
            restart_caffeinate()

    # Apply login item change immediately
    if "LAUNCH_ON_LOGIN" in changes:
        if new_values["LAUNCH_ON_LOGIN"] == "yes":
            add_login = callbacks.get('add_login_item')
            if add_login:
                add_login()
        else:
            remove_login = callbacks.get('remove_login_item')
            if remove_login:
                remove_login()

    # Trigger immediate analytics upload when sharing is turned on
    if "SHARE_ANALYTICS_WITH_ONIONHOME" in changes and new_values["SHARE_ANALYTICS_WITH_ONIONHOME"] == "yes":
        from onionpress.analytics_sharing import trigger_upload
        trigger_upload()

    # Sync config to Docker volume so WordPress settings page sees the update immediately
    sync_to_volume = callbacks.get('sync_to_volume')
    if sync_to_volume:
        sync_to_volume()

    change_details = [f"{k}={new_values[k]}" for k in changes]
    log_func(f"Settings updated: {', '.join(change_details)}")
    saved = AppKit.NSAlert.alloc().init()
    saved.setMessageText_("Settings Saved")
    if set(changes) & _NEEDS_RESTART:
        saved.setInformativeText_(
            "Settings saved. Restart OnionPress from the menu bar "
            "for changes to take effect.")
    else:
        saved.setInformativeText_("Settings saved.")
    if icon_path and os.path.exists(icon_path):
        icon3 = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
        if icon3:
            saved.setIcon_(icon3)
    saved.runModal()
