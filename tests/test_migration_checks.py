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
from unittest.mock import patch

from deploy.migration_checks import inspect_state
from orderflow.app import Store

PDF = b"%PDF-1.4\nsynthetic migration checksum fixture\n%%EOF\n"


class MigrationChecksTests(unittest.TestCase):
    def test_key_input_upgrade_accepts_existing_management_data_and_detects_loss(self):
        upgrade = runpy.run_path(str(Path(__file__).resolve().parents[1]
                                     / "deploy/upgrade-asus-key-input.py"))
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            store = Store(state)
            owner, _, _ = store.authenticate(None)
            document = store.add_document(owner, str(uuid.uuid4()), PDF,
                                          hashlib.sha256(PDF).hexdigest(), 1, 1, "management")
            job, _ = store.add_job(owner, document["id"], str(uuid.uuid4()), "real", "management")
            store.set_job(job["id"], state="done", result=[{"description": "fixture", "quantity": 1}])
            draft, _ = store.save_management_draft(owner, job["id"],
                                                   [{"description": "fixture", "quantity": 1}], 0)
            snapshot = Path(root) / "snapshot.sqlite3"
            with sqlite3.connect(store.db_path) as live, sqlite3.connect(snapshot) as saved:
                live.backup(saved)
            validate = upgrade["validate_unchanged_state"]
            with patch.dict(validate.__globals__, {"STATE": state}):
                validate(snapshot)
                with store.db() as db:
                    db.execute("DELETE FROM management_drafts WHERE id=?", (draft["id"],))
                with self.assertRaisesRegex(RuntimeError, "management_drafts row count changed"):
                    validate(snapshot)

    def test_key_input_rollback_stops_before_switch(self):
        recover = runpy.run_path(str(Path(__file__).resolve().parents[1]
                                   / "deploy/upgrade-asus-key-input.py"))["recover_previous_release"]
        events = []
        def fake_run(*command, phase, cwd=None): events.append(phase)
        with patch.dict(recover.__globals__, {
            "run": fake_run,
            "require_quiescent": lambda: events.append("quiescent"),
            "replace_current": lambda commit: events.append("replace-current"),
            "wait_app": lambda: events.append("new-health"),
        }):
            recover(True)
        self.assertEqual(events, ["rollback-stop-service", "quiescent", "replace-current",
                                  "rollback-start-service", "new-health"])

    def test_rollback_stops_before_write_check_and_keeps_all_live_rows(self):
        deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
        recover = runpy.run_path(str(deploy_dir / "upgrade-asus-management.py"))["recover_previous_release"]
        digest = hashlib.sha256(PDF).hexdigest()
        for management_write in (False, True):
            with self.subTest(management_write=management_write), tempfile.TemporaryDirectory() as root:
                state = Path(root)
                store = Store(state)
                owner, _, _ = store.authenticate(None)
                first = store.add_document(owner, str(uuid.uuid4()), PDF, digest, 1, 1)
                store.add_job(owner, first["id"], str(uuid.uuid4()), "success")
                events = []
                def fake_run(*command, phase, cwd=None):
                    events.append(phase)
                    if phase == "rollback-stop-service":
                        # A write finished after the preflight check but before stop returned.
                        purpose = "management" if management_write else "diagnostic"
                        session = owner if management_write else store.authenticate(None)[0]
                        doc = store.add_document(session, str(uuid.uuid4()), PDF, digest, 1, 1, purpose)
                        if not management_write:
                            store.add_job(session, doc["id"], str(uuid.uuid4()), "success")
                def fake_quiesce(): events.append("quiescent")
                def fake_replace(commit): events.append("replace-current")
                def fake_wait(expect_management): events.append("old-health")
                with patch.dict(recover.__globals__, {"STATE": state, "run": fake_run,
                                                      "require_quiescent": fake_quiesce,
                                                      "replace_current": fake_replace,
                                                      "wait_app": fake_wait}):
                    if management_write:
                        with self.assertRaisesRegex(RuntimeError, "management writes exist"):
                            recover(True)
                    else:
                        recover(True)
                if management_write:
                    self.assertEqual(events, ["rollback-stop-service", "quiescent"])
                    self.assertEqual(len(store.documents(owner, "management")), 1)
                    self.assertEqual(len(store.documents(owner)), 1)
                else:
                    self.assertEqual(events, ["rollback-stop-service", "quiescent", "replace-current",
                                              "rollback-start-service", "old-health"])
                    with store.db() as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
                    self.assertEqual(len(list((state / "files").glob("*.pdf"))), 2)

    def test_quiescence_rejects_other_service_account_process(self):
        deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
        check = runpy.run_path(str(deploy_dir / "upgrade-asus-management.py"))["require_quiescent"]
        class Process:
            name = "123"
            def __init__(self, uid): self.uid = uid
            def stat(self): return type("Stat", (), {"st_uid": self.uid})()
        class ProcRoot:
            def __init__(self, processes): self.processes = processes
            def iterdir(self): return self.processes
        account = type("Pwd", (), {"getpwnam": staticmethod(lambda name: type("Account", (), {"pw_uid": 456})())})()
        with patch.dict(check.__globals__, {"pwd": account, "Path": lambda path: ProcRoot([Process(789)])}):
            check()
        with patch.dict(check.__globals__, {"pwd": account, "Path": lambda path: ProcRoot([Process(456)])}):
            with self.assertRaisesRegex(RuntimeError, "still has a process"):
                check()

    def test_management_upgrade_health_checks_new_and_old_runtime(self):
        deploy_dir = Path(__file__).resolve().parents[1] / "deploy"
        wait_app = runpy.run_path(str(deploy_dir / "upgrade-asus-management.py"))["wait_app"]
        class Response:
            status = 200
            def __init__(self, path, managed):
                self.path, self.managed = path, managed
                if path.startswith("/orderflow/api/"):
                    self.status = 401 if managed or path == "/orderflow/api/bootstrap" else 404
            def read(self):
                if self.path == "/orderflow/":
                    return b"OrderFlow manage.js" if self.managed else b"OrderFlow app.js"
                return b'{"error_code":"AUTH_REQUIRED"}' if self.status == 401 else b'{}'
        class Connection:
            path = None
            def __init__(self, *args, **kwargs): pass
            def request(self, method, path, headers): self.path = path
            def getresponse(self): return Response(self.path, self.managed)
            def close(self): pass
        for managed in (True, False):
            class Runtime(Connection): pass
            Runtime.managed = managed
            with patch.dict(wait_app.__globals__, {"http": type("HTTP", (), {"client": type("Client", (), {"HTTPConnection": Runtime})})()}):
                wait_app(expect_management=managed)

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

    def test_pre_management_schema_upgrades_atomically_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "files").mkdir()
            session_id, document_id, job_id = (str(uuid.uuid4()) for _ in range(3))
            with sqlite3.connect(state / "orderflow.sqlite3") as db:
                db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created_ms INTEGER NOT NULL)")
                db.execute("CREATE TABLE documents (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_ms INTEGER NOT NULL, upload_ms INTEGER NOT NULL, steps TEXT NOT NULL, page_count INTEGER, UNIQUE(session_id,request_key))")
                db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, document_id TEXT NOT NULL, request_key TEXT NOT NULL, attempt INTEGER NOT NULL, scenario TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT, result TEXT, steps TEXT NOT NULL, created_ms INTEGER NOT NULL, started_ms INTEGER, finished_ms INTEGER, UNIQUE(session_id,document_id,request_key))")
                db.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, "token-hash", 1))
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)", (document_id, session_id, str(uuid.uuid4()), len(PDF), hashlib.sha256(PDF).hexdigest(), 1, 1, '{}', 1))
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, session_id, document_id, str(uuid.uuid4()), 1, "success", "done", None, '[{"description":"legacy","quantity":1}]', '{}', 1, 1, 2))
            (state / "files" / f"{document_id}.pdf").write_bytes(PDF)
            first = Store(state)
            second = Store(state)
            self.assertEqual(first.documents(session_id)[0]["id"], document_id)
            self.assertEqual(second.jobs(session_id)[0]["id"], job_id)
            self.assertEqual(second.documents(session_id, "management"), [])
            with second.db() as db:
                self.assertEqual(db.execute("SELECT purpose FROM documents WHERE id=?", (document_id,)).fetchone()[0], "diagnostic")
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM management_drafts").fetchone()[0], 0)

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
