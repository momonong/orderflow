"""Offline preservation and fail-closed checks for the Broker deployment gate."""
import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from orderflow.app import Store
from orderflow.broker_jobs import migrate

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/upgrade-asus-broker.py"
SPEC = importlib.util.spec_from_file_location("broker_upgrade", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PDF = b"%PDF-1.4\nsynthetic preservation fixture\n%%EOF\n"


class BrokerUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.before = self.root / "before"
        self.store = Store(self.before)
        owner, _, _ = self.store.authenticate(None)
        self.document = self.store.add_document(owner, "fixture", PDF, hashlib.sha256(PDF).hexdigest(), 1, 1)
        with self.store.db() as db:
            db.execute("DROP TABLE broker_jobs")
        self.after = self.root / "after"
        shutil.copytree(self.before, self.after)
        Store(self.after)
        db = sqlite3.connect(":memory:")
        migrate(db)
        self.expected = set(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"))
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def compare(self):
        gate.compare_migration(self.before / "orderflow.sqlite3", self.after / "orderflow.sqlite3", self.expected)

    def test_additive_table_preserves_all_rows_and_pdf(self):
        self.compare()
        gate.shared.existing_files_unchanged(self.before, self.after)
        gate.check_document_files(self.after)
        self.assertEqual((self.after / "files" / (self.document["id"] + ".pdf")).read_bytes(), PDF)

    def test_changed_private_row_is_rejected(self):
        with sqlite3.connect(self.after / "orderflow.sqlite3") as db:
            db.execute("UPDATE sessions SET token_hash='changed'")
        with self.assertRaisesRegex(RuntimeError, "rows changed"):
            self.compare()

    def test_unreviewed_new_schema_is_rejected(self):
        with sqlite3.connect(self.after / "orderflow.sqlite3") as db:
            db.execute("CREATE TABLE unexpected (value TEXT)")
        with self.assertRaisesRegex(RuntimeError, "unexpected additive schema"):
            self.compare()

    def test_existing_mapping_table_cannot_be_treated_as_fresh_migration(self):
        with sqlite3.connect(self.before / "orderflow.sqlite3") as db:
            migrate(db)
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.compare()

    def test_missing_pdf_and_symlinks_stop_gate(self):
        path = self.after / "files" / (self.document["id"] + ".pdf")
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, "PDF missing"):
            gate.check_document_files(self.after)
        path.symlink_to(self.before / "files" / path.name)
        with self.assertRaisesRegex(RuntimeError, "unexpected state"):
            gate.file_manifest(self.after)

    def test_after_stop_failure_preserves_raw_backup_and_hard_stops(self):
        backup = self.root / "backup"
        backup.mkdir()
        stderr = io.StringIO()
        with (patch.object(gate.shared, "STATE", self.before),
              patch.object(gate, "check_installed_base"),
              patch.object(gate.shared, "require_quiescent"),
              patch.object(gate.shared, "pending_jobs", return_value=0),
              patch.object(gate, "run") as commands,
              patch.object(gate, "migrate_copy", side_effect=RuntimeError("trial failed")),
              patch.object(gate.shared, "replace_current") as switch,
              patch.object(gate.subprocess, "run") as stop,
              patch.object(gate.sys, "stderr", stderr)):
            with self.assertRaisesRegex(RuntimeError, "trial failed"):
                gate.deploy(self.root / "release", self.root / "config", backup,
                            self.expected, lambda: None)
        self.assertTrue((backup / "raw/orderflow.sqlite3").is_file())
        gate.check_document_files(backup / "raw")
        switch.assert_not_called()
        commands.assert_called_once_with("systemctl", "stop", "orderflow", phase="stop-orderflow")
        self.assertEqual(stop.call_args.args[0], ["systemctl", "stop", "orderflow"])
        self.assertIn("HARD STOP", stderr.getvalue())
        self.assertFalse((self.before / "broker.json").exists())

    def test_broker_runtime_drift_fails_before_stop(self):
        backup = self.root / "backup"
        backup.mkdir()
        with (patch.object(gate, "run") as commands,
              patch.object(gate.subprocess, "run") as stop):
            with self.assertRaisesRegex(RuntimeError, "runtime changed"):
                gate.deploy(self.root / "release", self.root / "config", backup,
                            self.expected, lambda: gate.check_broker_pin(self.root / "missing", "/wrong"))
        commands.assert_not_called()
        stop.assert_not_called()

    def test_existing_candidate_runtime_is_never_reused(self):
        releases = self.root / "releases"
        existing = releases / ("a" * 40)
        (existing / ".venv/bin").mkdir(parents=True)
        fake = existing / ".venv/bin/python"
        fake.write_text("unreviewed runtime")
        fake.chmod(0o777)
        with patch.object(gate.shared, "RELEASES", releases):
            with self.assertRaisesRegex(RuntimeError, "target release exists"):
                gate.stage_release(self.root / "missing.tar", "b" * 64, "a" * 40)
        self.assertEqual(fake.read_text(), "unreviewed runtime")

    def test_nested_symlink_is_not_followed_or_chowned_during_online_trial(self):
        outside = self.root / "outside-private"
        outside.write_bytes(b"synthetic outside state")
        (self.before / "files" / "late-link").symlink_to(outside)
        snapshot = self.root / "online"
        with patch.object(gate.shared, "STATE", self.before):
            with self.assertRaisesRegex(RuntimeError, "unexpected state file type"):
                gate.copy_online_state(snapshot)
        self.assertTrue((snapshot / "files/late-link").is_symlink())
        with patch.object(gate, "run") as commands:
            with self.assertRaisesRegex(RuntimeError, "unexpected state file type"):
                gate.migrate_copy(self.root / "release", snapshot, self.expected)
        commands.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"synthetic outside state")


if __name__ == "__main__":
    unittest.main()
