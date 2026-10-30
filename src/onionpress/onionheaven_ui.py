"""The OnionHeaven screen (macOS).

One dialog that says which hub keeps this site reachable (the configured
hub, whether it is a host the project knows, the registration record,
the last heartbeat, copies of the key an old hub may still hold) and whom
this OnionPress keeps reachable (a hub: the sites in its registry), with
the controls that belong to each: the registration choice and the hub
address; on a hub, the takeover settings.

What it shows comes from the daemon's state file (onionheaven.overview,
published by the daemon's check_status under "onionheaven"); while that is
stale the client half is read from the config and the registration record
in-process (no docker, no network: the menubar is a view). Every change
goes through callbacks['write_config'] (`onionpress config set`), after the
same hazards confirmation as the Settings dialog
(settings_ui.confirm_and_write_changes).
"""

import os
from datetime import datetime, timezone

import AppKit

from . import config as op_config
from . import key_manager
from . import onionheaven
from .settings_ui import (SETTINGS_HELP, _icon_alert, confirm_and_write_changes,
                          probe_hub_address, validate_hub_address)
from .ui_helpers import HelpButtonTarget

# Form definition: (key, default). The choice is on by default; unset shows
# checked (onionheaven.registration_choice).
KEYS = [
    ("REGISTER_WITH_ONIONHEAVEN", "yes"),
    ("ONIONHEAVEN_ADDRESS", onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS),
]
HUB_KEYS = list(onionheaven.HUB_SETTINGS)

HUB_LABELS = {
    "ONIONHEAVEN_PROPAGATION_DELAY": ("Takeover delay (seconds)",
                                      "Seconds after the last heartbeat before this hub takes an address over."),
    "ONIONHEAVEN_MAX_SERVICES": ("Sites per takeover worker",
                                 "Lower uses more containers, each more reliable."),
}

_WIDTH = 700
_MONO = 11


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ago(value, now=None):
    """'40 s ago', '6 min ago', '3 h ago', '2 d ago' for an ISO time; ''
    when there is none."""
    dt = _parse_iso(value)
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return f"{secs} s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def when(value):
    """'14 Sep 2026, 09:12' (local time) for an ISO time; '' when none."""
    dt = _parse_iso(value)
    if dt is None:
        return ""
    return dt.astimezone().strftime("%d %b %Y, %H:%M").lstrip("0")


def client_headline(c):
    """(title, status line) for the 'kept reachable by' section."""
    if c["enabled"] is False:
        return ("This site is not kept reachable by a hub",
                "Disabled: no heartbeats are sent. Visitors get an error whenever this Mac is off.")
    if c["enabled"] is None:
        return ("This site is not kept reachable by a hub",
                f"REGISTER_WITH_ONIONHEAVEN={c['value']!r} is neither yes nor no, so nothing is sent.")
    if c["registered"]:
        beat = ago(c.get("last_heartbeat_at"))
        return ("This site is kept reachable by a hub",
                f"Registered · heartbeat accepted {beat}" if beat else "Registered")
    return ("This site is set to be kept reachable by a hub",
            "Pending: no heartbeat accepted yet")


def hub_badge(c):
    """What the screen says about the configured hub."""
    if c.get("hub_known_as"):
        kind = ""
        if c.get("hub_kind") == "onionhome":
            kind = " (the onion name registry, not an OnionHeaven hub)"
        default = " (default)" if c.get("hub_is_default") else ""
        return (f"Known host · {c['hub_known_as']}{default}{kind}", "known")
    return ("Not a known host · a copy of your key goes to whoever runs it", "unknown")


def hub_headline(h):
    if not h:
        return ("Whether this OnionPress is a hub is not known",
                "The OnionPress service is not running, so the registry could not be asked.")
    if h.get("active"):
        n = h.get("registered_count", 0)
        return (f"This OnionPress is an OnionHeaven hub for {n} site{'s' if n != 1 else ''}",
                "It holds a copy of each registered site's key and takes an address over when its heartbeats stop.")
    return ("This OnionPress is not a hub for other sites",
            "It accepts no registrations and holds no other site's key.")


class _ButtonTarget(AppKit.NSObject):
    """ObjC target for the dialog's buttons: tag -> Python callable."""
    _actions = {}

    def clicked_(self, sender):
        fn = self._actions.get(sender.tag())
        if fn:
            fn()


