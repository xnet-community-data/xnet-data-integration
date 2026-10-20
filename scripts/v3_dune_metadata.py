#!/usr/bin/env python3
"""Read-only Dune MCP metadata client for the v3 credit guard.

This module talks to Dune's official remote MCP server
(``https://api.dune.com/mcp/v1``, Streamable HTTP) and exposes exactly one
operation: the documented ``getUsage`` account-metadata tool. It deliberately
has no API for executions, results, SQL, or any other MCP tool, so it cannot
spend credits.

The v1 ``/api/v1/usage`` endpoint returns date-only billing-period edges, which
the strict v3 budget guard refuses (midnight truncation is not an exact
renewal instant). ``getUsage`` returns exact ``billingPeriodStart`` /
``billingPeriodEnd`` instants plus the used/included credit counters; this
client normalizes them into the same period shape that
``v3_credit_budget.normalize_usage`` accepts:

    {
      "credits_used": float,
      "credits_included": float,
      "period_start_utc": "....Z",
      "period_end_utc": "....Z",
      "exact_end": True,
      "observed_at_utc": "....Z",
      "provider": {...},   # transport/tool/protocol proof
      "raw": {...},        # untouched getUsage metadata for audit
    }

Design constraints:

* stdlib only, single attempt per HTTP request (no automatic retries);
* ``x-dune-api-key`` comes from ``DUNE_API_KEY`` (or an injected key for
  tests) and is never logged, returned, or embedded in exception messages;
* HTTP timeouts are capped at 45 seconds;
* response bodies are bounded at 1 MB and parsed as JSON or as SSE
  (``data:`` events, including multiple events per response);
* period edges must be exact, timezone-aware UTC instants, counters must be
  finite nonnegative numbers (booleans rejected), and the period must be
  active at the observation instant.

The opener, API key, and current time are injectable for unit tests; no test
performs network I/O and no repository configuration is read.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:  # production: scripts/ is the script directory on sys.path
    from v3_credit_budget import BudgetInputError, normalize_usage, parse_instant
except ImportError:  # pragmatic import for tests loading this file by path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from v3_credit_budget import BudgetInputError, normalize_usage, parse_instant

MCP_ENDPOINT = "https://api.dune.com/mcp/v1"
MCP_PROTOCOL_VERSION = "2025-03-26"
CLIENT_NAME = "xnet-v3-dune-metadata"
CLIENT_VERSION = "1.0.0"
USAGE_TOOL = "getUsage"
MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 45.0
API_KEY_ENV = "DUNE_API_KEY"

_ALLOWED_TOOLS = frozenset({USAGE_TOOL})
_CREDITS_USED_KEYS = ("credits_used", "creditsUsed")
_CREDITS_INCLUDED_KEYS = (
    "credits_included",
    "creditsIncluded",
    "credits_quota",
    "creditsQuota",
)
_PERIOD_START_KEYS = ("period_start_utc", "billingPeriodStart", "billing_period_start")
_PERIOD_END_KEYS = ("period_end_utc", "billingPeriodEnd", "billing_period_end")
_NESTED_PAYLOAD_KEYS = ("usage", "result", "data")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(x-dune-api-key\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(r"(?i)(api_key\s*=\s*)[^&\s\"']+"),
)

__all__ = [
    "API_KEY_ENV",
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "DuneMetadataClient",
    "DuneMetadataError",
    "DuneMetadataProtocolError",
    "DuneMetadataTransportError",
    "DuneMetadataValidationError",
    "MAX_RESPONSE_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "MCP_ENDPOINT",
    "MCP_PROTOCOL_VERSION",
    "USAGE_TOOL",
]


# ---- errors -----------------------------------------------------------------

class DuneMetadataError(RuntimeError):
    """Base error for every metadata-client failure.

    Messages are sanitized of API-key material before they are raised; callers
    may log them verbatim.
    """

    code = "dune_metadata_error"


class DuneMetadataTransportError(DuneMetadataError):
    """Network/HTTP failure. Never retried automatically."""

    code = "transport_error"


class DuneMetadataProtocolError(DuneMetadataError):
    """Malformed JSON-RPC, MCP framing, or tool response."""

    code = "protocol_error"


class DuneMetadataValidationError(DuneMetadataError):
    """getUsage metadata failed strict budget validation."""

    code = "validation_error"


# ---- helpers ----------------------------------------------------------------

def _sanitize(message, secret):
    """Remove injected API-key material from any diagnostic text."""
    text = str(message)
    if secret:
        text = text.replace(str(secret), "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[redacted]", text)
    return text


def _z(value):
    """Render a UTC datetime as an ISO-8601 instant ending in Z."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _header(headers, name):
    """Case-insensitive header lookup for dicts, Messages, and mock objects."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        for candidate in (name, name.lower(), name.title()):
            try:
                value = getter(candidate)
            except (KeyError, TypeError):
                value = None
            if value:
                return value
    items = getattr(headers, "items", None)
    if callable(items):
        for key, value in headers.items():
            if str(key).lower() == name.lower() and value:
                return value
    return None


def _same_value(left, right):
    numeric = (int, float)
    if (
        isinstance(left, numeric)
        and isinstance(right, numeric)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ):
        return float(left) == float(right)
    return str(left).strip() == str(right).strip()


def _pick(payload, keys, label):
    """Resolve one logical field from candidate aliases, rejecting conflicts."""
    present = [
        (key, payload[key])
        for key in keys
        if key in payload and payload[key] is not None
    ]
    if not present:
        raise DuneMetadataValidationError(
            f"getUsage metadata has no {label} field (looked for "
            + ", ".join(keys)
            + ")"
        )
    first_key, first_value = present[0]
    for key, value in present[1:]:
        if not _same_value(first_value, value):
            raise DuneMetadataValidationError(
                f"getUsage metadata has conflicting {label} values: "
                f"{first_key}={first_value!r} vs {key}={value!r}"
            )
    return first_key, first_value


def _has_any(payload, keys):
    return any(key in payload and payload[key] is not None for key in keys)


def _unwrap_payload(payload):
    """Accept an optional single ``usage``/``result``/``data`` wrapper."""
    if _has_any(payload, _CREDITS_USED_KEYS) or _has_any(
        payload, _CREDITS_INCLUDED_KEYS
    ):
        return payload
    candidates = [
        payload[key]
        for key in _NESTED_PAYLOAD_KEYS
        if isinstance(payload.get(key), dict)
        and (
            _has_any(payload[key], _CREDITS_USED_KEYS)
            or _has_any(payload[key], _CREDITS_INCLUDED_KEYS)
        )
    ]
    if len(candidates) > 1:
        raise DuneMetadataValidationError(
            "getUsage metadata has multiple nested usage objects; refusing to "
            "guess which counters are authoritative"
        )
    return candidates[0] if candidates else payload


def _credit_counter(value, label):
    """Finite, nonnegative, non-boolean credit counter (strict)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DuneMetadataValidationError(
            f"{label} must be a finite nonnegative number, got "
            f"{type(value).__name__}: {value!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise DuneMetadataValidationError(
            f"{label} must be finite (no NaN/Infinity), got {value!r}"
        )
    if number < 0:
        raise DuneMetadataValidationError(
            f"{label} must be nonnegative, got {value!r}"
        )
    return number


