import hashlib
import io
import http.client
import json
import socket
from unittest.mock import patch
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from orderflow.app import AppServer, MAX_PDF_BYTES, Store
from orderflow.ai import AIError, AIUnknown
from pypdf import PdfWriter

writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
writer.add_metadata({"/Title": "PRIVATE_CUSTOMER_MARKER"})
buffer = io.BytesIO()
writer.write(buffer)
PDF = buffer.getvalue()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.server = AppServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = self.bootstrap()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = dict(headers or {})
        if cookie is None:
            cookie = getattr(self, "cookie", None)
        if cookie:
            headers["Cookie"] = cookie
        if method == "POST":
            headers["X-Orderflow-Request"] = "1"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        data = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        result = response.status, data, response.getheader("Set-Cookie")
        conn.close()
        return result

    def bootstrap(self):
        status, data, cookie = self.request("GET", "/orderflow/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "mock-and-real")
        return cookie.split(";", 1)[0]

    def upload(self, data=PDF, key=None, sha=None, declared=None, cookie=None):
        return self.request("POST", "/orderflow/api/documents", data, {
            "Content-Type": "application/pdf", "X-File-Size": str(len(data) if declared is None else declared),
            "X-File-SHA256": sha or hashlib.sha256(data).hexdigest(),
            "X-Request-Key": key or str(uuid.uuid4())
        }, cookie)

    def submit(self, doc, scenario="success", key=None, cookie=None):
        body = json.dumps({"document_id": doc, "request_key": key or str(uuid.uuid4()), "scenario": scenario})
        return self.request("POST", "/orderflow/api/jobs", body, {"Content-Type": "application/json"}, cookie)

    def wait_job(self, job_id):
        for _ in range(40):
            status, job, _ = self.request("GET", "/orderflow/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_subpath_assets_json_and_isolation(self):
        for path in ("/orderflow/", "/orderflow/app.js", "/orderflow/style.css"):
            self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(self.request("GET", "/orderflow")[0], 308)
        body = json.dumps({"nonce": "中文 <&>"})
        status, result, _ = self.request("POST", "/orderflow/api/echo", body, {"Content-Type": "application/json"})
        self.assertEqual((status, result["nonce"]), (200, "中文 <&>"))
        self.assertIn("中文 <測試>", self.request("GET", "/orderflow/api/sample")[1]["rows"][0]["description"])
        doc = self.upload()[1]
        job = self.submit(doc["id"])[1]
        second_cookie = self.request("GET", "/orderflow/api/bootstrap", cookie="of_session=invalid")[2].split(";", 1)[0]
        status, second, _ = self.request("GET", "/orderflow/api/bootstrap", cookie=second_cookie)
        self.assertEqual((status, second["documents"], second["jobs"]), (200, [], []))
        self.assertEqual(self.request("GET", "/orderflow/api/jobs/" + job["id"], cookie=second_cookie)[0], 404)
        self.assertEqual(self.submit(doc["id"], cookie=second_cookie)[0], 404)
        self.assertEqual(self.request("GET", "/orderflow/api/jobs/" + str(uuid.uuid4()))[0], 404)
        self.assertEqual(self.request("GET", "/orderflow/../../local-data")[0], 404)

    def test_upload_validation_and_receipt(self):
        status, doc, _ = self.upload()
        self.assertEqual(status, 201)
        self.assertEqual(doc["size"], len(PDF))
        self.assertEqual(doc["page_count"], 1)
        self.assertEqual(doc["sha256"], hashlib.sha256(PDF).hexdigest())
        self.assertEqual((Path(self.tmp.name) / "files" / (doc["id"] + ".pdf")).read_bytes(), PDF)
        self.assertEqual(self.upload(b"")[1]["error_code"], "BAD_SIZE")
        self.assertEqual(self.upload(b"not a PDF")[1]["error_code"], "PDF_INVALID")
        self.assertEqual(self.upload(b"%PDF-1.4\n%%EOF")[1]["error_code"], "PDF_INVALID")
        self.assertEqual(self.upload(sha="0" * 64)[1]["error_code"], "HASH_MISMATCH")
        self.assertEqual(self.upload(declared=len(PDF) + 1)[1]["error_code"], "SIZE_MISMATCH")
        status, data, _ = self.upload(declared=MAX_PDF_BYTES + 1)
        self.assertEqual((status, data["error_code"]), (413, "FILE_TOO_LARGE"))
        status, data, _ = self.request("POST", "/orderflow/api/documents", b"", {"Content-Length": str(MAX_PDF_BYTES + 1), "Content-Type": "application/pdf", "X-File-Size": str(MAX_PDF_BYTES + 1), "X-File-SHA256": "0" * 64, "X-Request-Key": str(uuid.uuid4())})
        self.assertEqual((status, data["error_code"]), (413, "FILE_TOO_LARGE"))
        self.assertNotIn("PRIVATE_CUSTOMER_MARKER", json.dumps(doc))
        self.assertEqual(len(list((Path(self.tmp.name) / "files").iterdir())), 1)

    def test_idempotency_attempts_failures_and_restore(self):
        key = str(uuid.uuid4())
        doc = self.upload(key=key)[1]
        self.assertEqual(self.upload(key=key)[1]["id"], doc["id"])
        self.assertEqual(self.upload(data=PDF + b" ", key=key)[1]["error_code"], "IDEMPOTENCY_CONFLICT")
        job_key = str(uuid.uuid4())
        job = self.submit(doc["id"], key=job_key)[1]
        self.assertEqual(self.submit(doc["id"], key=job_key)[1]["id"], job["id"])
        done = self.wait_job(job["id"])
        self.assertEqual((done["state"], done["attempt"], done["mode"]), ("done", 1, "mock"))
        self.assertIn("<測試>", done["result"][0]["description"])
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap")[1]["jobs"][0]["id"], job["id"])
        for scenario, state, code in (("fail", "failed", "AI_UNAVAILABLE"), ("timeout", "unknown", "AI_TIMEOUT_UNKNOWN"), ("invalid", "failed", "RESULT_FORMAT_INVALID")):
            rerun = self.submit(doc["id"], scenario)[1]
            result = self.wait_job(rerun["id"])
            self.assertEqual((result["state"], result["error_code"]), (state, code))
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap")[1]["jobs"][0]["attempt"], 4)
        self.store.set_job(job["id"], state="running")
        restarted = Store(Path(self.tmp.name))
        recovered = restarted.job(self.store.session(self.cookie.split("=", 1)[1]), job["id"])
        self.assertEqual((recovered["state"], recovered["error_code"]), ("unknown", "SERVER_RESTART"))

    def test_invalid_json_and_partial_json(self):
        status, data, _ = self.request("POST", "/orderflow/api/echo", b"\xff", {"Content-Type": "application/json"})
        self.assertEqual((status, data["error_code"]), (400, "BAD_JSON"))
        with patch("orderflow.app.JSON_DEADLINE_SECONDS", 0.2):
            sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
            sock.settimeout(2)
            request = ("POST /orderflow/api/echo HTTP/1.1\r\n"
                       f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                       f"Cookie: {self.cookie}\r\n"
                       "X-Orderflow-Request: 1\r\n"
                       "Content-Type: application/json\r\n"
                       "Content-Length: 100\r\n\r\n").encode()
            sock.sendall(request + b"{")
            response = sock.recv(4096)
            self.assertIn(b"408 Request Timeout", response)
            sock.close()

    def test_partial_upload_has_deadline(self):
        with patch("orderflow.app.UPLOAD_DEADLINE_SECONDS", 0.2):
            sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
            sock.settimeout(2)
            request = ("POST /orderflow/api/documents HTTP/1.1\r\n"
                       f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                       f"Cookie: {self.cookie}\r\n"
                       "X-Orderflow-Request: 1\r\n"
                       "Content-Type: application/pdf\r\n"
                       f"Content-Length: {len(PDF)}\r\n"
                       f"X-File-Size: {len(PDF)}\r\n"
                       f"X-File-SHA256: {hashlib.sha256(PDF).hexdigest()}\r\n"
                       f"X-Request-Key: {uuid.uuid4()}\r\n\r\n").encode()
            sock.sendall(request + PDF[:5])
            response = sock.recv(4096)
            self.assertIn(b"408 Request Timeout", response)
            sock.close()

    def test_pdf_checker_timeout_is_bounded(self):
        with patch("orderflow.app.subprocess.run", side_effect=subprocess.TimeoutExpired("pdfcheck", 5)):
            status, data, _ = self.upload()
        self.assertEqual((status, data["error_code"]), (408, "PDF_CHECK_TIMEOUT"))
        self.assertEqual(self.request("GET", "/orderflow/api/bootstrap")[1]["documents"], [])

    def test_host_header_rejected(self):
        status, data, _ = self.request("GET", "/orderflow/api/bootstrap", headers={"Host": "other.example"})
        self.assertEqual((status, data["error_code"]), (403, "HOST_NOT_ALLOWED"))

    def test_exact_pdf_limit_and_encrypted_rejection(self):
        def sized_pdf(title_size):
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_metadata({"/Title": "X" * title_size})
            buffer = io.BytesIO()
            writer.write(buffer)
            return buffer.getvalue()
        base_size = len(sized_pdf(0))
        title_size = MAX_PDF_BYTES - base_size
        candidate = sized_pdf(title_size)
        candidate = sized_pdf(title_size - (len(candidate) - MAX_PDF_BYTES))
        self.assertEqual(len(candidate), MAX_PDF_BYTES)
        status, doc, _ = self.upload(candidate)
        self.assertEqual((status, doc["page_count"]), (201, 1))
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("test-password")
        buffer = io.BytesIO()
        writer.write(buffer)
        status, data, _ = self.upload(buffer.getvalue())
        self.assertEqual((status, data["error_code"]), (415, "PDF_INVALID"))
        self.assertEqual(len(list((Path(self.tmp.name) / "files").iterdir())), 1)

    def test_lost_upload_response_same_key(self):
        key = str(uuid.uuid4())
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = ("POST /orderflow/api/documents HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{self.server.server_port}\r\n"
                   f"Cookie: {self.cookie}\r\n"
                   "X-Orderflow-Request: 1\r\n"
                   "Content-Type: application/pdf\r\n"
                   f"Content-Length: {len(PDF)}\r\n"
                   f"X-File-Size: {len(PDF)}\r\n"
                   f"X-File-SHA256: {hashlib.sha256(PDF).hexdigest()}\r\n"
                   f"X-Request-Key: {key}\r\n\r\n").encode()
        sock.sendall(request + PDF)
        sock.close()  # Deliberately discard the receipt after sending the full body.
        session_id = self.store.session(self.cookie.split("=", 1)[1])
        for _ in range(30):
            documents = self.store.documents(session_id)
            if documents:
                break
            time.sleep(0.05)
        self.assertEqual(len(documents), 1)
        status, replay, _ = self.upload(key=key)
        self.assertEqual((status, replay["id"]), (201, documents[0]["id"]))
        self.assertEqual(len(self.store.documents(session_id)), 1)

    def test_adapter_error_codes_are_sanitized(self):
        doc = self.upload()[1]
        class LeakyAdapter:
            def __init__(self, scenario):
                self.scenario = scenario
            def recognize(self, pdf_path, *, deadline_seconds):
                if self.scenario == "timeout":
                    raise AIUnknown("RAW_AI_RESPONSE PRIVATE_CUSTOMER")
                raise AIError("RAW_AI_RESPONSE PRIVATE_CUSTOMER")
        self.server.adapter_factory = LeakyAdapter
        failed = self.wait_job(self.submit(doc["id"], "fail")[1]["id"])
        unknown = self.wait_job(self.submit(doc["id"], "timeout")[1]["id"])
        self.assertEqual((failed["state"], failed["error_code"]), ("failed", "AI_FAILURE"))
        self.assertEqual((unknown["state"], unknown["error_code"]), ("unknown", "AI_RESULT_UNKNOWN"))
        self.assertNotIn("PRIVATE_CUSTOMER", json.dumps(self.request("GET", "/orderflow/api/bootstrap")[1]))

    def test_parallel_same_key(self):
        from concurrent.futures import ThreadPoolExecutor
        key = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.upload(key=key), range(4)))
        self.assertEqual({response[0] for response in responses}, {201})
        self.assertEqual(len({response[1]["id"] for response in responses}), 1)
        doc_id = responses[0][1]["id"]
        job_key = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.submit(doc_id, key=job_key), range(4)))
        self.assertEqual(len({response[1]["id"] for response in jobs}), 1)
        self.assertEqual({response[0] for response in jobs}, {200, 202})


if __name__ == "__main__":
    unittest.main()
