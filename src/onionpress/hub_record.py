# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OnionPress: Copyright (C) 2026 Internet Archive and OnionPress contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""The OnionHeaven registration record, as plain dicts (issue #407).

~/.onionpress/onionheaven-registration.json says which hubs may hold this
install's onion keys. The Mac client (onionheaven.py) and the Linux
heartbeat daemon (linux/onionpress-heartbeat.py) both keep it; this is
the one copy of how a record is read: the left_on_hubs entries, the
"released" hold and the fields a rewrite carries over. Each client still
decides where the file lives and what lock orders its writers.
"""

import time
from datetime import datetime, timezone

from . import key_manager

# What a write that records a registration must not forget: the hubs that
# may still hold a key, and the launcher's "this key is about to go" hold.
CARRIED = ("left_on_hubs", "released")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def carry_over(data, previous):
    """*data* ready to be written: the CARRIED fields *previous* (the
    record on disk) has and *data* lacks are kept, and a CARRIED field
    *data* sets to None is dropped. Returns a new dict."""
    data = dict(data)
    for key in CARRIED:
        if key not in data and key in previous:
            data[key] = previous[key]
    for key in CARRIED:
        if data.get(key) is None:
            data.pop(key, None)
    return data


def missing_carried(data):
    """True when *data* leaves a CARRIED field unsaid (so the record on
    disk has to be read before writing)."""
    return any(key not in data for key in CARRIED)


def left_on_hubs(record):
    """The well-formed left_on_hubs entries of *record*."""
    entries = record.get("left_on_hubs")
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict)
            and key_manager.is_valid_onion_address(e.get("content_address"))
            and key_manager.is_valid_onion_address(e.get("onionheaven_address"))]


def pair(entry):
    return entry["content_address"], entry["onionheaven_address"]


def note_left_on_hub(record, addr, hub, reason):
    """Add (addr, hub) to *record*'s left_on_hubs, once."""
    entries = left_on_hubs(record)
    if not any(pair(e) == (addr, hub) for e in entries):
        entries.append({"content_address": addr, "onionheaven_address": hub,
                        "since": now_iso(), "reason": reason})
    record["left_on_hubs"] = entries


def forget_left_on_hub(record, addr, hub):
    """Drop (addr, hub) from *record*'s left_on_hubs; True if it was there."""
    entries = left_on_hubs(record)
    kept = [e for e in entries if pair(e) != (addr, hub)]
    record["left_on_hubs"] = kept
    return len(kept) != len(entries)


def address_on_hold(record, addr, now=None):
    """True while *record*'s "released" hold covers *addr*."""
    released = record.get("released")
    if not isinstance(released, dict) or released.get("content_address") != addr:
        return False
    until = released.get("until")
    now = time.time() if now is None else now
    return isinstance(until, (int, float)) and now < until
