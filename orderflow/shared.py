"""Explicit, session-owned PDF sharing for the isolated family trial."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .records import KINDS


class SharedLibrary:
    def __init__(self, store, pdf_check, *, max_pdf_bytes: int,
                 max_stored_bytes: int, max_documents_per_session: int):
        self.store = store
        self.pdf_check = pdf_check
        self.max_pdf_bytes = max_pdf_bytes
        self.max_stored_bytes = max_stored_bytes
        self.max_documents_per_session = max_documents_per_session
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS shared_documents (
                id TEXT PRIMARY KEY,
                owner_session_id TEXT NOT NULL REFERENCES sessions(id),
                source_purpose TEXT NOT NULL CHECK(source_purpose IN ('diagnostic','management')),
                source_document_id TEXT NOT NULL,
                title TEXT NOT NULL,
                document_kind TEXT CHECK(document_kind IN ('purchase_order','invoice')),
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                created_ms INTEGER NOT NULL,
                revoked_ms INTEGER,
                UNIQUE(owner_session_id,source_purpose,source_document_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS shared_claims (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                share_id TEXT NOT NULL REFERENCES shared_documents(id),
                target_purpose TEXT NOT NULL CHECK(target_purpose IN ('diagnostic','management')),
                target_kind TEXT NOT NULL CHECK(target_kind IN ('','purchase_order','invoice')),
                document_id TEXT NOT NULL UNIQUE REFERENCES documents(id),
                created_ms INTEGER NOT NULL,
                PRIMARY KEY(session_id,share_id,target_purpose,target_kind)
            )""")

    @staticmethod
    def _public(row, session_id: str) -> dict:
        return {"id": row["id"], "title": row["title"],
                "source_purpose": row["source_purpose"],
                "document_kind": row["document_kind"], "size": row["size"],
                "page_count": row["page_count"], "created_ms": row["created_ms"],
                "can_revoke": row["owner_session_id"] == session_id}

    def list(self, session_id: str) -> list[dict]:
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM shared_documents WHERE revoked_ms IS NULL "
                              "ORDER BY created_ms DESC,id").fetchall()
        return [self._public(row, session_id) for row in rows]

    def _source(self, db, session_id: str, purpose: str, document_id: str):
        if purpose in {"diagnostic", "management"}:
            row = db.execute("SELECT id,size,sha256,page_count,document_kind FROM documents "
                             "WHERE id=? AND session_id=? AND purpose=? AND NOT EXISTS "
                             "(SELECT 1 FROM shared_claims WHERE document_id=documents.id)",
                             (document_id, session_id, purpose)).fetchone()
            if not row:
                raise LookupError("SHARED_SOURCE_NOT_FOUND")
            return (self.store.files / f"{row['id']}.pdf", row["size"], row["sha256"],
                    row["page_count"], row["document_kind"])
        raise ValueError("SHARED_REQUEST_INVALID")

    @staticmethod
    def _read(path: Path, expected_size: int, expected_sha: str) -> bytes:
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError("SHARED_FILE_UNAVAILABLE") from None
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha:
            raise ValueError("SHARED_FILE_UNAVAILABLE")
        return data

    def share(self, session_id: str, purpose: str, document_id: str,
              title: str, now_ms) -> tuple[dict, bool]:
        if (not isinstance(purpose, str) or purpose not in {"diagnostic", "management"} or
                not isinstance(title, str) or not 1 <= len(title.strip()) <= 80 or
                any(ord(char) < 32 or ord(char) == 127 for char in title)):
            raise ValueError("SHARED_REQUEST_INVALID")
        title = title.strip()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            path, size, sha, pages, kind = self._source(db, session_id, purpose, document_id)
            if not size or size > self.max_pdf_bytes:
                raise ValueError("SHARED_FILE_UNAVAILABLE")
            data = self._read(path, size, sha)
            if pages is None:
                pages = self.pdf_check(data)
            if not pages:
                raise ValueError("SHARED_FILE_UNAVAILABLE")
            existing = db.execute("SELECT * FROM shared_documents WHERE owner_session_id=? "
                                  "AND source_purpose=? AND source_document_id=?",
                                  (session_id, purpose, document_id)).fetchone()
            if existing:
                if existing["revoked_ms"] is None:
                    if existing["title"] != title:
                        raise ValueError("SHARED_TITLE_CONFLICT")
                    return self._public(existing, session_id), False
                db.execute("UPDATE shared_documents SET title=?,size=?,sha256=?,page_count=?,"
                           "document_kind=?,revoked_ms=NULL WHERE id=?",
                           (title, size, sha, pages, kind, existing["id"]))
                row = db.execute("SELECT * FROM shared_documents WHERE id=?", (existing["id"],)).fetchone()
                return self._public(row, session_id), True
            share_id = str(uuid.uuid4())
            db.execute("INSERT INTO shared_documents VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                       (share_id, session_id, purpose, document_id, title, kind,
                        size, sha, pages, now_ms()))
            row = db.execute("SELECT * FROM shared_documents WHERE id=?", (share_id,)).fetchone()
        return self._public(row, session_id), True

    def revoke(self, session_id: str, share_id: str, now_ms) -> bool:
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revoked_ms FROM shared_documents WHERE id=? "
                             "AND owner_session_id=?", (share_id, session_id)).fetchone()
            if not row:
                raise LookupError("SHARED_NOT_FOUND")
            if row["revoked_ms"] is not None:
                return False
            db.execute("UPDATE shared_documents SET revoked_ms=? WHERE id=?",
                       (now_ms(), share_id))
        return True

    def file(self, share_id: str) -> bytes:
        with self.store.db() as db:
            row = db.execute("SELECT * FROM shared_documents WHERE id=? AND revoked_ms IS NULL",
                             (share_id,)).fetchone()
            if not row:
                raise LookupError("SHARED_NOT_FOUND")
            try:
                path, size, sha, _, _ = self._source(db, row["owner_session_id"],
                                                      row["source_purpose"], row["source_document_id"])
            except LookupError:
                raise ValueError("SHARED_FILE_UNAVAILABLE") from None
            if size != row["size"] or sha != row["sha256"]:
                raise ValueError("SHARED_FILE_UNAVAILABLE")
            return self._read(path, size, sha)

    def claim(self, session_id: str, share_id: str, purpose: str,
              kind: str | None, now_ms) -> tuple[dict, bool]:
        if not isinstance(purpose, str) or purpose not in {"diagnostic", "management"}:
            raise ValueError("SHARED_REQUEST_INVALID")
        if purpose == "diagnostic":
            if kind is not None:
                raise ValueError("SHARED_REQUEST_INVALID")
            target_kind = ""
        else:
            if not isinstance(kind, str) or kind not in KINDS:
                raise ValueError("SHARED_KIND_REQUIRED")
            target_kind = kind
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT d.id,d.size,d.sha256,d.created_ms,d.upload_ms,d.steps,"
                                  "d.page_count,d.document_kind,'shared' AS source_type FROM shared_claims c "
                                  "JOIN documents d ON d.id=c.document_id AND d.session_id=c.session_id "
                                  "WHERE c.session_id=? AND c.share_id=? AND c.target_purpose=? "
                                  "AND c.target_kind=?",
                                  (session_id, share_id, purpose, target_kind)).fetchone()
            if existing:
                return dict(existing) | {"steps": json.loads(existing["steps"])}, False
            share = db.execute("SELECT * FROM shared_documents WHERE id=? AND revoked_ms IS NULL",
                               (share_id,)).fetchone()
            if not share:
                raise LookupError("SHARED_NOT_FOUND")
            if purpose == "management" and share["document_kind"] and share["document_kind"] != kind:
                raise ValueError("SHARED_KIND_CONFLICT")
            if share["size"] > self.max_pdf_bytes:
                raise ValueError("SHARED_FILE_UNAVAILABLE")
            try:
                path, size, sha, _, _ = self._source(db, share["owner_session_id"],
                                                      share["source_purpose"], share["source_document_id"])
            except LookupError:
                raise ValueError("SHARED_FILE_UNAVAILABLE") from None
            if size != share["size"] or sha != share["sha256"]:
                raise ValueError("SHARED_FILE_UNAVAILABLE")
            data = self._read(path, size, sha)
            if self.store.public_limits:
                count = db.execute("SELECT COUNT(*) FROM documents WHERE session_id=? AND purpose=?",
                                   (session_id, purpose)).fetchone()[0]
                total = db.execute("SELECT COALESCE(SUM(size),0) FROM documents").fetchone()[0]
                if count >= self.max_documents_per_session or total + size > self.max_stored_bytes:
                    raise ValueError("STORAGE_LIMIT")
            document_id = str(uuid.uuid4())
            output = self.store.files / f"{document_id}.pdf"
            try:
                with output.open("xb") as stream:
                    os.chmod(output, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                timestamp = now_ms()
                steps = json.dumps({"upload": "not_run", "integrity": "pass"})
                db.execute("INSERT INTO documents (id,session_id,request_key,size,sha256,created_ms,"
                           "upload_ms,steps,page_count,purpose,document_kind) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (document_id, session_id, f"shared:{share_id}:{purpose}:{target_kind}",
                            size, sha, timestamp, 0, steps, share["page_count"], purpose,
                            kind if purpose == "management" else None))
                db.execute("INSERT INTO shared_claims VALUES (?,?,?,?,?,?)",
                           (session_id, share_id, purpose, target_kind, document_id, timestamp))
            except Exception:
                output.unlink(missing_ok=True)
                raise
            row = db.execute("SELECT id,size,sha256,created_ms,upload_ms,steps,page_count,"
                             "document_kind FROM documents WHERE id=?",
                             (document_id,)).fetchone()
        return dict(row) | {"steps": json.loads(row["steps"]), "source_type": "shared"}, True
