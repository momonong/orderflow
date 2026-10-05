"""Cross-session sharing remains explicit and keeps private document ownership."""

import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import bcrypt
from pypdf import PdfWriter

from orderflow.app import AppServer, Store


writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
buffer = io.BytesIO()
writer.write(buffer)
PDF = buffer.getvalue()
SHA = hashlib.sha256(PDF).hexdigest()
ORIGIN = "https://momonong.me"


class SharedDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.server = AppServer(("127.0.0.1", 0), self.store, ORIGIN,
                                bcrypt.hashpw(b"test-password", bcrypt.gensalt(rounds=4)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.owner, self.owner_token, _ = self.store.authenticate(None)
        self.taker, self.taker_token, _ = self.store.authenticate(None)
        self.other, self.other_token, _ = self.store.authenticate(None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, token=None, value=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        headers = {"Host": "momonong.me"}
        if token:
            headers["Cookie"] = "of_session=" + token
        body = None
        if method in {"POST", "DELETE"}:
            headers.update({"Origin": ORIGIN, "X-Orderflow-Request": "1"})
        if value is not None:
            body = json.dumps(value)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        raw = response.read()
        result = (response.status, json.loads(raw) if response.getheader(
            "Content-Type", "").startswith("application/json") else raw)
        conn.close()
        return result

    def test_opt_in_private_claim_revoke_and_no_reshare(self):
        private = self.store.add_document(self.owner, str(uuid.uuid4()), PDF, SHA, 1, 1)
        base = "/orderflow/api/shared-documents"
        self.assertEqual(self.call("GET", base, self.taker_token)[1]["documents"], [])
        self.assertEqual(self.call("GET", base)[0], 401)
        payload = {"source_purpose": "diagnostic", "document_id": private["id"],
                   "title": "合成範例"}
        self.assertEqual(self.call("POST", base, self.taker_token, payload)[0], 404)
        self.assertEqual(self.call("POST", base, self.owner_token,
                                   {**payload, "source_purpose": []})[0], 400)
        code, share = self.call("POST", base, self.owner_token, payload)
        self.assertEqual(code, 201)
        self.assertEqual(self.call("POST", base, self.owner_token, payload)[0], 200)
        listing = self.call("GET", base, self.taker_token)[1]["documents"]
        self.assertEqual(len(listing), 1)
        self.assertFalse(listing[0]["can_revoke"])
        self.assertNotIn("owner_session_id", listing[0])
        self.assertNotIn("sha256", listing[0])
        self.assertEqual(self.call("GET", f"{base}/{share['id']}/file", self.taker_token),
                         (200, PDF))
        claim = {"purpose": "diagnostic", "document_kind": None}
        code, copy = self.call("POST", f"{base}/{share['id']}/claim", self.taker_token, claim)
        self.assertEqual(code, 201)
        self.assertEqual(copy["source_type"], "shared")
        self.assertEqual(copy["steps"]["upload"], "not_run")
        self.assertEqual(self.call("POST", f"{base}/{share['id']}/claim", self.taker_token,
                                   claim)[0], 200)
        self.assertEqual(self.call("POST", base, self.taker_token,
                                   {**payload, "document_id": copy["id"]})[0], 404)
        self.assertEqual(self.store.documents(self.owner)[0]["id"], private["id"])
        self.assertEqual(self.store.documents(self.taker)[0]["id"], copy["id"])
        self.assertEqual(self.store.documents(self.other), [])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.call("DELETE", f"{base}/{share['id']}", self.taker_token)[0], 404)
        self.assertEqual(self.call("DELETE", f"{base}/{share['id']}", self.owner_token)[0], 200)
        self.assertEqual(self.call("GET", base, self.taker_token)[1]["documents"], [])
        self.assertEqual(self.call("GET", f"{base}/{share['id']}/file", self.taker_token)[0], 404)
        self.assertEqual(self.call("POST", f"{base}/{share['id']}/claim", self.other_token,
                                   claim)[0], 404)
        self.assertEqual(self.call("POST", f"{base}/{share['id']}/claim", self.taker_token,
                                   claim)[1]["id"], copy["id"])
        self.assertEqual((self.store.files / f"{copy['id']}.pdf").read_bytes(), PDF)

    def test_public_help_serves_only_the_guide(self):
        source = Path(__file__).resolve().parents[1] / "docs/user-guide/index.html"
        self.assertEqual(self.call("GET", "/orderflow/help/")[0:2], (200, source.read_bytes()))
        for route in ("/orderflow/", "/orderflow/test/", "/orderflow/integration/"):
            status, page = self.call("GET", route)
            self.assertEqual(status, 200)
            self.assertIn(b'/orderflow/help/', page)
        self.assertEqual(self.call("GET", "/orderflow/help")[0], 308)
        self.assertEqual(self.call("GET", "/orderflow/help/other.html")[0], 404)
        self.assertEqual(self.call("GET", "/orderflow/docs/user-guide/index.html")[0], 404)

    def test_management_kind_and_file_integrity(self):
        doc = self.store.add_document(self.owner, str(uuid.uuid4()), PDF, SHA, 1, 1,
                                      "management", "invoice")
        base = "/orderflow/api/shared-documents"
        share = self.call("POST", base, self.owner_token,
                          {"source_purpose": "management", "document_id": doc["id"],
                           "title": "範例發票"})[1]
        url = f"{base}/{share['id']}/claim"
        self.assertEqual(self.call("POST", url, self.taker_token,
                                   {"purpose": "management", "document_kind": "purchase_order"})[0], 409)
        code, copy = self.call("POST", url, self.taker_token,
                               {"purpose": "management", "document_kind": "invoice"})
        self.assertEqual((code, copy["document_kind"], copy["source_type"]),
                         (201, "invoice", "shared"))
        self.assertEqual(self.store.documents(self.taker, "management")[0]["id"], copy["id"])
        (self.store.files / f"{doc['id']}.pdf").write_bytes(b"damaged")
        self.assertEqual(self.call("GET", f"{base}/{share['id']}/file", self.other_token)[0], 409)
        self.assertEqual(self.call("POST", url, self.other_token,
                                   {"purpose": "management", "document_kind": "invoice"})[0], 409)

    def test_claim_obeys_public_storage_quota(self):
        doc = self.store.add_document(self.owner, str(uuid.uuid4()), PDF, SHA, 1, 1)
        base = "/orderflow/api/shared-documents"
        share = self.call("POST", base, self.owner_token,
                          {"source_purpose": "diagnostic", "document_id": doc["id"],
                           "title": "配額測試"})[1]
        self.store.shared.max_documents_per_session = 0
        code, result = self.call("POST", f"{base}/{share['id']}/claim", self.taker_token,
                                 {"purpose": "diagnostic", "document_kind": None})
        self.assertEqual((code, result["error_code"]), (507, "STORAGE_LIMIT"))
        self.assertEqual(self.store.documents(self.taker), [])


if __name__ == "__main__":
    unittest.main()
