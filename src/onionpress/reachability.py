"""The onion service recovery step both platforms run (issues #266, #310,
#480).

RecoveryWindow is the one #266 decision: each cycle's dual-probe outcome
goes to the RepublishTrigger; when a request is due it asks onionpress-tor's
watchdog for DEL_ONION + ADD_ONION (launcher_ops.request_watchdog_republish),
or with REPUBLISH_ON_UNREACHABLE=no only logs that it would have. The
window is reset on wake (note_wake) and on every cycle where no probe ran
(note_offline: no internet, stack stopped). The daemon (onionpress.daemon,
on both platforms since issue #407) owns one and feeds it its own
check_tor_reachability (which also drives the menu's colours,
ReachabilityStats and the VM-wedge probe); nothing touches the trigger
directly. Until #407 Linux's heartbeat client ran a ReachabilityLoop of
its own here, writing its verdict to reachability.json for the bash
status writer.
"""

import time
from typing import Callable, Optional

from . import launcher_ops
from .health import HealthResult, RepublishTrigger

def republish_enabled(read_config_value: Callable[[str, str], str]) -> bool:
    """REPUBLISH_ON_UNREACHABLE in ~/.onionpress/config; default on.
    "no" keeps the trigger in log-only mode, on both platforms."""
    value = read_config_value("REPUBLISH_ON_UNREACHABLE", "yes")
    return str(value).strip().lower() not in ("no", "0", "false", "off")


class RecoveryWindow:
    """The #266 recovery decision, shared by the Mac daemon and Linux.

    One instance per process, fed one dual-probe outcome per cycle. It
    owns the RepublishTrigger (rate-based window, cooldown: see its
    docstring) and the request to the watchdog; the caller owns the probe.
    """

    def __init__(
        self,
        docker,
        log_func: Callable[[str], None],
        read_config_value: Callable[[str, str], str],
        container: str = "onionpress-tor",
        trigger: Optional[RepublishTrigger] = None,
    ):
        self.docker = docker
        self.log = log_func
        self.read_config_value = read_config_value
        self.container = container
        self.trigger = trigger if trigger is not None else RepublishTrigger()

    def republish_enabled(self) -> bool:
        return republish_enabled(self.read_config_value)

    def decide(self, internally_ready: bool, reachable: Optional[bool],
               http_code: Optional[str], wedge_class: Optional[str] = None) -> str:
        """Feed one probe outcome to the trigger. Returns the reason when
        a republish is due this cycle, else "". Sends nothing."""
        now = time.time()
        if not self.trigger.observe(now, internally_ready, reachable,
                                    http_code, wedge_class):
            return ""
        return self.trigger.describe(now)

    def decide_result(self, hr: HealthResult) -> str:
        """decide() for a HealthChecker.full_check() result."""
        return self.decide(hr.tor_internally_ready, hr.tor_externally_reachable,
                           hr.external_http_code, hr.wedge_class)

    def request_republish(self, reason: str) -> None:
        """The trigger fired: ask the watchdog for DEL+ADD, or only log
        it with REPUBLISH_ON_UNREACHABLE=no."""
        if not self.republish_enabled():
            self.log(f"reachability: {reason} — would request descriptor "
                     f"republish (REPUBLISH_ON_UNREACHABLE=no, log-only)")
            return
        status = launcher_ops.request_watchdog_republish(
            self.docker, self.container, reason)
        self.log(f"reachability: {reason} — "
                 f"{launcher_ops.describe_republish_request(status)}")

    def step(self, internally_ready: bool, reachable: Optional[bool],
             http_code: Optional[str], wedge_class: Optional[str] = None) -> bool:
        """decide() and, when due, request_republish(). True if it fired."""
        reason = self.decide(internally_ready, reachable, http_code, wedge_class)
        if reason:
            self.request_republish(reason)
        return bool(reason)

    def note_wake(self) -> None:
        """Back from suspend. Issue #266: forget any pre-sleep failure
        window so the first probe after wake can't look like 10+ min of
        unreachability and trigger DEL+ADD on top of the watchdog's own
        wake ADD."""
        self.trigger.reset()

    def note_offline(self) -> None:
        """No probe ran this cycle (no internet, stack stopped): it is
        evidence of nothing, so an offline gap never counts towards a
        republish."""
        self.trigger.reset()
