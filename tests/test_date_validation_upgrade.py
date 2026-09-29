"""Offline safety checks for the ASUS date-validation release gate."""

import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from orderflow.app import Store

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/upgrade-asus-date-validation.py"
spec = importlib.util.spec_from_file_location("date_validation_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self):
        return self.body


class Connection:
    responses = []

    def __init__(self, *args, **kwargs):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class DateValidationUpgradeChecks(unittest.TestCase):
    def test_fixed_source_and_protected_health(self):
        self.assertEqual(upgrade.OLD_COMMIT, "ee41445565a73ea7e0b3444f0bb1bfad5e6ddd2a")
        auth = b'{"error_code":"AUTH_REQUIRED"}'
        Connection.responses = [Response(200, (ROOT / "web/index.html").read_bytes()),
                                Response(200, (ROOT / "web/manage.js").read_bytes()),
                                Response(200, (ROOT / "web/manage.css").read_bytes()),
                                Response(401, auth), Response(401, auth)]
        with patch.object(upgrade.http.client, "HTTPConnection", Connection):
            upgrade.wait_app(ROOT)
        self.assertEqual(Connection.responses, [])
        Connection.responses = [Response(200, b'<title>OrderFlow</title>manage.js'),
                                Response(401, auth), Response(401, auth)]
        with patch.object(upgrade.http.client, "HTTPConnection", Connection):
            upgrade.wait_app()
        self.assertEqual(Connection.responses, [])

    def test_snapshot_detects_changes_in_existing_typed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            store = Store(state)
            owner, _, _ = store.authenticate(None)
            document = store.add_document(owner, str(uuid.uuid4()), b"synthetic-pdf",
                                          hashlib.sha256(b"synthetic-pdf").hexdigest(),
                                          1, 1, "management", "invoice")
            job, _ = store.add_job(owner, document["id"], str(uuid.uuid4()),
                                    "real", "management")
            store.set_job(job["id"], state="done", result=[{"date": "2026/9/22"}])
            item = {"id": str(uuid.uuid4()), "orderNo": None, "invoiceNo": "INV-1",
                    "client": None, "product": "synthetic", "code": None, "qty": "1",
                    "unitPrice": None, "amount": None, "currency": None,
                    "date": "2026-09-22", "incoterms": None, "unit": None,
                    "status": None, "linked_order_row_id": None, "deleted": False}
            store.save_record_set(owner, document["id"], job["id"], 0, [item])
            snapshot = state / "snapshot.sqlite3"
            with sqlite3.connect(store.db_path) as live, sqlite3.connect(snapshot) as copy:
                live.backup(copy)
            with patch.object(upgrade, "STATE", state):
                upgrade.validate_unchanged_state(snapshot)
                with store.db() as db:
                    db.execute("UPDATE management_record_sets SET revision=revision+1")
                with self.assertRaisesRegex(RuntimeError, "management_record_sets data changed"):
                    upgrade.validate_unchanged_state(snapshot)

    def test_rollback_stops_and_quiesces_before_returning_to_previous_release(self):
        events = []
        with patch.object(upgrade, "run", side_effect=lambda *args, phase, **kwargs: events.append(phase)), \
             patch.object(upgrade, "require_quiescent", side_effect=lambda: events.append("quiescent")), \
             patch.object(upgrade, "replace_current", side_effect=lambda *_: events.append("switch")), \
             patch.object(upgrade, "wait_app", side_effect=lambda *_: events.append("health")):
            upgrade.recover_previous_release(True)
        self.assertEqual(events, ["rollback-stop-service", "quiescent", "switch",
                                  "rollback-start-service", "health"])


if __name__ == "__main__":
    unittest.main()
