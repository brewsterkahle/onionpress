"""Mac power-management handler via IOKit (delay-inhibitor for system suspend).

This is the macOS equivalent of Linux's logind delay-inhibitor. macOS's
``NSWorkspaceWillSleepNotification`` is *informational* — observers are told
the system is going to sleep but cannot delay it. The actual mechanism for
holding the suspend is the lower-level IOKit ``IORegisterForSystemPower``
callback, which receives ``kIOMessageSystemWillSleep`` and holds the
suspend until we call ``IOAllowPowerChange`` (or until ~30s elapses, at
which point macOS forces sleep regardless).

Inside the will-sleep window we run our cleanup synchronously:
``/offline`` POST to OnionHeaven, ``USR1`` to the in-container tor-watchdog
so it DEL_ONIONs services before Colima freezes the VM. As soon as both
return (or a budget expires), we call ``IOAllowPowerChange`` and sleep
proceeds. Typical work is well under 5s; the 30s ceiling is purely
defensive.

PyObjC has no bindings for ``IOKit/pwr_mgt`` functions. We load them via
``ctypes`` from ``/System/Library/Frameworks/IOKit.framework/IOKit``.
CoreFoundation is loaded the same way for the run-loop integration.

See: Apple's IOKit/IOPMLib.h and IOKit/IOMessage.h for the underlying API
contract.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
from ctypes import (
    CFUNCTYPE,
    POINTER,
    byref,
    c_int,
    c_uint32,
    c_void_p,
)


# ---------------------------------------------------------------------------
# Framework loading (deferred so import on Linux is harmless)
# ---------------------------------------------------------------------------

_iokit = None
_cf = None
_load_error: Exception | None = None


def _load_frameworks() -> None:
    """Load IOKit and CoreFoundation via ctypes. Idempotent; sets module-
    level globals on first success. On failure stores the exception in
    ``_load_error`` so callers can surface a useful diagnostic."""
    global _iokit, _cf, _load_error
    if _iokit is not None and _cf is not None:
        return
    try:
        iokit_path = ctypes.util.find_library("IOKit") or \
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        cf_path = ctypes.util.find_library("CoreFoundation") or \
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        iokit = ctypes.CDLL(iokit_path)
        cf = ctypes.CDLL(cf_path)

        # IOServiceInterestCallback:
        #   void (*)(void *refcon, io_service_t service,
        #            uint32_t messageType, void *messageArgument)
        iokit.IORegisterForSystemPower.restype = c_uint32  # io_connect_t
        iokit.IORegisterForSystemPower.argtypes = [
            c_void_p,                       # refcon
            POINTER(c_void_p),              # IONotificationPortRef* (out)
            _IOServiceInterestCallback,     # callback
            POINTER(c_uint32),              # io_object_t* notifier (out)
        ]
        iokit.IOAllowPowerChange.restype = c_int  # IOReturn / kern_return_t
        iokit.IOAllowPowerChange.argtypes = [c_uint32, c_void_p]
        iokit.IODeregisterForSystemPower.restype = c_int
        iokit.IODeregisterForSystemPower.argtypes = [POINTER(c_uint32)]
        iokit.IONotificationPortGetRunLoopSource.restype = c_void_p
        iokit.IONotificationPortGetRunLoopSource.argtypes = [c_void_p]

        cf.CFRunLoopGetMain.restype = c_void_p
        cf.CFRunLoopGetMain.argtypes = []
        cf.CFRunLoopAddSource.restype = None
        cf.CFRunLoopAddSource.argtypes = [c_void_p, c_void_p, c_void_p]

        _iokit = iokit
        _cf = cf
    except Exception as e:  # pragma: no cover (only reachable on broken macOS install / wrong platform)
        _load_error = e


# ctypes callback signature for IOServiceInterestCallback. Defined at module
# scope because referencing it inside _load_frameworks would need a forward
# declaration; this is cheaper.
_IOServiceInterestCallback = CFUNCTYPE(None, c_void_p, c_uint32, c_uint32, c_void_p)


# ---------------------------------------------------------------------------
# IOKit power-management message constants (IOKit/IOMessage.h)
# ---------------------------------------------------------------------------

kIOMessageCanSystemSleep     = 0xE0000270  # may be vetoed by IOCancelPowerChange
kIOMessageSystemWillSleep    = 0xE0000280  # MUST call IOAllowPowerChange to proceed
kIOMessageSystemWillPowerOn  = 0xE0000320  # wake started, system not fully up
kIOMessageSystemHasPoweredOn = 0xE0000300  # wake complete


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class MacPowerHandler:
    """Holds an IOKit delay-inhibitor and dispatches sleep/wake to callbacks.

    Usage::

        handler = MacPowerHandler(
            on_will_sleep=app.handle_sleep,
            on_has_powered_on=app.handle_wake,
            log=app.log,
        )
        if not handler.register():
            # fall back to NSWorkspaceWillSleepNotification observer
            ...

    ``on_will_sleep`` is invoked synchronously on the main run loop while
    macOS holds the suspend. It must return within the IOKit deadline
    (~30s; we recommend keeping work under 5s). Exceptions are caught so
    they can't strand the system in the will-sleep state — we always call
    ``IOAllowPowerChange`` in a ``finally`` block.

    ``on_has_powered_on`` is invoked when the system has finished waking
    up. No timing constraint; just routine wake-handling.

    ``run_loop_once``, ``stop`` and ``OFFLINE_MAX_TIME`` are the interface
    onionpress.power.LogindPowerHandler shares, so the daemon drives
    either the same way (issue #407).
    """

    # The /offline inside the will-sleep window may take this long: IOKit
    # allows ~30 s; 5 s has always been the budget.
    OFFLINE_MAX_TIME = 5

    def __init__(self, on_will_sleep, on_has_powered_on, log=print):
        self._on_will_sleep = on_will_sleep
        self._on_has_powered_on = on_has_powered_on
        self._log = log
        self._root_port = c_uint32(0)
        self._notification_port = c_void_p()
        self._notifier = c_uint32(0)
        # Keep the callback alive — ctypes does NOT retain CFUNCTYPE
        # instances passed to C, so losing this reference would cause the
        # callback to be garbage-collected and IOKit would crash the
        # process on its next invocation.
        self._callback_ref = _IOServiceInterestCallback(self._dispatch)

    def register(self) -> bool:
        """Register for IOKit power events. Returns True on success.

        On failure (wrong platform, missing framework, IOKit refused
        registration) returns False so callers can fall back to a
        less-capable mechanism. The exception that caused the failure is
        logged via the supplied ``log`` callable.
        """
        _load_frameworks()
        if _iokit is None or _cf is None:
            self._log(
                "MacPowerHandler: IOKit/CoreFoundation unavailable "
                f"({_load_error})"
            )
            return False
        if self._root_port.value:
            return True  # already registered

        try:
            port = _iokit.IORegisterForSystemPower(
                None,
                byref(self._notification_port),
                self._callback_ref,
                byref(self._notifier),
            )
            if port == 0:
                self._log("MacPowerHandler: IORegisterForSystemPower returned 0")
                return False
            self._root_port = c_uint32(port)

            source = _iokit.IONotificationPortGetRunLoopSource(
                self._notification_port
            )
            if not source:
                self._log("MacPowerHandler: IONotificationPortGetRunLoopSource returned NULL")
                return False

            # CFRunLoopAddSource on the main run loop with kCFRunLoopCommonModes.
            # We pass NULL for the mode here, which CoreFoundation interprets
            # as kCFRunLoopDefaultMode — sufficient for our needs since the
            # main run loop is always in default mode when not modal. (Pulling
            # the actual kCFRunLoopCommonModes CFStringRef out of CoreFoundation
            # via ctypes is finicky with the *_p_t indirection; default mode
            # is the pragmatic choice and works identically here.)
            run_loop = _cf.CFRunLoopGetMain()
            # Resolve kCFRunLoopCommonModes for completeness — it's a CFStringRef
            # exposed as a global.
            try:
                common_modes = c_void_p.in_dll(_cf, "kCFRunLoopCommonModes").value
            except Exception:
                common_modes = None
            _cf.CFRunLoopAddSource(run_loop, source, common_modes)

            self._log(
                f"MacPowerHandler: registered for IOKit power events "
                f"(root_port={port})"
            )
            return True
        except Exception as e:
            self._log(f"MacPowerHandler: register failed: {e}")
            return False

    def run_loop_once(self, seconds: float) -> bool:
        """Turn the main thread's CFRunLoop (the module's run_loop_once)
        so the callbacks fire; False when the frameworks are not loaded."""
        return run_loop_once(seconds)

    def stop(self) -> None:
        """Deregister (the daemon's shutdown); best effort."""
        if _iokit is None or not self._root_port.value:
            return
        try:
            _iokit.IODeregisterForSystemPower(byref(self._notifier))
        except Exception:
            pass

    def _dispatch(self, refcon, service, message_type, message_arg):
        """C callback. Runs on the main run loop, synchronous to IOKit."""
        try:
            if message_type == kIOMessageSystemWillSleep:
                self._handle_will_sleep(message_arg)
            elif message_type == kIOMessageSystemHasPoweredOn:
                self._handle_has_powered_on()
            elif message_type == kIOMessageCanSystemSleep:
                # We don't veto sleeps; allow.
                if _iokit is not None:
                    _iokit.IOAllowPowerChange(self._root_port, message_arg)
        except Exception as e:
            # Defensive: never let an exception escape into ctypes' frame
            # boundary, and always try to allow the change so we don't
            # strand the system.
            self._log(f"MacPowerHandler: dispatch error: {e}")
            try:
                if _iokit is not None:
                    _iokit.IOAllowPowerChange(self._root_port, message_arg)
            except Exception:
                pass

    def _handle_will_sleep(self, message_arg):
        """Run on-will-sleep callback synchronously, then allow sleep."""
        t0 = time.monotonic()
        try:
            self._on_will_sleep()
        except Exception as e:
            self._log(f"MacPowerHandler: on_will_sleep raised: {e}")
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            # IOAllowPowerChange MUST be called — otherwise macOS forces
            # sleep after its internal deadline (~30s), but during that
            # window the system is in a wedged state. Always call.
            if _iokit is not None:
                _iokit.IOAllowPowerChange(self._root_port, message_arg)
            self._log(
                f"MacPowerHandler: sleep allowed after {elapsed_ms:.0f}ms work"
            )

    def _handle_has_powered_on(self):
        try:
            self._on_has_powered_on()
        except Exception as e:
            self._log(f"MacPowerHandler: on_has_powered_on raised: {e}")


# ---------------------------------------------------------------------------
# Driving the main run loop outside an NSApplication (the daemon)
# ---------------------------------------------------------------------------

def run_loop_once(seconds: float) -> bool:
    """Run the main thread's CFRunLoop for up to *seconds* so IOKit's
    callbacks (registered above) fire. The menubar's NSApplication ran
    the loop itself; `onionpress daemon` has no AppKit, so it calls this
    a second at a time, and between calls Python's own signal handlers
    run (they never run while the interpreter is inside a C call).
    Returns False when the frameworks are not loaded (not a Mac): the
    caller sleeps instead."""
    _load_frameworks()
    if _cf is None:
        return False
    try:
        if not hasattr(_cf.CFRunLoopRunInMode, "_configured"):
            _cf.CFRunLoopRunInMode.restype = ctypes.c_int32
            _cf.CFRunLoopRunInMode.argtypes = [c_void_p, ctypes.c_double, ctypes.c_bool]
            _cf.CFRunLoopRunInMode._configured = True
        mode = c_void_p.in_dll(_cf, "kCFRunLoopDefaultMode").value
        _cf.CFRunLoopRunInMode(mode, float(seconds), False)
        return True
    except Exception:
        return False
