"""Local parser provenance is separate from Gemini and scoped to a session."""

import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid

from orderflow.app import Store


def candidate():
    return {"id": str(uuid.uuid4()), "orderNo": "20260923001", "invoiceNo": None,
            "client": "科雅先端股份有限公司", "product": "Synthetic part", "code": "ABC-001",
            "qty": "2", "unitPrice": "5.00", "amount": "10.00", "currency": "USD",
            "date": "2026-09-23", "incoterms": None, "unit": None, "status": "確認中",
            "linked_order_row_id": None, "deleted": False}


class LocalSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.owner, _, _ = self.store.authenticate(None)
        self.other, _, _ = self.store.authenticate(None)

    def tearDown(self):
        self.tmp.cleanup()

    def document(self, kind="purchase_order", purpose="management", data=b"synthetic-pdf"):
        return self.store.add_document(self.owner, str(uuid.uuid4()), data,
                                       hashlib.sha256(data).hexdigest(), 1, 1, purpose, kind)

    def test_candidate_replay_scope_revision_and_source_conflict(self):
        document = self.document()
        row = candidate()
        key = str(uuid.uuid4())
        source, created = self.store.add_local_source(self.owner, document["id"], key,
                                                       "koya-purchase-v1", [row])
        self.assertTrue(created)
        replay, created = self.store.add_local_source(self.owner, document["id"], key,
                                                       "koya-purchase-v1", [row])
        self.assertFalse(created)
        self.assertEqual(replay["id"], source["id"])
        with self.assertRaisesRegex(ValueError, "LOCAL_SOURCE_CONFLICT"):
            self.store.add_local_source(self.owner, document["id"], key,
                                        "koya-purchase-v1", [candidate()])
        with self.assertRaisesRegex(LookupError, "LOCAL_DOCUMENT_NOT_FOUND"):
            self.store.add_local_source(self.other, document["id"], str(uuid.uuid4()),
                                        "koya-purchase-v1", [row])
        saved, created = self.store.save_record_set(self.owner, document["id"], None, 0, [row],
                                                     source_local_id=source["id"])
        self.assertTrue(created)
        self.assertIsNone(saved["source_job_id"])
        self.assertEqual(saved["source_local_id"], source["id"])
        replay, created = self.store.save_record_set(self.owner, document["id"], None, 0, [row],
                                                      source_local_id=source["id"])
        self.assertFalse(created)
        self.assertEqual(replay["revision"], 1)
        edited = {**row, "product": "Human checked part"}
        updated, _ = self.store.save_record_set(self.owner, document["id"], None, 1, [edited],
                                                 source_local_id=source["id"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(self.store.local_sources(self.owner)[0]["candidate_rows"][0]["product"],
                         "Synthetic part")
        self.assertEqual(self.store.record_set(self.owner, document["id"])["rows"][0]["product"],
                         "Human checked part")
        job, _ = self.store.add_job(self.owner, document["id"], str(uuid.uuid4()), "real", "management")
        self.store.set_job(job["id"], state="done", result=[])
        with self.assertRaisesRegex(ValueError, "RECORD_SOURCE_CONFLICT"):
            self.store.save_record_set(self.owner, document["id"], job["id"], 2, [edited])
        self.assertEqual(self.store.local_sources(self.other), [])
        self.assertEqual(self.store.record_sets(self.other), [])
        self.assertEqual(Store(Path(self.tmp.name)).record_set(self.owner, document["id"])["revision"], 2)

    def test_ai_record_prevents_orphan_local_source(self):
        document = self.document(data=b"ai-saved")
        row = candidate()
        job, _ = self.store.add_job(self.owner, document["id"], str(uuid.uuid4()), "real", "management")
        self.store.set_job(job["id"], state="done", result=[])
        self.store.save_record_set(self.owner, document["id"], job["id"], 0, [row])
        with self.assertRaisesRegex(ValueError, "LOCAL_SOURCE_CONFLICT"):
            self.store.add_local_source(self.owner, document["id"], str(uuid.uuid4()),
                                        "koya-purchase-v1", [candidate()])
        self.assertEqual(self.store.local_sources(self.owner), [])

    def test_reject_other_kind_purpose_parser_and_missing_page(self):
        row = candidate()
        for kind, purpose in [("invoice", "management"), (None, "diagnostic")]:
            document = self.document(kind, purpose, data=str(uuid.uuid4()).encode())
            with self.assertRaisesRegex(LookupError, "LOCAL_DOCUMENT_NOT_FOUND"):
                self.store.add_local_source(self.owner, document["id"], str(uuid.uuid4()),
                                            "koya-purchase-v1", [row])
        document = self.document(data=b"other")
        with self.assertRaisesRegex(ValueError, "LOCAL_SOURCE_INVALID"):
            self.store.add_local_source(self.owner, document["id"], str(uuid.uuid4()),
                                        "other-parser", [row])

    def test_existing_record_set_migration_preserves_gemini_source(self):
        document = self.document()
        row = candidate()
        job, _ = self.store.add_job(self.owner, document["id"], str(uuid.uuid4()), "real", "management")
        self.store.set_job(job["id"], state="done", result=[])
        saved, _ = self.store.save_record_set(self.owner, document["id"], job["id"], 0, [row])
        with self.store.db() as db:
            db.execute("ALTER TABLE management_record_sets RENAME TO management_record_sets_new")
            db.execute("""CREATE TABLE management_record_sets (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                document_id TEXT NOT NULL UNIQUE REFERENCES documents(id),
                source_job_id TEXT NOT NULL REFERENCES jobs(id),
                kind TEXT NOT NULL, rows_json TEXT NOT NULL,
                revision INTEGER NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL)""")
            db.execute("""INSERT INTO management_record_sets
                SELECT id,session_id,document_id,source_job_id,kind,rows_json,revision,created_ms,updated_ms
                FROM management_record_sets_new""")
            db.execute("DROP TABLE management_record_sets_new")
        reopened = Store(Path(self.tmp.name))
        restored = reopened.record_set(self.owner, document["id"])
        self.assertEqual(restored["id"], saved["id"])
        self.assertEqual(restored["source_job_id"], job["id"])
        self.assertIsNone(restored["source_local_id"])
        self.assertEqual(restored["rows"], [row])


if __name__ == "__main__":
    unittest.main()
