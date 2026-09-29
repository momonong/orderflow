"""Confirmed document data stays scoped, durable and safe under retries."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid

from orderflow.app import Store
from orderflow.records import validate_rows


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
