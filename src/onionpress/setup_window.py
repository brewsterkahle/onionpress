#!/usr/bin/env python3
"""
OnionPress Setup Progress Window — safe implementation.

Uses only standard AppKit controls (NSTextField, NSProgressIndicator,
NSButton, NSScrollView/NSTextView).  No custom drawRect_, no CGColor
layer styling, no NSTimer animations.  Clean native macOS appearance
with system fonts and standard colors.

Two phases:
  1. Welcome — collects Site Title, Username, Password then starts setup
  2. Progress — step checklist, progress bar, live log tail
"""

import AppKit
from AppKit import (
    NSWindow, NSView, NSTextField, NSSecureTextField, NSProgressIndicator,
    NSButton, NSImage, NSImageView, NSFont, NSColor, NSMakeRect,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBackingStoreBuffered, NSCenterTextAlignment, NSLeftTextAlignment,
    NSLineBreakByWordWrapping, NSApp, NSScrollView, NSTextView,
    NSProgressIndicatorStyleBar,
)
import objc
import threading
import os

try:
    from onionpress import setup_logic as _setup_logic
except ImportError:
    _setup_logic = None


# ---------------------------------------------------------------------------
# Standard macOS colors
# ---------------------------------------------------------------------------

_TEXT_SECONDARY   = NSColor.secondaryLabelColor()
_GREEN            = NSColor.systemGreenColor()


def _bold(size):
    return NSFont.boldSystemFontOfSize_(size)

def _sys(size):
    return NSFont.systemFontOfSize_(size)


def _label(frame, text, font=None, color=None, align=NSLeftTextAlignment, wrap=False):
    """Create a read-only NSTextField (label)."""
    tf = NSTextField.alloc().initWithFrame_(frame)
    tf.setStringValue_(text)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setAlignment_(align)
    if font:
        tf.setFont_(font)
    if color:
        tf.setTextColor_(color)
    if wrap:
        tf.setLineBreakMode_(NSLineBreakByWordWrapping)
    return tf


def _input_field(frame, placeholder="", secure=False):
    """Create an editable NSTextField (or NSSecureTextField)."""
    cls = NSSecureTextField if secure else NSTextField
    tf = cls.alloc().initWithFrame_(frame)
    tf.setPlaceholderString_(placeholder)
    tf.setBezeled_(True)
    tf.setDrawsBackground_(True)
    tf.setEditable_(True)
    tf.setSelectable_(True)
    tf.setFont_(NSFont.systemFontOfSize_(13))
    return tf


