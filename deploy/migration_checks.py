"""Read-only integrity checks for a stopped OrderFlow state snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


def inspect_state(data_dir: Path) -> dict[str, object]:
    db_path = data_dir / "orderflow.sqlite3"
    if not db_path.is_file() or not (data_dir / "files").is_dir():
        raise ValueError("state database or PDF directory missing")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        sessions = [tuple(row) for row in db.execute(
            "SELECT id,token_hash,created_ms FROM sessions ORDER BY id")]
        documents = [tuple(row) for row in db.execute(
            "SELECT id,session_id,request_key,size,sha256,created_ms FROM documents ORDER BY id")]
        jobs = [tuple(row) for row in db.execute(
            "SELECT id,session_id,document_id,request_key,attempt,scenario FROM jobs ORDER BY id")]
        pending = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
    finally:
        db.close()
    session_ids = {row[0] for row in sessions}
    document_owners = {row[0]: row[1] for row in documents}
    if len(session_ids) != len(sessions) or len(document_owners) != len(documents):
        raise ValueError("duplicate session or document ID")
    if any(row[1] not in session_ids for row in documents):
        raise ValueError("document has no session")
    if any(row[1] not in session_ids or document_owners.get(row[2]) != row[1] for row in jobs):
        raise ValueError("job owner or document relation invalid")
    for doc_id, _session, _key, size, digest, _created in documents:
        file = data_dir / "files" / f"{doc_id}.pdf"
        if not file.is_file() or file.stat().st_size != size:
            raise ValueError("PDF missing or size changed")
        checksum = hashlib.sha256()
        with file.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != digest:
            raise ValueError("PDF checksum changed")
    stable = hashlib.sha256(json.dumps(
        {"sessions": sessions, "documents": documents, "jobs": jobs},
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return {"sessions": len(sessions), "documents": len(documents),
            "jobs": len(jobs), "pending_jobs": pending, "stable_digest": stable}
