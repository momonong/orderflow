"""Single-process, loopback-only diagnostic application."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import logging
import http.cookies
import json
import os
import secrets
import sqlite3
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

VERSION = "0.1.0"
PREFIX = "/orderflow/"
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 4096
UPLOAD_DEADLINE_SECONDS = 15
JSON_DEADLINE_SECONDS = 5
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web"
SCENARIOS = {"success", "fail", "timeout", "invalid"}


def now_ms() -> int:
    return int(time.time() * 1000)


def valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def pdf_page_count(data: bytes) -> int | None:
    if not (data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")):
        return None
    # Suppress parser warnings: diagnostics must never contain document content.
    logger = logging.getLogger("pypdf")
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return None
        count = len(reader.pages)
        return count if count > 0 else None
    except Exception:
        return None


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.files = self.data_dir / "files"
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.files.chmod(0o700)
        self.db_path = self.data_dir / "orderflow.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                  size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_ms INTEGER NOT NULL,
                  upload_ms INTEGER NOT NULL, steps TEXT NOT NULL, page_count INTEGER,
                  UNIQUE(session_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, document_id TEXT NOT NULL,
                  request_key TEXT NOT NULL, attempt INTEGER NOT NULL, scenario TEXT NOT NULL,
                  state TEXT NOT NULL, error_code TEXT, result TEXT, steps TEXT NOT NULL,
                  created_ms INTEGER NOT NULL, started_ms INTEGER, finished_ms INTEGER,
                  UNIQUE(session_id, document_id, request_key)
                );
            """)
            if "page_count" not in {row[1] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN page_count INTEGER")
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

    def session(self, token: str | None) -> str | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            row = db.execute("SELECT id FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        return row["id"] if row else None

    def create_session(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        session_id = str(uuid.uuid4())
        with self.db() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (session_id, hashlib.sha256(token.encode()).hexdigest(), now_ms()))
        return session_id, token

    def documents(self, session_id: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT id,size,sha256,created_ms,upload_ms,steps,page_count FROM documents "
                              "WHERE session_id=? ORDER BY created_ms DESC", (session_id,)).fetchall()
        return [dict(row) | {"steps": json.loads(row["steps"])} for row in rows]

    def jobs(self, session_id: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT id,document_id,attempt,scenario,state,error_code,result,steps,"
                              "created_ms,started_ms,finished_ms FROM jobs WHERE session_id=? "
                              "ORDER BY created_ms DESC", (session_id,)).fetchall()
        return [self.public_job(row) for row in rows]

    @staticmethod
    def public_job(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item["result"] else None
        item["steps"] = json.loads(item["steps"])
        item["mode"] = "mock"
        return item

    def job(self, session_id: str, job_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT id,document_id,attempt,scenario,state,error_code,result,steps,"
                             "created_ms,started_ms,finished_ms FROM jobs WHERE id=? AND session_id=?",
                             (job_id, session_id)).fetchone()
        return self.public_job(row) if row else None

    def add_document(self, session_id: str, request_key: str, data: bytes, sha: str, elapsed: int, page_count: int) -> dict:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,size,sha256,created_ms,upload_ms,steps,page_count FROM documents "
                                  "WHERE session_id=? AND request_key=?", (session_id, request_key)).fetchone()
            if existing:
                if existing["sha256"] != sha or existing["size"] != len(data):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return dict(existing) | {"steps": json.loads(existing["steps"])}
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
                db.execute("INSERT INTO documents (id,session_id,request_key,size,sha256,created_ms,upload_ms,steps,page_count) VALUES (?,?,?,?,?,?,?,?,?)",
                           (doc_id, session_id, request_key, len(data), sha, timestamp, elapsed, steps, page_count))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return {"id": doc_id, "size": len(data), "sha256": sha, "created_ms": timestamp,
                "upload_ms": elapsed, "steps": json.loads(steps), "page_count": page_count}

    def add_job(self, session_id: str, document_id: str, request_key: str, scenario: str) -> tuple[dict, bool]:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            document = db.execute("SELECT id FROM documents WHERE id=? AND session_id=?",
                                  (document_id, session_id)).fetchone()
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
            job_id = str(uuid.uuid4())
            timestamp = now_ms()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, session_id, document_id, request_key, attempt, scenario,
                        "queued", None, None, json.dumps({"ai": "not_run", "format": "not_run"}),
                        timestamp, None, None))
        return {"id": job_id, "document_id": document_id, "attempt": attempt,
                "scenario": scenario, "state": "queued", "error_code": None, "result": None,
                "steps": {"ai": "not_run", "format": "not_run"}, "created_ms": timestamp,
                "started_ms": None, "finished_ms": None, "mode": "mock"}, True

    def set_job(self, job_id: str, **fields: object) -> None:
        if "steps" in fields:
            fields["steps"] = json.dumps(fields["steps"], ensure_ascii=False)
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                       (*fields.values(), job_id))


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


