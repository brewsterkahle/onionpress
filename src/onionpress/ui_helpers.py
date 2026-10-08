"""macOS AppKit UI helper classes for OnionPress.

Extracted from menubar.py — these are self-contained UI components
with no dependency on the OnionPressApp instance.
"""

import os
import threading
import time

import AppKit


class HelpButtonTarget(AppKit.NSObject):
    """ObjC target for (?) help buttons in the settings dialog."""
    _help_texts = {}
    _icon_path = None

    def helpClicked_(self, sender):
        text = self._help_texts.get(sender.tag(), "")
        if text:
            a = AppKit.NSAlert.alloc().init()
            a.setMessageText_("Help")
            a.setInformativeText_(text)
            if self._icon_path and os.path.exists(self._icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(self._icon_path)
                if icon:
                    a.setIcon_(icon)
            a.runModal()


def parse_version(version_str):
    """Parse a version string like '2.10.3' into a tuple of ints for comparison."""
    try:
        return tuple(int(x) for x in version_str.split('.'))
    except (ValueError, AttributeError):
        return (0,)


_main_thread_log_func = None


def set_main_thread_logger(log_func):
    """Set the log function used by main_thread() for error reporting."""
    global _main_thread_log_func
    _main_thread_log_func = log_func


def main_thread(func):
    """Run func on the main thread (required for AppKit UI updates).

    Wraps the callback in a try/except so Python exceptions don't
    propagate through PyObjC into Objective-C — that causes SIGABRT.
    """
    def _safe_wrapper():
        try:
            func()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            if _main_thread_log_func:
                _main_thread_log_func(f"CRASH PREVENTED in UI callback:\n{tb}")
            else:
                import sys
                print(f"[main_thread] Exception in UI callback:\n{tb}", file=sys.stderr)

    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_safe_wrapper)


