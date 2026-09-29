"""Offline checks for the next ASUS schema-upgrade gate; no host operations."""

import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from orderflow.app import Store

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/upgrade-asus-prototype-records.py"
spec = importlib.util.spec_from_file_location("prototype_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body
    def read(self):
        return self.body


class FakeConnection:
    responses = []
    def __init__(self, *args, **kwargs):
        pass
    def request(self, *args, **kwargs):
        pass
    def getresponse(self):
        return self.responses.pop(0)
    def close(self):
        pass


class PrototypeUpgradeChecks(unittest.TestCase):
    def test_old_and_new_management_health_markers(self):
        auth = b'{"error_code":"AUTH_REQUIRED"}'
        for records, html in [(False, b'<title>OrderFlow</title>manage.js'),
                              (True, b'<title>OrderFlow</title>manage.js page-invoices page-comparison')]:
            with self.subTest(records=records):
                FakeConnection.responses = [FakeResponse(200, html), FakeResponse(401, auth),
                                            FakeResponse(401, auth)]
                with patch.object(upgrade.http.client, "HTTPConnection", FakeConnection):
                    upgrade.wait_app(expect_records=records)
                self.assertEqual(FakeConnection.responses, [])

    def test_legacy_rows_survive_migration_and_typed_write_blocks_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            store = Store(state)
            owner, _, _ = store.authenticate(None)
            legacy = store.add_document(owner, str(uuid.uuid4()), b"old-management-pdf",
                                        hashlib.sha256(b"old-management-pdf").hexdigest(),
                                        1, 1, "management")
            job, _ = store.add_job(owner, legacy["id"], str(uuid.uuid4()), "real", "management")
            store.set_job(job["id"], state="done", result=[{"description": "old", "quantity": 2}])
            store.save_management_draft(owner, job["id"], [{"description": "old", "quantity": 2}], 0)
            with store.db() as db:
                db.execute("DROP TABLE management_record_sets")
                db.execute("DROP TABLE management_upload_keys")
                db.execute("ALTER TABLE documents DROP COLUMN document_kind")
            snapshot = state / "before.sqlite3"
            source = sqlite3.connect(state / "orderflow.sqlite3")
            backup = sqlite3.connect(snapshot)
            source.backup(backup)
            source.close(); backup.close()
            with patch.object(upgrade, "STATE", state):
                self.assertFalse(upgrade.typed_writes_exist())
                upgraded = Store(state)
                upgrade.validate_upgraded_state(snapshot)
                self.assertEqual(upgraded.management_draft(owner, job["id"])["rows"],
                                 [{"description": "old", "quantity": 2}])
                typed = upgraded.add_document(owner, str(uuid.uuid4()), b"new-purchase-order",
                                              hashlib.sha256(b"new-purchase-order").hexdigest(),
                                              1, 1, "management", "purchase_order")
                self.assertEqual(typed["document_kind"], "purchase_order")
                self.assertTrue(upgrade.typed_writes_exist())
                with self.assertRaisesRegex(RuntimeError, "data changed"):
                    upgrade.validate_upgraded_state(snapshot)


    def test_rollback_stops_and_quiesces_before_typed_check_and_preserves_legacy_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            store = Store(state)
            owner, _, _ = store.authenticate(None)
            store.add_document(owner, str(uuid.uuid4()), b"old-pdf",
                                     hashlib.sha256(b"old-pdf").hexdigest(), 1, 1,
                                     "management")
            events = []
            def fake_run(*command, phase, cwd=None):
                events.append(phase)
                if phase == "rollback-stop-service":
                    # A legacy write may finish after the new release was switched.
                    store.add_document(owner, str(uuid.uuid4()), b"post-switch-legacy",
                                       hashlib.sha256(b"post-switch-legacy").hexdigest(),
                                       1, 1, "management")
            original_check = upgrade.typed_writes_exist
            def check_typed():
                events.append("typed-check")
                return original_check()
            with patch.object(upgrade, "STATE", state), \
                 patch.object(upgrade, "run", side_effect=fake_run), \
                 patch.object(upgrade, "require_quiescent", side_effect=lambda: events.append("uid-quiescent")), \
                 patch.object(upgrade, "typed_writes_exist", side_effect=check_typed), \
                 patch.object(upgrade, "replace_current", side_effect=lambda commit: events.append("switch-old")), \
                 patch.object(upgrade, "wait_app", side_effect=lambda expect_records: events.append("health-old")):
                upgrade.recover_previous_release(switched=True)
            self.assertEqual(events, ["rollback-stop-service", "uid-quiescent", "typed-check",
                                      "switch-old", "rollback-start-service", "health-old"])
            self.assertEqual(len(store.documents(owner, "management")), 2,
                             "rollback must not restore the stale snapshot or erase post-switch legacy writes")

    def test_typed_write_race_hard_stops_without_starting_old_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            store = Store(state)
            owner, _, _ = store.authenticate(None)
            events = []
            def fake_run(*command, phase, cwd=None):
                events.append(phase)
                if phase == "rollback-stop-service":
                    # A request accepted by the new runtime immediately before stop becomes durable.
                    store.add_document(owner, str(uuid.uuid4()), b"post-switch-typed",
                                       hashlib.sha256(b"post-switch-typed").hexdigest(),
                                       1, 1, "management", "invoice")
            original_check = upgrade.typed_writes_exist
            def check_typed():
                events.append("typed-check")
                return original_check()
            with patch.object(upgrade, "STATE", state), \
                 patch.object(upgrade, "run", side_effect=fake_run), \
                 patch.object(upgrade, "require_quiescent", side_effect=lambda: events.append("uid-quiescent")), \
                 patch.object(upgrade, "typed_writes_exist", side_effect=check_typed), \
                 patch.object(upgrade, "replace_current", side_effect=lambda commit: events.append("switch-old")), \
                 patch.object(upgrade, "wait_app", side_effect=lambda expect_records: events.append("health-old")):
                with self.assertRaisesRegex(RuntimeError, "fix forward"):
                    upgrade.recover_previous_release(switched=True)
            self.assertEqual(events, ["rollback-stop-service", "uid-quiescent", "typed-check"])
            with patch.object(upgrade, "STATE", state):
                self.assertTrue(original_check())
            self.assertEqual(len(store.documents(owner, "management")), 1,
                             "HARD STOP must retain the typed document and never start old runtime")


if __name__ == "__main__":
    unittest.main()
