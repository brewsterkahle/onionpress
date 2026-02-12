#!/usr/bin/env python3
"""
OnionPress Setup Progress Window
Neo-Dialup Aesthetic: CRT terminal meets 90s pastel-neon
"""

import AppKit
from AppKit import (
    NSApplication, NSWindow, NSView, NSTextField, NSProgressIndicator,
    NSButton, NSImage, NSImageView, NSFont, NSColor, NSMakeRect,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskBorderless,
    NSBackingStoreBuffered, NSCenterTextAlignment, NSLeftTextAlignment,
    NSLineBreakByWordWrapping, NSLineBreakByClipping,
    NSProgressIndicatorSpinningStyle, NSProgressIndicatorBarStyle,
    NSViewWidthSizable, NSViewMinYMargin, NSApp, NSBezierPath,
    NSGradient, NSCompositingOperationSourceOver, NSFontManager,
    NSAttributedString, NSForegroundColorAttributeName, NSFontAttributeName
)
from AppKit import NSTimer, NSRunLoop, NSDefaultRunLoopMode
import objc
import threading
import time
import os
import math


# =============================================================================
# COLOR PALETTE (info site: cream boxes, dark purple headings, orange accents)
# =============================================================================

class Colors:
    """OnionPress info site palette: cream, purple, orange, black borders"""

    # Main background (light grey like info site)
    LIGHT_BG = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.90, 0.90, 0.91, 1.0)

    # Content boxes: light cream / pale yellow (info site)
    LIGHT_PANEL = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.97, 0.92, 1.0)
    CREAM = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.97, 0.92, 1.0)

    # Headings / titles: dark purple-magenta
    HEADING_PURPLE = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.25, 0.50, 1.0)
    DARK_PURPLE = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.25, 0.50, 1.0)

    # Accent / links / CTA: orange-gold
    ACCENT_ORANGE = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.55, 0.20, 1.0)
    ORANGE_GOLD = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.55, 0.20, 1.0)

    # Borders: thin black
    BORDER_BLACK = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.15, 1.0)

    # Text
    TEXT_DARK = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.18, 1.0)
    TEXT_DIM = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.4, 0.4, 0.45, 1.0)
    DARK_GRAY = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.35, 0.35, 0.4, 1.0)
    LIGHT_GRAY = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.7, 0.7, 0.72, 1.0)

    # Legacy / compatibility
    POWDER_BLUE = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.69, 0.82, 0.96, 1.0)
    CRT_BLACK = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.08, 0.12, 1.0)
    CRT_DARK = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0)
    PANEL_BG = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.20, 1.0)
    DARK_CYAN = HEADING_PURPLE
    DARK_AMBER = ACCENT_ORANGE
    DARK_GREEN = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.5, 0.2, 1.0)
    DARK_PINK = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.7, 0.2, 0.4, 1.0)
    PASTEL_CYAN = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.4, 0.75, 0.85, 1.0)
    PASTEL_PINK = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.5, 0.6, 1.0)
    PASTEL_ORANGE = ACCENT_ORANGE
    PASTEL_GREEN = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.4, 0.7, 0.45, 1.0)
    TEXT_BRIGHT = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.95, 0.98, 0.95, 1.0)
    TEXT_AMBER = ACCENT_ORANGE

    # Status LEDs
    LED_OFF = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.2, 0.2, 0.2, 1.0)
    LED_GREEN = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.2, 1.0, 0.3, 1.0)
    LED_YELLOW = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.9, 0.2, 1.0)
    LED_RED = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.3, 0.2, 1.0)


# =============================================================================
# CUSTOM VIEWS
# =============================================================================

class ScanlineView(NSView):
    """Overlay view that draws CRT scanlines"""

    def drawRect_(self, rect):
        bounds = self.bounds()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0, 0, 0, 0.06).set()
        y = 0
        while y < bounds.size.height:
            line = NSBezierPath.bezierPath()
            line.moveToPoint_((0, y))
            line.lineToPoint_((bounds.size.width, y))
            line.setLineWidth_(1)
            line.stroke()
            y += 3


class PixelProgressBar(NSView):
    """Retro segmented progress bar with neon glow"""

    def initWithFrame_(self, frame):
        self = objc.super(PixelProgressBar, self).initWithFrame_(frame)
        if self:
            self._progress = 0.0
            self._segments = 20
            self._animate_pulse = 0
        return self

    def setProgress_(self, value):
        self._progress = min(1.0, max(0.0, value))
        self.setNeedsDisplay_(True)

    def setPulse_(self, value):
        self._animate_pulse = value
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        Colors.LIGHT_PANEL.set()
        bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 4, 4)
        bg.fill()
        Colors.TEXT_DIM.set()
        bg.setLineWidth_(1)
        bg.stroke()
        segment_width = (bounds.size.width - 8) / self._segments
        filled_segments = int(self._progress * self._segments)
        for i in range(self._segments):
            x = 4 + i * segment_width
            segment_rect = NSMakeRect(x + 1, 4, segment_width - 2, bounds.size.height - 8)
            if i < filled_segments:
                if i < self._segments * 0.6:
                    color = Colors.PASTEL_CYAN
                elif i < self._segments * 0.85:
                    color = Colors.PASTEL_GREEN
                else:
                    color = Colors.PASTEL_PINK
                if i == filled_segments - 1 and self._animate_pulse:
                    pulse = 0.5 + 0.5 * math.sin(self._animate_pulse * 0.3)
                    color = color.colorWithAlphaComponent_(0.7 + 0.3 * pulse)
                color.set()
                segment = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(segment_rect, 2, 2)
                segment.fill()
            else:
                Colors.LIGHT_GRAY.set()
                segment = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(segment_rect, 2, 2)
                segment.fill()