def _exact_instant(value, label):
    """Exact timezone-aware instant; date-only values are refused outright."""
    if isinstance(value, str) and "T" not in value.strip():
        raise DuneMetadataValidationError(
            f"{label} is date-only ({value!r}); an exact UTC instant with a "
            "time component is required (midnight truncation is refused)"
        )
    if not isinstance(value, (str, datetime)):
        raise DuneMetadataValidationError(
            f"{label} must be an ISO-8601 instant string, got "
            f"{type(value).__name__}"
        )
    try:
        return parse_instant(value, label)
    except BudgetInputError as error:
        raise DuneMetadataValidationError(str(error)) from None


def _valid_api_key(value):
    """Header-safe API key: visible ASCII only, no whitespace/control chars."""
    return (
        isinstance(value, str)
        and bool(value)
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def _normalize_payload(payload, now):
    """Strictly normalize getUsage metadata into the v3 usage-period shape."""
    if not isinstance(payload, dict):
        raise DuneMetadataValidationError("getUsage metadata must be a JSON object")
    payload = _unwrap_payload(payload)

    _, used_raw = _pick(payload, _CREDITS_USED_KEYS, "credits_used")
    _, included_raw = _pick(payload, _CREDITS_INCLUDED_KEYS, "credits_included")
    _, start_raw = _pick(payload, _PERIOD_START_KEYS, "period_start_utc")
    _, end_raw = _pick(payload, _PERIOD_END_KEYS, "period_end_utc")

    used = _credit_counter(used_raw, "credits_used")
    included = _credit_counter(included_raw, "credits_included")
    start = _exact_instant(start_raw, "period_start_utc")
    end = _exact_instant(end_raw, "period_end_utc")

    if start >= end:
        raise DuneMetadataValidationError(
            "billing period start must precede its end: "
            f"{_z(start)} >= {_z(end)}"
        )
    if now < start:
        raise DuneMetadataValidationError(
            f"billing period has not started yet: starts {_z(start)}, "
            f"observed {_z(now)}"
        )
    if now >= end:
        raise DuneMetadataValidationError(
            f"billing period has expired: ended {_z(end)}, observed {_z(now)}; "
            "a renewed period is required for budget admission"
        )

    candidate = {
        "credits_used": used,
        "credits_included": included,
        "period_start_utc": _z(start),
        "period_end_utc": _z(end),
        "exact_end": True,
    }
    # One-period parsing is owned by the budget guard; use it verbatim rather
    # than a weaker local re-implementation.
    try:
        checked = normalize_usage(candidate, _z(now))
    except BudgetInputError as error:
        raise DuneMetadataValidationError(
            f"budget period parser rejected getUsage metadata: {error}"
        ) from None
    period = checked.get("period")
    if period is None or checked.get("blockers"):
        raise DuneMetadataValidationError(
            "budget period parser did not accept the getUsage period: "
            + "; ".join(checked.get("reasons") or ["no usable period"])
        )
    if period.get("period_end_utc") is None:
        raise DuneMetadataValidationError(
            "getUsage period_end_utc is not an exact instant"
        )
    for key in ("credits_used", "credits_included"):
        if abs(float(period[key]) - candidate[key]) > 1e-9:
            raise DuneMetadataValidationError(
                f"getUsage {key} changed during budget normalization"
            )

    return {
        "credits_used": used,
        "credits_included": included,
        "period_start_utc": candidate["period_start_utc"],
        "period_end_utc": candidate["period_end_utc"],
        "exact_end": True,
        "observed_at_utc": _z(now),
        "raw": payload,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the API key is never forwarded to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Dune MCP redirect refused to avoid forwarding credentials",
            headers,
            fp,
        )


# ---- client -----------------------------------------------------------------

class DuneMetadataClient:
    """Metadata-only Dune MCP client. The only public operation is get_usage."""

    def __init__(
        self,
        *,
        endpoint=MCP_ENDPOINT,
        api_key=None,
        opener=None,
        now=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        session_id=None,
        max_response_bytes=MAX_RESPONSE_BYTES,
        send_initialized=True,
    ):
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("Dune MCP endpoint must be an https:// URL")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a number of seconds")
        if not 0 < float(timeout) <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout must be > 0 and <= {MAX_TIMEOUT_SECONDS:g} seconds"
            )
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")

        self._endpoint = endpoint
        self._api_key = (
            api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        )
        self._default_opener = None
        if opener is None:
            self._default_opener = urllib.request.build_opener(_NoRedirect())
            default_opener = self._default_opener
            self._opener = lambda request, timeout: default_opener.open(
                request, timeout=timeout
            )
        elif callable(opener):
            self._opener = opener
        else:
            raise ValueError("opener must be callable")
        self._now = now
        self._timeout = float(timeout)
        self._session_id = session_id
        self._max_response_bytes = max_response_bytes
        self._send_initialized = bool(send_initialized)
        self._initialized = False
        self._request_id = 0
        self._server_protocol_version = None

    def __repr__(self):
        return f"DuneMetadataClient(endpoint={self._endpoint!r})"

    # -- public ---------------------------------------------------------------

    def get_usage(self, now=None):
        """Return the active billing period's normalized credit metadata.

        Raises ``DuneMetadataError`` subclasses on missing key, transport,
        protocol, or strict-validation failure. There is no retry and no
        fallback to the date-only v1 usage endpoint.
        """
        if not _valid_api_key(self._api_key):
            raise DuneMetadataValidationError(
                f"{API_KEY_ENV} is missing or not a header-safe Dune API key; "
                "Dune MCP metadata requires an API key in the environment"
            )
        observed_at = self._resolve_now(now)
        self._initialize()
        result = self._tools_call(USAGE_TOOL, {})
        payload = self._extract_payload(result)
        normalized = _normalize_payload(payload, observed_at)
        normalized["provider"] = {
            "transport": "mcp_streamable_http",
            "endpoint": self._endpoint,
            "tool": USAGE_TOOL,
            "protocol_version": (
                self._server_protocol_version or MCP_PROTOCOL_VERSION
            ),
            "session_established": bool(self._session_id),
        }
        return normalized

    # -- time -----------------------------------------------------------------

    def _resolve_now(self, override):
        value = override if override is not None else self._now
        if value is None:
            value = datetime.now(timezone.utc)
        if callable(value):
            value = value()
        try:
            return parse_instant(value, "now")
        except BudgetInputError as error:
            raise DuneMetadataValidationError(str(error)) from None

    # -- transport ------------------------------------------------------------

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if _valid_api_key(self._api_key):
            headers["x-dune-api-key"] = self._api_key
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers=self._headers(),
        )
        try:
            opened = self._opener(request, timeout=self._timeout)
        except DuneMetadataError:
            raise
        except Exception as error:  # network/HTTP failure; no retry
            raise DuneMetadataTransportError(
                self._transport_message(error)
            ) from None
        try:
            if hasattr(opened, "__enter__"):
                with opened as response:
                    return self._read_response(response)
            return self._read_response(opened)
        except DuneMetadataError:
            raise
        except Exception as error:
            raise DuneMetadataTransportError(
                self._transport_message(error)
            ) from None

    def _transport_message(self, error):
        status = getattr(error, "code", None)
        detail = _sanitize(error, self._api_key)[:300]
        if status is not None:
            return f"Dune MCP request failed with HTTP {status}: {detail}"
        return f"Dune MCP transport failed: {detail}"

    def _read_response(self, response):
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        if status is None:
            status = 200
        if isinstance(status, bool) or not isinstance(status, int):
            raise DuneMetadataProtocolError(
                f"Dune MCP returned an invalid HTTP status: {status!r}"
            )
        headers = getattr(response, "headers", None)
        read = getattr(response, "read", None)
        if not callable(read):
            raise DuneMetadataProtocolError(
                "Dune MCP response has no readable body"
            )
        raw = read(self._max_response_bytes + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)):
            raise DuneMetadataProtocolError(
                "Dune MCP response body is not bytes"
            )
        if len(raw) > self._max_response_bytes:
            raise DuneMetadataProtocolError(
                "Dune MCP response body exceeds the "
                f"{self._max_response_bytes} byte limit"
            )
        text = bytes(raw).decode("utf-8", errors="replace")
        if not 200 <= status < 300:
            snippet = _sanitize(text.strip()[:200], self._api_key)
            raise DuneMetadataTransportError(
                f"Dune MCP returned HTTP {status}"
                + (f": {snippet}" if snippet else "")
            )
        self._capture_session(headers)
        return headers, text

    def _capture_session(self, headers):
        value = _header(headers, "mcp-session-id")
        if value is None:
            return
        if not isinstance(value, str):
            raise DuneMetadataProtocolError(
                "Dune MCP returned a non-text Mcp-Session-Id header"
            )
        session = value.strip()
        if not session:
            return
        if any(not 0x21 <= ord(char) <= 0x7E for char in session):
            raise DuneMetadataProtocolError(
                "Dune MCP returned an invalid Mcp-Session-Id header"
            )
        self._session_id = session

    # -- JSON-RPC / SSE -------------------------------------------------------

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _request(self, method, params):
        request_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers, text = self._post(payload)
        messages = self._decode_messages(headers, text)
        return self._select_result(messages, request_id)

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        _, text = self._post(payload)
        if not text.strip():
            return
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # A notification needs no payload; opaque non-JSON is harmless.
            return
        if isinstance(parsed, dict) and parsed.get("error"):
            raise DuneMetadataProtocolError(
                self._rpc_error_message(parsed)
            )

    def _decode_messages(self, headers, text):
        content_type = str(_header(headers, "content-type") or "").lower()
        body = text.strip()
        if "text/event-stream" in content_type:
            return self._parse_sse(text)
        if not body:
            raise DuneMetadataProtocolError("Dune MCP returned an empty response")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            if body.startswith("data:") or "\ndata:" in body:
                return self._parse_sse(text)
            raise DuneMetadataProtocolError(
                "Dune MCP returned a non-JSON response body"
            ) from None
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        raise DuneMetadataProtocolError(
            "Dune MCP response must be a JSON-RPC object"
        )

    @staticmethod
    def _parse_sse(text):
        messages = []
        data_lines = []

        def flush():
            if not data_lines:
                return
            raw = "\n".join(data_lines)
            data_lines.clear()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise DuneMetadataProtocolError(
                    "Dune MCP SSE data is not valid JSON"
                ) from None
            if isinstance(parsed, list):
                messages.extend(parsed)
            else:
                messages.append(parsed)

        for line in text.splitlines():
            if not line.strip():
                flush()
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if not separator or field != "data":
                continue
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        flush()
        return messages

    def _rpc_error_message(self, message):
        error = message.get("error")
        if not isinstance(error, dict):
            return "Dune MCP returned a JSON-RPC error"
        code = error.get("code")
        detail = _sanitize(error.get("message", ""), self._api_key)[:300]
        return f"Dune MCP returned JSON-RPC error {code!r}: {detail}"

    def _select_result(self, messages, request_id):
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise DuneMetadataProtocolError(
                    self._rpc_error_message(message)
                )
            if "jsonrpc" in message and message["jsonrpc"] != "2.0":
                raise DuneMetadataProtocolError(
                    f"unsupported JSON-RPC version: {message['jsonrpc']!r}"
                )
            if "result" in message:
                return message["result"]
        raise DuneMetadataProtocolError(
            f"Dune MCP response contained no JSON-RPC result for request id "
            f"{request_id} ({len(messages)} message(s) received)"
        )

    # -- MCP operations -------------------------------------------------------

    def _initialize(self):
        if self._initialized:
            return
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "version": CLIENT_VERSION,
                },
            },
        )
        if not isinstance(result, dict):
            raise DuneMetadataProtocolError(
                "Dune MCP initialize result must be an object"
            )
        version = result.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise DuneMetadataProtocolError(
                "Dune MCP initialize result has no protocolVersion"
            )
        self._server_protocol_version = version
        if self._send_initialized:
            self._notify("notifications/initialized")
        self._initialized = True

    def _tools_call(self, name, arguments):
        # Hard allowlist: this client can only ever read account metadata.
        if name not in _ALLOWED_TOOLS:
            raise DuneMetadataError(
                f"tool {name!r} is not permitted; this client is metadata-only"
            )
        if arguments not in ({}, None):
            raise DuneMetadataError(
                "getUsage does not accept arguments; refusing to send any"
            )
        return self._request(
            "tools/call",
            {"name": USAGE_TOOL, "arguments": {}},
        )

    @staticmethod
    def _extract_payload(result):
        if not isinstance(result, dict):
            raise DuneMetadataProtocolError(
                "Dune MCP tools/call result must be an object"
            )
        if result.get("isError") is True:
            raise DuneMetadataProtocolError(
                "Dune MCP getUsage reported isError"
            )
        structured = result.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, dict):
                raise DuneMetadataProtocolError(
                    "Dune MCP structuredContent must be an object"
                )
            return structured

        content = result.get("content")
        if isinstance(content, list):
            texts = [
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            for text in texts:
                try:
                    candidate = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    return candidate
            raise DuneMetadataProtocolError(
                "Dune MCP getUsage content did not contain a JSON object"
            )

        # Some servers return the structured fields directly.
        if _has_any(result, _CREDITS_USED_KEYS) or _has_any(
            result, _CREDITS_INCLUDED_KEYS
        ):
            return result
        raise DuneMetadataProtocolError(
            "Dune MCP getUsage returned neither structuredContent nor content"
        )


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Read exact Dune billing-period usage via official MCP "
        "(metadata only)."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (max {MAX_TIMEOUT_SECONDS:g})",
    )
    args = parser.parse_args(argv)
    try:
        usage = DuneMetadataClient(timeout=args.timeout).get_usage()
    except (DuneMetadataError, ValueError) as error:
        print(f"dune metadata error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(usage, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
