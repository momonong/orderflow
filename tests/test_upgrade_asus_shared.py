"""Data-preserving checks for the ASUS family-trial upgrade gate."""

import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from orderflow.app import Store


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/upgrade-asus-shared-trial.py"
SPEC = importlib.util.spec_from_file_location("shared_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class SharedUpgradeGateTests(unittest.TestCase):
    def test_runtime_gate_allows_only_package_version_change(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as name:
            old, release = Path(name) / "old", Path(name) / "release"
            old.mkdir()
            release.mkdir()
            for filename in ("pyproject.toml", "uv.lock"):
                candidate = (repository / filename).read_text()
                (release / filename).write_text(candidate)
                (old / filename).write_text(candidate.replace('version = "0.3.1"',
                                                             'version = "0.3.0"'))
            upgrade.require_same_runtime(old, release)
            (release / "pyproject.toml").write_text(
                (release / "pyproject.toml").read_text().replace("bcrypt==5.0.0", "bcrypt==4.0.0"))
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                upgrade.require_same_runtime(old, release)

    def test_current_schema_rows_and_pdf_survive_additive_migration(self):
        with tempfile.TemporaryDirectory() as name:
            before = Path(name) / "before"
            after = Path(name) / "after"
            store = Store(before)
            session, _, _ = store.authenticate(None)
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            stream = io.BytesIO()
            writer.write(stream)
            data = stream.getvalue()
            document = store.add_document(session, "synthetic-request", data,
                                          hashlib.sha256(data).hexdigest(), 1, 1)
            with sqlite3.connect(before / "orderflow.sqlite3") as db:
                db.execute("DROP TABLE shared_claims")
                db.execute("DROP TABLE shared_documents")
            shutil.copytree(before, after)
            Store(after)
            upgrade.check_db(after / "orderflow.sqlite3")
            upgrade.existing_rows_unchanged(before / "orderflow.sqlite3",
                                            after / "orderflow.sqlite3", migrated=True)
            upgrade.existing_files_unchanged(before, after)
            self.assertEqual((after / "files" / f"{document['id']}.pdf").read_bytes(), data)

    def test_gate_is_pinned_to_current_runtime_and_single_help_file(self):
        self.assertEqual(upgrade.OLD_COMMIT, "b245b1a1da252f08a2f39a192346892d44832dc0")
        self.assertEqual(upgrade.STATIC_ROUTES["docs/user-guide/index.html"],
                         "/orderflow/help/")
        self.assertEqual(upgrade.release_file(Path("/release"), "docs/user-guide/index.html"),
                         Path("/release/docs/user-guide/index.html"))
        self.assertIn("shared_documents", upgrade.TRIAL_TABLES)
        self.assertIn("shared_claims", upgrade.TRIAL_TABLES)

    def test_failure_after_stop_preserves_backup_and_hard_stops(self):
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
                  patch.object(upgrade, "run"),
                  patch.object(upgrade, "migrate_as_orderflow",
                               side_effect=RuntimeError("synthetic migration failure"))):
                command.return_value.returncode = 0
                with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                    upgrade.main()
            self.assertEqual(current.readlink(), Path("releases") / upgrade.OLD_COMMIT)
            self.assertTrue((backups / f"before-shared-trial-{target[:12]}" /
                             "raw/files/synthetic.pdf").is_file())
            self.assertIn("HARD STOP", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
