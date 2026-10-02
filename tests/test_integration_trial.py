"""Synthetic integration-trial isolation, revisions, links, and file receipts."""

import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
import zipfile
import bcrypt

from pypdf import PdfWriter
from orderflow.app import AppServer, Store
from orderflow.trial import MAX_DOCUMENTS_PER_SESSION


def fields(*, number="PO-1", quantity="10", currency="USD", unit="EA", code="P-1", description="Original", amount="20.00"):
    return {"header": {"company": "Synthetic Co", "number": number,
                       "date": "2026-10-02", "currency": currency},
            "rows": [{"id": str(uuid.uuid4()), "code": code, "description": description,
                      "quantity": quantity, "unit": unit, "unit_price": "2.00",
                      "amount": amount}]}


def document(kind="purchase_order", *, confirmed=None, source="manual", file=None, candidate=None):
    return {"request_key": str(uuid.uuid4()), "kind": kind, "source": source,
            "candidate": candidate, "confirmed": confirmed or fields(), "file": file}


class TrialBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.owner, _, _ = self.store.authenticate(None)
        self.other, _, _ = self.store.authenticate(None)
        self.trial = self.store.trial

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolation_idempotency_revision_and_reopen(self):
        original = fields()
        checked = json.loads(json.dumps(original))
        checked["rows"][0]["description"] = "Human confirmed"
        value = document(candidate=original, confirmed=checked)
        saved, created = self.trial.add_document(self.owner, value)
        self.assertTrue(created)
        self.assertEqual(saved["candidate"]["rows"][0]["description"], "Original")
        self.assertEqual(saved["confirmed"]["rows"][0]["description"], "Human confirmed")
        replay, created = self.trial.add_document(self.owner, value)
        self.assertFalse(created)
        self.assertEqual(replay["id"], saved["id"])
        changed = {**value, "confirmed": original}
        with self.assertRaisesRegex(ValueError, "TRIAL_IDEMPOTENCY_CONFLICT"):
            self.trial.add_document(self.owner, changed)
        self.assertEqual(self.trial.documents(self.other), [])
        self.assertIsNone(self.trial.document(self.other, saved["id"]))
        self.assertEqual(self.store.documents(self.owner, "management"), [])
        self.assertEqual(self.store.documents(self.owner, "diagnostic"), [])
        edited = json.loads(json.dumps(checked))
        edited["header"]["number"] = "PO-2"
        updated = self.trial.update_document(self.owner, saved["id"],
                                             {"revision": 1, "confirmed": edited})
        self.assertEqual(updated["revision"], 2)
        with self.assertRaisesRegex(ValueError, "TRIAL_VERSION_CONFLICT"):
            self.trial.update_document(self.owner, saved["id"],
                                       {"revision": 1, "confirmed": original})
        self.assertEqual(Store(Path(self.tmp.name)).trial.document(self.owner, saved["id"])["revision"], 2)

    def test_allocations_do_not_double_count_and_can_be_revoked(self):
        orders = []
        for number in ("PO-A", "PO-B"):
            saved, _ = self.trial.add_document(self.owner, document(confirmed=fields(number=number)))
            orders.append(saved)
        invoice, _ = self.trial.add_document(self.owner, document("invoice", confirmed=fields(number="INV-1", quantity="6", amount="12.00")))
        item = invoice["confirmed"]["rows"][0]["id"]
        def link(order, qty):
            return {"invoice_document_id": invoice["id"], "invoice_item_id": item,
                    "po_document_id": order["id"], "po_item_id": order["confirmed"]["rows"][0]["id"],
                    "quantity": qty}
        first, created = self.trial.add_link(self.owner, link(orders[0], "4"))
        self.assertTrue(created)
        with self.assertRaisesRegex(ValueError, "TRIAL_ALLOCATION_CONFLICT"):
            self.trial.add_link(self.owner, link(orders[1], "3"))
        second, _ = self.trial.add_link(self.owner, link(orders[1], "2"))
        self.assertEqual(sum(int(row["quantity"]) for row in self.trial.links(self.owner)), 6)
        self.assertEqual(self.trial.links(self.other), [])
        with self.assertRaisesRegex(LookupError, "TRIAL_LINK_NOT_FOUND"):
            self.trial.revoke_link(self.other, first["id"])
        self.trial.revoke_link(self.owner, first["id"])
        self.assertEqual(len(self.trial.links(self.owner)), 1)
        third, _ = self.trial.add_link(self.owner, link(orders[0], "4"))
        self.assertNotEqual(third["id"], first["id"])
        edited = json.loads(json.dumps(invoice["confirmed"]))
        edited["rows"][0]["quantity"] = "5"
        with self.assertRaisesRegex(ValueError, "TRIAL_ALLOCATION_CONFLICT"):
            self.trial.update_document(self.owner, invoice["id"],
                                       {"revision": 1, "confirmed": edited})
        self.assertEqual(second["quantity"], "2")

    def test_original_files_and_safe_csv(self):
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(pdf)
        raw = pdf.getvalue()
        file = {"base64": base64.b64encode(raw).decode(),
                "sha256": hashlib.sha256(raw).hexdigest()}
        original = fields(description="=HYPERLINK(1)")
        saved, _ = self.trial.add_document(self.owner, document(source="pdf", file=file,
                                                                candidate=original, confirmed=original))
        self.assertEqual(self.trial.file(self.owner, saved["id"])[0], raw)
        self.assertIsNone(self.trial.file(self.other, saved["id"]))
        csv_data = self.trial.csv_bytes(self.trial.documents(self.owner)).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1)", csv_data)
        with self.assertRaisesRegex(ValueError, "TRIAL_FILE_INVALID"):
            self.trial.add_document(self.owner, document(source="pdf", file={**file, "sha256": "0" * 64}))

    def test_xlsx_scope_search_totals_and_aliases(self):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        file = {"base64": base64.b64encode(raw.getvalue()).decode(),
                "sha256": hashlib.sha256(raw.getvalue()).hexdigest()}
        order, _ = self.trial.add_document(self.owner, document(source="xlsx", file=file,
                                                               confirmed=fields(currency="USD")))
        self.trial.add_document(self.owner, document("invoice", confirmed=fields(number="INV-X", currency="EUR", amount="3.00")))
        self.assertEqual(len(self.trial.search(self.owner, {"number": "PO-1"})), 1)
        self.assertEqual(self.trial.totals(self.trial.documents(self.owner)), {
            "purchase_order": {"USD": "20.00"}, "invoice": {"EUR": "3.00"}})
        product = self.trial.add_product(self.owner, {"label": "Test component", "unit": "EA", "aliases": [
            {"company": "Synthetic Co", "code": "P-1"},
            {"company": "Other Co", "code": "Q-9"}]})
        self.assertEqual(len(product["aliases"]), 2)
        self.assertEqual(product["unit"], "EA")
        self.assertEqual(self.trial.products(self.other), [])
        self.assertEqual(self.trial.document(self.owner, order["id"])["confirmed"]["rows"][0]["description"], "Original")
        with self.assertRaisesRegex(ValueError, "TRIAL_ALIAS_CONFLICT"):
            self.trial.add_product(self.owner, {"label": "Different", "unit": "", "aliases": [
                {"company": "Synthetic Co", "code": "P-1"}]})

    def test_pdf_timeout_is_bounded_and_storage_is_limited(self):
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(pdf)
        raw = pdf.getvalue()
        file = {"base64": base64.b64encode(raw).decode(),
                "sha256": hashlib.sha256(raw).hexdigest()}
        from orderflow.app import PDFCheckTimeout
        self.trial.pdf_check = lambda _: (_ for _ in ()).throw(PDFCheckTimeout())
        with self.assertRaises(PDFCheckTimeout):
            self.trial.add_document(self.owner, document(source="pdf", file=file))
        self.assertEqual(self.trial.documents(self.owner), [])
        self.assertEqual(list(self.trial.files.iterdir()), [])
        self.trial.pdf_check = lambda _: 1
        for index in range(MAX_DOCUMENTS_PER_SESSION):
            self.trial.add_document(self.owner, document(confirmed=fields(number=f"PO-{index}")))
        with self.assertRaisesRegex(ValueError, "TRIAL_STORAGE_LIMIT"):
            self.trial.add_document(self.owner, document())
        self.assertEqual(len(self.trial.documents(self.owner)), MAX_DOCUMENTS_PER_SESSION)

    def test_product_revision_many_aliases_and_document_values(self):
        saved_doc, _ = self.trial.add_document(self.owner, document(confirmed=fields(
            description="Original source description", amount="12.00")))
        original = self.trial.add_product(self.owner, {"label": "Original suggestion", "unit": "EA",
            "aliases": [{"company": "Synthetic Co", "code": "P-1"}]})
        updated = self.trial.update_product(self.owner, original["id"], {
            "revision": 1, "label": "Corrected suggestion", "unit": "BOX",
            "aliases": [{"company": f"Company {index}", "code": f"PART-{index}"}
                        for index in range(3)]})
        self.assertEqual((updated["revision"], len(updated["aliases"])), (2, 3))
        self.assertEqual(self.trial.products(self.owner)[0]["unit"], "BOX")
        with self.assertRaisesRegex(ValueError, "TRIAL_PRODUCT_VERSION_CONFLICT"):
            self.trial.update_product(self.owner, original["id"], {"revision": 1,
                "label": "Stale", "unit": "EA", "aliases": original["aliases"]})
        with self.assertRaisesRegex(LookupError, "TRIAL_PRODUCT_NOT_FOUND"):
            self.trial.update_product(self.other, original["id"], {"revision": 2,
                "label": "Foreign", "unit": "EA", "aliases": original["aliases"]})
        document_after = self.trial.document(self.owner, saved_doc["id"])
        self.assertEqual(document_after["confirmed"]["rows"][0]["description"],
                         "Original source description")
        self.assertEqual(document_after["confirmed"]["rows"][0]["unit_price"], "2.00")
        self.assertEqual(document_after["confirmed"]["rows"][0]["amount"], "12.00")


class TrialApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        auth_hash = bcrypt.hashpw(b"test-password", bcrypt.gensalt(rounds=4))
        self.server = AppServer(("127.0.0.1", 0), self.store, None, auth_hash)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, value=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if method in {"POST", "PUT", "DELETE"}:
            headers["X-Orderflow-Request"] = "1"
        if value is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=json.dumps(value) if value is not None else None, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        code = response.status
        issued = response.getheader("Set-Cookie")
        content_type = response.getheader("Content-Type", "")
        connection.close()
        return code, json.loads(payload) if content_type.startswith("application/json") else payload, issued

    def login(self):
        code, _, issued = self.request("POST", "/orderflow/api/login", {"password": "test-password"})
        self.assertEqual(code, 200)
        return issued.split(";", 1)[0]

    def test_session_isolation_and_existing_api_boundary(self):
        code, _, _ = self.request("GET", "/orderflow/integration/")
        self.assertEqual(code, 200)
        code, _, _ = self.request("GET", "/orderflow/integration-xlsx.mjs")
        self.assertEqual(code, 200)
        code, _, _ = self.request("GET", "/orderflow/integration-comparison.mjs")
        self.assertEqual(code, 200)
        code, _, _ = self.request("GET", "/orderflow/integration-duplicates.mjs")
        self.assertEqual(code, 200)
        code, _, _ = self.request("GET", "/orderflow/integration-activity.mjs")
        self.assertEqual(code, 200)
        code, result, _ = self.request("GET", "/orderflow/api/integration/bootstrap")
        self.assertEqual((code, result["error_code"]), (401, "AUTH_REQUIRED"))
        owner, other = self.login(), self.login()
        code, saved, _ = self.request("POST", "/orderflow/api/integration/documents",
                                      document(confirmed=fields()), owner)
        self.assertEqual(code, 201)
        code, mine, _ = self.request("GET", "/orderflow/api/integration/bootstrap", cookie=owner)
        self.assertEqual([item["id"] for item in mine["documents"]], [saved["id"]])
        code, yours, _ = self.request("GET", "/orderflow/api/integration/bootstrap", cookie=other)
        self.assertEqual((code, yours["documents"]), (200, []))
        code, _, _ = self.request("GET", "/orderflow/api/integration/documents/" + saved["id"], cookie=other)
        self.assertEqual(code, 404)
        for path in ("/orderflow/api/bootstrap", "/orderflow/api/management/bootstrap"):
            code, existing, _ = self.request("GET", path, cookie=owner)
            self.assertEqual((code, existing["documents"]), (200, []))

    def test_search_unknown_currency_and_missing_amount(self):
        owner = self.login()
        for index in range(2):
            value = fields(number=f"PO-{index}", currency="", amount="20.00" if index else None)
            code, _, _ = self.request("POST", "/orderflow/api/integration/documents",
                                      document(confirmed=value), owner)
            self.assertEqual(code, 201)
        code, result, _ = self.request("GET", "/orderflow/api/integration/search", cookie=owner)
        self.assertEqual(code, 200)
        self.assertEqual(result["totals_by_currency"], {"purchase_order": {}, "invoice": {}})
        self.assertEqual(result["missing_currency_documents"], 2)
        self.assertEqual(result["missing_amount_documents"], 1)

    def test_product_put_revision_conflict_and_session(self):
        owner, other = self.login(), self.login()
        code, created, _ = self.request("POST", "/orderflow/api/integration/products",
                                        {"label": "Old", "unit": "EA", "aliases": [
                                            {"company": "Synthetic Co", "code": "P-1"}]}, owner)
        self.assertEqual(code, 201)
        path = "/orderflow/api/integration/products/" + created["id"]
        value = {"revision": 1, "label": "New", "unit": "BOX", "aliases": [
            {"company": "Synthetic Co", "code": "P-1"},
            {"company": "Other Co", "code": "Q-2"}]}
        code, saved, _ = self.request("PUT", path, value, owner)
        self.assertEqual((code, saved["revision"], len(saved["aliases"])), (200, 2, 2))
        code, stale, _ = self.request("PUT", path, {**value, "label": "Stale"}, owner)
        self.assertEqual((code, stale["error_code"]), (409, "TRIAL_PRODUCT_VERSION_CONFLICT"))
        code, foreign, _ = self.request("PUT", path, {**value, "revision": 2}, other)
        self.assertEqual((code, foreign["error_code"]), (404, "TRIAL_PRODUCT_NOT_FOUND"))


if __name__ == "__main__":
    unittest.main()
