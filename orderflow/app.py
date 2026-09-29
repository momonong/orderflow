"""Single-process, loopback-only diagnostic application."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import hashlib
import http.cookies
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from pypdf import PdfReader

from .ai import AIAdapter, AIError, AIUnknown, MockAdapter
from .auth import BCRYPT_HASH, load_caddy_hash, verify_password
from .gemini import GeminiAdapter, MODEL as GEMINI_MODEL

VERSION = "0.3.0"
PREFIX = "/orderflow/"
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 4096
UPLOAD_DEADLINE_SECONDS = 15
JSON_DEADLINE_SECONDS = 5
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web"
SCENARIOS = {"success", "fail", "timeout", "invalid", "real"}
KEY_TTL_SECONDS = 15 * 60
PURPOSES = {"diagnostic", "management"}
MAX_DRAFT_JSON_BYTES = 32 * 1024
MAX_STORED_BYTES = 128 * 1024 * 1024
MAX_DOCUMENTS_PER_SESSION = 20
MAX_JOBS_PER_DOCUMENT = 10
MAX_ACTIVE_KEYS = 32
SESSION_TTL_MS = 8 * 60 * 60 * 1000
LOGIN_ATTEMPTS_PER_MINUTE = 5
SAFE_AI_CODES = {"AI_UNAVAILABLE", "AI_TIMEOUT_UNKNOWN", "AI_NOT_CONFIGURED", "AI_RATE_LIMITED", "AI_HTTP_ERROR", "AI_BAD_RESPONSE", "AI_AUTH_FAILED", "AI_MODEL_UNAVAILABLE", "AI_BAD_REQUEST", "AI_HTTP_UNKNOWN"}
SAFE_UPSTREAM_REASONS = {"INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNCLASSIFIED"}


def safe_ai_code(code: str, fallback: str) -> str:
    return code if code in SAFE_AI_CODES else fallback


def safe_ai_metadata(error: AIError) -> dict[str, int | str]:
    metadata: dict[str, int | str] = {}
    status = error.upstream_http_status
    if type(status) is int and 100 <= status <= 599:
        metadata["upstream_http_status"] = status
    if error.upstream_reason in SAFE_UPSTREAM_REASONS:
        metadata["upstream_reason"] = error.upstream_reason
    return metadata


def audit_event(event: str, reference: str, phase: str,
                metadata: dict[str, int | str] | None = None) -> None:
    # Only fixed event/phase labels, canonical UUIDs, and whitelisted metadata.
    record = {"event": event, "id": reference if valid_uuid(reference) else "invalid",
              "phase": phase}
    record.update(metadata or {})
    print("orderflow_audit " + json.dumps(record, separators=(",", ":")),
          file=sys.stderr, flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class PDFCheckTimeout(Exception):
    pass


def pdf_page_count(data: bytes) -> int | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "orderflow.pdfcheck"], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
            env={"PYTHONPATH": str(ROOT)},
        )
    except subprocess.TimeoutExpired:
        raise PDFCheckTimeout from None
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        count = int(result.stdout)
        return count if count > 0 else None
    except ValueError:
        return None


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.public_limits = False
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.files = self.data_dir / "files"
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.files.chmod(0o700)
        self.db_path = self.data_dir / "orderflow.sqlite3"
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for statement in (
                """CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created_ms INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS session_auth (
                  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                  authenticated_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS documents (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                  size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_ms INTEGER NOT NULL,
                  upload_ms INTEGER NOT NULL, steps TEXT NOT NULL, page_count INTEGER,
                  purpose TEXT NOT NULL DEFAULT 'diagnostic'
                    CHECK (purpose IN ('diagnostic','management')),
                  UNIQUE(session_id, request_key)
                )""",
                """CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, document_id TEXT NOT NULL,
                  request_key TEXT NOT NULL, attempt INTEGER NOT NULL, scenario TEXT NOT NULL,
                  state TEXT NOT NULL, error_code TEXT, result TEXT, steps TEXT NOT NULL,
                  created_ms INTEGER NOT NULL, started_ms INTEGER, finished_ms INTEGER,
                  UNIQUE(session_id, document_id, request_key)
                )""",
            ):
                db.execute(statement)
            columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            if "page_count" not in columns:
                db.execute("ALTER TABLE documents ADD COLUMN page_count INTEGER")
            if "purpose" not in columns:
                db.execute("ALTER TABLE documents ADD COLUMN purpose TEXT NOT NULL "
                           "DEFAULT 'diagnostic' CHECK (purpose IN ('diagnostic','management'))")
            db.execute("""CREATE TABLE IF NOT EXISTS management_drafts (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(id),
                source_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                rows_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
                updated_ms INTEGER NOT NULL
            )""")
            db.execute("UPDATE jobs SET state='unknown', error_code='SERVER_RESTART', finished_ms=? "
                       "WHERE state IN ('queued', 'running')", (now_ms(),))
        self.db_path.chmod(0o600)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def session_status(self, token: str | None) -> tuple[str | None, str]:
        if not token:
            return None, "AUTH_REQUIRED"
        with self.db() as db:
            row = db.execute("SELECT s.id,a.authenticated_ms,a.expires_ms FROM sessions s "
                             "LEFT JOIN session_auth a ON a.session_id=s.id WHERE s.token_hash=?",
                             (self.token_digest(token),)).fetchone()
        if not row:
            return None, "AUTH_REQUIRED"
        if row["authenticated_ms"] is None or row["expires_ms"] is None:
            return row["id"], "AUTH_REQUIRED"
        if row["expires_ms"] <= now_ms():
            return row["id"], "SESSION_EXPIRED"
        return row["id"], "ok"

    def session(self, token: str | None) -> str | None:
        session_id, status = self.session_status(token)
        return session_id if status == "ok" else None

    def authenticate(self, old_token: str | None) -> tuple[str, str, int]:
        """Rotate even a legacy anonymous token while keeping its document owner."""
        token = secrets.token_urlsafe(32)
        timestamp = now_ms()
        expires = timestamp + SESSION_TTL_MS
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM sessions WHERE token_hash=?",
                                  (self.token_digest(old_token),)).fetchone() if old_token else None
            if existing:
                session_id = existing["id"]
                db.execute("UPDATE sessions SET token_hash=? WHERE id=?",
                           (self.token_digest(token), session_id))
            else:
                session_id = str(uuid.uuid4())
                db.execute("INSERT INTO sessions VALUES (?,?,?)",
                           (session_id, self.token_digest(token), timestamp))
            db.execute("INSERT INTO session_auth (session_id,authenticated_ms,expires_ms) VALUES (?,?,?) "
                       "ON CONFLICT(session_id) DO UPDATE SET authenticated_ms=excluded.authenticated_ms, "
                       "expires_ms=excluded.expires_ms", (session_id, timestamp, expires))
        return session_id, token, expires

    def deauthenticate(self, old_token: str | None) -> tuple[str | None, str | None]:
        """Invalidate the privileged token and retain an unprivileged browser owner."""
        if not old_token:
            return None, None
        anonymous_token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM sessions WHERE token_hash=?",
                             (self.token_digest(old_token),)).fetchone()
            if not row:
                return None, None
            session_id = row["id"]
            db.execute("UPDATE sessions SET token_hash=? WHERE id=?",
                       (self.token_digest(anonymous_token), session_id))
            db.execute("DELETE FROM session_auth WHERE session_id=?", (session_id,))
        return session_id, anonymous_token

    def session_expires(self, session_id: str) -> int:
        with self.db() as db:
            row = db.execute("SELECT expires_ms FROM session_auth WHERE session_id=?", (session_id,)).fetchone()
        return int(row["expires_ms"]) if row else 0

    def documents(self, session_id: str, purpose: str = "diagnostic") -> list[dict]:
        if purpose not in PURPOSES:
            raise ValueError("invalid purpose")
        with self.db() as db:
            rows = db.execute("SELECT id,size,sha256,created_ms,upload_ms,steps,page_count FROM documents "
                              "WHERE session_id=? AND purpose=? ORDER BY created_ms DESC",
                              (session_id, purpose)).fetchall()
        return [dict(row) | {"steps": json.loads(row["steps"])} for row in rows]

    def jobs(self, session_id: str, purpose: str = "diagnostic") -> list[dict]:
        if purpose not in PURPOSES:
            raise ValueError("invalid purpose")
        with self.db() as db:
            rows = db.execute("SELECT j.id,j.document_id,j.attempt,j.scenario,j.state,j.error_code,"
                              "j.result,j.steps,j.created_ms,j.started_ms,j.finished_ms FROM jobs j "
                              "JOIN documents d ON d.id=j.document_id "
                              "WHERE j.session_id=? AND d.session_id=? AND d.purpose=? "
                              "ORDER BY j.created_ms DESC", (session_id, session_id, purpose)).fetchall()
        return [self.public_job(row) for row in rows]

    @staticmethod
    def public_job(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item["result"] else None
        item["steps"] = json.loads(item["steps"])
        item["mode"] = "real" if item["scenario"] == "real" else "mock"
        return item

    def job(self, session_id: str, job_id: str, purpose: str = "diagnostic") -> dict | None:
        if purpose not in PURPOSES:
            raise ValueError("invalid purpose")
        with self.db() as db:
            row = db.execute("SELECT j.id,j.document_id,j.attempt,j.scenario,j.state,j.error_code,"
                             "j.result,j.steps,j.created_ms,j.started_ms,j.finished_ms FROM jobs j "
                             "JOIN documents d ON d.id=j.document_id "
                             "WHERE j.id=? AND j.session_id=? AND d.session_id=? AND d.purpose=?",
                             (job_id, session_id, session_id, purpose)).fetchone()
        return self.public_job(row) if row else None

    def add_document(self, session_id: str, request_key: str, data: bytes, sha: str,
                     elapsed: int, page_count: int, purpose: str = "diagnostic") -> dict:
        if purpose not in PURPOSES:
            raise ValueError("invalid purpose")
        stored_key = request_key if purpose == "diagnostic" else f"management:{request_key}"
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,size,sha256,created_ms,upload_ms,steps,page_count FROM documents "
                                  "WHERE session_id=? AND request_key=? AND purpose=?",
                                  (session_id, stored_key, purpose)).fetchone()
            if existing:
                if existing["sha256"] != sha or existing["size"] != len(data):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return dict(existing) | {"steps": json.loads(existing["steps"])}
            if self.public_limits:
                count = db.execute("SELECT COUNT(*) FROM documents WHERE session_id=? AND purpose=?",
                                   (session_id, purpose)).fetchone()[0]
                total = db.execute("SELECT COALESCE(SUM(size),0) FROM documents").fetchone()[0]
                if count >= MAX_DOCUMENTS_PER_SESSION or total + len(data) > MAX_STORED_BYTES:
                    raise ValueError("STORAGE_LIMIT")
            doc_id = str(uuid.uuid4())
            path = self.files / f"{doc_id}.pdf"
            with path.open("xb") as file:
                os.chmod(path, 0o600)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            timestamp = now_ms()
            steps = json.dumps({"upload": "pass", "integrity": "pass"})
            try:
                db.execute("INSERT INTO documents (id,session_id,request_key,size,sha256,created_ms,"
                           "upload_ms,steps,page_count,purpose) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (doc_id, session_id, stored_key, len(data), sha, timestamp, elapsed,
                            steps, page_count, purpose))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return {"id": doc_id, "size": len(data), "sha256": sha, "created_ms": timestamp,
                "upload_ms": elapsed, "steps": json.loads(steps), "page_count": page_count}

    def add_job(self, session_id: str, document_id: str, request_key: str,
                scenario: str, purpose: str = "diagnostic") -> tuple[dict, bool]:
        if purpose not in PURPOSES or purpose == "management" and scenario != "real":
            raise ValueError("invalid purpose/scenario")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            document = db.execute("SELECT id FROM documents WHERE id=? AND session_id=? AND purpose=?",
                                  (document_id, session_id, purpose)).fetchone()
            if not document:
                raise LookupError("DOCUMENT_NOT_FOUND")
            existing = db.execute("SELECT id,document_id,attempt,scenario,state,error_code,result,steps,"
                                  "created_ms,started_ms,finished_ms FROM jobs WHERE session_id=? "
                                  "AND document_id=? AND request_key=?",
                                  (session_id, document_id, request_key)).fetchone()
            if existing:
                if existing["scenario"] != scenario:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return self.public_job(existing), False
            attempt = db.execute("SELECT COUNT(*) FROM jobs WHERE document_id=?", (document_id,)).fetchone()[0] + 1
            if self.public_limits and attempt > MAX_JOBS_PER_DOCUMENT:
                raise ValueError("JOB_LIMIT")
            job_id = str(uuid.uuid4())
            timestamp = now_ms()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, session_id, document_id, request_key, attempt, scenario,
                        "queued", None, None, json.dumps({"ai": "not_run", "format": "not_run"}),
                        timestamp, None, None))
        return {"id": job_id, "document_id": document_id, "attempt": attempt,
                "scenario": scenario, "state": "queued", "error_code": None, "result": None,
                "steps": {"ai": "not_run", "format": "not_run"}, "created_ms": timestamp,
                "started_ms": None, "finished_ms": None, "mode": "real" if scenario == "real" else "mock"}, True

    @staticmethod
    def public_draft(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "document_id": row["document_id"],
                "source_job_id": row["source_job_id"], "rows": json.loads(row["rows_json"]),
                "revision": row["revision"], "updated_ms": row["updated_ms"]}

    def management_drafts(self, session_id: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT m.id,m.document_id,m.source_job_id,m.rows_json,m.revision,"
                              "m.updated_ms FROM management_drafts m "
                              "JOIN jobs j ON j.id=m.source_job_id AND j.document_id=m.document_id "
                              "JOIN documents d ON d.id=m.document_id "
                              "WHERE m.session_id=? AND j.session_id=? AND d.session_id=? "
                              "AND d.purpose='management' ORDER BY m.updated_ms DESC",
                              (session_id, session_id, session_id)).fetchall()
        return [self.public_draft(row) for row in rows]

    def management_draft(self, session_id: str, source_job_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT m.id,m.document_id,m.source_job_id,m.rows_json,m.revision,"
                             "m.updated_ms FROM management_drafts m "
                             "JOIN jobs j ON j.id=m.source_job_id AND j.document_id=m.document_id "
                             "JOIN documents d ON d.id=m.document_id "
                             "WHERE m.session_id=? AND j.session_id=? AND d.session_id=? "
                             "AND d.purpose='management' AND m.source_job_id=?",
                             (session_id, session_id, session_id, source_job_id)).fetchone()
        return self.public_draft(row) if row else None

    def save_management_draft(self, session_id: str, source_job_id: str,
                              rows: object, expected_revision: int) -> tuple[dict, bool]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("DRAFT_VERSION_INVALID")
        checked_rows = validate_draft_rows(rows)
        rows_json = json.dumps(checked_rows, ensure_ascii=False, separators=(",", ":"))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute("SELECT j.document_id FROM jobs j "
                                "JOIN documents d ON d.id=j.document_id "
                                "WHERE j.id=? AND j.session_id=? AND d.session_id=? "
                                "AND d.purpose='management' AND j.scenario='real' AND j.state='done'",
                                (source_job_id, session_id, session_id)).fetchone()
            if not source:
                raise LookupError("DRAFT_SOURCE_NOT_FOUND")
            existing = db.execute("SELECT id,document_id,source_job_id,rows_json,revision,updated_ms "
                                  "FROM management_drafts WHERE source_job_id=? AND session_id=?",
                                  (source_job_id, session_id)).fetchone()
            if existing:
                if existing["rows_json"] == rows_json:
                    return self.public_draft(existing), False
                if existing["revision"] != expected_revision:
                    raise ValueError("DRAFT_VERSION_CONFLICT")
                revision = existing["revision"] + 1
                db.execute("UPDATE management_drafts SET rows_json=?,revision=?,updated_ms=? "
                           "WHERE id=?", (rows_json, revision, now_ms(), existing["id"]))
                draft_id = existing["id"]
                created = False
            else:
                if expected_revision != 0:
                    raise ValueError("DRAFT_VERSION_CONFLICT")
                draft_id = str(uuid.uuid4())
                db.execute("INSERT INTO management_drafts "
                           "(id,session_id,document_id,source_job_id,rows_json,revision,updated_ms) "
                           "VALUES (?,?,?,?,?,?,?)", (draft_id, session_id, source["document_id"],
                                                     source_job_id, rows_json, 1, now_ms()))
                created = True
            saved = db.execute("SELECT id,document_id,source_job_id,rows_json,revision,updated_ms "
                               "FROM management_drafts WHERE id=?", (draft_id,)).fetchone()
        return self.public_draft(saved), created

    def set_job(self, job_id: str, **fields: object) -> None:
        if "steps" in fields:
            fields["steps"] = json.dumps(fields["steps"], ensure_ascii=False)
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                       (*fields.values(), job_id))


def run_real_job(store: Store, job_id: str, document_id: str, adapter: GeminiAdapter,
                 slot: threading.BoundedSemaphore) -> None:
    if not slot.acquire(blocking=False):
        store.set_job(job_id, state="failed", error_code="AI_RATE_LIMITED",
                      steps={"ai": "fail", "format": "not_run"}, finished_ms=now_ms())
        return
    try:
        run_job(store, job_id, document_id, adapter)
    finally:
        slot.release()


def validate_result(value: object) -> list[dict]:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("RESULT_FORMAT_INVALID")
    rows = value["items"]
    if len(rows) > 100:
        raise ValueError("RESULT_FORMAT_INVALID")
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("description"), str)
                or len(row["description"]) > 200 or type(row.get("quantity")) is not int
                or row["quantity"] < 0):
            raise ValueError("RESULT_FORMAT_INVALID")
    return [{"description": row["description"], "quantity": row["quantity"]} for row in rows]


def validate_draft_rows(value: object) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("DRAFT_ROWS_INVALID")
    rows = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"description", "quantity"}:
            raise ValueError("DRAFT_ROWS_INVALID")
        description = item["description"]
        quantity = item["quantity"]
        if (not isinstance(description, str) or not description.strip()
                or len(description) > 200 or type(quantity) is not int
                or not 0 <= quantity <= 1_000_000_000):
            raise ValueError("DRAFT_ROWS_INVALID")
        rows.append({"description": description.strip(), "quantity": quantity})
    return rows


def run_job(store: Store, job_id: str, document_id: str, adapter: AIAdapter) -> None:
    real = isinstance(adapter, GeminiAdapter)
    if real:
        audit_event("real_job", job_id, "start")
    store.set_job(job_id, state="running", started_ms=now_ms())
    try:
        raw = adapter.recognize(str(store.files / f"{document_id}.pdf"), deadline_seconds=25 if real else 5)
        rows = validate_result(raw)
        store.set_job(job_id, state="done", result=rows, steps={"ai": "pass", "format": "pass"},
                      finished_ms=now_ms())
        if real:
            audit_event("real_job", job_id, "done")
    except AIUnknown as exc:
        metadata = safe_ai_metadata(exc)
        store.set_job(job_id, state="unknown", error_code=safe_ai_code(exc.code, "AI_RESULT_UNKNOWN"),
                      steps={"ai": "unknown", "format": "not_run", **metadata}, finished_ms=now_ms())
        if real:
            audit_event("real_job", job_id, "unknown", metadata)
    except AIError as exc:
        metadata = safe_ai_metadata(exc)
        store.set_job(job_id, state="failed", error_code=safe_ai_code(exc.code, "AI_FAILURE"),
                      steps={"ai": "fail", "format": "not_run", **metadata}, finished_ms=now_ms())
        if real:
            audit_event("real_job", job_id, "failed", metadata)
    except ValueError:
        store.set_job(job_id, state="failed", error_code="RESULT_FORMAT_INVALID",
                      steps={"ai": "pass", "format": "fail"}, finished_ms=now_ms())
        if real:
            audit_event("real_job", job_id, "result_format_invalid")
    except Exception:
        store.set_job(job_id, state="unknown", error_code="INTERNAL_UNKNOWN",
                      steps={"ai": "unknown", "format": "not_run"}, finished_ms=now_ms())
        if real:
            audit_event("real_job", job_id, "internal_unknown")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: Store, public_origin: str | None,
                 auth_hash: bytes):
        if address[0] != "127.0.0.1":
            raise ValueError("OrderFlow must bind to 127.0.0.1")
        if public_origin:
            parsed = urlsplit(public_origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                    or parsed.fragment or parsed.username or parsed.password or parsed.port):
                raise ValueError("public origin must be an HTTPS origin without a port or path")
        if not isinstance(auth_hash, bytes) or not BCRYPT_HASH.fullmatch(auth_hash):
            raise ValueError("valid login credential required")
        super().__init__(address, Handler)
        self.auth_hash = auth_hash
        self.login_times: deque[float] = deque()
        self.login_lock = threading.Lock()
        self.store = store
        self.store.public_limits = bool(public_origin)
        self.upload_slots = threading.BoundedSemaphore(2)
        self.real_job_slot = threading.BoundedSemaphore(1)
        self.adapter_factory = MockAdapter
        self.public_origin = public_origin
        self.public_host = urlsplit(public_origin).hostname if public_origin else None
        self.keys: dict[str, tuple[str, float]] = {}
        self.key_lock = threading.Lock()
        self.key_wakeup = threading.Event()
        self.key_stopping = False
        self.key_sweeper = threading.Thread(target=self._expire_keys, daemon=True)
        self.key_sweeper.start()

    def allow_login_attempt(self) -> bool:
        now = time.monotonic()
        with self.login_lock:
            while self.login_times and self.login_times[0] <= now - 60:
                self.login_times.popleft()
            if len(self.login_times) >= LOGIN_ATTEMPTS_PER_MINUTE:
                return False
            self.login_times.append(now)
            return True

    def _prune_keys(self, now: float) -> None:
        for session_id, (_, expiry) in list(self.keys.items()):
            if expiry <= now:
                self.keys.pop(session_id, None)

    def _expire_keys(self) -> None:
        while True:
            with self.key_lock:
                if self.key_stopping:
                    return
                now = time.monotonic()
                self._prune_keys(now)
                timeout = max(0, min(expiry for _, expiry in self.keys.values()) - now) if self.keys else None
            self.key_wakeup.wait(timeout)
            self.key_wakeup.clear()

    def set_key(self, session_id: str, key: str) -> bool:
        with self.key_lock:
            self._prune_keys(time.monotonic())
            if session_id not in self.keys and len(self.keys) >= MAX_ACTIVE_KEYS:
                return False
            self.keys[session_id] = (key, time.monotonic() + KEY_TTL_SECONDS)
        self.key_wakeup.set()
        return True

    def get_key(self, session_id: str) -> str | None:
        with self.key_lock:
            self._prune_keys(time.monotonic())
            value = self.keys.get(session_id)
            return value[0] if value else None

    def clear_key(self, session_id: str) -> None:
        with self.key_lock:
            self.keys.pop(session_id, None)
        self.key_wakeup.set()

    def server_close(self) -> None:
        with self.key_lock:
            self.key_stopping = True
            self.keys.clear()
        self.key_wakeup.set()
        self.key_sweeper.join(timeout=1)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format: str, *args: object) -> None:
        # Access logs could leak paths or headers. Operational logging needs a reviewed policy.
        pass

    def request_id(self) -> str:
        if not hasattr(self, "_request_id"):
            supplied = self.headers.get("X-Orderflow-Request-Id")
            self._request_id = supplied if valid_uuid(supplied) else str(uuid.uuid4())
        return self._request_id

    def end_headers(self) -> None:
        self.send_header("X-Orderflow-Origin", "app")
        self.send_header("X-Orderflow-Request-Id", self.request_id())
        super().end_headers()

    def json_response(self, status: int, value: object, cookie: str | None = None,
                      clear_cookie: bool = False) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie or clear_cookie:
            value = f"of_session={cookie or ''}; Path={PREFIX}; HttpOnly; SameSite=Strict"
            if clear_cookie:
                value += "; Max-Age=0"
            if self.server.public_origin:
                value += "; Secure"
            self.send_header("Set-Cookie", value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The request may already be committed; the client can query or replay its key.
            pass

    def error(self, status: int, code: str) -> None:
        self.json_response(status, {"error_code": code})

    def session_token(self) -> str | None:
        try:
            cookies = http.cookies.SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            return cookies["of_session"].value if "of_session" in cookies else None
        except http.cookies.CookieError:
            return None

    def get_session(self) -> str | None:
        session_id, status = self.server.store.session_status(self.session_token())
        if status != "ok":
            if status == "SESSION_EXPIRED" and session_id:
                self.server.clear_key(session_id)
            self.error(HTTPStatus.UNAUTHORIZED, status)
            return None
        return session_id

    def read_body(self, length: int, seconds: float) -> bytes:
        deadline = time.monotonic() + seconds
        chunks = []
        remaining = length
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError
            self.connection.settimeout(budget)
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def get_json(self, max_bytes: int = MAX_JSON_BYTES) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= max_bytes:
                raise ValueError
            raw = self.read_body(length, JSON_DEADLINE_SECONDS)
            if len(raw) != length:
                self.error(400, "REQUEST_INCOMPLETE")
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (TimeoutError, OSError):
            self.error(408, "REQUEST_INCOMPLETE")
            return None
        except (ValueError, UnicodeError):
            self.error(HTTPStatus.BAD_REQUEST, "BAD_JSON")
            return None

    def valid_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        allowed = {self.server.public_host} if self.server.public_host else {"127.0.0.1", "localhost"}
        if host not in allowed:
            self.error(403, "HOST_NOT_ALLOWED")
            return False
        return True

    def valid_origin(self) -> bool:
        if self.server.public_origin and self.headers.get("Origin") != self.server.public_origin:
            self.error(403, "ORIGIN_NOT_ALLOWED")
            return False
        return True

    def do_GET(self) -> None:
        if not self.valid_host():
            return
        path = urlsplit(self.path).path
        if path == "/orderflow":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", PREFIX)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        static_routes = {
            PREFIX: ("index.html", "text/html"),
            PREFIX + "manage.js": ("manage.js", "text/javascript"),
            PREFIX + "manage.css": ("manage.css", "text/css"),
            PREFIX + "test/": ("test.html", "text/html"),
            PREFIX + "app.js": ("app.js", "text/javascript"),
            PREFIX + "style.css": ("style.css", "text/css"),
        }
        if path == PREFIX + "test":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", PREFIX + "test/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in static_routes:
            filename, content_type = static_routes[path]
            data = (STATIC / filename).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == PREFIX + "api/health":
            if not self.get_session():
                return
            self.json_response(200, {"status": "ok", "version": VERSION, "mode": "mock-and-real"})
            return
        if path == PREFIX + "api/bootstrap":
            session_id = self.get_session()
            if not session_id:
                return
            self.json_response(200, {"version": VERSION, "max_pdf_bytes": MAX_PDF_BYTES,
                                     "mode": "mock-and-real", "documents": self.server.store.documents(session_id),
                                     "jobs": self.server.store.jobs(session_id),
                                     "ai_key_configured": self.server.get_key(session_id) is not None,
                                     "ai_model": GEMINI_MODEL,
                                     "auth_expires_ms": self.server.store.session_expires(session_id)})
            return
        if path == PREFIX + "api/management/bootstrap":
            session_id = self.get_session()
            if not session_id:
                return
            self.json_response(200, {"version": VERSION, "max_pdf_bytes": MAX_PDF_BYTES,
                                     "documents": self.server.store.documents(session_id, "management"),
                                     "jobs": self.server.store.jobs(session_id, "management"),
                                     "drafts": self.server.store.management_drafts(session_id),
                                     "ai_key_configured": self.server.get_key(session_id) is not None,
                                     "ai_model": GEMINI_MODEL,
                                     "auth_expires_ms": self.server.store.session_expires(session_id)})
            return
        if path == PREFIX + "api/key":
            session_id = self.get_session()
            if session_id:
                self.json_response(200, {"configured": self.server.get_key(session_id) is not None,
                                         "model": GEMINI_MODEL})
            return
        if path == PREFIX + "api/sample":
            if not self.get_session():
                return
            self.json_response(200, {"rows": [{"description": "中文 <測試> & \"引號\"", "quantity": 2},
                                              {"description": "Apostrophe ' / slash", "quantity": 1}]})
            return
        if path.startswith(PREFIX + "api/jobs/"):
            session_id = self.get_session()
            if not session_id:
                return
            job_id = path[len(PREFIX + "api/jobs/"):]
            if not valid_uuid(job_id):
                self.error(404, "JOB_NOT_FOUND")
                return
            job = self.server.store.job(session_id, job_id)
            self.json_response(200, job) if job else self.error(404, "JOB_NOT_FOUND")
            return
        if path.startswith(PREFIX + "api/management/jobs/"):
            session_id = self.get_session()
            if not session_id:
                return
            job_id = path[len(PREFIX + "api/management/jobs/"):]
            if not valid_uuid(job_id):
                self.error(404, "JOB_NOT_FOUND")
                return
            job = self.server.store.job(session_id, job_id, "management")
            self.json_response(200, job) if job else self.error(404, "JOB_NOT_FOUND")
            return
        if path.startswith(PREFIX + "api/management/drafts/"):
            session_id = self.get_session()
            if not session_id:
                return
            job_id = path[len(PREFIX + "api/management/drafts/"):]
            if not valid_uuid(job_id):
                self.error(404, "DRAFT_NOT_FOUND")
                return
            draft = self.server.store.management_draft(session_id, job_id)
            self.json_response(200, draft) if draft else self.error(404, "DRAFT_NOT_FOUND")
            return
        if path.startswith(PREFIX + "api/") and not self.get_session():
            return
        self.error(404, "NOT_FOUND")

    def login(self) -> None:
        if not self.server.allow_login_attempt():
            self.error(HTTPStatus.TOO_MANY_REQUESTS, "LOGIN_RATE_LIMITED")
            return
        value = self.get_json()
        if value is None:
            return
        if not verify_password(value.get("password"), self.server.auth_hash):
            self.error(HTTPStatus.UNAUTHORIZED, "INVALID_CREDENTIALS")
            return
        old_token = self.session_token()
        old_session_id, _ = self.server.store.session_status(old_token)
        session_id, token, expires = self.server.store.authenticate(old_token)
        if old_session_id:
            self.server.clear_key(old_session_id)
        self.json_response(200, {"status": "ok", "auth_expires_ms": expires}, token)

    def logout(self) -> None:
        session_id, token = self.server.store.deauthenticate(self.session_token())
        if session_id:
            self.server.clear_key(session_id)
            self.json_response(200, {"status": "signed_out"}, token)
        else:
            self.json_response(200, {"status": "signed_out"}, clear_cookie=True)

    def do_POST(self) -> None:
        if not self.valid_host() or not self.valid_origin():
            return
        path = urlsplit(self.path).path
        # A custom header blocks ordinary cross-site forms; no CORS headers are served.
        if self.headers.get("X-Orderflow-Request") != "1":
            self.error(403, "REQUEST_HEADER_REQUIRED")
            return
        if path == PREFIX + "api/login":
            self.login()
            return
        if path == PREFIX + "api/logout":
            self.logout()
            return
        session_id = self.get_session()
        if not session_id:
            return
        if path == PREFIX + "api/key":
            value = self.get_json()
            if value is None:
                return
            key = value.get("key")
            if (not isinstance(key, str) or not 20 <= len(key) <= 256
                    or any(ord(char) < 33 or ord(char) > 126 for char in key)):
                self.error(400, "BAD_KEY")
                return
            if not self.server.set_key(session_id, key):
                self.error(429, "KEY_CAPACITY")
                return
            self.json_response(200, {"configured": True, "model": GEMINI_MODEL,
                                     "expires_in_seconds": KEY_TTL_SECONDS})
            return
        if path == PREFIX + "api/key/check":
            key = self.server.get_key(session_id)
            if not key:
                self.error(409, "KEY_REQUIRED")
                return
            request_id = self.request_id()
            audit_event("key_check", request_id, "start")
            try:
                GeminiAdapter(key).check_text()
            except (AIError, AIUnknown) as exc:
                metadata = safe_ai_metadata(exc)
                audit_event("key_check", request_id, "failed", {"app_http_status": 502, **metadata})
                self.json_response(502, {"error_code": safe_ai_code(exc.code, "AI_HTTP_UNKNOWN"),
                                         **metadata})
                return
            except Exception:
                audit_event("key_check", request_id, "internal_unknown", {"app_http_status": 502})
                self.error(502, "AI_HTTP_UNKNOWN")
                return
            audit_event("key_check", request_id, "done", {"app_http_status": 200})
            self.json_response(200, {"status": "ok", "model": GEMINI_MODEL})
            return
        if path == PREFIX + "api/echo":
            value = self.get_json()
            if value is None:
                return
            nonce = value.get("nonce")
            if not isinstance(nonce, str) or len(nonce) > 128:
                self.error(400, "BAD_NONCE")
                return
            self.json_response(200, {"nonce": nonce})
            return
        if path in {PREFIX + "api/documents", PREFIX + "api/management/documents"}:
            purpose = "management" if path == PREFIX + "api/management/documents" else "diagnostic"
            self.upload(session_id, purpose)
            return
        if path in {PREFIX + "api/jobs", PREFIX + "api/management/jobs"}:
            purpose = "management" if path == PREFIX + "api/management/jobs" else "diagnostic"
            value = self.get_json()
            if value is None:
                return
            document_id, key, scenario = (value.get(name) for name in ("document_id", "request_key", "scenario"))
            if (not valid_uuid(document_id) or not valid_uuid(key) or scenario not in SCENARIOS
                    or purpose == "management" and scenario != "real"):
                self.error(400, "BAD_JOB_REQUEST")
                return
            ai_key = self.server.get_key(session_id) if scenario == "real" else None
            if scenario == "real" and not ai_key:
                self.error(409, "KEY_REQUIRED")
                return
            try:
                job, created = self.server.store.add_job(session_id, document_id, key, scenario, purpose)
            except LookupError:
                self.error(404, "DOCUMENT_NOT_FOUND")
                return
            except ValueError as exc:
                code = str(exc)
                self.error(429 if code == "JOB_LIMIT" else 409,
                           "JOB_LIMIT" if code == "JOB_LIMIT" else "IDEMPOTENCY_CONFLICT")
                return
            if created:
                adapter = GeminiAdapter(ai_key) if scenario == "real" else self.server.adapter_factory(scenario)
                target = run_real_job if scenario == "real" else run_job
                arguments = (self.server.store, job["id"], document_id, adapter)
                if scenario == "real":
                    arguments += (self.server.real_job_slot,)
                threading.Thread(target=target, args=arguments, daemon=True).start()
            self.json_response(202 if created else 200, job)
            return
        self.error(404, "NOT_FOUND")

    def do_DELETE(self) -> None:
        if not self.valid_host() or not self.valid_origin():
            return
        if self.headers.get("X-Orderflow-Request") != "1":
            self.error(403, "REQUEST_HEADER_REQUIRED")
            return
        session_id = self.get_session()
        if not session_id:
            return
        if urlsplit(self.path).path != PREFIX + "api/key":
            self.error(404, "NOT_FOUND")
            return
        self.server.clear_key(session_id)
        self.json_response(200, {"configured": False, "model": GEMINI_MODEL})

    def _unsupported_api_method(self) -> None:
        if not self.valid_host():
            return
        if urlsplit(self.path).path.startswith(PREFIX + "api/") and not self.get_session():
            return
        self.error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")

    def do_PUT(self) -> None:
        if not self.valid_host():
            return
        session_id = self.get_session()
        if not session_id or not self.valid_origin():
            return
        if self.headers.get("X-Orderflow-Request") != "1":
            self.error(403, "REQUEST_HEADER_REQUIRED")
            return
        path = urlsplit(self.path).path
        if not path.startswith(PREFIX + "api/management/drafts/"):
            self.error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")
            return
        job_id = path[len(PREFIX + "api/management/drafts/"):]
        if not valid_uuid(job_id):
            self.error(404, "DRAFT_SOURCE_NOT_FOUND")
            return
        value = self.get_json(MAX_DRAFT_JSON_BYTES)
        if value is None:
            return
        if set(value) != {"rows", "revision"}:
            self.error(400, "DRAFT_ROWS_INVALID")
            return
        try:
            draft, created = self.server.store.save_management_draft(
                session_id, job_id, value["rows"], value["revision"])
        except LookupError:
            self.error(404, "DRAFT_SOURCE_NOT_FOUND")
            return
        except ValueError as exc:
            code = str(exc)
            self.error(409 if code == "DRAFT_VERSION_CONFLICT" else 400,
                       code if code in {"DRAFT_VERSION_CONFLICT", "DRAFT_VERSION_INVALID",
                                        "DRAFT_ROWS_INVALID"} else "DRAFT_ROWS_INVALID")
            return
        self.json_response(201 if created else 200, draft)

    do_PATCH = _unsupported_api_method
    do_OPTIONS = _unsupported_api_method
    do_HEAD = _unsupported_api_method

    def upload(self, session_id: str, purpose: str = "diagnostic") -> None:
        if not self.server.public_origin:
            self._upload_impl(session_id, purpose)
            return
        if not self.server.upload_slots.acquire(blocking=False):
            self.error(429, "UPLOAD_BUSY")
            return
        try:
            self._upload_impl(session_id, purpose)
        finally:
            self.server.upload_slots.release()

    def _upload_impl(self, session_id: str, purpose: str) -> None:
        start = time.monotonic()
        key = self.headers.get("X-Request-Key")
        sha = self.headers.get("X-File-SHA256", "")
        try:
            length = int(self.headers.get("Content-Length", ""))
            declared_size = int(self.headers.get("X-File-Size", ""))
        except ValueError:
            self.error(400, "BAD_SIZE")
            return
        if not valid_uuid(key) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            self.error(400, "BAD_UPLOAD_METADATA")
            return
        if length < 1 or declared_size < 1:
            self.error(400, "BAD_SIZE")
            return
        if length > MAX_PDF_BYTES or declared_size > MAX_PDF_BYTES:
            self.error(413, "FILE_TOO_LARGE")
            return
        if length != declared_size:
            self.error(400, "SIZE_MISMATCH")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/pdf":
            self.error(415, "PDF_REQUIRED")
            return
        try:
            data = self.read_body(length, UPLOAD_DEADLINE_SECONDS)
        except (TimeoutError, OSError):
            self.error(408, "UPLOAD_INCOMPLETE")
            return
        if len(data) != length:
            self.error(400, "UPLOAD_INCOMPLETE")
            return
        try:
            page_count = pdf_page_count(data)
        except PDFCheckTimeout:
            self.error(408, "PDF_CHECK_TIMEOUT")
            return
        if page_count is None:
            self.error(415, "PDF_INVALID")
            return
        calculated = hashlib.sha256(data).hexdigest()
        if not secrets.compare_digest(calculated, sha):
            self.error(422, "HASH_MISMATCH")
            return
        try:
            doc = self.server.store.add_document(session_id, key, data, calculated,
                                                 int((time.monotonic() - start) * 1000), page_count,
                                                 purpose)
        except ValueError as exc:
            code = str(exc)
            self.error(507 if code == "STORAGE_LIMIT" else 409,
                       "STORAGE_LIMIT" if code == "STORAGE_LIMIT" else "IDEMPOTENCY_CONFLICT")
            return
        self.json_response(201, doc)


def main() -> None:
    parser = argparse.ArgumentParser(description="OrderFlow local feasibility check")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local-data")
    parser.add_argument("--public-origin", help="HTTPS origin allowed through a same-host loopback proxy")
    parser.add_argument("--auth-file", type=Path, help="Caddy bcrypt credential file for local testing")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("This PoC is loopback-only; public access requires a separate security decision")
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    auth_file = args.auth_file or (Path(credential_dir) / "login-auth" if credential_dir else None)
    if not auth_file:
        parser.error("login credential required")
    try:
        auth_hash = load_caddy_hash(auth_file)
    except (OSError, ValueError):
        parser.error("login credential unavailable or invalid")
    server = AppServer((args.host, args.port), Store(args.data_dir), args.public_origin, auth_hash)
    print(f"OrderFlow {VERSION} at http://{args.host}:{server.server_port}{PREFIX}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