class _Form:
    """Top-down layout for an NSView: items are added in reading order
    with their heights; build() places them in AppKit's bottom-up frame."""

    def __init__(self, width):
        self.width = width
        self.items = []

    def add(self, make, height, gap=6):
        self.items.append((make, height, gap))

    def build(self):
        total = sum(h + g for _, h, g in self.items) + 4
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.width, total))
        y = total
        for make, h, g in self.items:
            y -= h + g
            make(view, y, h)
        return view


def _label(view, x, y, w, h, text, size=13, bold=False, color=None, mono=False, wrap=False):
    if wrap:
        field = AppKit.NSTextField.wrappingLabelWithString_(text)
    else:
        field = AppKit.NSTextField.labelWithString_(text)
    field.setFrame_(AppKit.NSMakeRect(x, y, w, h))
    if mono:
        font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(_MONO, 0)
    elif bold:
        font = AppKit.NSFont.boldSystemFontOfSize_(size)
    else:
        font = AppKit.NSFont.systemFontOfSize_(size)
    field.setFont_(font)
    if color is not None:
        field.setTextColor_(color)
    view.addSubview_(field)
    return field


def _lines_for(text, width, size=12):
    """Rough line count of *text* wrapped at *width* px."""
    per_line = max(20, int(width / (size * 0.52)))
    lines = 0
    for para in str(text).split("\n"):
        lines += max(1, -(-len(para) // per_line))
    return lines


def _ensure_overview(app, state, log_func):
    """The overview to show: the daemon's (fresh state), else the client
    half from the config and the record, with the hub half unknown."""
    ov = (state or {}).get("onionheaven") if isinstance(state, dict) else None
    if isinstance(ov, dict) and isinstance(ov.get("client"), dict):
        return ov, True
    try:
        return onionheaven.overview(app, hub=False), False
    except Exception as e:
        log_func(f"OnionHeaven screen: could not read the registration record: {e}")
        return {"client": onionheaven.client_overview(app), "hub": None,
                "known_hosts": [dict(h) for h in onionheaven.KNOWN_HOSTS]}, False


def show_onionheaven_dialog(app, state, icon_path, launcher_script, log_func, callbacks):
    """Show the OnionHeaven screen.

    Args:
        app: the menubar app (read_config_value, app_support, onion_address,
            healthcheck_address): the context for the in-process fallback.
        state: the daemon's state document while fresh, else None.
        icon_path: app-icon.png for dialogs.
        launcher_script: the onionpress launcher (validate-oh-address).
        log_func: logging callable.
        callbacks: {'write_config': func(key, value) -> bool}.
    """
    config_path = app.config_file
    if not os.path.exists(config_path):
        _icon_alert("OnionHeaven", "Settings file not found", icon_path)
        return

    ov, live = _ensure_overview(app, state, log_func)
    c = ov["client"]
    h = ov.get("hub")
    hub_active = bool(h and h.get("active"))

    old_values = {}
    for key, default in KEYS:
        old_values[key] = op_config.read_value(config_path, key, default)
    if old_values["REGISTER_WITH_ONIONHEAVEN"].strip() == "":
        old_values["REGISTER_WITH_ONIONHEAVEN"] = "yes"
    if old_values["ONIONHEAVEN_ADDRESS"].strip() == "":
        old_values["ONIONHEAVEN_ADDRESS"] = onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
    hub_keys = HUB_KEYS if hub_active else []
    for key in hub_keys:
        old_values[key] = str((h.get("settings") or {}).get(key)
                              or op_config.read_value(config_path, key, "") or "")

    help_target = HelpButtonTarget.alloc().init()
    help_target._help_texts = {0: SETTINGS_HELP["REGISTER_WITH_ONIONHEAVEN"],
                               1: SETTINGS_HELP["ONIONHEAVEN_ADDRESS"]}
    help_target._icon_path = icon_path
    show_onionheaven_dialog._help_target = help_target
    button_target = _ButtonTarget.alloc().init()
    show_onionheaven_dialog._button_target = button_target

    form_values = dict(old_values)
    gray = AppKit.NSColor.secondaryLabelColor()
    green = AppKit.NSColor.systemGreenColor()
    orange = AppKit.NSColor.systemOrangeColor()

    while True:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("OnionHeaven")
        alert.setInformativeText_(
            "An OnionHeaven hub keeps a .onion address reachable when the computer "
            "behind it is off, asleep or disconnected, by holding a copy of the site's "
            "onion service key and serving the Wayback Machine's copy of the site in "
            "its place.")
        if icon_path and os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        form = _Form(_WIDTH)
        fields = {}
        button_target._actions = {}
        tag = [0]

        def add_button(view, x, y, w, title, fn, h=28):
            btn = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
            btn.setTitle_(title)
            btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
            btn.setTag_(tag[0])
            button_target._actions[tag[0]] = fn
            tag[0] += 1
            btn.setTarget_(button_target)
            btn.setAction_(button_target.clicked_)
            view.addSubview_(btn)
            return btn

        def add_help(view, y, index):
            btn = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(_WIDTH - 26, y, 25, 24))
            btn.setBezelStyle_(9)  # NSBezelStyleHelpButton
            btn.setTag_(index)
            btn.setTarget_(help_target)
            btn.setAction_(help_target.helpClicked_)
            view.addSubview_(btn)

        def heading(text, sub=None):
            form.add(lambda v, y, hh: _label(v, 0, y, _WIDTH, hh, text, size=15, bold=True), 20, 2)
            if sub:
                form.add(lambda v, y, hh: _label(v, 0, y, _WIDTH, hh, sub, size=12, color=gray, wrap=True),
                         16 * _lines_for(sub, _WIDTH), 8)

        def separator():
            def make(v, y, hh):
                box = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(0, y, _WIDTH, 1))
                box.setBoxType_(2)  # NSBoxSeparator
                v.addSubview_(box)
            form.add(make, 1, 12)

        def row(label_text, value, mono=False, color=None, wrap=True):
            lines = _lines_for(value, _WIDTH - 160, _MONO if mono else 13) if wrap else 1
            hh = 16 * lines + 2

            def make(v, y, hh, label_text=label_text, value=value, mono=mono, color=color):
                _label(v, 0, y + hh - 18, 150, 18, label_text, size=12, color=gray)
                _label(v, 150, y, _WIDTH - 150, hh, value, mono=mono, color=color, wrap=True)
            form.add(make, hh, 6)

        def status_line(text, color):
            form.add(lambda v, y, hh: _label(v, 0, y, _WIDTH, hh, text, size=12, bold=True, color=color), 16, 10)

        # ---- kept reachable by ----
        title, status = client_headline(c)
        heading(title, "Your site registers with an OnionHeaven hub and sends it a heartbeat every minute.")
        status_line(status, green if (c["enabled"] is True and c["registered"]) else
                    (orange if c["enabled"] is True else gray))
        if not live:
            status_line("The OnionPress service is not running: what is shown is the saved "
                        "record, and the hub side could not be checked.", orange)
        row("Hub", c["hub"], mono=True)
        badge, kind = hub_badge(c)
        row("", badge, color=green if kind == "known" else orange)
        if c.get("content_address"):
            row("Your site", c["content_address"], mono=True)
        if c.get("healthcheck_address"):
            row("Health check", c["healthcheck_address"], mono=True)
        if c.get("registered_at"):
            row("Registered since", when(c["registered_at"]) or c["registered_at"])
        if c.get("last_heartbeat_at"):
            row("Last heartbeat", f"{when(c['last_heartbeat_at'])} ({ago(c['last_heartbeat_at'])})")
        row("What the hub holds", "A copy of your onion service private key, used only to publish "
            "your address while your site is offline. Turning registration off asks the hub to "
            "delete it.")

        for entry in c.get("left_on_hubs") or []:
            who = entry["onionheaven_address"]
            if entry.get("hub_known_as"):
                who += f" ({entry['hub_known_as']})"
            if entry.get("key_here"):
                what = (f"A copy of your key may still be held by {who}: "
                        f"{entry.get('reason') or 'left behind'}, since {when(entry.get('since')) or '?'}. "
                        f"OnionPress asks it to delete the copy every {c.get('retry_minutes', 15)} min.")
            else:
                what = (f"A copy of the key of {entry['content_address']} (an address this install "
                        f"no longer has) may still be held by {who}. OnionPress cannot ask for its "
                        "deletion; ask the hub's operator.")
            row("Key copy elsewhere", what, color=orange)

        # controls
        def make_check(v, y, hh):
            cb = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, y, _WIDTH - 40, 24))
            cb.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb.setTitle_("Keep this site reachable via OnionHeaven (recommended)")
            cb.setState_(AppKit.NSControlStateValueOn
                         if form_values["REGISTER_WITH_ONIONHEAVEN"].lower() == "yes"
                         else AppKit.NSControlStateValueOff)
            v.addSubview_(cb)
            fields["REGISTER_WITH_ONIONHEAVEN"] = cb
            add_help(v, y, 0)
        form.add(make_check, 24, 8)

        def make_hub_field(v, y, hh):
            _label(v, 0, y + 3, 150, 18, "Hub address:", size=12, color=gray)
            field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(150, y, _WIDTH - 150 - 30, 24))
            field.setStringValue_(form_values["ONIONHEAVEN_ADDRESS"])
            field.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(_MONO, 0))
            field.setPlaceholderString_("oheavenfhb...onion")
            v.addSubview_(field)
            fields["ONIONHEAVEN_ADDRESS"] = field
            add_help(v, y, 1)
        form.add(make_hub_field, 24, 8)

        def use_default():
            fields["ONIONHEAVEN_ADDRESS"].setStringValue_(onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS)

        def check_now():
            addr = fields["ONIONHEAVEN_ADDRESS"].stringValue().strip()
            if not key_manager.is_valid_onion_address(addr):
                _icon_alert("Check Hub", "Address must be a valid 56-character .onion address.", icon_path)
                return
            status, message = probe_hub_address(addr, launcher_script, log_func)
            verdict = {"hub": "Answers as an OnionHeaven hub.",
                       "site": "Answers, but not as an OnionHeaven hub.",
                       "unreachable": "Did not answer over Tor.",
                       "self": "That is this site's own address.",
                       "invalid_format": "Not a valid address.",
                       "error": "The check could not run."}.get(status, status)
            host = onionheaven.known_host(addr)
            known = f"\nKnown host: {host['name']}." if host else "\nNot a host OnionPress knows."
            _icon_alert("Check Hub", f"{addr}\n\n{verdict}{known}\n\n{message}".rstrip(), icon_path)

        def make_hub_buttons(v, y, hh):
            add_button(v, 150, y, 250, "Use the Internet Archive's hub", use_default)
            add_button(v, 410, y, 130, "Check hub now", check_now)
        form.add(make_hub_buttons, 28, 4)
        form.add(lambda v, y, hh: _label(v, 150, y, _WIDTH - 150, hh,
                                         "A hub change takes effect when OnionPress restarts; the old hub "
                                         "is asked to delete its copy of the key.", size=11, color=gray, wrap=True),
                 30, 6)

        separator()

        # ---- serving as a hub ----
        title, sub = hub_headline(h)
        heading(title, sub)
        if h and h.get("active"):
            status_line("Accepting registrations" if h.get("accepting_registrations")
                        else "Serving the canonical hub address", green)
            if h.get("address"):
                who = h.get("known_as")
                row("Hub address", h["address"] + (f"\nKnown host · this is {who}" if who else ""), mono=True)
            counts = (f"{h.get('registered_count', 0)} registered · {h.get('online_count', 0)} online · "
                      f"{h.get('taken_over_count', 0)} taken over · "
                      f"{h.get('takeover_containers', 0)} takeover worker(s)")
            if h.get("counts_unavailable"):
                counts = "The registry did not answer."
            row("Right now", counts)
            sites = h.get("sites") or []
            if sites or h.get("sites_unavailable"):
                lines = []
                for s in sites:
                    beat = ago(s.get("last_healthy")) or "-"
                    wp = "" if s.get("wordpress_healthy") in (None, True) else " · WordPress unhealthy"
                    lines.append(f"{s['content_address']}  {s['status']:<11} {beat:<10} {s.get('version') or '-'}{wp}")
                if h.get("sites_truncated"):
                    lines.append(f"... first {h.get('sites_limit', len(sites))} of {h.get('registered_count', 0)}")
                text = "\n".join(lines) if lines else "The registry did not answer."
                table_h = min(220, 16 * (len(lines) + 1) + 8)

                def make_table(v, y, hh, text=text):
                    scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, y, _WIDTH, hh))
                    scroll.setHasVerticalScroller_(True)
                    scroll.setBorderType_(1)  # NSLineBorder
                    tv = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, _WIDTH - 20, hh))
                    tv.setEditable_(False)
                    tv.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(_MONO, 0))
                    tv.setString_("site                                                      status      last beat  version\n" + text)
                    tv.setMinSize_(AppKit.NSMakeSize(0, hh))
                    tv.setVerticallyResizable_(True)
                    tv.setHorizontallyResizable_(False)
                    scroll.setDocumentView_(tv)
                    v.addSubview_(scroll)
                form.add(lambda v, y, hh: _label(v, 0, y, _WIDTH, hh, "Sites this hub keeps reachable",
                                                 size=12, bold=True), 16, 4)
                form.add(make_table, table_h, 10)
            for key in hub_keys:
                label_text, explain = HUB_LABELS[key]

                def make_num(v, y, hh, key=key, label_text=label_text, explain=explain):
                    _label(v, 0, y + hh - 18, 300, 18, label_text, size=12)
                    _label(v, 0, y, 380, hh - 18, explain, size=11, color=gray, wrap=True)
                    field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(400, y + hh - 24, 90, 24))
                    field.setStringValue_(form_values.get(key, ""))
                    field.setAlignment_(AppKit.NSTextAlignmentRight)
                    v.addSubview_(field)
                    fields[key] = field
                form.add(make_num, 36, 6)
            form.add(lambda v, y, hh: _label(
                v, 0, y, _WIDTH, hh,
                "Accepting registrations is set by hand (ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes in "
                "~/.onionpress/config) and cannot be switched off here: the registered sites would "
                "lose their fallback.", size=11, color=gray, wrap=True), 32, 6)
        elif h:
            form.add(lambda v, y, hh: _label(
                v, 0, y, _WIDTH, hh,
                "Running a hub is a deliberate, always-on role: it holds other people's keys. It is "
                "turned on by hand, with ONIONHEAVEN_ACCEPT_REGISTRATIONS=yes in ~/.onionpress/config, "
                "and needs a machine that never sleeps.", size=12, color=gray, wrap=True), 48, 6)

        separator()

        # ---- known hosts ----
        form.add(lambda v, y, hh: _label(v, 0, y, _WIDTH, hh, "KNOWN HOSTS", size=11, bold=True, color=gray), 14, 4)
        for host in ov.get("known_hosts") or []:
            note = f" — {host['note']}" if host.get("note") else ""
            own = " (this node)" if h and h.get("address") == host["address"] else ""
            row(host["name"].replace("the Internet Archive's ", "Internet Archive "),
                f"{host['address']}{own}{note}", mono=True)

        alert.setAccessoryView_(form.build())
        save_btn = alert.addButtonWithTitle_("Save")
        cancel_btn = alert.addButtonWithTitle_("Cancel")
        save_btn.setKeyEquivalent_("\r")
        cancel_btn.setKeyEquivalent_("\033")

        response = alert.runModal()
        if response != 1000:
            return

        new_values = {}
        cb = fields["REGISTER_WITH_ONIONHEAVEN"]
        new_values["REGISTER_WITH_ONIONHEAVEN"] = (
            "yes" if cb.state() == AppKit.NSControlStateValueOn else "no")
        addr = fields["ONIONHEAVEN_ADDRESS"].stringValue().strip()
        new_values["ONIONHEAVEN_ADDRESS"] = addr or onionheaven.DEFAULT_ONIONHEAVEN_ADDRESS
        for key in hub_keys:
            new_values[key] = fields[key].stringValue().strip()

        # -- Validate --
        ok = True
        for key in hub_keys:
            check = op_config.SETTINGS_PAGE_KEYS[key]
            if new_values[key] != old_values[key] and not check(new_values[key]):
                _icon_alert("Invalid Value",
                            f"{HUB_LABELS[key][0]} must be {check.describe}.", icon_path)
                form_values = dict(new_values)
                form_values[key] = old_values[key]
                ok = False
                break
        if not ok:
            continue
        if (new_values["ONIONHEAVEN_ADDRESS"] != old_values["ONIONHEAVEN_ADDRESS"]
                and not validate_hub_address(new_values["ONIONHEAVEN_ADDRESS"],
                                             launcher_script, log_func, icon_path)):
            form_values = dict(new_values)
            form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
            continue
        break

    confirm_and_write_changes([k for k, _ in KEYS] + hub_keys, old_values, new_values,
                              callbacks, log_func, icon_path)
