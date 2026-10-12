import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.server
import io
import json
import os
import stat
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter

from orderflow.broker import BrokerClient, BrokerConfig, BrokerError, MIB, make_task, validate_outcome, _credential_mode_ok, _read_token
from orderflow.gemini import GeminiAdapter, ITEM_SCHEMA, MANAGEMENT_SCHEMA, recognition_prompt

FAKE_TOKEN = "fixture-client-token-never-live"


class SystemdCredentialTests(unittest.TestCase):
    path = Path('/run/credentials/orderflow.service/client_token')

    def metadata(self, mode=0o440, uid=0, gid=0):
        return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid, st_size=len(FAKE_TOKEN))

    def parent(self, path):
        mode = 0o550 if path == self.path.parent else 0o755
        return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=0, st_gid=0)

    def test_systemd_0440_is_readable_without_exposing_value(self):
        with tempfile.TemporaryFile() as source:
            source.write(FAKE_TOKEN.encode()); source.seek(0)
            descriptor = os.dup(source.fileno())
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY':str(self.path.parent)}), \
                    patch('orderflow.broker.os.open', return_value=descriptor) as opened, \
                    patch('orderflow.broker.os.fstat', return_value=self.metadata()), \
                    patch.object(Path, 'lstat', autospec=True, side_effect=self.parent):
                self.assertEqual(_read_token(self.path), FAKE_TOKEN)
                self.assertEqual(opened.call_args.args[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    def test_group_read_exception_requires_exact_trusted_credential(self):
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY':str(self.path.parent)}), \
                patch.object(Path, 'lstat', autospec=True, side_effect=self.parent):
            for mode in (0o640, 0o444, 0o450, 0o460, 0o2440):
                self.assertFalse(_credential_mode_ok(self.path, self.metadata(mode)))
            self.assertFalse(_credential_mode_ok(self.path, self.metadata(uid=1000)))
            self.assertFalse(_credential_mode_ok(self.path, self.metadata(gid=1000)))
            self.assertFalse(_credential_mode_ok(Path('/tmp/client_token'), self.metadata()))
            self.assertFalse(_credential_mode_ok(self.path.with_name('other'), self.metadata()))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_credential_mode_ok(self.path, self.metadata()))

    def test_symlink_or_writable_parent_is_rejected(self):
        for mode, uid in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFDIR | 0o770, 0), (stat.S_IFDIR | 0o550, 1000)):
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY':str(self.path.parent)}), \
                    patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=0)):
                self.assertFalse(_credential_mode_ok(self.path, self.metadata()))
writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
stream = io.BytesIO()
writer.write(stream)
PDF = stream.getvalue()


def expiry():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self._respond()

    def do_GET(self):
        self._respond()

    def _respond(self):
        if self.server.replies and self.server.replies[0][2].get("X-Fixture-Early"):
            status, data, headers = self.server.replies.pop(0)
            self.server.requests.append((self.command, self.path, dict(self.headers), b""))
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            # Deliberately close without reading a byte of the request body.
            length_header = "" if headers.get("X-Fixture-No-Length") else f"Content-Length: {headers.get('Content-Length', len(raw))}\r\n"
            wire = (f"HTTP/1.1 {status} Service Unavailable\r\nContent-Type: application/json\r\n"
                    + length_header + "Connection: close\r\n\r\n").encode()
            self.wfile.write(wire + raw)
            self.wfile.flush()
            self.close_connection = True
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        status, data, headers = self.server.replies.pop(0)
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        if headers.get("X-Fixture-Trickle"):
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Fixture: ")
                for _ in range(50):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (OSError, ConnectionError):
                pass
            return
        self.send_response(status)
        self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
        if not headers.get("X-Fixture-No-Length"):
            self.send_header("Content-Length", headers.get("Content-Length", str(len(raw))))
        for name, value in headers.items():
            if name not in {"Content-Length", "Content-Type"}:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)


class BrokerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "credential"
        self.token_file.write_text(FAKE_TOKEN)
        self.token_file.chmod(0o600)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server.requests = []
        self.server.replies = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = BrokerConfig(token_file=self.token_file,
            endpoint=f"http://127.0.0.1:{self.server.server_port}", input_bound_verified=True,
            input_bound_evidence="fixture-bound-v1", input_bound_expires_at=expiry())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def assert_code(self, code, callback):
        with self.assertRaises(BrokerError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(FAKE_TOKEN, repr(raised.exception))

    def test_task_matches_existing_prompts_schema_and_wire_bytes(self):
        for kind in (None, "invoice", "purchase_order"):
            prompt, schema = recognition_prompt(kind)
            task = make_task(self.config, PDF, kind, "orderflow-fixture-1")
            content = task["input"]["messages"][0]["content"]
            self.assertEqual(content[0], {"type": "text", "text": prompt})
            self.assertEqual(base64.b64decode(content[1]["data"]), PDF)
            self.assertEqual(content[1]["mime_type"], "application/pdf")
            self.assertEqual(task["max_attempts"], 1)
            self.assertEqual(task["max_output_tokens"], 8192)
            self.assertNotIn("input_tokens", task)
            def uppercase_types(value):
                if isinstance(value, dict):
                    return {k: (v.upper() if k == "type" else uppercase_types(v)) for k, v in value.items()}
                return [uppercase_types(v) for v in value] if isinstance(value, list) else value
            self.assertEqual(uppercase_types(task["options"]["json_schema"]), schema)
            self.server.replies.append((200, {"request_key": task["request_key"], "state": "queued"}, {}))
            # Ambient proxy configuration cannot influence http.client's loopback target.
            with patch.dict(os.environ, {"HTTP_PROXY": "http://invalid.example:9", "http_proxy": "http://invalid.example:9"}):
                BrokerClient(self.config).submit(task)
            method, path, headers, raw = self.server.requests[-1]
            self.assertEqual((method, path), ("POST", "/v1/queue"))
            self.assertEqual(json.loads(raw), task)
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            self.assertEqual(headers["Authorization"], "Bearer " + FAKE_TOKEN)

    def test_direct_gemini_payload_preserves_prompt_and_schema(self):
        source = Path(self.tmp.name) / "fixture.pdf"
        source.write_bytes(PDF)
        for kind, schema in ((None, ITEM_SCHEMA), ("invoice", MANAGEMENT_SCHEMA), ("purchase_order", MANAGEMENT_SCHEMA)):
            with patch("orderflow.gemini._generate", return_value='{"items":[]}') as generate:
                self.assertEqual(GeminiAdapter("fixture", kind).recognize(str(source), deadline_seconds=3), {"items": []})
            args, kwargs = generate.call_args
            self.assertEqual(args[1][0], {"text": recognition_prompt(kind)[0]})
            self.assertEqual(kwargs, {"timeout": 3, "structured": True, "schema": schema})
            self.assertEqual(base64.b64decode(args[1][1]["inline_data"]["data"]), PDF)

    def test_pdf_limits_one_and_eight_mib_and_actual_request_bytes(self):
        for maximum in (MIB, 8 * MIB):
            config = replace(self.config, max_pdf_bytes=maximum, max_request_bytes=16 * MIB)
            exact = b"%PDF-" + b"x" * (maximum - 5)
            self.assertEqual(len(base64.b64decode(make_task(config, exact, None, "boundary")["input"]["messages"][0]["content"][1]["data"])), maximum)
            self.assert_code("BROKER_PDF_TOO_LARGE", lambda: make_task(config, exact + b"x", None, "boundary"))
        tiny = replace(self.config, max_request_bytes=100)
        self.assert_code("BROKER_REQUEST_TOO_LARGE", lambda: make_task(tiny, PDF, None, "bounded"))
        self.assert_code("BROKER_REQUEST_TOO_LARGE", lambda: BrokerClient(tiny).submit({"request_key": "bounded", "input": "x" * 200}))
        self.assertEqual(self.server.requests, [])

    def test_snapshot_and_public_status_are_content_free(self):
        self.assertTrue(self.config.public_status()["ready"])
        self.assertEqual(BrokerConfig.from_snapshot(self.config.snapshot()), self.config)
        for value in (repr(self.config), json.dumps(self.config.snapshot()), json.dumps(self.config.public_status())):
            self.assertNotIn(FAKE_TOKEN, value)
        for mode in (0o400, 0o600):
            self.token_file.chmod(mode)
            self.assertTrue(self.config.public_status()["ready"])
        self.token_file.chmod(0o640)
        self.assertEqual(self.config.public_status()["unavailable_code"], "BROKER_NOT_CONFIGURED")
        symlink = Path(self.tmp.name) / "link"
        symlink.symlink_to(self.token_file)
        self.assertFalse(replace(self.config, token_file=symlink).public_status()["ready"])

    def test_input_bound_gate_is_closed_but_original_queries_still_work(self):
        for changes in ({"input_bound_verified": False}, {"input_bound_evidence": None},
                        {"input_bound_expires_at": None}, {"input_bound_expires_at": "2000-01-01T00:00:00+00:00"}):
            config = replace(self.config, **changes)
            self.assertFalse(config.public_status()["ready"])
            self.assert_code("BROKER_INPUT_BOUND_REQUIRED", lambda: make_task(config, PDF, None, "original"))
            self.assert_code("BROKER_INPUT_BOUND_REQUIRED", lambda: BrokerClient(config).submit({"request_key": "original"}))
            self.server.replies.append((200, {"request_key": "original", "state": "completed"}, {}))
            self.assertEqual(BrokerClient(config).status("original")["state"], "completed")

    def test_endpoint_and_keys_cannot_escape_loopback_path(self):
        for endpoint in ("http://localhost:18084", "https://127.0.0.1:18084", "http://127.0.0.1:18084/",
                         "http://user@127.0.0.1:18084", "http://127.0.0.1:18084?x=1", "http://127.1:18084"):
            self.assert_code("BROKER_CONFIG_INVALID", lambda: replace(self.config, endpoint=endpoint))
        for key in ("../other", "a/b", "x?admin=1", "", "x" * 121, "line\n"):
            self.assert_code("BROKER_BAD_REQUEST", lambda: BrokerClient(self.config).status(key))
        self.assertEqual(self.server.requests, [])

    def test_no_redirect_or_retry_and_safe_error_body(self):
        for status in (301, 302, 307, 308, 500):
            self.server.replies.append((status, {"message": FAKE_TOKEN}, {"Location": self.config.endpoint + "/sensitive"}))
            before = len(self.server.requests)
            self.assert_code("BROKER_HTTP_UNKNOWN", lambda: BrokerClient(self.config).submit({"request_key": "original"}))
            self.assertEqual(len(self.server.requests), before + 1)
        self.assertTrue(all(item[1] == "/v1/queue" for item in self.server.requests))

    def test_bounded_read_and_http_code_classification(self):
        errors = ((401, {}, "BROKER_UNAUTHORIZED"), (409, {}, "BROKER_CONFLICT"),
                  (400, {}, "BROKER_REJECTED"), (404, {}, "BROKER_NOT_FOUND"),
                  (503, {"error": "queue_result_pending"}, "BROKER_PENDING"),
                  (503, {"error": "queue_content_expired"}, "BROKER_RESULT_EXPIRED"),
                  (503, {"error": "untrusted-" + FAKE_TOKEN}, "BROKER_HTTP_UNKNOWN"),
                  (503, {"error": [FAKE_TOKEN]}, "BROKER_HTTP_UNKNOWN"))
        for status, body, code in errors:
            self.server.replies.append((status, body, {}))
            self.assert_code(code, lambda: BrokerClient(self.config).result("original"))
        for raw, code in ((b"x" * 129, "BROKER_RESULT_TOO_LARGE"), (b"{broken", "BROKER_BAD_RESPONSE"),
                          (b'[]', "BROKER_BAD_RESPONSE"), (b'{"x":1,"x":2}', "BROKER_BAD_RESPONSE")):
            self.server.replies.append((200, raw, {}))
            self.assert_code(code, lambda: BrokerClient(replace(self.config, max_result_bytes=128)).result("original"))

    def test_interrupted_response_and_timeout_do_not_retry(self):
        self.server.replies.append((200, {"request_key": "original"}, {"Content-Length": "9999"}))
        self.assert_code("BROKER_HTTP_UNKNOWN", lambda: BrokerClient(self.config).status("original"))
        with patch("orderflow.broker.http.client.HTTPConnection") as connection:
            connection.return_value.connect.side_effect = TimeoutError(FAKE_TOKEN)
            self.assert_code("BROKER_HTTP_UNKNOWN", lambda: BrokerClient(self.config).submit({"request_key": "original"}))
            self.assertEqual(connection.call_count, 1)
            connection.return_value.request.assert_not_called()
        self.assertEqual(len(self.server.requests), 1)

    def test_only_complete_exact_json_503_is_work_busy(self):
        for method in ("submit", "status", "result"):
            self.server.replies.append((503, {"error": "work_busy"}, {"Connection": "close"}))
            client = BrokerClient(self.config)
            callback = (lambda: client.submit({"request_key": "original"})) if method == "submit" else lambda: getattr(client, method)("original")
            before = len(self.server.requests)
            self.assert_code("BROKER_WORK_BUSY", callback)
            self.assertEqual(len(self.server.requests), before + 1, 'transport never retries itself')
        ambiguous = (
            (503, {"error": "work_busy"}, {}),
            (503, b'{"error":"work_busy"} extra', {"Content-Length": "21"}),
            (503, {"error": "work_busy", "message": "extra"}, {}),
            (503, {"error": "work_busy", "accepted": False}, {}),
            (503, b'{"error":"work_busy","error":"work_busy"}', {}),
            (503, b'{"error":"work_busy"} trailing', {}),
            (503, b'{"error":"work_busy"', {}),
            (503, {"error": "work_busy"}, {"Content-Type": "text/html"}),
            (503, b'<html>work_busy</html>', {}),
            (503, {"error": "work_busy"}, {"Content-Length": "9999"}),
            (500, {"error": "work_busy"}, {}),
        )
        for status, raw, headers in ambiguous:
            with self.subTest(status=status, raw=raw, headers=headers):
                self.server.replies.append((status, raw, headers))
                self.assert_code("BROKER_HTTP_UNKNOWN", lambda: BrokerClient(self.config).submit({"request_key": "original"}))

    def test_eight_mib_headers_only_early_busy_and_ambiguous_close(self):
        config = replace(self.config, max_pdf_bytes=8 * MIB, max_request_bytes=16 * MIB)
        task = make_task(config, b"%PDF-" + b"x" * (8 * MIB - 5), None, "original-large")
        send_errors = []
        real_connection = http.client.HTTPConnection
        class ObservedConnection(real_connection):
            def request(self, *args, **kwargs):
                try:
                    return super().request(*args, **kwargs)
                except OSError as exc:
                    send_errors.append(type(exc).__name__)
                    raise
        for data, headers, code in (
            ({"error": "work_busy"}, {}, "BROKER_WORK_BUSY"),
            ({"error": "work_busy", "accepted": False}, {}, "BROKER_HTTP_UNKNOWN"),
            (b'{"error":"work_busy"', {}, "BROKER_HTTP_UNKNOWN"),
            ({"error": "work_busy"}, {"Content-Length": "9999"}, "BROKER_HTTP_UNKNOWN"),
            ({"error": "work_busy"}, {"X-Fixture-No-Length": "true"}, "BROKER_HTTP_UNKNOWN"),
            (b'{"error":"work_busy"} ', {"Content-Length": "21"}, "BROKER_HTTP_UNKNOWN"),
        ):
            with self.subTest(data=data, headers=headers):
                self.server.replies.append((503, data, {"X-Fixture-Early": "true", **headers}))
                before = len(self.server.requests)
                with patch("orderflow.broker.http.client.HTTPConnection", ObservedConnection):
                    self.assert_code(code, lambda: BrokerClient(config).submit(task))
                self.assertEqual(len(self.server.requests), before + 1)
                self.assertEqual(self.server.requests[-1][3], b"", 'the server reads no request body')
        self.assertTrue(send_errors, 'a real early close interrupts the large send before its response is read')

    def test_total_deadline_bounds_trickling_headers(self):
        self.server.replies.append((200, b"", {"X-Fixture-Trickle": "true"}))
        started = time.monotonic()
        self.assert_code("BROKER_HTTP_UNKNOWN", lambda: BrokerClient(replace(self.config, timeout_seconds=0.08)).status("original"))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(self.server.requests), 1)

    def test_outcome_completion_identity_and_safe_metadata(self):
        base = {"request_key": "q-execution", "state": "completed", "finish_reason": "stop",
                "response_truncated": False, "result": {"text": '{"items":[]}'}, "provider_request_id": FAKE_TOKEN}
        for state in ("completed", "completed_usage_unknown"):
            parsed, metadata = validate_outcome({**base, "state": state}, "q-execution", state)
            self.assertEqual(parsed, {"items": []})
            self.assertEqual(metadata["usage_unknown"], state == "completed_usage_unknown")
            self.assertNotIn(FAKE_TOKEN, json.dumps(metadata))
        for finish in (None, "unclassified", "MAX_TOKENS", "max_tokens", "length", "STOP", "safety"):
            self.assert_code("BROKER_RESULT_INCOMPLETE", lambda: validate_outcome({**base, "finish_reason": finish}, "q-execution", "completed"))
        for truncated in (None, True, 0, "false"):
            self.assert_code("BROKER_RESULT_INCOMPLETE", lambda: validate_outcome({**base, "response_truncated": truncated}, "q-execution", "completed"))
        for changes in ({"request_key": "outer-key"}, {"state": "completed_usage_unknown"}, {"result": {"text": "broken"}},
                        {"result": {"text": '{"items":[],"items":[]}'}}):
            self.assert_code("BROKER_BAD_RESPONSE", lambda: validate_outcome({**base, **changes}, "q-execution", "completed"))
        self.assert_code("BROKER_BAD_RESPONSE", lambda: validate_outcome({"result": base}, "q-execution", "completed"))
        self.assert_code("BROKER_BAD_RESPONSE", lambda: validate_outcome(base, "q-execution", ["completed"]))
        # Business validation belongs to the application, not the transport module.
        malformed, _ = validate_outcome({**base, "result": {"text": '{"items":"bad"}'}}, "q-execution", "completed")
        self.assertEqual(malformed, {"items": "bad"})


if __name__ == "__main__":
    unittest.main()
