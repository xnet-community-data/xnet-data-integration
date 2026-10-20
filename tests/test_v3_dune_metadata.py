import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v3_credit_budget as budget
import v3_dune_metadata as metadata

KEY = "unit-test-key-never-log-93f1"
START = "2026-10-04T16:35:49Z"
END = "2026-11-04T00:00:00Z"
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
SESSION = "session-abc-123"

BASE_PAYLOAD = {
    "creditsUsed": 1798.673,
    "creditsIncluded": 2500,
    "billingPeriodStart": START,
    "billingPeriodEnd": END,
}


class FakeResponse:
    """Minimal urllib-response stand-in; read() honors the requested bound."""

    def __init__(self, body=b"", status=200, headers=None):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = dict(headers or {})

    def read(self, amount=None):
        if amount is None:
            return self._body
        return self._body[:amount]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeTransport:
    """Callable opener that records requests and replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.responses:
            raise AssertionError("unexpected extra Dune MCP request")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request)
        return item

    def bodies(self):
        return [json.loads(request.data.decode("utf-8")) for request in self.requests]


def header(request, name):
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


def body_of(request):
    return json.loads(request.data.decode("utf-8"))


def rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def json_response(message, *, status=200, headers=None):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return FakeResponse(
        json.dumps(message).encode("utf-8"), status=status, headers=merged
    )


def init_response(request, session=SESSION, headers=None):
    merged = dict(headers or {})
    if session is not None:
        merged["Mcp-Session-Id"] = session
    return json_response(
        rpc_result(
            body_of(request)["id"],
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dune", "version": "1.0.0"},
            },
        ),
        headers=merged,
    )


def initialized_response():
    return FakeResponse(b"", status=202)


def usage_message(payload, request_id, *, structured=True):
    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if structured:
        result["structuredContent"] = payload
    return rpc_result(request_id, result)


def usage_response(payload, *, structured=True, sse=False):
    def respond(request):
        message = usage_message(
            payload, body_of(request)["id"], structured=structured
        )
        if sse:
            return FakeResponse(
                ("event: message\ndata: " + json.dumps(message) + "\n\n").encode(),
                headers={"Content-Type": "text/event-stream"},
            )
        return json_response(message, headers={"Mcp-Session-Id": SESSION})

    return respond


def client_with(responses, **overrides):
    transport = FakeTransport(responses)
    options = dict(api_key=KEY, opener=transport, now=NOW)
    options.update(overrides)
    return metadata.DuneMetadataClient(**options), transport


class TransportContractTests(unittest.TestCase):
    def test_json_transport_initialize_session_and_get_usage(self):
        client, transport = client_with(
            [init_response, initialized_response(), usage_response(BASE_PAYLOAD)]
        )
        usage = client.get_usage()

        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(transport.timeouts, [30.0, 30.0, 30.0])
        for request in transport.requests:
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, metadata.MCP_ENDPOINT)
            self.assertEqual(header(request, "x-dune-api-key"), KEY)
            self.assertEqual(
                header(request, "Accept"),
                "application/json, text/event-stream",
            )
            self.assertEqual(header(request, "Content-Type"), "application/json")
            self.assertEqual(header(request, "MCP-Protocol-Version"), "2025-03-26")

        initialize, initialized, tools_call = transport.bodies()
        self.assertEqual(initialize["jsonrpc"], "2.0")
        self.assertEqual(initialize["method"], "initialize")
        self.assertIsInstance(initialize["id"], int)
        self.assertEqual(
            initialize["params"],
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": metadata.CLIENT_NAME,
                    "version": metadata.CLIENT_VERSION,
                },
            },
        )
        # The server assigns the session id on initialize; it must not be sent
        # before the server provides it.
        self.assertIsNone(header(transport.requests[0], "Mcp-Session-Id"))

        self.assertNotIn("id", initialized)
        self.assertEqual(initialized["method"], "notifications/initialized")
        self.assertEqual(header(transport.requests[1], "Mcp-Session-Id"), SESSION)

        self.assertEqual(tools_call["method"], "tools/call")
        self.assertEqual(tools_call["params"], {"name": "getUsage", "arguments": {}})
        self.assertEqual(header(transport.requests[2], "Mcp-Session-Id"), SESSION)

        self.assertEqual(usage["credits_used"], 1798.673)
        self.assertEqual(usage["credits_included"], 2500.0)
        self.assertEqual(usage["period_start_utc"], START)
        self.assertEqual(usage["period_end_utc"], END)
        self.assertIs(usage["exact_end"], True)
        self.assertEqual(usage["observed_at_utc"], "2026-10-16T12:00:00Z")
        self.assertEqual(usage["provider"]["tool"], "getUsage")
        self.assertEqual(
            usage["provider"]["protocol_version"], "2025-03-26"
        )
        self.assertTrue(usage["provider"]["session_established"])
        self.assertEqual(usage["raw"]["creditsUsed"], 1798.673)
        self.assertNotIn(KEY, json.dumps(usage))

        # The guard's single-period parser accepts the normalized output as-is.
        checked = budget.normalize_usage(usage, NOW)
        self.assertEqual(checked["blockers"], [])
        self.assertIsNotNone(checked["period"]["period_end_utc"])
        self.assertEqual(checked["period"]["credits_used"], 1798.673)

    def test_sse_multi_event_response_with_leading_notification(self):
        def respond(request):
            request_id = body_of(request)["id"]
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progress": 1},
            }
            message = usage_message(BASE_PAYLOAD, request_id)
            text = (
                "event: message\n"
                "data: " + json.dumps(notification) + "\n\n"
                ": keepalive comment\n\n"
                "event: message\n"
                "id: 7\n"
                "data: " + json.dumps(message) + "\n\n"
            )
            return FakeResponse(
                text.encode("utf-8"),
                headers={"Content-Type": "text/event-stream; charset=utf-8"},
            )

        client, transport = client_with(
            [init_response, initialized_response(), respond]
        )
        usage = client.get_usage()
        self.assertEqual(usage["period_end_utc"], END)
        self.assertEqual(len(transport.requests), 3)

    def test_sse_fallback_when_content_type_is_wrong(self):
        def respond(request):
            message = usage_message(BASE_PAYLOAD, body_of(request)["id"])
            return FakeResponse(
                ("data: " + json.dumps(message) + "\n\n").encode(),
                headers={"Content-Type": "application/json"},
            )

        client, _ = client_with([init_response, initialized_response(), respond])
        self.assertEqual(client.get_usage()["credits_used"], 1798.673)

    def test_content_text_json_and_camel_case_aliases(self):
        payload = {
            "billingPeriodStart": START,
            "billingPeriodEnd": END,
            "creditsUsed": 12.5,
            "creditsQuota": 2500,
        }
        client, _ = client_with(
            [
                init_response,
                initialized_response(),
                usage_response(payload, structured=False),
            ]
        )
        usage = client.get_usage()
        self.assertEqual(usage["credits_used"], 12.5)
        self.assertEqual(usage["credits_included"], 2500.0)
        self.assertIs(usage["exact_end"], True)

    def test_injected_session_id_is_sent_on_every_request(self):
        client, transport = client_with(
            [
                lambda request: init_response(request, session=None),
                initialized_response(),
                usage_response(BASE_PAYLOAD),
            ],
            session_id="resume-session-9",
        )
        client.get_usage()
        for request in transport.requests:
            self.assertEqual(
                header(request, "Mcp-Session-Id"), "resume-session-9"
            )

    def test_second_call_reuses_initialized_session_without_reinitializing(self):
        client, transport = client_with(
            [
                init_response,
                initialized_response(),
                usage_response(BASE_PAYLOAD),
                usage_response(BASE_PAYLOAD),
            ]
        )
        first = client.get_usage()
        second = client.get_usage()
        self.assertEqual(first, second)
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(transport.bodies()[3]["method"], "tools/call")
        self.assertEqual(header(transport.requests[3], "Mcp-Session-Id"), SESSION)

    def test_default_timeout_is_at_most_45_and_cap_enforced(self):
        with self.assertRaises(ValueError):
            metadata.DuneMetadataClient(
                api_key=KEY, opener=FakeTransport([]), timeout=45.1
            )
        metadata.DuneMetadataClient(
            api_key=KEY, opener=FakeTransport([]), timeout=45
        )


class DefaultOpenerTests(unittest.TestCase):
    def test_default_real_opener_uses_open_without_network(self):
        client = metadata.DuneMetadataClient(api_key=KEY)
        self.assertTrue(callable(client._opener))
        self.assertIsNotNone(client._default_opener)
        self.assertTrue(
            any(
                isinstance(handler, metadata._NoRedirect)
                for handler in client._default_opener.handlers
            )
        )
        response = FakeResponse(b"{}")
        with mock.patch.object(
            urllib.request.OpenerDirector,
            "open",
            autospec=True,
            return_value=response,
        ) as opened:
            result = client._post(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            )
        self.assertEqual(result, ({}, "{}"))
        self.assertEqual(opened.call_count, 1)
        request = opened.call_args.args[1]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(opened.call_args.kwargs["timeout"], client._timeout)


class FailureTests(unittest.TestCase):
    def test_missing_api_key_fails_before_network(self):
        client, transport = client_with([], api_key="")
        with self.assertRaises(metadata.DuneMetadataValidationError):
            client.get_usage()
        self.assertEqual(transport.requests, [])

    def test_header_unsafe_api_key_is_rejected_before_network(self):
        for value in ("key with space", "key\ninjected: yes"):
            with self.subTest(api_key=repr(value)):
                client, transport = client_with([], api_key=value)
                with self.assertRaises(metadata.DuneMetadataValidationError):
                    client.get_usage()
                self.assertEqual(transport.requests, [])

    def test_http_404_body_does_not_leak_api_key(self):
        client, transport = client_with(
            [
                FakeResponse(
                    f"unauthorized for key {KEY}".encode(),
                    status=404,
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        with self.assertRaises(metadata.DuneMetadataTransportError) as caught:
            client.get_usage()
        message = str(caught.exception)
        self.assertNotIn(KEY, message)
        self.assertIn("[redacted]", message)
        self.assertEqual(len(transport.requests), 1)

    def test_http_error_exception_does_not_leak_api_key(self):
        error = urllib.error.HTTPError(
            metadata.MCP_ENDPOINT,
            401,
            f"Unauthorized for {KEY}",
            {},
            io.BytesIO(f"body {KEY}".encode()),
        )
        client, transport = client_with([error])
        with self.assertRaises(metadata.DuneMetadataTransportError) as caught:
            client.get_usage()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_transport_failure_is_not_retried(self):
        client, transport = client_with([OSError(f"connection reset {KEY}")])
        with self.assertRaises(metadata.DuneMetadataTransportError) as caught:
            client.get_usage()
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn(KEY, str(caught.exception))

    def test_sse_body_over_one_megabyte_is_rejected(self):
        oversized = "data: " + ("x" * (metadata.MAX_RESPONSE_BYTES + 10))
        client, transport = client_with(
            [
                init_response,
                initialized_response(),
                FakeResponse(
                    oversized.encode(),
                    headers={"Content-Type": "text/event-stream"},
                ),
            ]
        )
        with self.assertRaises(metadata.DuneMetadataProtocolError) as caught:
            client.get_usage()
        self.assertIn("byte limit", str(caught.exception))
        self.assertEqual(len(transport.requests), 3)

    def test_jsonrpc_error_event_is_sanitized(self):
        def respond(request):
            request_id = body_of(request)["id"]
            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": f"denied for {KEY}",
                },
            }
            return FakeResponse(
                ("data: " + json.dumps(message) + "\n\n").encode(),
                headers={"Content-Type": "text/event-stream"},
            )

        client, _ = client_with([init_response, initialized_response(), respond])
        with self.assertRaises(metadata.DuneMetadataProtocolError) as caught:
            client.get_usage()
        self.assertIn("JSON-RPC error", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_is_error_result_is_rejected(self):
        def respond(request):
            return json_response(
                rpc_result(
                    body_of(request)["id"],
                    {"content": [], "isError": True},
                )
            )

        client, _ = client_with([init_response, initialized_response(), respond])
        with self.assertRaises(metadata.DuneMetadataProtocolError):
            client.get_usage()

    def test_malformed_json_is_rejected_without_retry(self):
        client, transport = client_with(
            [
                init_response,
                initialized_response(),
                FakeResponse(
                    b"<html>bad gateway</html>",
                    headers={"Content-Type": "application/json"},
                ),
            ]
        )
        with self.assertRaises(metadata.DuneMetadataProtocolError):
            client.get_usage()
        self.assertEqual(len(transport.requests), 3)

    def test_tools_allowlist_blocks_any_other_operation(self):
        client, transport = client_with([])
        with self.assertRaises(metadata.DuneMetadataError):
            client._tools_call("executeQueryById", {})
        with self.assertRaises(metadata.DuneMetadataError):
            client._tools_call("getUsage", {"query_id": 1})
        self.assertEqual(transport.requests, [])

    def test_public_surface_is_metadata_only(self):
        client, _ = client_with([])
        public = {name for name in dir(client) if not name.startswith("_")}
        self.assertEqual(public, {"get_usage"})
        self.assertFalse(hasattr(metadata, "call_tool"))


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, payload, *, now=NOW, message_part=None):
        client, transport = client_with(
            [init_response, initialized_response(), usage_response(payload)],
            now=now,
        )
        with self.assertRaises(metadata.DuneMetadataValidationError) as caught:
            client.get_usage()
        if message_part:
            self.assertIn(message_part, str(caught.exception))
        self.assertEqual(len(transport.requests), 3)
        return caught.exception

    def test_date_only_period_end_is_rejected(self):
        payload = dict(BASE_PAYLOAD, billingPeriodEnd="2026-11-04")
        error = self.assert_rejected(payload)
        self.assertIn("date-only", str(error))
        self.assertIn("exact UTC instant", str(error))

    def test_date_only_period_start_is_rejected(self):
        payload = dict(BASE_PAYLOAD, billingPeriodStart="2026-10-04")
        self.assert_rejected(payload, message_part="date-only")

    def test_missing_period_end_is_rejected(self):
        payload = dict(BASE_PAYLOAD)
        payload.pop("billingPeriodEnd")
        self.assert_rejected(payload, message_part="period_end_utc")

    def test_null_period_end_is_rejected(self):
        payload = dict(BASE_PAYLOAD, billingPeriodEnd=None)
        self.assert_rejected(payload, message_part="period_end_utc")

    def test_future_period_is_rejected(self):
        error = self.assert_rejected(
            BASE_PAYLOAD, now=datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        self.assertIn("has not started", str(error))

    def test_expired_period_is_rejected(self):
        error = self.assert_rejected(
            BASE_PAYLOAD, now=datetime(2026, 11, 4, tzinfo=timezone.utc)
        )
        self.assertIn("expired", str(error))

    def test_reversed_period_is_rejected(self):
        payload = dict(
            BASE_PAYLOAD,
            billingPeriodStart=END,
            billingPeriodEnd=START,
        )
        self.assert_rejected(payload, message_part="start must precede")

    def test_null_quota_is_rejected(self):
        payload = {
            "creditsUsed": 10,
            "creditsQuota": None,
            "billingPeriodStart": START,
            "billingPeriodEnd": END,
        }
        self.assert_rejected(payload, message_part="credits_included")

    def test_corrupt_counters_are_rejected(self):
        cases = {
            "boolean": True,
            "negative": -1,
            "nan": float("nan"),
            "string": "1798.673",
            "infinity": float("inf"),
        }
        for label, value in cases.items():
            with self.subTest(counter=label):
                payload = dict(BASE_PAYLOAD, creditsUsed=value)
                error = self.assert_rejected(payload)
                self.assertIn("credits_used", str(error))

    def test_conflicting_alias_values_are_rejected(self):
        payload = dict(BASE_PAYLOAD, credits_used=1800)
        self.assert_rejected(payload, message_part="conflicting")

    def test_naive_timestamp_is_rejected(self):
        payload = dict(BASE_PAYLOAD, billingPeriodEnd="2026-11-04T00:00:00")
        self.assert_rejected(payload, message_part="timezone-aware")


if __name__ == "__main__":
    unittest.main()
