"""Migration checks must preserve real Store relations and PDF bytes."""
import hashlib
import io
import os
import runpy
import sys
import stat
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tarfile
import unittest
import uuid

from deploy.migration_checks import inspect_state
from orderflow.app import Store

PDF = b"%PDF-1.4\nsynthetic migration checksum fixture\n%%EOF\n"


class MigrationChecksTests(unittest.TestCase):
    def test_upgrade_release_restores_tarfile_directory_access(self):
        deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
        make_readable = runpy.run_path(str(deploy_dir / "upgrade-asus-release.py"))["make_release_readable"]
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
            directory = tarfile.TarInfo("orderflow")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
            source = tarfile.TarInfo("orderflow/gemini.py")
            source.mode = 0o644
            source.size = 6
            archive.addfile(source, io.BytesIO(b"source"))
        with tempfile.TemporaryDirectory() as root:
            stage = Path(root) / "release"
            stage.mkdir()
            archive_bytes.seek(0)
            previous_umask = os.umask(0o077)
            try:
                with tarfile.open(fileobj=archive_bytes, mode="r:") as archive:
                    archive.extractall(stage, filter="data")
            finally:
                os.umask(previous_umask)
            package = stage / "orderflow"
            self.assertEqual(stat.S_IMODE(package.stat().st_mode), 0o700)
            make_readable(stage)
            self.assertEqual(stat.S_IMODE(package.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((package / "gemini.py").stat().st_mode), 0o644)

    def test_copied_private_stage_is_traversable_by_service_account(self):
        deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
        sys.path.insert(0, str(deploy_dir))
        try:
            finalize = runpy.run_path(str(deploy_dir / "import-asus-state.py"))["make_release_traversable"]
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as root:
            stage = Path(root) / "stage"
            nested = stage / "orderflow"
            nested.mkdir(parents=True)
            nested.chmod(0o775)
            (nested / "app.py").write_text("fixture\n")
            stage.chmod(0o700)
            release = Path(root) / "release"
            shutil.copytree(stage, release)
            self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
            finalize(release)
            self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((release / "orderflow").stat().st_mode), 0o755)

    def test_release_manifest_accepts_archive_paths_and_rejects_extra_source(self):
        deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
        sys.path.insert(0, str(deploy_dir))
        try:
            verify = runpy.run_path(str(deploy_dir / "import-asus-state.py"))["verify_manifest"]
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as root:
            stage = Path(root)
            source = stage / "orderflow.py"
            source.write_text("source fixture\n")
            manifest = stage / "release-manifest.sha256"
            manifest.write_text(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  ./orderflow.py\n")
            verify(stage, hashlib.sha256(manifest.read_bytes()).hexdigest())
            (stage / "unlisted.py").write_text("unexpected\n")
            with self.assertRaisesRegex(RuntimeError, "inventory"):
                verify(stage, hashlib.sha256(manifest.read_bytes()).hexdigest())

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
