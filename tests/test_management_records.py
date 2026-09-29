"""Confirmed document data stays scoped, durable and safe under retries."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid

from orderflow.app import Store
from orderflow.records import RecordRowsError, canonical_record_date, validate_rows


def row(kind, **changes):
    value = dict(id=str(uuid.uuid4()), orderNo="PO-001" if kind == "purchase_order" else None,
                 invoiceNo="INV-001" if kind == "invoice" else None, client="Buyer A",
                 product=" Part // [β]  _ ", code="A-1", qty="10.50", unitPrice="3.25",
                 amount="34.1250", currency="USD", date="2026-09-29", incoterms="DAP",
                 unit="PCS", status="確認中" if kind == "purchase_order" else None,
                 linked_order_row_id=None, deleted=False)
    value.update(changes)
    return value


class RecordStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.owner, _, _ = self.store.authenticate(None)
        self.other, _, _ = self.store.authenticate(None)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, kind, data=b"synthetic-pdf-content"):
        document = self.store.add_document(self.owner, str(uuid.uuid4()), data,
                                           hashlib.sha256(data).hexdigest(), 1, 1,
                                           "management", kind)
        job, _ = self.store.add_job(self.owner, document["id"], str(uuid.uuid4()),
                                    "real", "management")
        self.store.set_job(job["id"], state="done", result=[{"product": "suggestion"}])
        return document, job

    def test_duplicate_kind_and_session_scope(self):
        purchase, _ = self.source("purchase_order")
        same = self.store.add_document(self.owner, str(uuid.uuid4()), b"synthetic-pdf-content",
                                       purchase["sha256"], 1, 1, "management", "purchase_order")
        self.assertEqual(same["id"], purchase["id"])
        self.assertTrue(same["duplicate"])
        alias_key = str(uuid.uuid4())
        aliased = self.store.add_document(self.owner, alias_key, b"synthetic-pdf-content",
                                          purchase["sha256"], 1, 1, "management", "purchase_order")
        self.assertEqual(aliased["id"], purchase["id"])
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            self.store.add_document(self.owner, alias_key, b"different-pdf",
                                    hashlib.sha256(b"different-pdf").hexdigest(), 1, 1,
                                    "management", "purchase_order")
        invoice, _ = self.source("invoice")
        self.assertNotEqual(invoice["id"], purchase["id"])
        self.assertTrue(invoice["same_pdf_other_kind"])
        separate = self.store.add_document(self.other, str(uuid.uuid4()), b"synthetic-pdf-content",
                                           purchase["sha256"], 1, 1, "management", "purchase_order")
        self.assertNotEqual(separate["id"], purchase["id"])
        old = self.store.add_document(self.owner, str(uuid.uuid4()), b"old-draft",
                                      hashlib.sha256(b"old-draft").hexdigest(), 1, 1, "management")
        self.assertIsNone(self.store.document_info(self.owner, old["id"], "management")["document_kind"])
        self.assertEqual(len(self.store.documents(self.owner, "management")), 3)
        self.assertIsNone(self.store.document_info(self.other, purchase["id"], "management"))

    def test_save_conflict_link_deletion_and_restart(self):
        purchase, purchase_job = self.source("purchase_order", b"order")
        order = row("purchase_order")
        saved, created = self.store.save_record_set(self.owner, purchase["id"],
                                                     purchase_job["id"], 0, [order])
        self.assertTrue(created)
        self.assertEqual(saved["rows"][0]["product"], " Part // [β]  _ ")
        replay, created = self.store.save_record_set(self.owner, purchase["id"],
                                                      purchase_job["id"], 0, [order])
        self.assertFalse(created)
        self.assertEqual(replay["revision"], 1)
        candidates = []
        for amount in ["35.00", "36.00"]:
            changed = deepcopy(order)
            changed["amount"] = amount
            candidates.append(changed)
        def concurrent(candidate):
            try:
                return self.store.save_record_set(self.owner, purchase["id"],
                                                  purchase_job["id"], 1, [candidate])[0]["revision"]
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(concurrent, candidates))
        self.assertCountEqual(results, [2, "RECORD_VERSION_CONFLICT"])
        invoice, invoice_job = self.source("invoice", b"invoice")
        with self.assertRaisesRegex(ValueError, "RECORD_ROW_ID_CONFLICT"):
            self.store.save_record_set(self.owner, invoice["id"], invoice_job["id"], 0,
                                       [row("invoice", id=order["id"])])
        item = row("invoice", qty="3", linked_order_row_id=order["id"])
        invoice_set, _ = self.store.save_record_set(self.owner, invoice["id"],
                                                    invoice_job["id"], 0, [item])
        self.assertEqual(invoice_set["rows"][0]["linked_order_row_id"], order["id"])
        changed_order = deepcopy(self.store.record_set(self.owner, purchase["id"])["rows"][0])
        changed_order["deleted"] = True
        with self.assertRaisesRegex(ValueError, "RECORD_LINKED_ROW"):
            self.store.save_record_set(self.owner, purchase["id"], purchase_job["id"], 2, [changed_order])
        changed_invoice = deepcopy(item)
        changed_invoice["client"] = "Different buyer"
        with self.assertRaisesRegex(ValueError, "RECORD_LINK_CONFLICT"):
            self.store.save_record_set(self.owner, invoice["id"], invoice_job["id"], 1, [changed_invoice])
        unlinked = deepcopy(item)
        unlinked["linked_order_row_id"] = None
        self.store.save_record_set(self.owner, invoice["id"], invoice_job["id"], 1, [unlinked])
        self.store.save_record_set(self.owner, purchase["id"], purchase_job["id"], 2, [changed_order])
        reopened = Store(Path(self.tmp.name))
        self.assertTrue(reopened.record_set(self.owner, purchase["id"])["rows"][0]["deleted"])
        self.assertEqual(reopened.record_set(self.owner, invoice["id"])["rows"][0]["linked_order_row_id"], None)
        self.assertEqual(reopened.job(self.owner, purchase_job["id"], "management")["result"],
                         [{"product": "suggestion"}])
        self.assertIsNone(reopened.record_set(self.other, purchase["id"]))
        self.assertEqual(reopened.record_sets(self.other), [])

    def test_date_normalization_and_bounded_field_errors(self):
        for raw, expected in (("2026/09/22", "2026-09-22"), ("2026/9/2", "2026-09-02"),
                              ("2024/2/29", "2024-02-29"), ("2026-09-22", "2026-09-22")):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_record_date(raw), expected)
                for kind in ("purchase_order", "invoice"):
                    self.assertEqual(validate_rows([row(kind, date=raw)], kind)[0]["date"], expected)
        self.assertIsNone(validate_rows([row("invoice", date=None)], "invoice")[0]["date"])
        for raw, code in (("09/10/2026", "DATE_FORMAT"), ("2026-9-22", "DATE_FORMAT"),
                          ("2026/2/29", "DATE_INVALID"), ("2026/02/30", "DATE_INVALID"),
                          ("2024/13/1", "DATE_INVALID"), ("0000/1/1", "DATE_INVALID")):
            with self.subTest(raw=raw), self.assertRaises(RecordRowsError) as caught:
                validate_rows([row("invoice", date=raw)], "invoice")
            self.assertEqual(caught.exception.errors[0]["field"], "date")
            self.assertEqual(caught.exception.errors[0]["code"], code)
        bad = [row("purchase_order", date="2026/2/30", qty="0", currency="usd",
                   product=None, code=None) for _ in range(6)]
        with self.assertRaises(RecordRowsError) as caught:
            validate_rows(bad, "purchase_order")
        self.assertEqual(str(caught.exception), "RECORD_ROWS_INVALID")
        self.assertEqual(len(caught.exception.errors), 20)
        self.assertEqual({issue["field"] for issue in caught.exception.errors[:4]},
                         {"date", "qty", "currency", "product"})
        self.assertEqual(caught.exception.errors[0]["index"], 1)
        self.assertEqual(caught.exception.errors[0]["row_id"], bad[0]["id"])
        self.assertNotIn("2026/2/30", str(caught.exception.errors))
        with self.assertRaises(RecordRowsError) as malformed:
            validate_rows([row("purchase_order", status=[])], "purchase_order")
        self.assertEqual(malformed.exception.errors[0]["code"], "STATUS_INVALID")

    def test_slash_date_save_restart_preserves_ai_source(self):
        document, job = self.source("invoice", b"synthetic-invoice")
        self.store.set_job(job["id"], result=[{"product": "suggestion", "date": "2026/9/22"}])
        original_result = self.store.job(self.owner, job["id"], "management")["result"]
        original = row("invoice", date="2026/9/22")
        saved, _ = self.store.save_record_set(self.owner, document["id"], job["id"], 0,
                                              [original])
        self.assertEqual(saved["rows"][0]["date"], "2026-09-22")
        replay, created = self.store.save_record_set(self.owner, document["id"], job["id"],
                                                     0, [original])
        self.assertFalse(created)
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(original["date"], "2026/9/22")
        reopened = Store(Path(self.tmp.name))
        self.assertEqual(reopened.record_set(self.owner, document["id"])["rows"][0]["date"],
                         "2026-09-22")
        self.assertEqual(reopened.job(self.owner, job["id"], "management")["result"],
                         original_result)

    def test_source_kind_and_values_are_checked(self):
        purchase, job = self.source("purchase_order", b"kind")
        with self.assertRaisesRegex(LookupError, "DRAFT_SOURCE_NOT_FOUND"):
            self.store.save_management_draft(self.owner, job["id"],
                                             [{"description": "legacy", "quantity": 1}], 0)
        wrong = row("invoice")
        with self.assertRaisesRegex(ValueError, "RECORD_ROWS_INVALID"):
            self.store.save_record_set(self.owner, purchase["id"], job["id"], 0, [wrong])
        with self.assertRaisesRegex(LookupError, "RECORD_SOURCE_NOT_FOUND"):
            self.store.save_record_set(self.other, purchase["id"], job["id"], 0,
                                       [row("purchase_order")])
        for changes in ({"date": "2026-02-30"}, {"qty": "-1"}, {"currency": ""},
                        {"qty": "1,000"}, {"product": None, "code": None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "RECORD_ROWS_INVALID"):
                validate_rows([row("purchase_order", **changes)], "purchase_order")
        self.assertEqual(validate_rows([row("purchase_order", client=None, amount=None)],
                                       "purchase_order")[0]["amount"], None)


if __name__ == "__main__":
    unittest.main()