class StatusLED(NSView):
    """Small LED indicator with glow effect"""

    def initWithFrame_(self, frame):
        self = objc.super(StatusLED, self).initWithFrame_(frame)
        if self:
            self._color = Colors.LED_OFF
            self._glowing = False
            self._blink_state = True
        return self

    def setColor_(self, color):
        self._color = color
        self.setNeedsDisplay_(True)

    def setGlowing_(self, glowing):
        self._glowing = glowing
        self.setNeedsDisplay_(True)

    def setBlink_(self, state):
        self._blink_state = state
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        center_x = bounds.size.width / 2
        center_y = bounds.size.height / 2
        radius = min(bounds.size.width, bounds.size.height) / 2 - 2
        if self._glowing and self._blink_state:
            glow_color = self._color.colorWithAlphaComponent_(0.3)
            glow_color.set()
            glow_path = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(center_x - radius - 3, center_y - radius - 3,
                          (radius + 3) * 2, (radius + 3) * 2)
            )
            glow_path.fill()
        if self._blink_state:
            self._color.set()
        else:
            Colors.LED_OFF.set()
        led_path = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(center_x - radius, center_y - radius, radius * 2, radius * 2)
        )
        led_path.fill()
        if self._blink_state and self._glowing:
            highlight = NSColor.whiteColor().colorWithAlphaComponent_(0.4)
            highlight.set()
            highlight_path = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(center_x - radius/2, center_y, radius, radius/2)
            )
            highlight_path.fill()


class TerminalTextView(NSView):
    """Terminal-style text display with cursor"""

    def initWithFrame_(self, frame):
        self = objc.super(TerminalTextView, self).initWithFrame_(frame)
        if self:
            self._lines = []
            self._cursor_visible = True
            self._max_lines = 8
        return self

    def addLine_(self, text):
        self._lines.append(text)
        if len(self._lines) > self._max_lines:
            self._lines.pop(0)
        self.setNeedsDisplay_(True)

    def clear(self):
        self._lines = []
        self.setNeedsDisplay_(True)

    def setCursorVisible_(self, visible):
        self._cursor_visible = visible
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        Colors.LIGHT_PANEL.set()
        NSBezierPath.bezierPathWithRect_(bounds).fill()
        # No outer border (caller may add a vertical divider between log areas)
        font = NSFont.fontWithName_size_("Monaco", 11) or NSFont.monospacedSystemFontOfSize_weight_(11, 0.0)
        y = bounds.size.height - 18
        for line in self._lines:
            if line.startswith("[OK]") or line.startswith("[✓]"):
                color = Colors.DARK_GREEN
            elif line.startswith("[!!]") or line.startswith("[✗]"):
                color = Colors.DARK_PINK
            elif line.startswith("[>>]") or line.startswith("[..]"):
                color = Colors.DARK_CYAN
            else:
                color = Colors.TEXT_DARK
            attrs = {
                NSForegroundColorAttributeName: color,
                NSFontAttributeName: font
            }
            text = NSAttributedString.alloc().initWithString_attributes_(line, attrs)
            text.drawAtPoint_((8, y))
            y -= 14
        if self._cursor_visible:
            Colors.DARK_CYAN.set()
            cursor_y = y + 14
            cursor_rect = NSMakeRect(8, cursor_y - 2, 8, 12)
            NSBezierPath.bezierPathWithRect_(cursor_rect).fill()


class ModemVisualizerView(NSView):
    """Animated modem-style visualization"""

    def initWithFrame_(self, frame):
        self = objc.super(ModemVisualizerView, self).initWithFrame_(frame)
        if self:
            self._bars = [0.0] * 16
            self._active = False
            self._frame_height = frame.size.height
            self._frame_width = frame.size.width
        return self

    def setActive_(self, active):
        self._active = active
        self.setNeedsDisplay_(True)

    def updateBars_(self, bars):
        self._bars = bars
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        width = bounds.size.width
        height = bounds.size.height
        Colors.CRT_BLACK.set()
        NSBezierPath.bezierPathWithRect_(bounds).fill()
        bar_count = len(self._bars)
        bar_width = width / bar_count
        max_height = height - 4
        for i, level in enumerate(self._bars):
            x = i * bar_width
            bar_height = max(2, level * max_height * 0.85)
            if self._active and level > 0:
                if level < 0.5:
                    color = Colors.PASTEL_CYAN
                elif level < 0.8:
                    color = Colors.PASTEL_GREEN
                else:
                    color = Colors.PASTEL_PINK
                color.set()
            else:
                Colors.CRT_DARK.set()
            bar_rect = NSMakeRect(x + 1, 2, bar_width - 2, bar_height)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar_rect, 1, 1).fill()


# =============================================================================
# TOR HOP VIEW (dial-up style: many hops = Tor circuit)
# =============================================================================

def _computer_icon_path():
    """Path to noun-computer SVG for Tor hop icons (dev or bundle)."""
    try:
        bundle = AppKit.NSBundle.mainBundle()
        if bundle and bundle.resourcePath():
            path = os.path.join(bundle.resourcePath(), "assets", "branding", "noun-computer-5963091.svg")
            if os.path.exists(path):
                return path
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(script_dir)
    return os.path.join(root, "assets", "branding", "noun-computer-5963091.svg")


def _logo_path():
    """Path to logo.png for welcome screen (dev or bundle)."""
    try:
        bundle = AppKit.NSBundle.mainBundle()
        if bundle and bundle.resourcePath():
            path = os.path.join(bundle.resourcePath(), "assets", "branding", "logo.png")
            if os.path.exists(path):
                return path
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(script_dir)
    return os.path.join(root, "assets", "branding", "logo.png")