def run_job(store: Store, job_id: str, document_id: str, adapter: AIAdapter) -> None:
    store.set_job(job_id, state="running", started_ms=now_ms())
    try:
        raw = adapter.recognize(str(store.files / f"{document_id}.pdf"), deadline_seconds=5)
        rows = validate_result(raw)
        store.set_job(job_id, state="done", result=rows, steps={"ai": "pass", "format": "pass"},
                      finished_ms=now_ms())
    except AIUnknown as exc:
        store.set_job(job_id, state="unknown", error_code=exc.code,
                      steps={"ai": "unknown", "format": "not_run"}, finished_ms=now_ms())
    except AIError as exc:
        store.set_job(job_id, state="failed", error_code=exc.code,
                      steps={"ai": "fail", "format": "not_run"}, finished_ms=now_ms())
    except ValueError:
        store.set_job(job_id, state="failed", error_code="RESULT_FORMAT_INVALID",
                      steps={"ai": "pass", "format": "fail"}, finished_ms=now_ms())
    except Exception:
        store.set_job(job_id, state="unknown", error_code="INTERNAL_UNKNOWN",
                      steps={"ai": "unknown", "format": "not_run"}, finished_ms=now_ms())


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: Store):
        super().__init__(address, Handler)
        self.store = store
        self.adapter_factory = MockAdapter


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format: str, *args: object) -> None:
        # Access logs could leak paths or headers. Operational logging needs a reviewed policy.
        pass

    def json_response(self, status: int, value: object, cookie: str | None = None) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", f"of_session={cookie}; Path={PREFIX}; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: int, code: str) -> None:
        self.json_response(status, {"error_code": code})

    def session_id(self) -> str | None:
        try:
            cookies = http.cookies.SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            token = cookies["of_session"].value if "of_session" in cookies else None
        except http.cookies.CookieError:
            token = None
        return self.server.store.session(token)

    def get_session(self) -> str | None:
        session_id = self.session_id()
        if not session_id:
            self.error(HTTPStatus.UNAUTHORIZED, "SESSION_REQUIRED")
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

    def get_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= MAX_JSON_BYTES:
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
        if host not in {"127.0.0.1", "localhost"}:
            self.error(403, "HOST_NOT_ALLOWED")
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
        if path in {PREFIX, PREFIX + "app.js", PREFIX + "style.css"}:
            filename = "index.html" if path == PREFIX else path.rsplit("/", 1)[-1]
            data = (STATIC / filename).read_bytes()
            content_type = {"index.html": "text/html", "app.js": "text/javascript",
                            "style.css": "text/css"}[filename]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == PREFIX + "api/bootstrap":
            session_id = self.session_id()
            cookie = None
            if not session_id:
                session_id, cookie = self.server.store.create_session()
            self.json_response(200, {"version": VERSION, "max_pdf_bytes": MAX_PDF_BYTES,
                                     "mode": "mock", "documents": self.server.store.documents(session_id),
                                     "jobs": self.server.store.jobs(session_id)}, cookie)
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
        self.error(404, "NOT_FOUND")

    def do_POST(self) -> None:
        if not self.valid_host():
            return
        path = urlsplit(self.path).path
        # A custom header blocks ordinary cross-site forms; no CORS headers are served.
        if self.headers.get("X-Orderflow-Request") != "1":
            self.error(403, "REQUEST_HEADER_REQUIRED")
            return
        session_id = self.get_session()
        if not session_id:
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
        if path == PREFIX + "api/documents":
            self.upload(session_id)
            return
        if path == PREFIX + "api/jobs":
            value = self.get_json()
            if value is None:
                return
            document_id, key, scenario = (value.get(name) for name in ("document_id", "request_key", "scenario"))
            if not valid_uuid(document_id) or not valid_uuid(key) or scenario not in SCENARIOS:
                self.error(400, "BAD_JOB_REQUEST")
                return
            try:
                job, created = self.server.store.add_job(session_id, document_id, key, scenario)
            except LookupError:
                self.error(404, "DOCUMENT_NOT_FOUND")
                return
            except ValueError as exc:
                self.error(409, str(exc))
                return
            if created:
                threading.Thread(target=run_job, args=(self.server.store, job["id"], document_id,
                                                   self.server.adapter_factory(scenario)),
                                 daemon=True).start()
            self.json_response(202 if created else 200, job)
            return
        self.error(404, "NOT_FOUND")

    def upload(self, session_id: str) -> None:
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
        page_count = pdf_page_count(data)
        if page_count is None:
            self.error(415, "PDF_INVALID")
            return
        calculated = hashlib.sha256(data).hexdigest()
        if not secrets.compare_digest(calculated, sha):
            self.error(422, "HASH_MISMATCH")
            return
        try:
            doc = self.server.store.add_document(session_id, key, data, calculated,
                                                 int((time.monotonic() - start) * 1000), page_count)
        except ValueError as exc:
            self.error(409, str(exc))
            return
        self.json_response(201, doc)


def main() -> None:
    parser = argparse.ArgumentParser(description="OrderFlow local feasibility check")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local-data")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("This PoC is loopback-only; public access requires a separate security decision")
    server = AppServer((args.host, args.port), Store(args.data_dir))
    print(f"OrderFlow {VERSION} mock mode at http://{args.host}:{server.server_port}{PREFIX}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