def _logo_path():
    """Path to logo.png (bundle or dev tree)."""
    try:
        bundle = AppKit.NSBundle.mainBundle()
        if bundle and bundle.resourcePath():
            p = os.path.join(bundle.resourcePath(), "logo.png")
            if os.path.exists(p):
                return p
            p = os.path.join(bundle.resourcePath(), "assets", "branding", "logo.png")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(script_dir)
    for candidate in [
        os.path.join(root, "assets", "branding", "logo.png"),
        os.path.join(root, "logo.png"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

STEPS = [
    "Checking system requirements",
    "Preparing your site",
    "Downloading container images",
    "Generating .onion address",
    "Starting WordPress + Tor",
    "Checking reachability",
    "Starting heartbeat",
    "Opening tor-enabled browser",
]

def _default_site_title():
    if _setup_logic is not None:
        return _setup_logic.default_site_title()
    return "My OnionPress Site"


_MARK_DONE    = "\u2713"   # ✓
_MARK_ACTIVE  = "\u27F3"  # ⟳
_MARK_PENDING = "\u00B7"  # ·


# ---------------------------------------------------------------------------
# Main window class
# ---------------------------------------------------------------------------

class SetupProgressWindow(AppKit.NSObject):
    """Safe setup progress window using only standard AppKit controls."""

    def init(self):
        self = objc.super(SetupProgressWindow, self).init()
        if self is None:
            return None
        self.window = None
        self.welcome_view = None    # Phase 1: credentials input
        self.progress_view = None   # Phase 2: step checklist + progress
        self.step_labels = []       # NSTextField per step
        self.progress_bar = None    # NSProgressIndicator
        self.percent_label = None   # NSTextField  "55%"
        self.status_label = None    # NSTextField  status line
        self.log_text_view = None   # NSTextView   log tail
        self.current_step = -1
        self._log_file_path = os.path.expanduser("~/.onionpress/onionpress.log")
        # Credentials from welcome phase
        self.site_title = _default_site_title()
        # Onionname starts empty — the user types their own name first.
        # A "Suggest" button offers random adjective-noun alternatives.
        self.admin_user = ""
        self.admin_pass = ""
        self._title_field = None
        self._user_field = None
        self._user_hint = None           # inline validation label
        self._pass_field = None
        self.language = "en_US"
        # "wordpress" (default) or "static" — chosen once, at setup, via the
        # segmented control at the top of the welcome view. Immutable after
        # setup; see src/onionpress/config.py's SITE_TYPE.
        self.site_type = "wordpress"
        self._wp_only_views = []  # hidden when site_type == "static"
        self._on_setup_callback = None  # Called when user clicks "Set Up"
        self._showing_welcome = True
        return self

    # -- window creation ----------------------------------------------------

    def create_window(self):
        width, height = 480, 690
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("OnionPress Setup")
        self.window.center()
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        self.window.setReleasedWhenClosed_(False)
        self.window.setHidesOnDeactivate_(False)

        content = self.window.contentView()
        self._create_welcome_view(content, width, height)
        self._create_progress_view(content, width, height)

        # Start showing welcome
        self.welcome_view.setHidden_(False)
        self.progress_view.setHidden_(True)
        self._showing_welcome = True

    def _create_welcome_view(self, content, width, height):
        """Phase 1: Logo + credential fields + Set Up button."""
        self.welcome_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(self.welcome_view)

        y = height - 20

        # -- Logo --
        logo_path = _logo_path()
        if logo_path:
            logo_image = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if logo_image:
                logo_h = 140
                logo_w = 168
                y -= logo_h
                logo_view = NSImageView.alloc().initWithFrame_(
                    NSMakeRect((width - logo_w) / 2, y, logo_w, logo_h)
                )
                logo_view.setImage_(logo_image)
                logo_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                self.welcome_view.addSubview_(logo_view)
                y -= 8

        # -- Title --
        y -= 28
        title = _label(
            NSMakeRect(20, y, width - 40, 28),
            "Welcome to OnionPress",
            font=_bold(18), color=NSColor.labelColor(),
            align=NSCenterTextAlignment,
        )
        self.welcome_view.addSubview_(title)

        # -- Subtitle --
        y -= 20
        subtitle = _label(
            NSMakeRect(20, y, width - 40, 18),
            "Set up your site and admin account",
            font=_sys(13), color=_TEXT_SECONDARY,
            align=NSCenterTextAlignment,
        )
        self.welcome_view.addSubview_(subtitle)

        y -= 30  # spacing

        # -- Form fields --
        label_x = 40
        field_x = 180
        field_w = width - field_x - 40

        # -- Site type: WordPress, or bring your own static site --
        seg_w = field_w
        self._site_type_seg = AppKit.NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(field_x, y - 2, seg_w, 24)
        )
        self._site_type_seg.setSegmentCount_(2)
        self._site_type_seg.setLabel_forSegment_("WordPress", 0)
        self._site_type_seg.setLabel_forSegment_("Bring your own static site", 1)
        self._site_type_seg.setSelectedSegment_(0)
        try:
            self._site_type_seg.setSegmentStyle_(AppKit.NSSegmentStyleRounded)
        except Exception:
            pass
        self._site_type_seg.setTarget_(self)
        self._site_type_seg.setAction_(
            objc.selector(self.siteTypeChanged_, signature=b'v@:@'))
        self.welcome_view.addSubview_(_label(
            NSMakeRect(label_x, y, 130, 20),
            "Publish with",
            font=_bold(13), color=NSColor.labelColor(),
        ))
        self.welcome_view.addSubview_(self._site_type_seg)
        y -= 18
        self.welcome_view.addSubview_(_label(
            NSMakeRect(field_x, y, field_w, 14),
            "Static: publish files you built yourself (Hugo, Jekyll, plain HTML, ...).",
            font=_sys(10), color=_TEXT_SECONDARY,
        ))

        y -= 30  # spacing

        # Site Title
        y -= 24
        title_label = _label(
            NSMakeRect(label_x, y, 130, 20),
            "Site Title",
            font=_bold(13), color=NSColor.labelColor(),
        )
        self.welcome_view.addSubview_(title_label)
        self._title_field = _input_field(
            NSMakeRect(field_x, y - 2, field_w, 24),
            placeholder=_default_site_title(),
        )
        self._title_field.setStringValue_(_default_site_title())
        self.welcome_view.addSubview_(self._title_field)
        self._wp_only_views += [title_label, self._title_field]

        # Onionname — the human-readable handle that OnionHome maps back to
        # this site's .onion address. Doubles as the WordPress admin username.
        y -= 40
        self.welcome_view.addSubview_(_label(
            NSMakeRect(label_x, y, 130, 20),
            "Onionname",
            font=_bold(13), color=NSColor.labelColor(),
        ))
        suggest_w = 100
        user_field_w = field_w - suggest_w - 6
        self._user_field = _input_field(
            NSMakeRect(field_x, y - 2, user_field_w, 24),
            placeholder="Your OnionName",
        )
        self.welcome_view.addSubview_(self._user_field)

        refresh_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(field_x + user_field_w + 6, y - 2, suggest_w, 24)
        )
        refresh_btn.setTitle_("Suggest")
        refresh_btn.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "arrow.clockwise", "Suggest a random onionname"
        ))
        refresh_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        refresh_btn.setImagePosition_(AppKit.NSImageLeading)
        refresh_btn.setTarget_(self)
        refresh_btn.setAction_(objc.selector(self.regenerateOnionname_, signature=b'v@:@'))
        self.welcome_view.addSubview_(refresh_btn)

        # Inline validation hint (hidden unless there's an error).
        y -= 18
        self._user_hint = _label(
            NSMakeRect(field_x, y, field_w, 14),
            "",
            font=_sys(10), color=NSColor.systemRedColor(),
        )
        self._user_hint.setHidden_(True)
        self.welcome_view.addSubview_(self._user_hint)

        # Password
        y -= 22
        password_label = _label(
            NSMakeRect(label_x, y, 130, 20),
            "Password",
            font=_bold(13), color=NSColor.labelColor(),
        )
        self.welcome_view.addSubview_(password_label)
        self._wp_only_views.append(password_label)
        eye_w = 36
        pass_frame = NSMakeRect(field_x, y - 2, field_w - eye_w - 6, 24)
        # Visible field (hidden by default; shown when user clicks eye)
        self._pass_field = _input_field(pass_frame, placeholder="Choose a password")
        self._pass_field.setHidden_(True)
        self.welcome_view.addSubview_(self._pass_field)
        # Secure field (shown by default — dots)
        self._pass_field_secure = _input_field(pass_frame, placeholder="Choose a password", secure=True)
        self.welcome_view.addSubview_(self._pass_field_secure)
        self._pass_visible = False
        # Eye toggle button
        eye_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(field_x + field_w - eye_w, y - 2, eye_w, 24)
        )
        eye_btn.setTitle_("")
        eye_btn.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_("eye.fill", "Show password"))
        eye_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        eye_btn.setImagePosition_(AppKit.NSImageOnly)
        eye_btn.setTarget_(self)
        eye_btn.setAction_(objc.selector(self.togglePasswordVisibility_, signature=b'v@:@'))
        self.welcome_view.addSubview_(eye_btn)
        self._wp_only_views += [self._pass_field, self._pass_field_secure, eye_btn]

        # Password hint
        y -= 20
        password_hint = _label(
            NSMakeRect(field_x, y, field_w, 16),
            "Save this password somewhere safe.",
            font=_sys(10), color=_TEXT_SECONDARY,
        )
        self.welcome_view.addSubview_(password_hint)
        self._wp_only_views.append(password_hint)

        y -= 30

        # -- Share analytics checkbox --
        self._analytics_check = NSButton.alloc().initWithFrame_(
            NSMakeRect(field_x, y, field_w, 18)
        )
        self._analytics_check.setButtonType_(AppKit.NSButtonTypeSwitch)
        self._analytics_check.setTitle_("Share diagnostic logs with OnionHome")
        self._analytics_check.setFont_(_sys(12))
        self._analytics_check.setState_(AppKit.NSControlStateValueOff)
        self.welcome_view.addSubview_(self._analytics_check)

        y -= 16
        self.welcome_view.addSubview_(_label(
            NSMakeRect(field_x + 18, y, field_w - 18, 14),
            "Helps the OnionPress project diagnose issues.",
            font=_sys(10), color=_TEXT_SECONDARY,
        ))

        y -= 30

        # -- Language selector --
        language_label = _label(
            NSMakeRect(label_x, y, 130, 20),
            "Language",
            font=_bold(13), color=NSColor.labelColor(),
        )
        self.welcome_view.addSubview_(language_label)
        self._wp_only_views.append(language_label)
        self._language_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(field_x, y - 2, field_w, 24), False
        )
        self._language_popup.setFont_(_sys(13))
        _languages = [
            ("English", "en_US"),
            ("Fran\u00e7ais", "fr_FR"),
            ("Espa\u00f1ol", "es_ES"),
            ("Deutsch", "de_DE"),
            ("Nederlands", "nl_NL"),
            ("Portugu\u00eas", "pt_BR"),
            ("\u65e5\u672c\u8a9e", "ja"),
            ("\u4e2d\u6587", "zh_CN"),
            ("\u0627\u0644\u0639\u0631\u0628\u064a\u0629", "ar"),
        ]
        self._language_codes = [code for _, code in _languages]
        for name, _ in _languages:
            self._language_popup.addItemWithTitle_(name)
        self.welcome_view.addSubview_(self._language_popup)
        self._wp_only_views.append(self._language_popup)

        y -= 30
        # -- Address prefix (advanced): choose the .onion vanity prefix now so
        # it's generated once at install (default op2; longer prefixes take
        # exponentially longer to generate). Restore-from-backup ignores this —
        # the address comes from the backup.
        self.welcome_view.addSubview_(_label(
            NSMakeRect(label_x, y, 130, 20),
            "Address prefix",
            font=_bold(13), color=NSColor.labelColor(),
        ))
        self._prefix_field = _input_field(
            NSMakeRect(field_x, y - 2, field_w, 24), placeholder="op2")
        self._prefix_field.setStringValue_("op2")
        self.welcome_view.addSubview_(self._prefix_field)
        y -= 18
        self.welcome_view.addSubview_(_label(
            NSMakeRect(field_x, y, field_w, 14),
            "Your .onion starts with this. Longer prefixes take much longer to make.",
            font=_sys(10), color=_TEXT_SECONDARY,
        ))

        y -= 40  # spacing

        # -- Setup button --
        btn_w = 220
        setup_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((width - btn_w) / 2, y, btn_w, 32)
        )
        setup_btn.setTitle_("Setup OnionPress")
        setup_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        setup_btn.setFont_(_bold(14))
        setup_btn.setTarget_(self)
        setup_btn.setAction_(objc.selector(self.setupClicked_, signature=b'v@:@'))
        setup_btn.setKeyEquivalent_("\r")
        # Force blue appearance even when window is not key
        setup_btn.setWantsLayer_(True)
        setup_btn.setBordered_(False)
        blue = NSColor.systemBlueColor()
        setup_btn.layer().setBackgroundColor_(blue.CGColor())
        setup_btn.layer().setCornerRadius_(7)
        # White text via attributed title
        attrs = {
            AppKit.NSFontAttributeName: _bold(14),
            AppKit.NSForegroundColorAttributeName: NSColor.whiteColor(),
        }
        attr_title = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "Setup OnionPress", attrs
        )
        setup_btn.setAttributedTitle_(attr_title)
        self.welcome_view.addSubview_(setup_btn)

        y -= 26

        # -- Secondary action: restore from an existing backup --
        # Builds this install directly from a backup (original op2\u2026 address +
        # content) instead of creating a new site \u2014 see install-from-backup.
        restore_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((width - btn_w) / 2, y, btn_w, 20)
        )
        restore_btn.setTitle_("Restore from backup\u2026")
        restore_btn.setBezelStyle_(AppKit.NSBezelStyleInline)
        restore_btn.setBordered_(False)
        restore_btn.setFont_(_sys(12))
        restore_btn.setTarget_(self)
        restore_btn.setAction_(
            objc.selector(self.restoreFromBackupClicked_, signature=b'v@:@'))
        self.welcome_view.addSubview_(restore_btn)

        y -= 20

        # -- Estimated time --
        self.welcome_view.addSubview_(_label(
            NSMakeRect(20, y, width - 40, 16),
            "Setup takes about 3\u20135 minutes",
            font=_sys(11), color=_TEXT_SECONDARY,
            align=NSCenterTextAlignment,
        ))

        # Tab order: title → onionname → password → analytics → language → setup
        self._title_field.setNextKeyView_(self._user_field)
        self._user_field.setNextKeyView_(self._pass_field_secure)
        self._pass_field.setNextKeyView_(self._analytics_check)
        self._pass_field_secure.setNextKeyView_(self._analytics_check)
        self._analytics_check.setNextKeyView_(self._language_popup)
        self._language_popup.setNextKeyView_(setup_btn)
        setup_btn.setNextKeyView_(self._title_field)
        self.window.setInitialFirstResponder_(self._title_field)

    def _create_progress_view(self, content, width, height):
        """Phase 2: Step checklist + progress bar + log area."""
        self.progress_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(self.progress_view)

        y = height - 20

        # -- Logo (smaller) --
        logo_path = _logo_path()
        if logo_path:
            logo_image = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if logo_image:
                logo_h = 80
                logo_w = 100
                y -= logo_h
                logo_view = NSImageView.alloc().initWithFrame_(
                    NSMakeRect((width - logo_w) / 2, y, logo_w, logo_h)
                )
                logo_view.setImage_(logo_image)
                logo_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                self.progress_view.addSubview_(logo_view)
                y -= 4

        # -- Title --
        y -= 24
        title = _label(
            NSMakeRect(20, y, width - 40, 24),
            "Setting Up Your Onion Service",
            font=_bold(15), color=NSColor.labelColor(),
            align=NSCenterTextAlignment,
        )
        self.progress_view.addSubview_(title)

        y -= 12  # spacing

        # -- Step checklist --
        self.step_labels = []
        for i, step_text in enumerate(STEPS):
            y -= 18
            mark = _MARK_PENDING
            text = f"  {mark}  {step_text}"
            lbl = _label(
                NSMakeRect(40, y, width - 80, 16),
                text,
                font=_sys(12), color=_TEXT_SECONDARY,
            )
            self.progress_view.addSubview_(lbl)
            self.step_labels.append(lbl)

        y -= 12  # spacing

        # -- Progress bar --
        y -= 20
        self.progress_bar = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(40, y, width - 120, 20)
        )
        self.progress_bar.setStyle_(NSProgressIndicatorStyleBar)
        self.progress_bar.setMinValue_(0)
        self.progress_bar.setMaxValue_(100)
        self.progress_bar.setDoubleValue_(0)
        self.progress_bar.setIndeterminate_(False)
        self.progress_view.addSubview_(self.progress_bar)

        self.percent_label = _label(
            NSMakeRect(width - 72, y, 50, 18),
            "0%",
            font=_sys(12), color=_TEXT_SECONDARY,
            align=NSLeftTextAlignment,
        )
        self.progress_view.addSubview_(self.percent_label)

        y -= 6  # spacing

        # -- Status line --
        y -= 16
        self.status_label = _label(
            NSMakeRect(40, y, width - 80, 14),
            "Initializing...",
            font=_sys(11), color=NSColor.labelColor(),
            align=NSCenterTextAlignment,
        )
        self.progress_view.addSubview_(self.status_label)

        y -= 8  # spacing

        # -- Log tail area --
        log_h = max(80, y - 50)
        y -= log_h
        log_scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(40, y, width - 80, log_h)
        )
        log_scroll.setHasVerticalScroller_(True)
        log_scroll.setBorderType_(AppKit.NSBezelBorder)

        self.log_text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width - 80 - 15, log_h)
        )
        self.log_text_view.setEditable_(False)
        self.log_text_view.setSelectable_(True)
        self.log_text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(10, 0.0))
        self.log_text_view.setTextColor_(NSColor.labelColor())
        self.log_text_view.setBackgroundColor_(NSColor.textBackgroundColor())
        self.log_text_view.setString_("Waiting for log entries...")
        self.log_text_view.setVerticallyResizable_(True)
        self.log_text_view.setHorizontallyResizable_(False)
        self.log_text_view.textContainer().setWidthTracksTextView_(True)

        log_scroll.setDocumentView_(self.log_text_view)
        self.progress_view.addSubview_(log_scroll)

        y -= 8  # spacing

        # -- Buttons --
        y -= 32
        view_log_btn = NSButton.alloc().initWithFrame_(NSMakeRect(40, y, 130, 32))
        view_log_btn.setTitle_("View Log")
        view_log_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        view_log_btn.setTarget_(self)
        view_log_btn.setAction_(objc.selector(self.viewLogClicked_, signature=b'v@:@'))
        self.progress_view.addSubview_(view_log_btn)

        dismiss_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 170, y, 130, 32))
        dismiss_btn.setTitle_("Dismiss")
        dismiss_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        dismiss_btn.setTarget_(self)
        dismiss_btn.setAction_(objc.selector(self.dismissClicked_, signature=b'v@:@'))
        self.progress_view.addSubview_(dismiss_btn)

    # -- button handlers ----------------------------------------------------

    def togglePasswordVisibility_(self, sender):
        """Toggle between visible and secure password fields."""
        if self._pass_visible:
            # Switch to secure: copy text, hide visible, show secure
            pw = self._pass_field.stringValue()
            self._pass_field_secure.setStringValue_(pw)
            self._pass_field.setHidden_(True)
            self._pass_field_secure.setHidden_(False)
            self._pass_field_secure.becomeFirstResponder()
            self._pass_visible = False
        else:
            # Switch to visible: copy text, hide secure, show visible
            pw = self._pass_field_secure.stringValue()
            self._pass_field.setStringValue_(pw)
            self._pass_field_secure.setHidden_(True)
            self._pass_field.setHidden_(False)
            self._pass_field.becomeFirstResponder()
            self._pass_visible = True

    def siteTypeChanged_(self, sender):
        """Toggle between WordPress and static-site mode.

        Static mode has no site title/admin-password/language concept
        (no wp-cli, no database) — hide those fields rather than validate
        and then ignore them.
        """
        is_static = self._site_type_seg.selectedSegment() == 1
        self.site_type = "static" if is_static else "wordpress"
        for view in self._wp_only_views:
            view.setHidden_(is_static)

    def regenerateOnionname_(self, sender):
        """Pick a fresh local adjective-noun suggestion in the current language."""
        if _setup_logic is None:
            return
        try:
            lang_idx = self._language_popup.indexOfSelectedItem()
        except Exception:
            lang_idx = -1
        lang = self._language_codes[lang_idx] if lang_idx >= 0 else "en_US"
        name = _setup_logic.suggest_onionname(lang)
        if name and self._user_field:
            self._user_field.setStringValue_(name)
            self.admin_user = name
            if self._user_hint:
                self._user_hint.setHidden_(True)

    def _show_user_hint(self, message):
        if self._user_hint:
            self._user_hint.setStringValue_(message)
            self._user_hint.setHidden_(False)

    def setupClicked_(self, sender):
        """User clicked Set Up — save credentials and switch to progress view."""
        # Read field values
        if self._title_field:
            self.site_title = self._title_field.stringValue() or _default_site_title()
        if self._user_field:
            self.admin_user = (self._user_field.stringValue() or "").strip()
        # Read from whichever password field is visible
        if self._pass_visible:
            self.admin_pass = self._pass_field.stringValue() or ""
        else:
            self.admin_pass = self._pass_field_secure.stringValue() or ""

        # Address prefix (advanced) — default op2. The launcher validates it
        # (base32, <=5 chars) and falls back to op2 if it's invalid.
        if getattr(self, "_prefix_field", None):
            p = (self._prefix_field.stringValue() or "").strip().lower()
            self.address_prefix = p or "op2"
        else:
            self.address_prefix = "op2"

        # Validate required fields
        missing = False
        if not self.admin_user:
            red_placeholder = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                "Choose an onionname", {
                    AppKit.NSForegroundColorAttributeName: NSColor.systemRedColor(),
                    AppKit.NSFontAttributeName: _sys(13),
                })
            self._user_field.setPlaceholderAttributedString_(red_placeholder)
            missing = True
        elif _setup_logic is not None:
            ok, reason = _setup_logic.validate_onionname(self.admin_user)
            if not ok:
                self._show_user_hint({
                    "too_short": "Onionname must be at least 5 characters.",
                    "too_long":  "Onionname must be 40 characters or fewer.",
                    "invalid_chars": "Use only letters, digits, '.', '_' or '-' "
                                     "(start and end with a letter or digit).",
                    "all_numeric": "Onionname cannot be all digits.",
                }.get(reason, f"Onionname is invalid ({reason})."))
                missing = True
            else:
                self._show_user_hint("")  # clear any prior error
                self._user_hint.setHidden_(True)
        # Static-site installs have no admin password (no wp-cli, no
        # database) — only require one for WordPress installs.
        if self.site_type != "static" and not self.admin_pass:
            red_placeholder = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                "Choose a password", {
                    AppKit.NSForegroundColorAttributeName: NSColor.systemRedColor(),
                    AppKit.NSFontAttributeName: _sys(13),
                })
            active_field = self._pass_field if self._pass_visible else self._pass_field_secure
            active_field.setPlaceholderAttributedString_(red_placeholder)
            missing = True
        if missing:
            return

        # Save analytics preference
        if self._analytics_check.state() == AppKit.NSControlStateValueOn:
            self.share_analytics = "yes"
        else:
            self.share_analytics = "no"

        # Save language choice
        lang_idx = self._language_popup.indexOfSelectedItem()
        self.language = self._language_codes[lang_idx] if lang_idx >= 0 else "en_US"

        # Switch to progress view
        self._showing_welcome = False
        if self.welcome_view:
            self.welcome_view.setHidden_(True)
        if self.progress_view:
            self.progress_view.setHidden_(False)

        # Fire callback to start setup
        if self._on_setup_callback:
            threading.Thread(target=self._on_setup_callback, daemon=True).start()

    def restoreFromBackupClicked_(self, sender):
        """Secondary welcome action: build this install from an existing backup
        instead of creating a new site. Pick a backup zip + password, validate
        the password up front, then hand off the same setup callback in restore
        mode (menubar._first_run_after_welcome branches on self.restore_mode)."""
        import os
        # 1. Pick the backup zip (default to ~/OnionPress/backups).
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setTitle_("Choose an OnionPress backup")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        backups_dir = os.path.expanduser("~/OnionPress/backups")
        if os.path.isdir(backups_dir):
            panel.setDirectoryURL_(AppKit.NSURL.fileURLWithPath_(backups_dir))
        try:
            panel.setAllowedContentTypes_(
                [AppKit.UTType.typeWithFilenameExtension_("zip")])
        except Exception:
            pass
        if panel.runModal() != 1:  # NSModalResponseOK
            return
        zip_path = panel.URL().path()

        # 2. Prompt for the backup password (hidden).
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Backup password")
        alert.setInformativeText_(
            "Enter the password for this backup (the WordPress admin password "
            "from when it was made).")
        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 24))
        alert.setAccessoryView_(pass_field)
        alert.addButtonWithTitle_("Restore")
        alert.addButtonWithTitle_("Cancel")
        alert.window().setInitialFirstResponder_(pass_field)
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        password = pass_field.stringValue() or ""

        # 3. Validate the password up front — no half-restore on a bad password.
        try:
            from onionpress import backup as _backup
            meta = _backup.peek_backup_metadata(zip_path, password)
        except Exception:
            warn = AppKit.NSAlert.alloc().init()
            warn.setMessageText_("Couldn't open that backup")
            warn.setInformativeText_(
                "Wrong password, or not a valid OnionPress backup. "
                "Please try again.")
            warn.runModal()
            return

        # 4. Stash restore intent + hand off to the same setup callback.
        self.restore_mode = True
        self.restore_zip = zip_path
        self.restore_password = password
        self.restore_address = meta.get("onion_address", "")

        self._showing_welcome = False
        if self.welcome_view:
            self.welcome_view.setHidden_(True)
        if self.progress_view:
            self.progress_view.setHidden_(False)
        if self._on_setup_callback:
            threading.Thread(target=self._on_setup_callback, daemon=True).start()

    def viewLogClicked_(self, sender):
        try:
            log_path = os.path.expanduser("~/.onionpress/onionpress.log")
            if os.path.exists(log_path):
                try:
                    from onionpress.ui_helpers import LogViewerWindow
                    LogViewerWindow.show_for_file(log_path, "OnionPress Log")
                except ImportError:
                    import subprocess
                    subprocess.Popen(["open", "-a", "Console", log_path])
        except Exception:
            pass

    def dismissClicked_(self, sender):
        self.hide()

    # -- public API ---------------------------------------------------------

    def set_on_setup(self, callback):
        """Set callback for when user clicks Set Up. Called in a background thread."""
        self._on_setup_callback = callback

    def show(self):
        def _show():
            if not self.window:
                self.create_window()
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        _on_main(_show)

    def show_welcome(self):
        """Show the welcome/credentials phase."""
        def _show():
            if not self.window:
                self.create_window()
            self._showing_welcome = True
            if self.welcome_view:
                self.welcome_view.setHidden_(False)
            if self.progress_view:
                self.progress_view.setHidden_(True)
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        _on_main(_show)

    def show_progress(self):
        """Switch to the progress phase (skip welcome)."""
        def _show():
            if not self.window:
                self.create_window()
            self._showing_welcome = False
            if self.welcome_view:
                self.welcome_view.setHidden_(True)
            if self.progress_view:
                self.progress_view.setHidden_(False)
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        _on_main(_show)

    def hide(self):
        def _hide():
            if self.window:
                self.window.orderOut_(None)
        _on_main(_hide)

    def close(self):
        def _close():
            if self.window:
                self.window.close()
                self.window = None
        _on_main(_close)

    def set_step(self, step_index):
        """Update checklist: steps < index get check, index gets spinner, rest pending."""
        def _update():
            self.current_step = step_index
            for i, lbl in enumerate(self.step_labels):
                step_text = STEPS[i]
                if i < step_index:
                    lbl.setStringValue_(f"  {_MARK_DONE}  {step_text}")
                    lbl.setTextColor_(_GREEN)
                elif i == step_index:
                    lbl.setStringValue_(f"  {_MARK_ACTIVE}  {step_text}")
                    lbl.setTextColor_(NSColor.labelColor())
                else:
                    lbl.setStringValue_(f"  {_MARK_PENDING}  {step_text}")
                    lbl.setTextColor_(_TEXT_SECONDARY)
        _on_main(_update)

    def complete_step(self, step_index):
        """Mark step as done, advance to next."""
        next_step = step_index + 1
        if next_step < len(STEPS):
            self.set_step(next_step)
        else:
            # All done — mark everything green
            def _update():
                for i, lbl in enumerate(self.step_labels):
                    lbl.setStringValue_(f"  {_MARK_DONE}  {STEPS[i]}")
                    lbl.setTextColor_(_GREEN)
            _on_main(_update)

    def set_progress(self, value, label=None):
        """Set progress bar (0.0-1.0) and optional label."""
        def _update():
            if self.progress_bar:
                self.progress_bar.setDoubleValue_(value * 100)
            if self.percent_label:
                self.percent_label.setStringValue_(f"{int(value * 100)}%")
            if label and self.status_label:
                self.status_label.setStringValue_(label)
        _on_main(_update)

    def set_status(self, message):
        """Update the status line below the progress bar."""
        def _update():
            if self.status_label:
                self.status_label.setStringValue_(message)
        _on_main(_update)

    def set_detail(self, message):
        """Alias for set_status (compatibility)."""
        self.set_status(message)

    def add_log(self, message, status="info"):
        """Append a line to the log tail area and auto-scroll."""
        def _update():
            if not self.log_text_view:
                return
            current = self.log_text_view.string()
            if current == "Waiting for log entries...":
                current = ""
            if current:
                current += "\n"
            current += message
            self.log_text_view.setString_(current)
            # Auto-scroll to bottom
            length = self.log_text_view.string().length() if hasattr(self.log_text_view.string(), 'length') else len(self.log_text_view.string())
            self.log_text_view.scrollRangeToVisible_(AppKit.NSMakeRange(length, 0))
        _on_main(_update)

    def show_completion(self, onion_address=None):
        """All steps done, progress 100%."""
        def _update():
            for i, lbl in enumerate(self.step_labels):
                lbl.setStringValue_(f"  {_MARK_DONE}  {STEPS[i]}")
                lbl.setTextColor_(_GREEN)
            if self.progress_bar:
                self.progress_bar.setDoubleValue_(100)
            if self.percent_label:
                self.percent_label.setStringValue_("100%")
            if self.status_label:
                self.status_label.setStringValue_("Setup complete!")
                self.status_label.setTextColor_(_GREEN)
        _on_main(_update)
        self.add_log("All systems operational")
        if onion_address:
            self.add_log(f"Address: {onion_address}")
        self.add_log("")
        self.add_log("Tip: Click the OnionPress icon in your")
        self.add_log("menu bar to manage your site.")

    # -- compatibility stubs ------------------------------------------------

    def set_modem_active(self, active):
        pass

    def set_tor_final_hop_connected(self):
        pass

    def transition_to_progress(self):
        self.show_progress()

    def set_callbacks(self, on_continue=None, on_cancel=None):
        pass