class TorHopView(NSView):
    """Row of computer icons (Tor relays); PCs start faded and become solid as each hop connects.
    First 3 connect on a fixed timer; last connects when set_final_hop_connected() is called.
    """
    NUM_HOPS = 5

    def initWithFrame_(self, frame):
        self = objc.super(TorHopView, self).initWithFrame_(frame)
        if self:
            self._connected = [False] * self.NUM_HOPS
            self._computer_image = None
            path = _computer_icon_path()
            if path and os.path.exists(path):
                self._computer_image = NSImage.alloc().initWithContentsOfFile_(path)
                if self._computer_image:
                    self._computer_image.setTemplate_(True)
        return self

    def setHopConnected_(self, index):
        if 0 <= index < self.NUM_HOPS:
            self._connected[index] = True
            self.setNeedsDisplay_(True)

    def setFinalHopConnected_(self, sender=None):
        """ObjC selector setFinalHopConnected: takes one argument (sender)."""
        self.setHopConnected_(self.NUM_HOPS - 1)

    def _tintedImageForRect_color_(self, icon_rect, tint_color):
        """Draw icon tinted with color: fill rect with color, then image as mask (DestinationIn)."""
        try:
            w = icon_rect.size.width
            h = icon_rect.size.height
            if w <= 0 or h <= 0:
                return None
            offscreen = NSImage.alloc().initWithSize_((w, h))
            offscreen.lockFocus()
            # Fill with tint color, then draw template image so it acts as mask (keeps color where image is opaque)
            tint_color.set()
            NSBezierPath.fillRect_(NSMakeRect(0, 0, w, h))
            self._computer_image.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(0, 0, w, h), NSMakeRect(0, 0, 128, 128),
                AppKit.NSCompositingOperationDestinationIn, 1.0
            )
            offscreen.unlockFocus()
            return offscreen
        except Exception:
            return None

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        n = self.NUM_HOPS
        icon_size = min(int(h) - 4, (w - 40) // n - 8)
        if icon_size < 8:
            icon_size = 8
        total_icons_width = n * icon_size
        gap = (w - total_icons_width) / (n + 1) if n > 0 else 0
        xs = [gap + (gap + icon_size) * i + icon_size / 2 for i in range(n)]
        line_y = h / 2
        NSBezierPath.setDefaultLineWidth_(2)

        # Lines: solid when that hop is connected
        for i in range(n - 1):
            x0 = xs[i] + icon_size / 2
            x1 = xs[i + 1] - icon_size / 2
            if self._connected[i]:
                Colors.ACCENT_ORANGE.set()
            else:
                Colors.LIGHT_GRAY.set()
            line_path = NSBezierPath.bezierPath()
            line_path.moveToPoint_((x0, line_y))
            line_path.lineToPoint_((x1, line_y))
            line_path.stroke()

        # Icons: draw template into offscreen image with fill color so tint is applied
        for i in range(n):
            cx = xs[i]
            icon_rect = NSMakeRect(cx - icon_size / 2, line_y - icon_size / 2, icon_size, icon_size)
            if self._computer_image:
                tint_color = Colors.ACCENT_ORANGE if self._connected[i] else Colors.LIGHT_GRAY.colorWithAlphaComponent_(0.4)
                tinted = self._tintedImageForRect_color_(icon_rect, tint_color)
                if tinted:
                    tinted.drawInRect_fromRect_operation_fraction_(
                        icon_rect, NSMakeRect(0, 0, icon_rect.size.width, icon_rect.size.height),
                        NSCompositingOperationSourceOver, 1.0
                    )
                else:
                    self._computer_image.drawInRect_fromRect_operation_fraction_(
                        icon_rect, NSMakeRect(0, 0, 128, 128), NSCompositingOperationSourceOver, 1.0
                    )
            else:
                c = Colors.LIGHT_GRAY.colorWithAlphaComponent_(0.4 if not self._connected[i] else 1.0)
                c.set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(icon_rect, 4, 4).fill()


# =============================================================================
# MAIN SETUP WINDOW
# =============================================================================

class SetupProgressWindow(AppKit.NSObject):
    """Neo-Dialup styled setup progress window"""

    def init(self):
        self = objc.super(SetupProgressWindow, self).init()
        if self is None:
            return None
        self._initialize()
        return self

    def _initialize(self):
        self.window = None
        self.status_label = None
        self.terminal_view = None
        self.progress_bar = None
        self.modem_viz = None
        self.percent_label = None
        self.log_tail_label = None
        self.log_file_path = None
        self.last_log_position = 0
        self.leds = []
        self.current_step = 0
        self.animation_timer = None
        self.pulse_counter = 0
        self.modem_active = False
        self.on_continue_callback = None
        self.on_cancel_callback = None
        self.showing_welcome = True
        self.welcome_view = None
        self.progress_view = None
        self.download_panel_view = None
        self.tor_animation_panel = None
        self.tor_connecting_label = None  # "< CONNECTING OVER TOR >" in gap between boxes
        self.tor_hop_view = None
        self.tor_status_label = None
        self.steps = [
            ("SYS_CHECK", "Checking system requirements"),
            ("RUNTIME_INIT", "Initializing container runtime"),
            ("IMG_DOWNLOAD", "Downloading container images"),
            ("VANITY_GEN", "Generating vanity onion address"),
            ("SVC_START", "Starting services"),
            ("FINALIZE", "Finalizing setup")
        ]

    def create_window(self):
        """Create the Neo-Dialup styled setup window"""
        width = 520
        height = 480
        frame = NSMakeRect(0, 0, width, height)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("OnionPress // SETUP")
        self.window.setBackgroundColor_(Colors.LIGHT_BG)
        self.window.center()
        self.window.setLevel_(3)
        content = self.window.contentView()
        # Scanlines at back so white panels draw solid on top
        scanlines = ScanlineView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(scanlines)
        self._create_welcome_view(content, width, height)
        self._create_progress_view(content, width, height)
        self.welcome_view.setHidden_(False)
        self.progress_view.setHidden_(True)

    def _create_welcome_view(self, content, width, height):
        """Create the welcome/confirmation screen"""
        self.welcome_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(self.welcome_view)

        header_panel = NSView.alloc().initWithFrame_(NSMakeRect(16, height - 70, width - 32, 60))
        header_panel.setWantsLayer_(True)
        header_panel.layer().setBackgroundColor_(Colors.LIGHT_PANEL.CGColor())
        header_panel.layer().setCornerRadius_(8)
        header_panel.layer().setBorderWidth_(1)
        header_panel.layer().setBorderColor_(Colors.BORDER_BLACK.CGColor())
        self.welcome_view.addSubview_(header_panel)

        title = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 20, width - 64, 30))
        title.setStringValue_("[ FIRST-TIME SETUP REQUIRED ]")
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        title.setAlignment_(NSCenterTextAlignment)
        title.setFont_(NSFont.fontWithName_size_("Monaco", 16) or NSFont.boldSystemFontOfSize_(16))
        title.setTextColor_(Colors.HEADING_PURPLE)
        header_panel.addSubview_(title)

        subtitle = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 5, width - 64, 18))
        subtitle.setStringValue_(">> ESTIMATED TIME: 2-3 MINUTES")
        subtitle.setBezeled_(False)
        subtitle.setDrawsBackground_(False)
        subtitle.setEditable_(False)
        subtitle.setSelectable_(False)
        subtitle.setAlignment_(NSCenterTextAlignment)
        subtitle.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.systemFontOfSize_(10))
        subtitle.setTextColor_(Colors.ACCENT_ORANGE)
        header_panel.addSubview_(subtitle)

        # Logo in the empty space below header (info site branding)
        logo_path = _logo_path()
        if logo_path and os.path.exists(logo_path):
            logo_image = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if logo_image:
                logo_w, logo_h = 240, 160
                logo_view = NSImageView.alloc().initWithFrame_(NSMakeRect((width - logo_w) / 2, height - 75 - logo_h, logo_w, logo_h))
                logo_view.setImage_(logo_image)
                logo_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                self.welcome_view.addSubview_(logo_view)

        steps_panel = NSView.alloc().initWithFrame_(NSMakeRect(16, height - 380, width - 32, 160))
        steps_panel.setWantsLayer_(True)
        steps_panel.layer().setBackgroundColor_(Colors.LIGHT_PANEL.CGColor())
        steps_panel.layer().setCornerRadius_(8)
        steps_panel.layer().setBorderWidth_(1)
        steps_panel.layer().setBorderColor_(Colors.BORDER_BLACK.CGColor())
        self.welcome_view.addSubview_(steps_panel)

        steps_header = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 130, width - 64, 20))
        steps_header.setStringValue_("< SETUP SEQUENCE >")
        steps_header.setBezeled_(False)
        steps_header.setDrawsBackground_(False)
        steps_header.setEditable_(False)
        steps_header.setSelectable_(False)
        steps_header.setFont_(NSFont.fontWithName_size_("Monaco", 11) or NSFont.systemFontOfSize_(11))
        steps_header.setTextColor_(Colors.HEADING_PURPLE)
        steps_panel.addSubview_(steps_header)

        setup_items = [
            ("01", "Checking system requirements"),
            ("02", "Initializing container runtime (Colima VM)"),
            ("03", "Downloading Docker images (~500MB)"),
            ("04", "Generating vanity .onion address"),
            ("05", "Starting WordPress + Tor services"),
        ]
        y_pos = 100
        for code, description in setup_items:
            num_label = NSTextField.alloc().initWithFrame_(NSMakeRect(16, y_pos, 30, 18))
            num_label.setStringValue_(f"[{code}]")
            num_label.setBezeled_(False)
            num_label.setDrawsBackground_(False)
            num_label.setEditable_(False)
            num_label.setSelectable_(False)
            num_label.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.monospacedSystemFontOfSize_weight_(10, 0.0))
            num_label.setTextColor_(Colors.HEADING_PURPLE)
            steps_panel.addSubview_(num_label)
            desc_label = NSTextField.alloc().initWithFrame_(NSMakeRect(50, y_pos, width - 100, 18))
            desc_label.setStringValue_(description)
            desc_label.setBezeled_(False)
            desc_label.setDrawsBackground_(False)
            desc_label.setEditable_(False)
            desc_label.setSelectable_(False)
            desc_label.setFont_(NSFont.fontWithName_size_("Monaco", 11) or NSFont.systemFontOfSize_(11))
            desc_label.setTextColor_(Colors.TEXT_DARK)
            steps_panel.addSubview_(desc_label)
            y_pos -= 22

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width/2 - 170, 30, 150, 40))
        cancel_btn.setBezelStyle_(1)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_(objc.selector(self.cancelButtonClicked_, signature=b'v@:@'))
        cancel_btn.setFont_(NSFont.fontWithName_size_("Monaco", 12) or NSFont.systemFontOfSize_(12))
        # Use attributed title so Cancel text is clearly visible (dark gray/black)
        cancel_attrs = {
            NSForegroundColorAttributeName: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.15, 1.0),
            NSFontAttributeName: NSFont.fontWithName_size_("Monaco", 12) or NSFont.systemFontOfSize_(12),
        }
        cancel_btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_("[ CANCEL ]", cancel_attrs))
        self.welcome_view.addSubview_(cancel_btn)

        continue_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width/2 + 20, 30, 150, 40))
        continue_btn.setTitle_("[ CONTINUE ]")
        continue_btn.setBezelStyle_(1)
        continue_btn.setTarget_(self)
        continue_btn.setAction_(objc.selector(self.continueButtonClicked_, signature=b'v@:@'))
        continue_btn.setFont_(NSFont.fontWithName_size_("Monaco", 12) or NSFont.systemFontOfSize_(12))
        continue_btn.setKeyEquivalent_("\r")
        continue_btn.setContentTintColor_(NSColor.whiteColor())
        self.welcome_view.addSubview_(continue_btn)

        footer = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 80, width - 32, 16))
        footer.setStringValue_(">> Click CONTINUE to begin or CANCEL to abort...")
        footer.setBezeled_(False)
        footer.setDrawsBackground_(False)
        footer.setEditable_(False)
        footer.setSelectable_(False)
        footer.setAlignment_(NSCenterTextAlignment)
        footer.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.systemFontOfSize_(10))
        footer.setTextColor_(Colors.DARK_GRAY)
        self.welcome_view.addSubview_(footer)

    def _create_progress_view(self, content, width, height):
        """Create the progress/status screen: header, system.log+live log side-by-side, progress bar, LEDs, Tor animation below."""
        self.progress_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(self.progress_view)
        header_y = height - 70
        self._create_header_panel(self.progress_view, width, header_y)
        terminal_y = header_y - 170
        self._create_terminal_and_live_log_panel(self.progress_view, width, terminal_y)
        progress_y = terminal_y - 100
        self._create_progress_panel(self.progress_view, width, progress_y)
        led_y = progress_y - 25
        self._create_led_panel(self.progress_view, width, led_y)
        # "< CONNECTING OVER TOR >" in the gap between LED panel and Tor white box
        gap_label_y = led_y - 22
        self.tor_connecting_label = NSTextField.alloc().initWithFrame_(NSMakeRect(16, gap_label_y, width - 32, 18))
        self.tor_connecting_label.setStringValue_("< CONNECTING OVER TOR >")
        self.tor_connecting_label.setBezeled_(False)
        self.tor_connecting_label.setDrawsBackground_(False)
        self.tor_connecting_label.setAlignment_(NSCenterTextAlignment)
        self.tor_connecting_label.setFont_(NSFont.fontWithName_size_("Monaco", 11) or NSFont.systemFontOfSize_(11))
        self.tor_connecting_label.setTextColor_(Colors.HEADING_PURPLE)
        self.tor_connecting_label.setHidden_(True)
        self.progress_view.addSubview_(self.tor_connecting_label)
        # Tor box fills from bottom (y=10) up to just below the label
        tor_panel_height = gap_label_y - 10
        tor_anim_y = 10
        self._create_tor_animation_panel(self.progress_view, width, tor_anim_y, tor_panel_height)

    def _create_header_panel(self, content, width, y):
        panel = NSView.alloc().initWithFrame_(NSMakeRect(16, y, width - 32, 60))
        panel.setWantsLayer_(True)
        panel.layer().setBackgroundColor_(Colors.LIGHT_PANEL.CGColor())
        panel.layer().setCornerRadius_(8)
        panel.layer().setBorderWidth_(1)
        panel.layer().setBorderColor_(Colors.BORDER_BLACK.CGColor())
        content.addSubview_(panel)
        title = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 20, width - 64, 30))
        title.setStringValue_("[ ONIONPRESS SETUP SEQUENCE ]")
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        title.setAlignment_(NSCenterTextAlignment)
        title.setFont_(NSFont.fontWithName_size_("Monaco", 16) or NSFont.boldSystemFontOfSize_(16))
        title.setTextColor_(Colors.HEADING_PURPLE)
        panel.addSubview_(title)
        subtitle = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 5, width - 64, 18))
        subtitle.setStringValue_(">> INITIALIZING SECURE ONION SERVICE...")
        subtitle.setBezeled_(False)
        subtitle.setDrawsBackground_(False)
        subtitle.setEditable_(False)
        subtitle.setSelectable_(False)
        subtitle.setAlignment_(NSCenterTextAlignment)
        subtitle.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.systemFontOfSize_(10))
        subtitle.setTextColor_(Colors.DARK_GRAY)
        panel.addSubview_(subtitle)
        self.status_label = subtitle

    def _create_terminal_and_live_log_panel(self, content, width, y):
        """Single panel: left = SYSTEM.LOG (terminal), right = LIVE LOG."""
        panel_w = width - 32
        panel_h = 150
        panel_frame = NSView.alloc().initWithFrame_(NSMakeRect(16, y, panel_w, panel_h))
        panel_frame.setWantsLayer_(True)
        panel_frame.layer().setBackgroundColor_(Colors.CREAM.CGColor())
        panel_frame.layer().setCornerRadius_(8)
        content.addSubview_(panel_frame)
        # Light vertical divider between SYSTEM.LOG and LIVE LOG
        left_w = int(panel_w * 0.55)
        divider = NSView.alloc().initWithFrame_(NSMakeRect(left_w, 4, 1, 122))
        divider.setWantsLayer_(True)
        divider.layer().setBackgroundColor_(Colors.LIGHT_GRAY.CGColor())
        panel_frame.addSubview_(divider)
        header = NSView.alloc().initWithFrame_(NSMakeRect(0, 130, panel_w, 20))
        header.setWantsLayer_(True)
        header.layer().setBackgroundColor_(Colors.LIGHT_PANEL.CGColor())
        panel_frame.addSubview_(header)
        header_left = NSTextField.alloc().initWithFrame_(NSMakeRect(8, 2, 120, 16))
        header_left.setStringValue_("SYSTEM.LOG")
        header_left.setBezeled_(False)
        header_left.setDrawsBackground_(False)
        header_left.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.systemFontOfSize_(10))
        header_left.setTextColor_(Colors.HEADING_PURPLE)
        header.addSubview_(header_left)
        header_right = NSTextField.alloc().initWithFrame_(NSMakeRect(panel_w - 100, 2, 92, 16))
        header_right.setStringValue_("LIVE LOG")
        header_right.setBezeled_(False)
        header_right.setDrawsBackground_(False)
        header_right.setAlignment_(NSCenterTextAlignment)
        header_right.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.systemFontOfSize_(10))
        header_right.setTextColor_(Colors.HEADING_PURPLE)
        header.addSubview_(header_right)
        left_w = int(panel_w * 0.55)
        right_w = panel_w - left_w - 8
        self.terminal_view = TerminalTextView.alloc().initWithFrame_(NSMakeRect(4, 4, left_w - 4, 122))
        panel_frame.addSubview_(self.terminal_view)
        self.terminal_view.addLine_("[>>] OnionPress")
        self.terminal_view.addLine_("[>>] BOOT SEQUENCE INITIATED")
        self.terminal_view.addLine_("[..]")
        self.log_tail_label = NSTextField.alloc().initWithFrame_(NSMakeRect(left_w + 4, 4, right_w - 4, 122))
        self.log_tail_label.setBezeled_(False)
        self.log_tail_label.setDrawsBackground_(True)
        self.log_tail_label.setBackgroundColor_(Colors.LIGHT_PANEL)
        self.log_tail_label.setEditable_(False)
        self.log_tail_label.setSelectable_(False)
        self.log_tail_label.setFont_(NSFont.fontWithName_size_("Menlo", 9) or NSFont.systemFontOfSize_(9))
        self.log_tail_label.setTextColor_(Colors.HEADING_PURPLE)
        self.log_tail_label.setStringValue_("Waiting for log entries...")
        panel_frame.addSubview_(self.log_tail_label)
        self.log_file_path = os.path.expanduser("~/.onionpress/onionpress.log")
        self.last_log_position = 0

    def _create_progress_panel(self, content, width, y):
        panel_height = 95
        self.download_panel_view = NSView.alloc().initWithFrame_(NSMakeRect(16, y, width - 32, panel_height))
        content.addSubview_(self.download_panel_view)
        dlabel = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 70, width - 32, 20))
        dlabel.setStringValue_("< DOWNLOAD PROGRESS >")
        dlabel.setBezeled_(False)
        dlabel.setDrawsBackground_(False)
        dlabel.setEditable_(False)
        dlabel.setSelectable_(False)
        dlabel.setAlignment_(NSCenterTextAlignment)
        dlabel.setFont_(NSFont.fontWithName_size_("Monaco", 11) or NSFont.systemFontOfSize_(11))
        dlabel.setTextColor_(Colors.HEADING_PURPLE)
        self.download_panel_view.addSubview_(dlabel)
        self.progress_bar = PixelProgressBar.alloc().initWithFrame_(NSMakeRect(0, 40, width - 32, 24))
        self.download_panel_view.addSubview_(self.progress_bar)
        self.percent_label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 15, width - 32, 20))
        self.percent_label.setStringValue_("0% // STANDBY")
        self.percent_label.setBezeled_(False)
        self.percent_label.setDrawsBackground_(False)
        self.percent_label.setEditable_(False)
        self.percent_label.setSelectable_(False)
        self.percent_label.setAlignment_(NSCenterTextAlignment)
        self.percent_label.setFont_(NSFont.fontWithName_size_("Monaco", 10) or NSFont.systemFontOfSize_(10))
        self.percent_label.setTextColor_(Colors.DARK_GRAY)
        self.download_panel_view.addSubview_(self.percent_label)

    def _create_tor_animation_panel(self, content, width, y, panel_h):
        """Tor hop animation (shown when step >= 3). Tall box fills gap; PCs up, status at bottom."""
        self.tor_animation_panel = NSView.alloc().initWithFrame_(NSMakeRect(16, y, width - 32, panel_h))
        content.addSubview_(self.tor_animation_panel)
        self.tor_animation_panel.setHidden_(True)
        self.tor_animation_panel.setWantsLayer_(True)
        self.tor_animation_panel.layer().setBackgroundColor_(Colors.CREAM.CGColor())
        self.tor_animation_panel.layer().setCornerRadius_(8)
        self.tor_animation_panel.layer().setBorderWidth_(1)
        self.tor_animation_panel.layer().setBorderColor_(Colors.BORDER_BLACK.CGColor())
        # PCs in upper area; status text at bottom with clear spacing
        hop_view_h = min(44, panel_h - 28)
        status_h = 14
        self.tor_hop_view = TorHopView.alloc().initWithFrame_(NSMakeRect(0, panel_h - 10 - hop_view_h, width - 32, hop_view_h))
        self.tor_animation_panel.addSubview_(self.tor_hop_view)
        self.tor_status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 6, width - 32, status_h))
        self.tor_status_label.setStringValue_("Status: Building circuit...")
        self.tor_status_label.setBezeled_(False)
        self.tor_status_label.setDrawsBackground_(False)
        self.tor_status_label.setAlignment_(NSCenterTextAlignment)
        self.tor_status_label.setFont_(NSFont.fontWithName_size_("Monaco", 9) or NSFont.systemFontOfSize_(9))
        self.tor_status_label.setTextColor_(Colors.DARK_GRAY)
        self.tor_animation_panel.addSubview_(self.tor_status_label)

    def _create_led_panel(self, content, width, y):
        panel = NSView.alloc().initWithFrame_(NSMakeRect(16, y, width - 32, 40))
        panel.setWantsLayer_(True)
        panel.layer().setBackgroundColor_(Colors.CREAM.CGColor())
        panel.layer().setCornerRadius_(6)
        panel.layer().setBorderWidth_(1)
        panel.layer().setBorderColor_(Colors.BORDER_BLACK.CGColor())
        content.addSubview_(panel)
        led_labels = ["SYS", "INIT", "IMG", "ADDR", "SVC", "OK"]
        led_width = (width - 64) / len(led_labels)
        for i, label_text in enumerate(led_labels):
            x = 16 + i * led_width
            led = StatusLED.alloc().initWithFrame_(NSMakeRect(x + led_width/2 - 8, 20, 16, 16))
            led.setColor_(Colors.LED_OFF)
            panel.addSubview_(led)
            self.leds.append(led)
            label = NSTextField.alloc().initWithFrame_(NSMakeRect(x, 2, led_width, 14))
            label.setStringValue_(label_text)
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            label.setAlignment_(NSCenterTextAlignment)
            label.setFont_(NSFont.fontWithName_size_("Monaco", 9) or NSFont.systemFontOfSize_(9))
            label.setTextColor_(Colors.DARK_GRAY)
            panel.addSubview_(label)

    def _update_log_tail(self):
        try:
            if self.log_file_path and os.path.exists(self.log_file_path):
                with open(self.log_file_path, 'r') as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    read_size = min(1000, file_size)
                    f.seek(max(0, file_size - read_size))
                    content = f.read()
                    lines = content.strip().split('\n')
                    last_lines = lines[-5:] if len(lines) >= 5 else lines
                    display_lines = []
                    for line in last_lines:
                        if '] ' in line:
                            line = line.split('] ', 1)[1]
                        if len(line) > 65:
                            line = line[:62] + "..."
                        display_lines.append(line)
                    display_text = '\n'.join(display_lines)

                    def _update():
                        if self.log_tail_label:
                            self.log_tail_label.setStringValue_(display_text)

                    if threading.current_thread() is threading.main_thread():
                        _update()
                    else:
                        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_update)
        except Exception:
            pass

    def _start_animations(self):
        def tick():
            self.pulse_counter += 1
            if self.progress_bar:
                self.progress_bar.setPulse_(self.pulse_counter)
            if self.terminal_view:
                self.terminal_view.setCursorVisible_(self.pulse_counter % 10 < 5)
            if self.pulse_counter % 10 == 0:
                self._update_log_tail()
            for i, led in enumerate(self.leds):
                if i == self.current_step:
                    led.setBlink_(self.pulse_counter % 6 < 3)

        def start_timer():
            self.animation_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.1, True, lambda timer: tick()
            )
        if threading.current_thread() is threading.main_thread():
            start_timer()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(start_timer)

    def cancelButtonClicked_(self, sender):
        if self.on_cancel_callback:
            self.on_cancel_callback()
        self.close()

    def continueButtonClicked_(self, sender):
        self.transition_to_progress()
        if self.on_continue_callback:
            self.on_continue_callback()

    def transition_to_progress(self):
        def _transition():
            self.showing_welcome = False
            if self.welcome_view:
                self.welcome_view.setHidden_(True)
            if self.progress_view:
                self.progress_view.setHidden_(False)
            self._start_animations()
        if threading.current_thread() is threading.main_thread():
            _transition()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_transition)

    def set_callbacks(self, on_continue=None, on_cancel=None):
        self.on_continue_callback = on_continue
        self.on_cancel_callback = on_cancel

    def show(self):
        def _show():
            if not self.window:
                self.create_window()
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        if threading.current_thread() is threading.main_thread():
            _show()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_show)

    def show_welcome(self):
        def _show():
            if not self.window:
                self.create_window()
            self.showing_welcome = True
            if self.welcome_view:
                self.welcome_view.setHidden_(False)
            if self.progress_view:
                self.progress_view.setHidden_(True)
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        if threading.current_thread() is threading.main_thread():
            _show()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_show)

    def hide(self):
        def _hide():
            if self.window:
                self.window.orderOut_(None)
        if threading.current_thread() is threading.main_thread():
            _hide()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_hide)

    def close(self):
        def _close():
            if self.animation_timer:
                self.animation_timer.invalidate()
                self.animation_timer = None
            if self.window:
                self.window.close()
                self.window = None
        if threading.current_thread() is threading.main_thread():
            _close()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_close)

    def set_status(self, message):
        def _update():
            if self.status_label:
                self.status_label.setStringValue_(f">> {message.upper()}...")
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_update)

    def set_detail(self, message):
        """Update subtitle/detail line (e.g. vanity address step)."""
        self.set_status(message)

    def add_log(self, message, status="info"):
        def _update():
            if self.terminal_view:
                if status == "ok":
                    prefix = "[OK]"
                elif status == "error":
                    prefix = "[!!]"
                elif status == "progress":
                    prefix = "[..]"
                else:
                    prefix = "[>>]"
                self.terminal_view.addLine_(f"{prefix} {message}")
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_update)

    def set_progress(self, value, label=None):
        def _update():
            if self.progress_bar:
                self.progress_bar.setProgress_(value)
            if self.percent_label:
                percent = int(value * 100)
                status = label or ("DOWNLOADING" if value < 1 else "COMPLETE")
                self.percent_label.setStringValue_(f"{percent}% // {status}")
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_update)

    def _start_tor_hop_timer(self):
        """Hops 0–3 connect at 0s, 2.5s, 5s, 7.5s; hop 4 (last) only via set_tor_final_hop_connected()."""
        if not self.tor_hop_view:
            return
        self.tor_hop_view.setHopConnected_(0)
        done = [False] * 3
        def set_hop(n):
            def _(_):
                if not done[n] and self.tor_hop_view:
                    done[n] = True
                    self.tor_hop_view.setHopConnected_(n + 1)
            return _
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(2.5, False, set_hop(0))
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(5.0, False, set_hop(1))
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(7.5, False, set_hop(2))

    def set_tor_final_hop_connected(self):
        """Call when setup is complete: ensure hops 0–3 are lit, then light hop 4 so order is left→right."""
        def _():
            if not self.tor_hop_view:
                return
            # Catch up so 0–3 are all lit (timers may not have fired yet), then light last hop after a short delay
            for i in range(4):
                self.tor_hop_view.setHopConnected_(i)
            def light_last(_):
                self.tor_hop_view.setHopConnected_(4)
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.35, False, light_last)
        if threading.current_thread() is threading.main_thread():
            _()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_)

    def set_step(self, step_index, status="in_progress"):
        def _update():
            self.current_step = step_index
            if step_index >= 3:
                if self.tor_connecting_label:
                    self.tor_connecting_label.setHidden_(False)
                if self.tor_animation_panel:
                    self.tor_animation_panel.setHidden_(False)
                    self._start_tor_hop_timer()
            else:
                if self.tor_connecting_label:
                    self.tor_connecting_label.setHidden_(True)
                if self.tor_animation_panel:
                    self.tor_animation_panel.setHidden_(True)
            for i, led in enumerate(self.leds):
                if i < step_index:
                    led.setColor_(Colors.LED_GREEN)
                    led.setGlowing_(True)
                    led.setBlink_(True)
                elif i == step_index:
                    if status == "in_progress":
                        led.setColor_(Colors.LED_YELLOW)
                        led.setGlowing_(True)
                        led.setBlink_(True)
                    elif status == "completed":
                        led.setColor_(Colors.LED_GREEN)
                        led.setGlowing_(True)
                        led.setBlink_(True)
                    elif status == "failed":
                        led.setColor_(Colors.LED_RED)
                        led.setGlowing_(True)
                        led.setBlink_(True)
                else:
                    led.setColor_(Colors.LED_OFF)
                    led.setGlowing_(False)
                    led.setBlink_(True)
        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_update)

    def complete_step(self, step_index):
        self.set_step(step_index, "completed")
        if step_index + 1 < len(self.steps):
            self.set_step(step_index + 1, "in_progress")

    def set_modem_active(self, active):
        self.modem_active = active

    def show_completion(self, onion_address=None):
        def _complete():
            self.set_tor_final_hop_connected()
            self.set_progress(1.0, "COMPLETE")
            for led in self.leds:
                led.setColor_(Colors.LED_GREEN)
                led.setGlowing_(True)
            if self.status_label:
                self.status_label.setStringValue_(">> SETUP COMPLETE // SECURE CONNECTION ESTABLISHED")
                self.status_label.setTextColor_(Colors.DARK_GREEN)
            if self.terminal_view:
                self.terminal_view.addLine_("[OK] ALL SYSTEMS OPERATIONAL")
                if onion_address:
                    self.terminal_view.addLine_(f"[OK] ADDR: {onion_address[:20]}...")
                self.terminal_view.addLine_("[OK] READY FOR CONNECTIONS")
            self.set_modem_active(False)
        if threading.current_thread() is threading.main_thread():
            _complete()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_complete)


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_setup_window = None

