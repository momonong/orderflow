import hashlib
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import shutil
import subprocess
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
        if method in {"POST", "PUT", "DELETE"}:
            hdr.setdefault("Origin", ORIGIN)
            hdr["X-Orderflow-Request"] = "1"
        if cookie:
            hdr["Cookie"] = cookie
        conn.request(method, path, body=body, headers=hdr)
        resp = conn.getresponse()
        self.last_headers = dict(resp.getheaders())
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
        status, home, _ = self.request("GET", "/orderflow/")
        self.assertEqual(status, 200)
        self.assertIn(b"manage.js", home)
        status, diagnostic, _ = self.request("GET", "/orderflow/test/")
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", diagnostic)
        self.assertEqual(self.request("GET", "/orderflow/test")[0], 308)

    def test_app_response_marker_and_safe_google_check_reason(self):
        request_id = str(uuid.uuid4())
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        with patch.object(GeminiAdapter, "check_text",
                          side_effect=AIError("AI_BAD_REQUEST", upstream_http_status=400,
                                              upstream_reason="INVALID_ARGUMENT")):
            status, body, _ = self.request("POST", "/orderflow/api/key/check",
                                           headers={"X-Orderflow-Request-Id": request_id}, cookie=self.cookie)
        self.assertEqual((status, body), (502, {"error_code": "AI_BAD_REQUEST",
                                                   "upstream_http_status": 400,
                                                   "upstream_reason": "INVALID_ARGUMENT"}))
        self.assertEqual(self.last_headers["X-Orderflow-Origin"], "app")
        self.assertEqual(self.last_headers["X-Orderflow-Request-Id"], request_id)
        status, _, _ = self.request("GET", "/orderflow/api/bootstrap",
                                    headers={"X-Orderflow-Request-Id": "not-a-uuid"}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertTrue(uuid.UUID(self.last_headers["X-Orderflow-Request-Id"]))
        self.assertNotEqual(self.last_headers["X-Orderflow-Request-Id"], "not-a-uuid")

    def test_typed_record_api_requires_real_source_and_session(self):
        upload_headers = {"Content-Type": "application/pdf", "X-File-Size": str(len(PDF)),
                          "X-File-SHA256": hashlib.sha256(PDF).hexdigest(),
                          "X-Request-Key": str(uuid.uuid4()), "X-Document-Kind": "purchase_order"}
        without_kind = dict(upload_headers)
        without_kind.pop("X-Document-Kind")
        self.assertEqual(self.request("POST", "/orderflow/api/management/documents",
                                      PDF, without_kind, self.cookie)[0], 400)
        status, document, _ = self.request("POST", "/orderflow/api/management/documents",
                                           PDF, upload_headers, self.cookie)
        self.assertEqual(status, 201)
        self.assertEqual(document["document_kind"], "purchase_order")
        upload_headers["X-Request-Key"] = str(uuid.uuid4())
        status, duplicate, _ = self.request("POST", "/orderflow/api/management/documents",
                                            PDF, upload_headers, self.cookie)
        self.assertEqual((status, duplicate["id"], duplicate["duplicate"]),
                         (201, document["id"], True))
        record_path = "/orderflow/api/management/record-sets/" + document["id"]
        row = {"id": str(uuid.uuid4()), "orderNo": "PO-[1]", "invoiceNo": None,
               "client": None, "product": " A//[β]  B ", "code": None, "qty": "2.50",
               "unitPrice": None, "amount": None, "currency": None, "date": None,
               "incoterms": None, "unit": "PCS", "status": "確認中",
               "linked_order_row_id": None, "deleted": False}
        body = {"source_job_id": str(uuid.uuid4()), "revision": 0, "rows": [row]}
        status, _, _ = self.request("PUT", record_path, json.dumps(body),
                                    {"Content-Type": "application/json"}, self.cookie)
        self.assertEqual(status, 404, "unrelated real job must not create records")
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        with patch.object(GeminiAdapter, "recognize", return_value={"items": [{
                "orderNo": "PO-[1]", "product": " A//[β]  B ", "qty": "2.50", "unit": "PCS"}]}):
            status, job, _ = self.post_json("/orderflow/api/management/jobs", {
                "document_id": document["id"], "request_key": str(uuid.uuid4()), "scenario": "real"})
            self.assertEqual(status, 202)
            for _ in range(100):
                ready = self.request("GET", "/orderflow/api/management/jobs/" + job["id"],
                                     cookie=self.cookie)[1]
                if ready["state"] == "done":
                    break
                time.sleep(0.02)
        self.assertEqual(ready["state"], "done")
        self.assertEqual(ready["result"][0]["product"], " A//[β]  B ")
        body["source_job_id"] = job["id"]
        status, saved, _ = self.request("PUT", record_path, json.dumps(body),
                                        {"Content-Type": "application/json"}, self.cookie)
        self.assertEqual((status, saved["revision"]), (201, 1))
        bootstrap = self.request("GET", "/orderflow/api/management/bootstrap", cookie=self.cookie)[1]
        self.assertEqual(bootstrap["record_sets"][0]["rows"][0]["product"], " A//[β]  B ")
        self.assertNotIn("record_sets", self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)[1])
        self.assertEqual(self.request("GET", record_path, cookie="of_session=bad")[0], 401)
        other_cookie, _ = self.login(cookie="")
        self.assertEqual(self.request("GET", record_path, cookie=other_cookie)[0], 404)
        self.assertEqual(self.request("PUT", record_path, json.dumps(body),
                                      {"Content-Type": "application/json"}, other_cookie)[0], 404)

    def test_management_scope_real_job_and_draft_revision(self):
        diagnostic = self.upload()[1]
        # Existing unclassified management documents remain readable/editable;
        # the new upload API requires an explicit kind for new documents.
        key = str(uuid.uuid4())
        owner = self.store.session(self.cookie.split("=", 1)[1])
        managed = self.store.add_document(owner, key, PDF, hashlib.sha256(PDF).hexdigest(),
                                          1, 1, "management")
        self.assertNotEqual(managed["id"], diagnostic["id"])
        replay = self.store.add_document(owner, key, PDF, hashlib.sha256(PDF).hexdigest(),
                                         1, 1, "management")
        self.assertEqual(replay["id"], managed["id"])
        diagnostic_view = self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)[1]
        management_view = self.request("GET", "/orderflow/api/management/bootstrap", cookie=self.cookie)[1]
        self.assertEqual([doc["id"] for doc in diagnostic_view["documents"]], [diagnostic["id"]])
        self.assertEqual([doc["id"] for doc in management_view["documents"]], [managed["id"]])
        self.assertEqual(management_view["drafts"], [])
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        self.assertEqual(self.post_json("/orderflow/api/management/jobs", {
            "document_id": diagnostic["id"], "request_key": str(uuid.uuid4()), "scenario": "real"
        })[0], 404)
        self.assertEqual(self.post_json("/orderflow/api/jobs", {
            "document_id": managed["id"], "request_key": str(uuid.uuid4()), "scenario": "real"
        })[0], 404)
        self.assertEqual(self.post_json("/orderflow/api/management/jobs", {
            "document_id": managed["id"], "request_key": str(uuid.uuid4()), "scenario": "success"
        })[0], 400)
        rows = [{"description": "人工核對品項", "quantity": 2}]
        with patch.object(GeminiAdapter, "recognize", return_value={"items": rows}):
            job_key = str(uuid.uuid4())
            status, job, _ = self.post_json("/orderflow/api/management/jobs", {
                "document_id": managed["id"], "request_key": job_key, "scenario": "real"
            })
            self.assertEqual(status, 202)
            status, same, _ = self.post_json("/orderflow/api/management/jobs", {
                "document_id": managed["id"], "request_key": job_key, "scenario": "real"
            })
            self.assertEqual(same["id"], job["id"])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = self.request("GET", "/orderflow/api/management/jobs/" + job["id"], cookie=self.cookie)[1]
                if result["state"] == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(result["state"], "done")
        self.assertEqual(self.request("GET", "/orderflow/api/jobs/" + job["id"], cookie=self.cookie)[0], 404)
        self.assertEqual(self.request("GET", "/orderflow/api/management/jobs/" + job["id"], cookie=self.cookie)[0], 200)
        path = "/orderflow/api/management/drafts/" + job["id"]
        self.assertEqual(self.request("PUT", path, json.dumps({"rows": rows, "revision": 0}),
                                      {"Content-Type": "application/json"}, self.cookie)[0], 201)
        saved = self.request("GET", path, cookie=self.cookie)[1]
        self.assertEqual((saved["rows"], saved["revision"]), (rows, 1))
        self.assertEqual(self.request("PUT", path, json.dumps({"rows": rows, "revision": 0}),
                                      {"Content-Type": "application/json"}, self.cookie)[1]["revision"], 1)
        changed = [{"description": "修正", "quantity": 3}]
        self.assertEqual(self.request("PUT", path, json.dumps({"rows": changed, "revision": 0}),
                                      {"Content-Type": "application/json"}, self.cookie)[1]["error_code"],
                         "DRAFT_VERSION_CONFLICT")
        status, updated, _ = self.request("PUT", path, json.dumps({"rows": changed, "revision": 1}),
                                          {"Content-Type": "application/json"}, self.cookie)
        self.assertEqual((status, updated["revision"]), (200, 2))
        self.assertEqual(self.request("PUT", path, json.dumps({"rows": [{"description": "", "quantity": 1}], "revision": 2}),
                                      {"Content-Type": "application/json"}, self.cookie)[0], 400)
        self.assertEqual(self.request("GET", path, cookie="of_session=invalid")[0], 401)
        restarted = Store(Path(self.tmp.name))
        self.assertEqual(restarted.management_draft(self.store.session(self.cookie.split("=", 1)[1]), job["id"])["rows"], changed)
        self.assertEqual(restarted.job(self.store.session(self.cookie.split("=", 1)[1]), job["id"], "management")["result"], rows)

    def test_management_first_save_concurrent_conflict(self):
        session_id = self.store.session(self.cookie.split("=", 1)[1])
        doc = self.store.add_document(session_id, str(uuid.uuid4()), PDF,
                                      hashlib.sha256(PDF).hexdigest(), 1, 1, "management")
        job, _ = self.store.add_job(session_id, doc["id"], str(uuid.uuid4()), "real", "management")
        original = [{"description": "來源", "quantity": 1}]
        self.store.set_job(job["id"], state="done", result=original)
        alternatives = [[{"description": "甲", "quantity": 2}],
                        [{"description": "乙", "quantity": 3}]]
        def attempt(rows):
            try:
                return self.store.save_management_draft(session_id, job["id"], rows, 0)
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, alternatives))
        self.assertEqual(sum(isinstance(item, tuple) for item in outcomes), 1)
        self.assertIn("DRAFT_VERSION_CONFLICT", outcomes)
        saved = self.store.management_draft(session_id, job["id"])
        self.assertEqual(saved["revision"], 1)
        self.assertIn(saved["rows"], alternatives)
        self.assertEqual(self.store.job(session_id, job["id"], "management")["result"], original)

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

    def test_key_input_shape_is_local_and_does_not_disclose_value(self):
        invalid = (None, "", "with space", "line\nbreak", "非ASCII",
                   "https://aistudio.google.com/apikey", "www.example.com/key", "x" * 257)
        for candidate in invalid:
            with self.subTest(kind=type(candidate).__name__, size=len(candidate) if isinstance(candidate, str) else 0):
                status, body, _ = self.post_json("/orderflow/api/key", {"key": candidate})
                self.assertEqual((status, body), (400, {"error_code": "BAD_KEY"}))
                self.assertFalse(self.request("GET", "/orderflow/api/key", cookie=self.cookie)[1]["configured"])

        # No fixed Google prefix or length is assumed. Saving is local; no Google request is made.
        opaque = "opaqueA12!"
        with patch.object(GeminiAdapter, "check_text", side_effect=AssertionError("network check")):
            status, body, _ = self.post_json("/orderflow/api/key", {"key": opaque})
        self.assertEqual(status, 200)
        self.assertTrue(body["configured"])
        self.assertNotIn(opaque, json.dumps(body))
        self.assertNotIn(opaque, json.dumps(self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)[1]))
        self.assertNotIn(opaque, self.store.db_path.read_bytes().decode("utf-8", errors="ignore"))


    def test_synthetic_pdf_to_typed_records_reopen_and_ui_reports(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is needed for the browser-model integration check")
        kinds = ("purchase_order", "invoice")
        jobs = {}
        for kind in kinds:
            upload_headers = {"Content-Type": "application/pdf", "X-File-Size": str(len(PDF)),
                              "X-File-SHA256": hashlib.sha256(PDF).hexdigest(),
                              "X-Request-Key": str(uuid.uuid4()), "X-Document-Kind": kind}
            status, doc, _ = self.request("POST", "/orderflow/api/management/documents",
                                           PDF, upload_headers, self.cookie)
            self.assertEqual(status, 201)
            self.assertEqual(doc["document_kind"], kind)
            jobs[kind] = {"document": doc}
        def fake_recognize(adapter, pdf_path, *, deadline_seconds):
            self.assertGreater(deadline_seconds, 0)
            self.assertEqual(Path(pdf_path).read_bytes(), PDF)
            if adapter.document_kind == "purchase_order":
                return {"items": [
                    {"orderNo": "=PO-1", "client": "Buyer A", "product": " PO Product A ",
                     "code": "A", "qty": "10.50", "amount": "21.00", "currency": "USD", "unit": "PCS"},
                    {"orderNo": "PO-2", "client": "Buyer B", "product": "PO Product B",
                     "code": "B", "qty": "5", "amount": "1200", "currency": "KRW", "unit": "PCS"}]}
            return {"items": [
                {"invoiceNo": "INV-1", "client": "Buyer A", "product": "PO Product A",
                 "code": "A", "qty": "3.25", "amount": "6.50", "currency": "USD", "unit": "PCS"},
                {"invoiceNo": "INV-2", "client": "Buyer B", "product": "PO Product B",
                 "code": "B", "qty": "1.5", "amount": "360", "currency": "KRW", "unit": "PCS"},
                {"invoiceNo": "INV-3", "client": "Buyer C", "product": "Unmatched",
                 "code": "C", "qty": "2", "amount": "8", "currency": "USD", "unit": "PCS"}]}
        self.post_json("/orderflow/api/key", {"key": FAKE_KEY})
        with patch.object(GeminiAdapter, "recognize", fake_recognize):
            for kind in kinds:
                status, job, _ = self.post_json("/orderflow/api/management/jobs", {
                    "document_id": jobs[kind]["document"]["id"],
                    "request_key": str(uuid.uuid4()), "scenario": "real"})
                self.assertEqual(status, 202)
                for _ in range(100):
                    ready = self.request("GET", "/orderflow/api/management/jobs/" + job["id"],
                                         cookie=self.cookie)[1]
                    if ready["state"] == "done":
                        break
                    time.sleep(0.02)
                self.assertEqual(ready["state"], "done")
                jobs[kind]["job"] = ready
        order_rows = []
        for suggestion in jobs["purchase_order"]["job"]["result"]:
            order_rows.append({"id": str(uuid.uuid4()), "orderNo": suggestion["orderNo"],
                               "invoiceNo": None, "client": suggestion["client"],
                               "product": suggestion["product"], "code": suggestion["code"],
                               "qty": suggestion["qty"], "unitPrice": None,
                               "amount": suggestion["amount"], "currency": suggestion["currency"],
                               "date": None, "incoterms": None, "unit": suggestion["unit"],
                               "status": "確定", "linked_order_row_id": None, "deleted": False})
        for kind in kinds:
            suggestions = jobs[kind]["job"]["result"]
            if kind == "purchase_order":
                rows = order_rows
            else:
                rows = []
                for index, suggestion in enumerate(suggestions):
                    rows.append({"id": str(uuid.uuid4()), "orderNo": None,
                                 "invoiceNo": suggestion["invoiceNo"], "client": suggestion["client"],
                                 "product": suggestion["product"], "code": suggestion["code"],
                                 "qty": suggestion["qty"], "unitPrice": None,
                                 "amount": suggestion["amount"], "currency": suggestion["currency"],
                                 "date": None, "incoterms": None, "unit": suggestion["unit"],
                                 "status": None,
                                 "linked_order_row_id": order_rows[index]["id"] if index < 2 else None,
                                 "deleted": False})
            path = "/orderflow/api/management/record-sets/" + jobs[kind]["document"]["id"]
            body = {"source_job_id": jobs[kind]["job"]["id"], "revision": 0, "rows": rows}
            status, saved, _ = self.request("PUT", path, json.dumps(body),
                                            {"Content-Type": "application/json"}, self.cookie)
            self.assertEqual((status, saved["revision"], len(saved["rows"])),
                             (201, 1, len(rows)))
        reopened = Store(Path(self.tmp.name))
        self.server.store = reopened
        status, bootstrap, _ = self.request("GET", "/orderflow/api/management/bootstrap",
                                             cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(bootstrap["record_sets"]), 2)
        self.assertEqual(len(bootstrap["documents"]), 2)
        script = Path(__file__).with_name("check_management_bootstrap_ui.cjs")
        checked = subprocess.run(["node", str(script)], input=json.dumps(bootstrap),
                                 text=True, capture_output=True, timeout=10,
                                 cwd=Path(__file__).resolve().parents[1], check=False)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("HTTP bootstrap to UI lists", checked.stdout)


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

    def test_typed_invoice_request_uses_full_nullable_field_schema(self):
        seen = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size):
                return b'{"candidates":[{"content":{"parts":[{"text":"{\\"items\\":[]}"}]}}]}'
        def fake_open(request, timeout):
            seen["request"] = request
            return Response()
        with tempfile.TemporaryDirectory() as temp, patch("orderflow.gemini.urllib.request.urlopen", side_effect=fake_open):
            file = Path(temp) / "sample.pdf"
            file.write_bytes(PDF)
            self.assertEqual(GeminiAdapter(FAKE_KEY, "invoice").recognize(str(file), deadline_seconds=25),
                             {"items": []})
        payload = json.loads(seen["request"].data)
        properties = payload["generationConfig"]["responseSchema"]["properties"]["items"]["items"]["properties"]
        self.assertTrue({"invoiceNo", "client", "product", "code", "qty", "unitPrice", "amount",
                         "currency", "date", "incoterms", "unit"}.issubset(properties))
        prompt = payload["contents"][0]["parts"][0]["text"]
        self.assertIn("a Company label may identify the issuer", prompt)
        self.assertIn("never infer customer, currency, date, zero", prompt)
        self.assertNotIn(FAKE_KEY, seen["request"].data.decode())

    def test_sanitized_google_errors(self):
        for status, code in [(401, "AI_AUTH_FAILED"), (403, "AI_AUTH_FAILED"), (429, "AI_RATE_LIMITED"), (400, "AI_BAD_REQUEST"), (404, "AI_MODEL_UNAVAILABLE")]:
            with self.subTest(status=status), patch("orderflow.gemini.urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", status, "secret", {}, None)):
                with self.assertRaises(AIError) as caught:
                    _generate(FAKE_KEY, [{"text": "test"}], timeout=1, structured=False)
                self.assertEqual(caught.exception.code, code)
        for raw, reason in [(b'{"error":{"status":"FAILED_PRECONDITION","message":"secret"}}',
                             "FAILED_PRECONDITION"),
                            (b'{"error":{"status":"PRIVATE_UNKNOWN","message":"secret"}}',
                             "UNCLASSIFIED"), (b"not json", "UNCLASSIFIED")]:
            error = urllib.error.HTTPError("url", 400, "secret", {}, io.BytesIO(raw))
            with self.subTest(reason=reason), patch("orderflow.gemini.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(AIError) as caught:
                    _generate(FAKE_KEY, [{"text": "test"}], timeout=1, structured=False)
                self.assertEqual(caught.exception.code, "AI_BAD_REQUEST")
                self.assertEqual(caught.exception.upstream_http_status, 400)
                self.assertEqual(caught.exception.upstream_reason, reason)
                self.assertNotIn("secret", str(caught.exception))
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
