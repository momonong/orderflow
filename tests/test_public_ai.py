import hashlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import uuid
from pathlib import Path

import bcrypt
from unittest.mock import patch

from pypdf import PdfWriter
from orderflow.ai import AIError, AIUnknown
from orderflow.app import AppServer, Store
from orderflow.gemini import GeminiAdapter, MODEL, _generate

ORIGIN = "https://momonong.me"
FAKE_KEY = "fake-test-key-never-use-12345"
TEST_PASSWORD = "test-password"
TEST_HASH = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4))
writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
stream = io.BytesIO()
writer.write(stream)
PDF = stream.getvalue()


class PublicAiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.server = AppServer(("127.0.0.1", 0), self.store, ORIGIN, TEST_HASH)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie, issued = self.login(cookie="")
        self.assertIn("; Secure", issued)
        status, data, _ = self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertFalse(data["ai_key_configured"])
        self.assertEqual(data["ai_model"], MODEL)

    def tearDown(self):
        # Request handlers may finish before their background recognition thread.
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            with self.store.db() as db:
                active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if not active:
                break
            time.sleep(0.05)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        hdr = {"Host": "momonong.me", **(headers or {})}
        if method in {"POST", "DELETE"}:
            hdr.setdefault("Origin", ORIGIN)
            hdr["X-Orderflow-Request"] = "1"
        if cookie:
            hdr["Cookie"] = cookie
        conn.request(method, path, body=body, headers=hdr)
        resp = conn.getresponse()
        raw = resp.read()
        data = json.loads(raw) if raw and resp.getheader("Content-Type", "").startswith("application/json") else raw
        result = resp.status, data, resp.getheader("Set-Cookie")
        conn.close()
        return result

    def login(self, cookie=""):
        status, data, issued = self.request("POST", "/orderflow/api/login",
                                            json.dumps({"password": TEST_PASSWORD}),
                                            {"Content-Type": "application/json"}, cookie=cookie)
        self.assertEqual((status, data["status"]), (200, "ok"))
        return issued.split(";", 1)[0], issued

    def post_json(self, path, value, *, cookie=None, headers=None):
        return self.request("POST", path, json.dumps(value), {"Content-Type": "application/json", **(headers or {})}, cookie or self.cookie)

    def upload(self):
        return self.request("POST", "/orderflow/api/documents", PDF, {
            "Content-Type": "application/pdf", "X-File-Size": str(len(PDF)),
            "X-File-SHA256": hashlib.sha256(PDF).hexdigest(), "X-Request-Key": str(uuid.uuid4()),
        }, self.cookie)

    def test_anonymous_api_and_unsupported_methods_are_closed(self):
        for method, path in (
            ("GET", "/orderflow/api/health"), ("GET", "/orderflow/api/bootstrap"),
            ("GET", "/orderflow/api/key"), ("GET", "/orderflow/api/sample"),
            ("GET", "/orderflow/api/jobs/" + str(uuid.uuid4())),
            ("GET", "/orderflow/api/unknown"), ("POST", "/orderflow/api/key/check"),
            ("POST", "/orderflow/api/jobs"), ("DELETE", "/orderflow/api/key"),
            ("PUT", "/orderflow/api/bootstrap"), ("HEAD", "/orderflow/api/health"),
            ("OPTIONS", "/orderflow/api/documents"),
        ):
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path, cookie="of_session=invalid")
                self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/orderflow/api/documents", b"fake",
                                    {"Content-Type": "application/pdf"}, "of_session=invalid")
        self.assertEqual(status, 401)
        self.assertEqual(self.request("GET", "/orderflow/")[0], 200)

    def test_login_rotation_logout_relogin_and_key_clear(self):
        original = self.cookie
        original_id = self.store.session(original.split("=", 1)[1])
        status, doc, _ = self.upload()
        self.assertEqual(status, 201)
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        again, issued = self.login(cookie=original)
        self.assertNotEqual(original, again)
        self.assertEqual(self.store.session(again.split("=", 1)[1]), original_id)
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap", cookie=original)[0], 401)
        self.assertFalse(self.request("GET", "/orderflow/api/key", cookie=again)[1]["configured"])
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY}, cookie=again)
        status, _, anon_issued = self.request("POST", "/orderflow/api/logout", cookie=again)
        self.assertEqual(status, 200)
        anon = anon_issued.split(";", 1)[0]
        self.assertNotEqual(again, anon)
        for cookie in (original, again, anon):
            self.assertEqual(self.request("GET", "/orderflow/api/bootstrap", cookie=cookie)[0], 401)
        restored, _ = self.login(cookie=anon)
        bootstrap = self.request("GET", "/orderflow/api/bootstrap", cookie=restored)[1]
        self.assertEqual(bootstrap["documents"][0]["id"], doc["id"])
        self.assertFalse(bootstrap["ai_key_configured"])
        self.assertEqual(self.store.session(restored.split("=", 1)[1]), original_id)
        self.assertNotIn(TEST_PASSWORD, self.store.db_path.read_bytes().decode("utf-8", "ignore"))
        self.assertIn("HttpOnly", issued)
        self.assertIn("SameSite=Strict", issued)
        self.assertIn("Path=/orderflow/", issued)
        self.assertIn("Secure", issued)

    def test_expiry_blocks_data_and_clears_key(self):
        session_id = self.store.session(self.cookie.split("=", 1)[1])
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        with self.store.db() as db:
            db.execute("UPDATE session_auth SET expires_ms=? WHERE session_id=?", (0, session_id))
        status, error, _ = self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)
        self.assertEqual((status, error["error_code"]), (401, "SESSION_EXPIRED"))
        self.assertIsNone(self.server.get_key(session_id))
        restored, _ = self.login(cookie=self.cookie)
        self.assertEqual(self.store.session(restored.split("=", 1)[1]), session_id)
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)[0], 401)

    def test_bad_login_rate_limit_and_origin(self):
        status, _, _ = self.post_json("/orderflow/api/login", {"password": TEST_PASSWORD},
                                      cookie="", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        for _ in range(4):
            status, data, _ = self.post_json("/orderflow/api/login", {"password": "wrong"}, cookie="")
            self.assertEqual((status, data["error_code"]), (401, "INVALID_CREDENTIALS"))
        status, data, _ = self.post_json("/orderflow/api/login", {"password": TEST_PASSWORD}, cookie="")
        self.assertEqual((status, data["error_code"]), (429, "LOGIN_RATE_LIMITED"))
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)[0], 200)

    def test_legacy_session_table_shape(self):
        with self.store.db() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(sessions)")]
            self.assertEqual(columns, ["id", "token_hash", "created_ms"])
            db.execute("INSERT INTO sessions VALUES (?,?,?)",
                       (str(uuid.uuid4()), "legacy-token-hash", 123))
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE token_hash='legacy-token-hash'").fetchone()[0], 1)

    def test_loopback_only_and_idle_key_expiry(self):
        with self.assertRaises(ValueError):
            AppServer(("0.0.0.0", 0), self.store, ORIGIN, TEST_HASH)
        second_cookie = self.login(cookie="of_session=invalid")[0]
        first = self.store.session(self.cookie.split("=", 1)[1])
        second = self.store.session(second_cookie.split("=", 1)[1])
        with patch("orderflow.app.KEY_TTL_SECONDS", 0.05):
            self.assertTrue(self.server.set_key(first, FAKE_KEY))
            self.assertTrue(self.server.set_key(second, FAKE_KEY))
        time.sleep(0.15)
        with self.server.key_lock:
            self.assertEqual(self.server.keys, {})
        with patch("orderflow.app.MAX_ACTIVE_KEYS", 1):
            self.assertTrue(self.server.set_key(first, FAKE_KEY))
            self.assertFalse(self.server.set_key(second, FAKE_KEY))
        self.server.clear_key(first)

    def test_host_origin_cookie_and_subpath(self):
        self.assertEqual(self.request("GET", "/orderflow/api/health")[0], 401)
        self.assertEqual(self.request("GET", "/orderflow/api/health", cookie=self.cookie)[0], 200)
        self.assertEqual(self.request("GET", "/orderflow/")[0], 200)
        self.assertEqual(self.request("GET", "/orderflow")[0], 308)
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap", headers={"Host": "evil.example"})[0], 403)
        body = {"nonce": "test"}
        self.assertEqual(self.post_json("/orderflow/api/echo", body, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post_json("/orderflow/api/echo", body, headers={"Origin": ""})[0], 403)
        self.assertEqual(self.post_json("/orderflow/api/echo", body)[0], 200)
        self.assertEqual(self.request("DELETE", "/orderflow/api/key", headers={"Origin": ""}, cookie=self.cookie)[0], 403)

    def test_public_storage_and_attempt_limits(self):
        with patch("orderflow.app.MAX_STORED_BYTES", len(PDF) - 1):
            status, error, _ = self.upload()
            self.assertEqual((status, error["error_code"]), (507, "STORAGE_LIMIT"))
            self.assertEqual(list(self.store.files.iterdir()), [])
        status, document, _ = self.upload()
        self.assertEqual(status, 201)
        with patch("orderflow.app.MAX_JOBS_PER_DOCUMENT", 0):
            status, error, _ = self.post_json("/orderflow/api/jobs", {
                "document_id": document["id"], "request_key": str(uuid.uuid4()), "scenario": "success"})
            self.assertEqual((status, error["error_code"]), (429, "JOB_LIMIT"))

    def test_google_protocol_error_returns_safe_json(self):
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        with patch("orderflow.gemini.urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"", 1)):
            status, error, _ = self.request("POST", "/orderflow/api/key/check", cookie=self.cookie)
        self.assertEqual((status, error["error_code"]), (502, "AI_HTTP_UNKNOWN"))

    def test_safe_api_error_codes(self):
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        with patch.object(GeminiAdapter, "check_text", side_effect=AIError("AI_AUTH_FAILED")):
            status, error, _ = self.request("POST", "/orderflow/api/key/check", cookie=self.cookie)
            self.assertEqual((status, error["error_code"]), (502, "AI_AUTH_FAILED"))
        with patch.object(GeminiAdapter, "check_text", side_effect=AIError("AI_MODEL_UNAVAILABLE")):
            status, error, _ = self.request("POST", "/orderflow/api/key/check", cookie=self.cookie)
            self.assertEqual((status, error["error_code"]), (502, "AI_MODEL_UNAVAILABLE"))
        status, document, _ = self.upload()
        self.assertEqual(status, 201)
        with patch.object(GeminiAdapter, "recognize", side_effect=AIError("AI_RATE_LIMITED")):
            status, job, _ = self.post_json("/orderflow/api/jobs", {
                "document_id": document["id"], "request_key": str(uuid.uuid4()), "scenario": "real"})
            self.assertEqual(status, 202)
            for _ in range(40):
                state = self.request("GET", "/orderflow/api/jobs/" + job["id"], cookie=self.cookie)[1]
                if state["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            self.assertEqual((state["state"], state["error_code"]), ("failed", "AI_RATE_LIMITED"))

    def test_new_server_does_not_restore_key(self):
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        newer = AppServer(("127.0.0.1", 0), self.store, ORIGIN, TEST_HASH)
        thread = threading.Thread(target=newer.serve_forever, daemon=True)
        thread.start()
        old_server = self.server
        try:
            self.server = newer
            self.assertFalse(self.request("GET", "/orderflow/api/key", cookie=self.cookie)[1]["configured"])
        finally:
            self.server = old_server
            newer.shutdown()
            newer.server_close()
            thread.join(timeout=2)

    def test_session_key_is_transient_and_real_job_requires_it(self):
        status, document, _ = self.upload()
        self.assertEqual(status, 201)
        body = {"document_id": document["id"], "request_key": str(uuid.uuid4()), "scenario": "real"}
        self.assertEqual(self.post_json("/orderflow/api/jobs", body)[1]["error_code"], "KEY_REQUIRED")
        status, response, _ = self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        self.assertEqual((status, response["configured"]), (200, True))
        second_cookie = self.login(cookie="of_session=invalid")[0]
        self.assertFalse(self.request("GET", "/orderflow/api/key", cookie=second_cookie)[1]["configured"])
        with patch.object(GeminiAdapter, "check_text", return_value=None):
            self.assertEqual(self.request("POST", "/orderflow/api/key/check", cookie=self.cookie)[0], 200)
        with patch.object(GeminiAdapter, "recognize", return_value={"items": [{"description": "測試", "quantity": 2}]}):
            status, job, _ = self.post_json("/orderflow/api/jobs", body)
            self.assertEqual(status, 202)
            for _ in range(40):
                state = self.request("GET", "/orderflow/api/jobs/" + job["id"], cookie=self.cookie)[1]
                if state["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            self.assertEqual((state["state"], state["mode"], state["result"][0]["description"]), ("done", "real", "測試"))
        self.assertNotIn(FAKE_KEY, self.store.db_path.read_bytes().decode("utf-8", errors="ignore"))
        self.assertNotIn(FAKE_KEY, json.dumps(self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)[1]))
        self.assertEqual(self.request("DELETE", "/orderflow/api/key", cookie=self.cookie)[1]["configured"], False)
        self.assertFalse(self.request("GET", "/orderflow/api/key", cookie=self.cookie)[1]["configured"])
        self.server.set_key(self.store.session(self.cookie.split("=", 1)[1]), FAKE_KEY)
        session_id = self.store.session(self.cookie.split("=", 1)[1])
        with self.server.key_lock:
            self.server.keys[session_id] = (FAKE_KEY, time.monotonic() - 1)
        self.assertFalse(self.request("GET", "/orderflow/api/key", cookie=self.cookie)[1]["configured"])


class GeminiAdapterTests(unittest.TestCase):
    def test_pdf_request_fixed_model_header_and_schema(self):
        seen = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size):
                return b'{"candidates":[{"content":{"parts":[{"text":"{\\"items\\":[]}"}]}}]}'
        def fake_open(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return Response()
        with tempfile.TemporaryDirectory() as temp, patch("orderflow.gemini.urllib.request.urlopen", side_effect=fake_open):
            file = Path(temp) / "sample.pdf"
            file.write_bytes(PDF)
            result = GeminiAdapter(FAKE_KEY).recognize(str(file), deadline_seconds=25)
        self.assertEqual(result, {"items": []})
        request = seen["request"]
        self.assertIn("/models/gemini-3.1-flash-lite:generateContent", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, request.data.decode())
        payload = json.loads(request.data)
        self.assertEqual(payload["contents"][0]["parts"][1]["inline_data"]["mime_type"], "application/pdf")
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")

    def test_sanitized_google_errors(self):
        for status, code in [(401, "AI_AUTH_FAILED"), (403, "AI_AUTH_FAILED"), (429, "AI_RATE_LIMITED"), (400, "AI_BAD_REQUEST"), (404, "AI_MODEL_UNAVAILABLE")]:
            with self.subTest(status=status), patch("orderflow.gemini.urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", status, "secret", {}, None)):
                with self.assertRaises(AIError) as caught:
                    _generate(FAKE_KEY, [{"text": "test"}], timeout=1, structured=False)
                self.assertEqual(caught.exception.code, code)
        with patch("orderflow.gemini.urllib.request.urlopen", side_effect=TimeoutError("secret")):
            with self.assertRaises(AIUnknown) as caught:
                _generate(FAKE_KEY, [{"text": "test"}], timeout=1, structured=False)
            self.assertEqual(caught.exception.code, "AI_TIMEOUT_UNKNOWN")
        with patch("orderflow.gemini.urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"", 1)):
            with self.assertRaises(AIUnknown) as caught:
                _generate(FAKE_KEY, [{"text": "test"}], timeout=1, structured=False)
            self.assertEqual(caught.exception.code, "AI_HTTP_UNKNOWN")
        class BadResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size): return b'not-json-and-no-secret'
        with patch("orderflow.gemini.urllib.request.urlopen", return_value=BadResponse()):
            with self.assertRaises(AIError) as caught:
                _generate(FAKE_KEY, [{"text": "test"}], timeout=1, structured=False)
            self.assertEqual(caught.exception.code, "AI_BAD_RESPONSE")


if __name__ == "__main__":
    unittest.main()
