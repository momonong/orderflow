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
from unittest.mock import patch

from pypdf import PdfWriter
from orderflow.ai import AIError, AIUnknown
from orderflow.app import AppServer, Store
from orderflow.gemini import GeminiAdapter, MODEL, _generate

ORIGIN = "https://momonong.me"
FAKE_KEY = "fake-test-key-never-use-12345"
writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
stream = io.BytesIO()
writer.write(stream)
PDF = stream.getvalue()


class PublicAiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.server = AppServer(("127.0.0.1", 0), self.store, ORIGIN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, data, cookie = self.request("GET", "/orderflow/api/bootstrap")
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0]
        self.assertIn("; Secure", cookie)
        self.assertFalse(data["ai_key_configured"])
        self.assertEqual(data["ai_model"], MODEL)

    def tearDown(self):
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
        data = json.loads(raw) if resp.getheader("Content-Type", "").startswith("application/json") else raw
        result = resp.status, data, resp.getheader("Set-Cookie")
        conn.close()
        return result

    def post_json(self, path, value, *, cookie=None, headers=None):
        return self.request("POST", path, json.dumps(value), {"Content-Type": "application/json", **(headers or {})}, cookie or self.cookie)

    def upload(self):
        return self.request("POST", "/orderflow/api/documents", PDF, {
            "Content-Type": "application/pdf", "X-File-Size": str(len(PDF)),
            "X-File-SHA256": hashlib.sha256(PDF).hexdigest(), "X-Request-Key": str(uuid.uuid4()),
        }, self.cookie)

    def test_loopback_only_and_idle_key_expiry(self):
        with self.assertRaises(ValueError):
            AppServer(("0.0.0.0", 0), self.store, ORIGIN)
        second_cookie = self.request("GET", "/orderflow/api/bootstrap", cookie="of_session=invalid")[2].split(";", 1)[0]
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
        self.assertEqual(self.request("GET", "/orderflow/api/health")[0], 200)
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
        newer = AppServer(("127.0.0.1", 0), self.store, ORIGIN)
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
        second_cookie = self.request("GET", "/orderflow/api/bootstrap", cookie="of_session=invalid")[2].split(";", 1)[0]
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
