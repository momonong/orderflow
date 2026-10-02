"""Synthetic preservation checks for the one-time ASUS trial upgrade gate."""

import importlib.util
import hashlib
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from orderflow.app import Store


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/upgrade-asus-integration-trial.py"
OLD_SCHEMA = Path(__file__).resolve().parent / "fixtures/orderflow-9182-schema.sql"
SPEC = importlib.util.spec_from_file_location("integration_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class UpgradeGateTests(unittest.TestCase):
    def test_failure_after_stop_keeps_backup_and_requires_manual_recovery(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            releases = root / "releases"
            releases.mkdir()
            current = root / "current"
            current.symlink_to(Path("releases") / upgrade.OLD_COMMIT)
            state = root / "state"
            (state / "files").mkdir(parents=True)
            (state / "files/synthetic.pdf").write_bytes(b"%PDF-synthetic")
            with sqlite3.connect(state / "orderflow.sqlite3") as db:
                db.execute("CREATE TABLE jobs (state TEXT)")
            backups = root / "backups"
            target = "a" * 40
            stderr = io.StringIO()
            with (patch.object(upgrade, "ROOT", root),
                  patch.object(upgrade, "RELEASES", releases),
                  patch.object(upgrade, "CURRENT", current),
                  patch.object(upgrade, "STATE", state),
                  patch.object(upgrade, "BACKUPS", backups),
                  patch.object(upgrade.os, "geteuid", return_value=0),
                  patch.object(upgrade.os, "umask"),
                  patch.object(upgrade.sys, "argv", [str(SCRIPT), "--archive", str(root / "source.tar"),
                                                    "--archive-sha256", "0" * 64, "--commit", target]),
                  patch.object(upgrade.sys, "stderr", stderr),
                  patch.object(upgrade.subprocess, "run") as command,
                  patch.object(upgrade, "check_app"),
                  patch.object(upgrade, "stage_release", return_value=releases / target),
                  patch.object(upgrade, "trial_migration"),
                  patch.object(upgrade, "require_quiescent"),
                  patch.object(upgrade, "run") as service,
                  patch.object(upgrade, "migrate_as_orderflow",
                               side_effect=RuntimeError("synthetic migration failure"))):
                command.return_value.returncode = 0
                with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                    upgrade.main()
            self.assertEqual(current.readlink(), Path("releases") / upgrade.OLD_COMMIT)
            self.assertTrue((backups / f"before-integration-trial-{target[:12]}" /
                             "raw/files/synthetic.pdf").is_file())
            self.assertIn("HARD STOP", stderr.getvalue())
            self.assertTrue(any(call.args[:2] == ("systemctl", "stop") for call in service.call_args_list))
            self.assertTrue(any(call.args[0] == ["systemctl", "stop", "orderflow"]
                                for call in command.call_args_list))

    def test_actual_candidate_migration_preserves_old_state(self):
        with tempfile.TemporaryDirectory() as name:
            before = Path(name) / "before"
            after = Path(name) / "after"
            (before / "files").mkdir(parents=True)
            original = b"%PDF-1.4\nsynthetic migration fixture\n%%EOF"
            (before / "files/synthetic.pdf").write_bytes(original)
            old_db = before / "orderflow.sqlite3"
            with sqlite3.connect(old_db) as db:
                db.executescript(OLD_SCHEMA.read_text())
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("INSERT INTO sessions VALUES ('session', 'synthetic-hash', 1)")
                db.execute("INSERT INTO session_auth VALUES ('session', 1, 2)")
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           ('document', 'session', 'request', len(original),
                            hashlib.sha256(original).hexdigest(), 1, 1, '[]', 1,
                            'management', 'purchase_order'))
                db.execute("INSERT INTO management_local_sources VALUES (?,?,?,?,?,?,?)",
                           ('source', 'session', 'document', 'source-request',
                            'koya-purchase-v1', '[]', 1))
                db.execute("INSERT INTO management_record_sets VALUES (?,?,?,?,?,?,?,?,?,?)",
                           ('record', 'session', 'document', None, 'source',
                            'purchase_order', '[]', 1, 1, 1))
            shutil.copytree(before, after)
            Store(after)
            upgrade.check_db(after / "orderflow.sqlite3")
            upgrade.existing_rows_unchanged(old_db, after / "orderflow.sqlite3", migrated=True)
            upgrade.existing_files_unchanged(before, after)

    def test_existing_rows_and_schema_survive_new_trial_tables(self):
        with tempfile.TemporaryDirectory() as name:
            before = Path(name) / "before.sqlite3"
            after = Path(name) / "after.sqlite3"
            with sqlite3.connect(before) as db:
                db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL)")
                db.execute("CREATE TABLE management_local_sources (id TEXT PRIMARY KEY, session_id TEXT NOT NULL)")
                db.execute("INSERT INTO sessions VALUES ('synthetic-session', 'synthetic-hash')")
                db.execute("INSERT INTO management_local_sources VALUES ('synthetic-source', 'synthetic-session')")
            shutil.copyfile(before, after)
            with sqlite3.connect(after) as db:
                for table in upgrade.TRIAL_TABLES:
                    db.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY)')
            upgrade.existing_rows_unchanged(before, after, migrated=True)
            with sqlite3.connect(after) as db:
                db.execute("DELETE FROM management_local_sources")
            with self.assertRaisesRegex(RuntimeError, "rows changed"):
                upgrade.existing_rows_unchanged(before, after, migrated=True)

    def test_existing_file_bytes_must_survive(self):
        with tempfile.TemporaryDirectory() as name:
            before = Path(name) / "before"
            after = Path(name) / "after"
            (before / "files").mkdir(parents=True)
            (after / "files").mkdir(parents=True)
            (before / "files/source.pdf").write_bytes(b"%PDF-synthetic")
            (after / "files/source.pdf").write_bytes(b"%PDF-synthetic")
            upgrade.existing_files_unchanged(before, after)
            (after / "files/source.pdf").write_bytes(b"%PDF-changed")
            with self.assertRaisesRegex(RuntimeError, "file changed"):
                upgrade.existing_files_unchanged(before, after)


if __name__ == "__main__":
    unittest.main()
