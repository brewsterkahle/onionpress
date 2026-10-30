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

"""The Linux launcher's status.json (issue #406).

linux/onionpress gathers the probes (docker, the log, the registry) and
hands them here by name, as `onionpress linux-status --field NAME=VALUE`;
this builds the document. It used to be an inline `python3 -c` fed 26
positional arguments.
"""

import json

# field name -> (how its text is read, default when absent or unreadable)
_INT, _BOOL, _STR, _JSON = "int", "bool", "str", "json"
FIELDS = {
    "state": (_STR, "unknown"),
    "version": (_STR, ""),
    "onion_address": (_STR, ""),
    "uptime_seconds": (_INT, 0),
    "bootstrap_pct": (_INT, 0),
    "containers": (_JSON, {}),
    "wayback_queue_count": (_INT, 0),
    "updated_at": (_STR, ""),
    "oh_server_active": (_BOOL, False),
    "oh_registered_count": (_INT, 0),
    "oh_online_count": (_INT, 0),
    "oh_taken_over_count": (_INT, 0),
    "oh_takeover_containers": (_INT, 0),
    "oh_client_registered": (_BOOL, False),
    "oh_client_hub": (_STR, ""),
    "oh_client_enabled": (_STR, "no"),
    "load_avg": (_JSON, []),
    "host_uptime_seconds": (_INT, 0),
    "wp_installed": (_BOOL, False),
    "onboarded": (_BOOL, False),
    "mkp224o_available": (_BOOL, False),
    "wp_admin_password_path": (_STR, ""),
    "vanity_in_progress": (_BOOL, False),
    "serving_from_wayback": (_BOOL, False),
    "wp_port": (_INT, 8080),
    "onionheaven_overview": (_JSON, {}),
}

# What the OnionHeaven overview contributes to status.json's block.
OVERVIEW_KEYS = ("client", "hub", "known_hosts", "generated_at")


def _read(kind, text, default):
    if text is None:
        return default
    if kind == _STR:
        return text
    if kind == _BOOL:
        return text == "true"
    try:
        if kind == _INT:
            return int(text)
        return json.loads(text)
    except ValueError:
        return default


def parse_fields(pairs):
    """{name: value} from "NAME=VALUE" strings. An unknown name is a
    mistake in the caller, so it raises ValueError."""
    raw = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or name not in FIELDS:
            raise ValueError(f"unknown status field: {name!r}")
        raw[name] = value
    return {name: _read(kind, raw.get(name), default)
            for name, (kind, default) in FIELDS.items()}


def build(f):
    """status.json's content from parse_fields' result."""
    overview = f["onionheaven_overview"]
    if not isinstance(overview, dict):
        overview = {}
    return {
        "state": f["state"],
        "version": f["version"],
        "onion_address": f["onion_address"],
        "uptime_seconds": f["uptime_seconds"],
        "bootstrap_pct": f["bootstrap_pct"],
        "containers": f["containers"],
        "wayback_queue_count": f["wayback_queue_count"],
        "updated_at": f["updated_at"],
        "platform": "linux",
        "load_avg": f["load_avg"],
        "host_uptime_seconds": f["host_uptime_seconds"],
        "onionheaven": {
            "server_active": f["oh_server_active"],
            "registered_count": f["oh_registered_count"],
            "online_count": f["oh_online_count"],
            "taken_over_count": f["oh_taken_over_count"],
            "takeover_containers": f["oh_takeover_containers"],
            "client_registered": f["oh_client_registered"],
            "client_hub": f["oh_client_hub"],
            "client_enabled": f["oh_client_enabled"] == "yes",
            **{k: v for k, v in overview.items() if k in OVERVIEW_KEYS},
        },
        "wp_installed": f["wp_installed"],
        "onboarded": f["onboarded"],
        "mkp224o_available": f["mkp224o_available"],
        "wp_admin_password_path": f["wp_admin_password_path"] or None,
        "vanity_in_progress": f["vanity_in_progress"],
        "serving_from_wayback": f["serving_from_wayback"],
        "wp_port": f["wp_port"],
    }
