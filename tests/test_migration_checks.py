"""Migration checks must preserve real Store relations and PDF bytes."""
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import uuid

from deploy.migration_checks import inspect_state
from orderflow.app import Store

PDF = b"%PDF-1.4\nsynthetic migration checksum fixture\n%%EOF\n"


class MigrationChecksTests(unittest.TestCase):
    def test_legacy_anonymous_session_survives_copy_and_pending_job_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            old = Store(source)
            owner, token, _ = old.authenticate(None)
            doc = old.add_document(owner, str(uuid.uuid4()), PDF,
                                   hashlib.sha256(PDF).hexdigest(), 1, 1)
            job, _ = old.add_job(owner, doc["id"], str(uuid.uuid4()), "success")
            with old.db() as db:
                db.execute("DROP TABLE session_auth")  # v0.2 had no login table.
            snapshot = Path(root) / "snapshot"
            (snapshot / "files").mkdir(parents=True)
            shutil.copy2(source / "files" / f"{doc['id']}.pdf", snapshot / "files")
            with sqlite3.connect(source / "orderflow.sqlite3") as before, \
                 sqlite3.connect(snapshot / "orderflow.sqlite3") as after:
                before.backup(after)
            self.assertEqual(inspect_state(snapshot)["pending_jobs"], 1)
            migrated = Store(snapshot)
            self.assertEqual(migrated.session_status(token), (owner, "AUTH_REQUIRED"))
            self.assertEqual(migrated.job(owner, job["id"])["state"], "unknown")
            restored_owner, rotated, _ = migrated.authenticate(token)
            self.assertEqual(restored_owner, owner)
            self.assertNotEqual(rotated, token)
            self.assertEqual(migrated.session_status(token)[1], "AUTH_REQUIRED")
            self.assertEqual(migrated.documents(owner)[0]["id"], doc["id"])
            self.assertEqual(migrated.jobs(owner)[0]["id"], job["id"])
            self.assertEqual(inspect_state(snapshot)["documents"], 1)

    def test_snapshot_preserves_rows_and_rejects_corrupt_pdf_or_job_owner(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            store = Store(source)
            owner, _, _ = store.authenticate(None)
            doc = store.add_document(owner, str(uuid.uuid4()), PDF,
                                     hashlib.sha256(PDF).hexdigest(), 1, 1)
            job, _ = store.add_job(owner, doc["id"], str(uuid.uuid4()), "success")
            snapshot = Path(root) / "snapshot"
            (snapshot / "files").mkdir(parents=True)
            shutil.copy2(source / "files" / f"{doc['id']}.pdf", snapshot / "files")
            with sqlite3.connect(source / "orderflow.sqlite3") as old, \
                 sqlite3.connect(snapshot / "orderflow.sqlite3") as new:
                old.backup(new)
            summary = inspect_state(snapshot)
            self.assertEqual((summary["sessions"], summary["documents"], summary["jobs"],
                              summary["pending_jobs"]), (1, 1, 1, 1))
            path = snapshot / "files" / f"{doc['id']}.pdf"
            path.write_bytes(PDF.replace(b"synthetic", b"incorrect"))
            with self.assertRaisesRegex(ValueError, "PDF"):
                inspect_state(snapshot)
            path.write_bytes(PDF)
            with sqlite3.connect(snapshot / "orderflow.sqlite3") as db:
                db.execute("UPDATE jobs SET session_id=? WHERE id=?", (str(uuid.uuid4()), job["id"]))
            with self.assertRaisesRegex(ValueError, "job owner"):
                inspect_state(snapshot)