class BackupProgressWindow:
    """A small floating window that shows backup/restore progress."""

    def __init__(self, title):
        self._title = title
        self._window = None
        self._status_field = None

    def show(self):
        w = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 380, 120),
            AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered,
            False
        )
        w.setTitle_(self._title)
        w.setLevel_(AppKit.NSFloatingWindowLevel)
        w.center()
        w.setReleasedWhenClosed_(False)
        w.setHidesOnDeactivate_(False)

        content = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 380, 120))

        # Spinner
        spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 75, 24, 24))
        spinner.setStyle_(1)  # NSProgressIndicatorStyleSpinning
        spinner.startAnimation_(None)
        content.addSubview_(spinner)
        self._spinner = spinner

        # Title label
        title_label = AppKit.NSTextField.labelWithString_(self._title + "...")
        title_label.setFrame_(AppKit.NSMakeRect(52, 77, 300, 20))
        title_label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(14))
        content.addSubview_(title_label)

        # Status text
        status = AppKit.NSTextField.labelWithString_("Starting...")
        status.setFrame_(AppKit.NSMakeRect(20, 20, 340, 45))
        status.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        status.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        content.addSubview_(status)
        self._status_field = status

        w.setContentView_(content)
        w.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._window = w

    def update(self, message):
        if self._status_field:
            self._status_field.setStringValue_(message)

    def finish(self, message, on_done=None):
        # Close the progress window and show a simple alert. If on_done is given,
        # run it AFTER the user dismisses this alert — but re-dispatched onto the
        # main queue (not synchronously), so the modal session fully unwinds
        # before any follow-up modal opens (avoids the DELETE/confirm window
        # appearing behind or without focus). Used by the uninstall "backup
        # first" flow to proceed only once the backup's Done alert is dismissed.
        if self._window:
            self._window.orderOut_(None)
            self._window = None
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Done")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        # Set app icon
        import os
        bundle = AppKit.NSBundle.mainBundle()
        if bundle and bundle.resourcePath():
            icon_path = os.path.join(bundle.resourcePath(), "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)
        alert.runModal()
        if on_done:
            main_thread(on_done)

    def finish_with_restart(self, message):
        """Close progress window, show alert with Restart Now button."""
        if self._window:
            self._window.orderOut_(None)
            self._window = None
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Done")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("Restart Now")
        alert.addButtonWithTitle_("Later")
        # Set app icon
        import os
        bundle = AppKit.NSBundle.mainBundle()
        if bundle and bundle.resourcePath():
            icon_path = os.path.join(bundle.resourcePath(), "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)
        response = alert.runModal()
        if response == AppKit.NSAlertFirstButtonReturn:
            # Restart: relaunch the outer OnionPress.app
            # bundlePath() is MenubarApp inside OnionPress.app/Contents/Resources/MenubarApp
            # Walk up to find the outer .app bundle
            import subprocess
            path = bundle.bundlePath()
            while path and path != "/":
                if path.endswith(".app") and not path.endswith("MenubarApp"):
                    break
                path = os.path.dirname(path)
            if not path or path == "/":
                # Fallback: try /Applications/OnionPress.app
                path = "/Applications/OnionPress.app"
            subprocess.Popen(["open", path])
            import rumps
            rumps.quit_application()


class LogViewerActions(AppKit.NSObject):
    """Singleton handling custom log viewer menu actions."""

    _shared = None

    @classmethod
    def shared(cls):
        if cls._shared is None:
            cls._shared = cls.alloc().init()
        return cls._shared

    @staticmethod
    def _active_viewer():
        key_win = AppKit.NSApp.keyWindow()
        if not key_win:
            return None
        for inst in LogViewerWindow._instances.values():
            if inst._window is key_win:
                return inst
        return None

    def clearLog_(self, sender):
        viewer = self._active_viewer()
        if viewer:
            try:
                open(viewer._file_path, 'w').close()
                viewer._text_view.textStorage().mutableString().setString_("")
                viewer._offset = 0
            except Exception:
                pass

    def toggleWordWrap_(self, sender):
        viewer = self._active_viewer()
        if not viewer:
            return
        tv = viewer._text_view
        container = tv.textContainer()
        scroll = tv.enclosingScrollView()
        if container.widthTracksTextView():
            # Disable word wrap — enable horizontal scrolling
            container.setWidthTracksTextView_(False)
            container.setContainerSize_(AppKit.NSMakeSize(1e7, 1e7))
            tv.setHorizontallyResizable_(True)
            if scroll:
                scroll.setHasHorizontalScroller_(True)
        else:
            # Enable word wrap
            if scroll:
                scroll.setHasHorizontalScroller_(False)
                width = scroll.contentView().bounds().size.width
            else:
                width = tv.frame().size.width
            container.setContainerSize_(AppKit.NSMakeSize(width, 1e7))
            container.setWidthTracksTextView_(True)
            tv.setHorizontallyResizable_(False)

    def increaseFontSize_(self, sender):
        viewer = self._active_viewer()
        if viewer:
            font = viewer._text_view.font()
            new_size = min(font.pointSize() + 2, 36)
            viewer._text_view.setFont_(
                AppKit.NSFont.fontWithName_size_(font.fontName(), new_size))

    def decreaseFontSize_(self, sender):
        viewer = self._active_viewer()
        if viewer:
            font = viewer._text_view.font()
            new_size = max(font.pointSize() - 2, 8)
            viewer._text_view.setFont_(
                AppKit.NSFont.fontWithName_size_(font.fontName(), new_size))


class LogViewerWindow:
    """A read-only log viewer window with live tailing."""

    _instances = {}  # file_path -> instance (singleton per file)

    @classmethod
    def show_for_file(cls, file_path, title):
        """Show (or refocus) a log viewer for the given file."""
        existing = cls._instances.get(file_path)
        if existing and existing._window and existing._window.isVisible():
            existing._window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            return existing
        inst = cls(file_path, title)
        cls._instances[file_path] = inst
        inst._show()
        return inst

    @classmethod
    def close_all(cls):
        """Close all open log viewer windows and stop their polling threads."""
        for inst in list(cls._instances.values()):
            inst._stop()
        cls._instances.clear()

    def __init__(self, file_path, title):
        self._file_path = file_path
        self._title = title
        self._window = None
        self._text_view = None
        self._offset = 0
        self._running = False

    def _show(self):
        style = (AppKit.NSWindowStyleMaskTitled
                 | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskResizable
                 | AppKit.NSWindowStyleMaskMiniaturizable)
        w = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 720, 480), style,
            AppKit.NSBackingStoreBuffered, False)
        w.setTitle_(self._title)
        w.setLevel_(AppKit.NSNormalWindowLevel)
        w.center()
        w.setReleasedWhenClosed_(False)
        w.setHidesOnDeactivate_(False)

        # Scroll view fills the window
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 720, 480))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        # Text view
        tv = AppKit.NSTextView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 720, 480))
        tv.setEditable_(False)
        tv.setSelectable_(True)
        self._log_font = AppKit.NSFont.fontWithName_size_("Menlo", 12)
        self._log_text_color = AppKit.NSColor.textColor()
        tv.setFont_(self._log_font)
        tv.setTextColor_(self._log_text_color)
        tv.setBackgroundColor_(AppKit.NSColor.textBackgroundColor())
        tv.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        # Allow horizontal scrolling for long lines
        tv.setHorizontallyResizable_(False)
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setUsesFindBar_(True)
        tv.setIncrementalSearchingEnabled_(True)

        scroll.setDocumentView_(tv)
        w.setContentView_(scroll)

        self._window = w
        self._text_view = tv

        # Load initial content (last 500 lines)
        self._load_initial()

        # Ensure the app has an Edit menu so Cmd+C/A/V work in the text view.
        self._ensure_edit_menu()

        w.makeKeyAndOrderFront_(None)
        w.makeFirstResponder_(tv)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

        # Start polling thread
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _load_initial(self):
        """Read last 500 lines of the file and display them."""
        try:
            if not os.path.exists(self._file_path):
                self._offset = 0
                return
            with open(self._file_path, 'r', encoding='utf-8', errors='replace') as f:
                # Seek backwards to find last 500 lines
                f.seek(0, 2)
                file_size = f.tell()
                if file_size == 0:
                    self._offset = 0
                    return
                # Read in chunks from the end to find 500 newlines
                chunk_size = 8192
                lines_found = 0
                pos = file_size
                while pos > 0 and lines_found < 500:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    lines_found += chunk.count('\n')
                # Now read from pos to end
                f.seek(pos)
                content = f.read()
                # If we overshot, trim to last 500 lines
                if lines_found > 500:
                    lines = content.split('\n')
                    content = '\n'.join(lines[-(500 + 1):])
                self._offset = file_size
            if content:
                self._append_attributed(content)
                # Scroll to bottom
                end = self._text_view.textStorage().length()
                self._text_view.scrollRangeToVisible_(AppKit.NSMakeRange(end, 0))
        except Exception:
            self._offset = 0

    def _is_near_bottom(self):
        """Check if the scroll position is near the bottom."""
        scroll_view = self._text_view.enclosingScrollView()
        if not scroll_view:
            return True
        clip = scroll_view.contentView()
        doc_height = self._text_view.frame().size.height
        clip_height = clip.bounds().size.height
        scroll_y = clip.bounds().origin.y
        # "Near bottom" = within 50 points of the end
        return (scroll_y + clip_height) >= (doc_height - 50)

    def _append_attributed(self, text):
        """Append text with correct font and color (respects dark mode)."""
        attrs = {
            AppKit.NSFontAttributeName: self._log_font,
            AppKit.NSForegroundColorAttributeName: self._log_text_color,
        }
        astr = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        self._text_view.textStorage().appendAttributedString_(astr)

    def _poll_loop(self):
        """Background thread: poll file for new content every 1.5s."""
        while self._running:
            time.sleep(1.5)
            if not self._running:
                break
            try:
                visible = self._window and self._window.isVisible()
            except Exception:
                visible = False
            if not visible:
                self._running = False
                LogViewerWindow._instances.pop(self._file_path, None)
                break
            try:
                if not os.path.exists(self._file_path):
                    continue
                file_size = os.path.getsize(self._file_path)
                if file_size < self._offset:
                    # File was truncated — reload
                    self._offset = 0
                    def reload():
                        storage = self._text_view.textStorage()
                        storage.deleteCharactersInRange_(AppKit.NSMakeRange(0, storage.length()))
                        self._load_initial()
                    main_thread(reload)
                    continue
                if file_size == self._offset:
                    continue
                # Read new content
                with open(self._file_path, 'r', encoding='utf-8',
                           errors='replace') as f:
                    f.seek(self._offset)
                    new_content = f.read()
                self._offset = file_size
                if new_content:
                    def append(text=new_content):
                        was_near = self._is_near_bottom()
                        self._append_attributed(text)
                        if was_near:
                            end = self._text_view.textStorage().length()
                            self._text_view.scrollRangeToVisible_(
                                AppKit.NSMakeRange(end, 0))
                    main_thread(append)
            except Exception:
                pass

    @staticmethod
    def _ensure_edit_menu():
        """Add Edit and View menus so standard key equivalents work."""
        main_menu = AppKit.NSApp.mainMenu()
        if not main_menu:
            main_menu = AppKit.NSMenu.alloc().init()
            AppKit.NSApp.setMainMenu_(main_menu)
        # Check if menus already exist
        for i in range(main_menu.numberOfItems()):
            if main_menu.itemAtIndex_(i).title() == "Edit":
                return

        actions = LogViewerActions.shared()

        # Edit menu — must include the full standard set: this app is
        # LSUIElement (no visible menu bar), but macOS still routes ⌘-key
        # equivalents through the main menu, so a missing Paste item means
        # ⌘V beeps in every text field (e.g. pasting a password from a
        # password manager into the setup window).
        edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
        edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
        edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        find_item = edit_menu.addItemWithTitle_action_keyEquivalent_("Find\u2026", "performFindPanelAction:", "f")
        find_item.setTag_(1)  # NSFindPanelActionShowFindPanel
        find_next = edit_menu.addItemWithTitle_action_keyEquivalent_("Find Next", "performFindPanelAction:", "g")
        find_next.setTag_(2)  # NSFindPanelActionNext
        find_prev = edit_menu.addItemWithTitle_action_keyEquivalent_("Find Previous", "performFindPanelAction:", "G")
        find_prev.setTag_(3)  # NSFindPanelActionPrevious
        edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        clear_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Clear Log", "clearLog:", "k")
        clear_item.setTarget_(actions)
        edit_menu.addItem_(clear_item)
        edit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Edit", None, "")
        edit_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_item)

        # View menu
        view_menu = AppKit.NSMenu.alloc().initWithTitle_("View")
        wrap_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Toggle Word Wrap", "toggleWordWrap:", "")
        wrap_item.setTarget_(actions)
        view_menu.addItem_(wrap_item)
        view_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        bigger = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Bigger", "increaseFontSize:", "=")
        bigger.setTarget_(actions)
        view_menu.addItem_(bigger)
        smaller = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Smaller", "decreaseFontSize:", "-")
        smaller.setTarget_(actions)
        view_menu.addItem_(smaller)
        view_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "View", None, "")
        view_item.setSubmenu_(view_menu)
        main_menu.addItem_(view_item)

    def _stop(self):
        """Stop polling and close the window."""
        self._running = False
        if self._window:
            try:
                self._window.orderOut_(None)
            except Exception:
                pass


def ensure_edit_menu():
    """Install the Edit/View main menu so ⌘C/⌘V/⌘X/⌘Z/⌘A work in all windows.

    Call once on the main thread at app startup. Safe to call repeatedly.
    """
    try:
        LogViewerWindow._ensure_edit_menu()
    except Exception:
        pass