def get_setup_window():
    global _setup_window
    if _setup_window is None:
        _setup_window = SetupProgressWindow.alloc().init()
    return _setup_window

def show_setup_progress():
    window = get_setup_window()
    window.show()
    return window

def show_welcome_screen(on_continue=None, on_cancel=None):
    """Show the welcome screen with callbacks for user actions."""
    window = get_setup_window()
    window.set_callbacks(on_continue=on_continue, on_cancel=on_cancel)
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


# =============================================================================
# DEMO (run this file directly to see the setup + Tor hop animation)
# =============================================================================

if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    window = get_setup_window()

    def on_continue():
        window.transition_to_progress()
        threading.Thread(target=_demo_setup, daemon=True).start()

    def on_cancel():
        app.terminate_(None)

    def _demo_setup():
        time.sleep(0.3)
        # Steps 0–2: quick run-through so we get to Tor phase
        window.set_step(0, "in_progress")
        window.set_status("Checking system requirements")
        window.add_log("CHECKING MACOS VERSION...", "progress")
        time.sleep(0.4)
        window.add_log("MACOS DETECTED", "ok")
        window.add_log("PYTHON 3 FOUND", "ok")
        window.complete_step(0)
        window.set_step(1, "in_progress")
        window.set_status("Initializing container runtime")
        window.add_log("STARTING COLIMA VM...", "progress")
        time.sleep(0.5)
        window.add_log("VM INITIALIZED", "ok")
        window.complete_step(1)
        window.set_step(2, "in_progress")
        window.set_status("Downloading container images")
        window.add_log("FETCHING CONTAINER IMAGES...", "progress")
        for i in range(1, 11):
            window.set_progress(i / 10.0, "DOWNLOADING")
            time.sleep(0.15)
        window.add_log("ALL IMAGES DOWNLOADED", "ok")
        window.complete_step(2)
        # Step 3+: Tor phase — this shows the hop animation
        window.set_step(3, "in_progress")
        window.set_status("Generating vanity onion address")
        window.add_log("GENERATING VANITY PREFIX...", "progress")
        time.sleep(3)
        window.add_log("ADDRESS: op2abc...onion", "ok")
        window.complete_step(3)
        window.set_step(4, "in_progress")
        window.add_log("ALL CONTAINERS RUNNING", "ok")
        window.complete_step(4)
        window.set_step(5, "in_progress")
        window.add_log("ONION SERVICE PUBLISHED", "ok")
        window.complete_step(5)
        window.show_completion("op2abc123xyz.onion")
        time.sleep(4)
        app.terminate_(None)

    window.set_callbacks(on_continue=on_continue, on_cancel=on_cancel)
    window.show_welcome()
    app.run()
