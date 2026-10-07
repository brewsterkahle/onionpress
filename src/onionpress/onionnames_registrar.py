"""
Mac-side client that talks to OnionHome's /api/name/* endpoints.

The flow mirrors the existing OnionHeaven heartbeat pattern: we extract the
onion's Ed25519 keys on the Mac side (via key_manager), sign the payload
locally, and use `docker exec onionpress-tor curl --socks5-hostname ...` to
push the request over Tor. The tor container is in the hot path only
because SOCKS via Colima port-forwarding is broken (see CLAUDE.md).

This module exposes a single call surface — Registrar — so the menubar code
doesn't have to care about signing, docker plumbing, or JSON parsing. A
real OnionHome HTTP round-trip is not exercised by the unit tests; the
integration path is validated manually against a live op2home onion.
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from onionpress import key_manager
from onionpress import onion_auth


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# OnionHome's registrar lives at the same onion address as its OnionHeaven
# API, on port 8083 (exposed through the onion service).
DEFAULT_ONIONHOME_ADDRESS = (
    "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion"
)
DEFAULT_ONIONHOME_PORT = 8083
DEFAULT_CONTAINER = "onionpress-tor"
DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RegistrarResult:
    """Structured outcome of a registrar call.

    status: 'ok' | 'collision' | 'invalid' | 'forbidden' | 'not_found' |
            'unreachable' | 'error'
    reason: short machine-readable string (server's 'reason' field, or a
            local diagnostic like 'sign_failed', 'bad_key', 'no_address').
    http_status: HTTP status code if we got a response from the server,
                 else None.
    body: parsed JSON response body (dict) when available.
    suggestions: list of alternative names (populated on collision).
    """
    status: str
    reason: Optional[str] = None
    http_status: Optional[int] = None
    body: Optional[dict] = None
    suggestions: List[str] = field(default_factory=list)

    @property
    def ok(self):
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------

class Registrar:
    """Low-level client for OnionHome's onionname registry.

    Callers:
      - suggest(lang)            → RegistrarResult; body["onionname"] on ok
      - check(name)              → RegistrarResult; body["available"] on ok
      - lookup(name)             → RegistrarResult; body has onionaddress+url
      - register(name, onionaddr)→ signed; 'ok' or 'collision'
      - release(name, onionaddr) → signed
    """

    def __init__(self, docker_bin="docker", container=DEFAULT_CONTAINER,
                 onionhome_address=DEFAULT_ONIONHOME_ADDRESS,
                 onionhome_port=DEFAULT_ONIONHOME_PORT,
                 timeout=DEFAULT_TIMEOUT, log=None,
                 runner=None, key_source=None):
        self.docker_bin = docker_bin
        self.container = container
        self.onionhome_address = onionhome_address
        self.onionhome_port = onionhome_port
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        # `runner` is injected in tests — a callable(args, timeout) returning
        # (rc, stdout_str). In prod we shell out to docker exec curl.
        self._run = runner or self._default_runner
        # `key_source` is injected in tests — a callable returning
        # (expanded_key_64, public_key_32). In prod we read the live keystore.
        self._key_source = key_source or key_manager.extract_keys

    def _url(self, path):
        return f"http://{self.onionhome_address}:{self.onionhome_port}{path}"

    def _default_runner(self, curl_args, timeout):
        """Run `docker exec <container> curl …`, return (rc, stdout str)."""
        try:
            result = subprocess.run(
                [self.docker_bin, "exec", self.container, "curl", *curl_args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
            return result.returncode, result.stdout or ""
        except subprocess.TimeoutExpired:
            return -1, ""
        except Exception as e:
            self.log(f"registrar: docker exec failed: {e}")
            return -2, ""

    def _get(self, path):
        curl_args = [
            "-s", "-S", "-o", "-",
            "--max-time", str(self.timeout),
            "--socks5-hostname", "127.0.0.1:9050",
            "-w", "\n__HTTP_STATUS__:%{http_code}",
            self._url(path),
        ]
        rc, out = self._run(curl_args, timeout=self.timeout + 5)
        return self._parse_response(rc, out)

    def _post_json(self, path, payload):
        body = json.dumps(payload)
        curl_args = [
            "-s", "-S", "-o", "-",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "--data", body,
            "--max-time", str(self.timeout),
            "--socks5-hostname", "127.0.0.1:9050",
            "-w", "\n__HTTP_STATUS__:%{http_code}",
            self._url(path),
        ]
        rc, out = self._run(curl_args, timeout=self.timeout + 5)
        return self._parse_response(rc, out)

    def _parse_response(self, rc, raw):
        """Turn (rc, raw_curl_output) into a RegistrarResult.

        curl is invoked with `-w '\\n__HTTP_STATUS__:%{http_code}'` so the
        HTTP status code is appended after the body and we can separate
        body from status without pulling in header parsing.
        """
        if rc != 0 or not raw:
            reason = {
                -1: "timeout", -2: "exec_failed",
                6: "dns_error", 7: "connect_refused",
                28: "timeout", 52: "empty_reply",
                56: "connection_reset",
            }.get(rc, f"curl_rc_{rc}")
            return RegistrarResult(status="unreachable", reason=reason)

        marker = "\n__HTTP_STATUS__:"
        idx = raw.rfind(marker)
        if idx == -1:
            return RegistrarResult(
                status="error", reason="missing_status_marker",
                body={"raw": raw[:200]},
            )
        body_str = raw[:idx]
        try:
            http_status = int(raw[idx + len(marker):].strip())
        except ValueError:
            return RegistrarResult(
                status="error", reason="bad_status_marker",
                body={"raw": raw[:200]},
            )

        body = None
        if body_str.strip():
            try:
                body = json.loads(body_str)
            except json.JSONDecodeError:
                return RegistrarResult(
                    status="error", reason="bad_json",
                    http_status=http_status,
                    body={"raw": body_str[:200]},
                )

        if 200 <= http_status < 300:
            return RegistrarResult(
                status="ok", http_status=http_status, body=body,
            )

        status = {
            400: "invalid",
            403: "forbidden",
            404: "not_found",
            409: "collision",
        }.get(http_status, "error")

        reason = body.get("error") if isinstance(body, dict) else None
        suggestions = body.get("suggestions", []) if isinstance(body, dict) else []
        return RegistrarResult(
            status=status,
            reason=reason,
            http_status=http_status,
            body=body,
            suggestions=suggestions or [],
        )

    # -- unsigned GETs ------------------------------------------------------

    def suggest(self, lang="en"):
        return self._get(f"/api/name/suggest?lang={lang}")

    def check(self, name):
        from urllib.parse import quote
        return self._get(f"/api/name/check?name={quote(name)}")

    def lookup(self, name):
        from urllib.parse import quote
        return self._get(f"/api/name/lookup/{quote(name)}")

    # -- signed POSTs -------------------------------------------------------

    def _signed_payload(self, endpoint, onionaddress, name):
        try:
            expanded, pub = self._key_source()
        except Exception as e:
            self.log(f"registrar: key extraction failed: {e}")
            return None
        if not expanded or not pub or len(pub) != 32:
            return None
        ts = onion_auth.make_timestamp()
        try:
            sig = onion_auth.sign_name_payload(
                expanded, pub, endpoint, onionaddress, name, ts,
            )
        except Exception as e:
            self.log(f"registrar: sign_name_payload failed: {e}")
            return None
        return {
            "onionname": name,
            "onionaddress": onionaddress,
            "timestamp": ts,
            "signature": sig,
        }

    def register(self, name, onionaddress):
        payload = self._signed_payload("register", onionaddress, name)
        if payload is None:
            return RegistrarResult(status="error", reason="sign_failed")
        return self._post_json("/api/name/register", payload)

    def release(self, name, onionaddress):
        payload = self._signed_payload("release", onionaddress, name)
        if payload is None:
            return RegistrarResult(status="error", reason="sign_failed")
        return self._post_json("/api/name/release", payload)