# ---------------------------------------------------------------------------
# Thread-safe main-thread dispatch
# ---------------------------------------------------------------------------

def _on_main(block):
    """Run block on the main thread."""
    if threading.current_thread() is threading.main_thread():
        block()
    else:
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)


# ---------------------------------------------------------------------------
# Singleton access (same API as old setup_window)
# ---------------------------------------------------------------------------

_setup_window = None

def get_setup_window():
    global _setup_window
    if _setup_window is None:
        _setup_window = SetupProgressWindow.alloc().init()
    return _setup_window

def show_setup_progress():
    window = get_setup_window()
    window.show_progress()
    return window

def show_welcome_screen(on_continue=None, on_cancel=None):
    """Show the welcome screen with credential fields."""
    window = get_setup_window()
    window.show_welcome()
    return window

def hide_setup_progress():
    window = get_setup_window()
    window.hide()

def close_setup_progress():
    global _setup_window
    if _setup_window:
        _setup_window.close()
        _setup_window = None


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    win = get_setup_window()

    def on_setup():
        print(f"Title: {win.site_title}")
        print(f"User: {win.admin_user}")
        print(f"Pass: {win.admin_pass}")
        time.sleep(1)
        for i in range(len(STEPS)):
            win.set_step(i)
            win.set_progress(i / len(STEPS), f"Step {i+1}/{len(STEPS)}")
            win.add_log(f"Running: {STEPS[i]}")
            time.sleep(1.5)
        win.show_completion("abc123xyz.onion")
        time.sleep(3)
        win.close()
        AppKit.NSApp.terminate_(None)

    win.set_on_setup(on_setup)
    win.show_welcome()

    app.run()
