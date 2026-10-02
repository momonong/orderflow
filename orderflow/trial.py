"""Session-owned, isolated document ledger for the integration trial.

This module never reads or changes the management and diagnostic document tables.
No inferred shipment, inventory, or payment state is stored here.
"""

from __future__ import annotations

import base64
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
import zipfile


KINDS = {"purchase_order", "invoice"}
SOURCES = {"manual", "pdf", "xlsx"}
ROW_FIELDS = {"id", "code", "description", "quantity", "unit", "unit_price", "amount"}
HEADER_FIELDS = {"company", "number", "date", "currency"}
MAX_ROWS = 100
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_XLSX_BYTES = 2 * 1024 * 1024
MAX_DOCUMENTS_PER_SESSION = 20
MAX_STORED_FILE_BYTES = 128 * 1024 * 1024
DECIMAL = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$")
MONEY = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?$")
DATE = re.compile(r"^([0-9]{4})[-/]([0-9]{1,2})[-/]([0-9]{1,2})$")
FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r", "\n")


def now_ms() -> int:
    return int(time.time() * 1000)


def valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("TRIAL_FIELDS_INVALID")
    return value.strip()


def decimal_string(value: object, *, money: bool = False) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not (MONEY if money else DECIMAL).fullmatch(value):
        raise ValueError("TRIAL_FIELDS_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("TRIAL_FIELDS_INVALID") from error
    if number > Decimal("1000000000000"):
        raise ValueError("TRIAL_FIELDS_INVALID")
    return value


def validate_fields(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"header", "rows"}:
        raise ValueError("TRIAL_FIELDS_INVALID")
    header = value["header"]
    rows = value["rows"]
    if not isinstance(header, dict) or set(header) != HEADER_FIELDS or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("TRIAL_FIELDS_INVALID")
    clean_header = {
        "company": text(header["company"], 120),
        "number": text(header["number"], 80),
        "date": text(header["date"], 10),
        "currency": text(header["currency"], 3).upper(),
    }
    if clean_header["date"]:
        from datetime import date
        match = DATE.fullmatch(clean_header["date"])
        if not match:
            raise ValueError("TRIAL_FIELDS_INVALID")
        try:
            clean_header["date"] = date(*map(int, match.groups())).isoformat()
        except ValueError as error:
            raise ValueError("TRIAL_FIELDS_INVALID") from error
    if clean_header["currency"] and not re.fullmatch(r"[A-Z]{3}", clean_header["currency"]):
        raise ValueError("TRIAL_FIELDS_INVALID")
    clean_rows = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_FIELDS or not valid_uuid(row["id"]) or row["id"] in ids:
            raise ValueError("TRIAL_FIELDS_INVALID")
        ids.add(row["id"])
        clean_rows.append({
            "id": row["id"], "code": text(row["code"], 80),
            "description": text(row["description"], 300),
            "quantity": decimal_string(row["quantity"]), "unit": text(row["unit"], 24),
            "unit_price": decimal_string(row["unit_price"], money=True),
            "amount": decimal_string(row["amount"], money=True),
        })
    return {"header": clean_header, "rows": clean_rows}


def validate_product(value: object) -> tuple[str, str, list[tuple[str, str]]]:
    if not isinstance(value, dict) or set(value) != {"label", "unit", "aliases"}:
        raise ValueError("TRIAL_PRODUCT_INVALID")
    label = text(value["label"], 120)
    unit = text(value["unit"], 24)
    aliases = value["aliases"]
    if not label or not isinstance(aliases, list) or not 1 <= len(aliases) <= 20:
        raise ValueError("TRIAL_PRODUCT_INVALID")
    clean = []
    for alias in aliases:
        if not isinstance(alias, dict) or set(alias) != {"company", "code"}:
            raise ValueError("TRIAL_PRODUCT_INVALID")
        company, code = text(alias["company"], 120), text(alias["code"], 80)
        if not company or not code:
            raise ValueError("TRIAL_PRODUCT_INVALID")
        clean.append((company, code))
    if len(set(clean)) != len(clean):
        raise ValueError("TRIAL_PRODUCT_INVALID")
    return label, unit, clean


def fingerprint(kind: str, source: str, candidate: dict | None,
                confirmed: dict, file_sha: str | None) -> str:
    body = json.dumps({"kind": kind, "source": source, "candidate": candidate,
                       "confirmed": confirmed, "file_sha": file_sha},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def validate_file(source: str, value: object, pdf_check) -> tuple[bytes | None, str | None]:
    if source == "manual":
        if value is not None:
            raise ValueError("TRIAL_FILE_INVALID")
        return None, None
    if not isinstance(value, dict) or set(value) != {"base64", "sha256"} or not isinstance(value["base64"], str) or not isinstance(value["sha256"], str):
        raise ValueError("TRIAL_FILE_INVALID")
    limit = MAX_PDF_BYTES if source == "pdf" else MAX_XLSX_BYTES
    if len(value["base64"]) > ((limit + 2) // 3) * 4:
        raise ValueError("TRIAL_FILE_TOO_LARGE")
    try:
        data = base64.b64decode(value["base64"], validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("TRIAL_FILE_INVALID") from error
    if not data or len(data) > limit:
        raise ValueError("TRIAL_FILE_TOO_LARGE")
    sha = hashlib.sha256(data).hexdigest()
    if sha != value["sha256"]:
        raise ValueError("TRIAL_FILE_INVALID")
    if source == "pdf":
        if not data.startswith(b"%PDF-") or not (page_count := pdf_check(data)) or page_count > 30:
            raise ValueError("TRIAL_FILE_INVALID")
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if (len(names) > 200 or "xl/workbook.xml" not in names or
                        not any(name.startswith("xl/worksheets/sheet") and name.endswith(".xml") for name in names) or
                        any(".." in Path(name).parts or name.startswith("/") or
                            name.startswith("xl/externalLinks/") or name.endswith("vbaProject.bin")
                            for name in names) or
                        sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024):
                    raise ValueError("TRIAL_FILE_INVALID")
        except (ValueError, zipfile.BadZipFile) as error:
            raise ValueError("TRIAL_FILE_INVALID") from error
    return data, sha


def public_document(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "source": row["source"],
            "candidate": json.loads(row["candidate_json"]) if row["candidate_json"] else None,
            "confirmed": json.loads(row["confirmed_json"]), "revision": row["revision"],
            "file_sha256": row["file_sha256"], "file_size": row["file_size"],
            "created_ms": row["created_ms"], "updated_ms": row["updated_ms"]}


class TrialBook:
    def __init__(self, store, pdf_check):
        self.store = store
        self.pdf_check = pdf_check
        self.files = store.data_dir / "trial-files"
        self.files.mkdir(mode=0o700, exist_ok=True)
        self.files.chmod(0o700)
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trial_documents (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('purchase_order','invoice')),
                source TEXT NOT NULL CHECK(source IN ('manual','pdf','xlsx')),
                candidate_json TEXT, confirmed_json TEXT NOT NULL,
                file_sha256 TEXT, file_size INTEGER, file_suffix TEXT,
                revision INTEGER NOT NULL CHECK(revision > 0),
                created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                UNIQUE(session_id,request_key)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS trial_links (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                invoice_document_id TEXT NOT NULL REFERENCES trial_documents(id),
                invoice_item_id TEXT NOT NULL, po_document_id TEXT NOT NULL REFERENCES trial_documents(id),
                po_item_id TEXT NOT NULL, quantity TEXT, created_ms INTEGER NOT NULL,
                revoked_ms INTEGER,
                UNIQUE(session_id,invoice_document_id,invoice_item_id,po_document_id,po_item_id,revoked_ms)
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS trial_active_link_unique ON trial_links
                (session_id,invoice_document_id,invoice_item_id,po_document_id,po_item_id)
                WHERE revoked_ms IS NULL""")
            db.execute("""CREATE TABLE IF NOT EXISTS trial_products (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                label TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL, created_ms INTEGER NOT NULL
            )""")
            if "unit" not in {row[1] for row in db.execute("PRAGMA table_info(trial_products)")}:
                db.execute("ALTER TABLE trial_products ADD COLUMN unit TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS trial_product_codes (
                product_id TEXT NOT NULL REFERENCES trial_products(id),
                session_id TEXT NOT NULL REFERENCES sessions(id),
                company TEXT NOT NULL, code TEXT NOT NULL,
                PRIMARY KEY(session_id,company,code)
            )""")

    def documents(self, session_id: str) -> list[dict]:
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM trial_documents WHERE session_id=? ORDER BY updated_ms DESC,id",
                              (session_id,)).fetchall()
        return [public_document(row) for row in rows]

    def document(self, session_id: str, document_id: str) -> dict | None:
        with self.store.db() as db:
            row = db.execute("SELECT * FROM trial_documents WHERE id=? AND session_id=?",
                             (document_id, session_id)).fetchone()
        return public_document(row) if row else None

    def add_document(self, session_id: str, value: object) -> tuple[dict, bool]:
        if not isinstance(value, dict) or set(value) != {"request_key", "kind", "source", "candidate", "confirmed", "file"}:
            raise ValueError("TRIAL_FIELDS_INVALID")
        key, kind, source = value["request_key"], value["kind"], value["source"]
        if not valid_uuid(key) or kind not in KINDS or source not in SOURCES:
            raise ValueError("TRIAL_FIELDS_INVALID")
        candidate = validate_fields(value["candidate"]) if value["candidate"] is not None else None
        confirmed = validate_fields(value["confirmed"])
        data, file_sha = validate_file(source, value["file"], self.pdf_check)
        digest = fingerprint(kind, source, candidate, confirmed, file_sha)
        suffix = {"pdf": "pdf", "xlsx": "xlsx"}.get(source)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM trial_documents WHERE session_id=? AND request_key=?",
                                  (session_id, key)).fetchone()
            if existing:
                if existing["fingerprint"] != digest:
                    raise ValueError("TRIAL_IDEMPOTENCY_CONFLICT")
                return public_document(existing), False
            count = db.execute("SELECT COUNT(*) FROM trial_documents WHERE session_id=?",
                               (session_id,)).fetchone()[0]
            stored = db.execute("SELECT COALESCE(SUM(file_size),0) FROM trial_documents").fetchone()[0]
            if count >= MAX_DOCUMENTS_PER_SESSION or stored + len(data or b"") > MAX_STORED_FILE_BYTES:
                raise ValueError("TRIAL_STORAGE_LIMIT")
            document_id = str(uuid.uuid4())
            path = self.files / f"{document_id}.{suffix}" if suffix else None
            if path:
                with path.open("xb") as stream:
                    os.chmod(path, 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            timestamp = now_ms()
            try:
                db.execute("""INSERT INTO trial_documents
                    (id,session_id,request_key,fingerprint,kind,source,candidate_json,confirmed_json,
                     file_sha256,file_size,file_suffix,revision,created_ms,updated_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (document_id, session_id, key, digest, kind, source,
                     json.dumps(candidate, ensure_ascii=False) if candidate else None,
                     json.dumps(confirmed, ensure_ascii=False), file_sha, len(data) if data else None,
                     suffix, 1, timestamp, timestamp))
                saved = db.execute("SELECT * FROM trial_documents WHERE id=?", (document_id,)).fetchone()
            except Exception:
                if path:
                    path.unlink(missing_ok=True)
                raise
        return public_document(saved), True

    def file(self, session_id: str, document_id: str) -> tuple[bytes, str] | None:
        with self.store.db() as db:
            row = db.execute("SELECT file_suffix,file_sha256 FROM trial_documents WHERE id=? AND session_id=?",
                             (document_id, session_id)).fetchone()
        if not row or not row["file_suffix"]:
            return None
        suffix = row["file_suffix"]
        path = self.files / f"{document_id}.{suffix}"
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["file_sha256"]:
            raise RuntimeError("trial source file checksum mismatch")
        return data, ("application/pdf" if suffix == "pdf" else
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @staticmethod
    def _row(document: sqlite3.Row, item_id: str) -> dict | None:
        rows = json.loads(document["confirmed_json"])["rows"]
        return next((row for row in rows if row["id"] == item_id), None)

    def update_document(self, session_id: str, document_id: str, value: object) -> dict:
        if (not valid_uuid(document_id) or not isinstance(value, dict) or
                set(value) != {"revision", "confirmed"} or
                type(value["revision"]) is not int or value["revision"] < 1):
            raise ValueError("TRIAL_FIELDS_INVALID")
        confirmed = validate_fields(value["confirmed"])
        encoded = json.dumps(confirmed, ensure_ascii=False)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM trial_documents WHERE id=? AND session_id=?",
                             (document_id, session_id)).fetchone()
            if not old:
                raise LookupError("TRIAL_DOCUMENT_NOT_FOUND")
            if json.loads(old["confirmed_json"]) == confirmed:
                return public_document(old)
            if old["revision"] != value["revision"]:
                raise ValueError("TRIAL_VERSION_CONFLICT")
            ids = {row["id"]: row for row in confirmed["rows"]}
            linked = db.execute("""SELECT invoice_item_id,po_item_id,quantity,
                       invoice_document_id,po_document_id FROM trial_links
                       WHERE session_id=? AND revoked_ms IS NULL AND
                       (invoice_document_id=? OR po_document_id=?)""",
                       (session_id, document_id, document_id)).fetchall()
            for link in linked:
                item_id = (link["invoice_item_id"] if link["invoice_document_id"] == document_id
                           else link["po_item_id"])
                item = ids.get(item_id)
                if not item:
                    raise ValueError("TRIAL_LINKED_ROW")
                if link["quantity"] is not None:
                    other_doc_id = (link["po_document_id"] if link["invoice_document_id"] == document_id
                                    else link["invoice_document_id"])
                    other = db.execute("SELECT * FROM trial_documents WHERE id=? AND session_id=?",
                                       (other_doc_id, session_id)).fetchone()
                    other_item_id = (link["po_item_id"] if link["invoice_document_id"] == document_id
                                     else link["invoice_item_id"])
                    other_item = self._row(other, other_item_id) if other else None
                    if (not other_item or not item["unit"] or item["unit"] != other_item["unit"] or
                            item["quantity"] is None):
                        raise ValueError("TRIAL_LINK_CONFLICT")
            # Existing allocations must fit the edited item quantities on both sides.
            for item_id, item in ids.items():
                total = Decimal("0")
                for link in linked:
                    if link["quantity"] is not None and (
                        link["invoice_document_id"] == document_id and link["invoice_item_id"] == item_id or
                        link["po_document_id"] == document_id and link["po_item_id"] == item_id
                    ):
                        total += Decimal(link["quantity"])
                if total and (item["quantity"] is None or total > Decimal(item["quantity"])):
                    raise ValueError("TRIAL_ALLOCATION_CONFLICT")
            db.execute("UPDATE trial_documents SET confirmed_json=?,revision=revision+1,updated_ms=? "
                       "WHERE id=? AND session_id=? AND revision=?",
                       (encoded, now_ms(), document_id, session_id, old["revision"]))
            saved = db.execute("SELECT * FROM trial_documents WHERE id=?", (document_id,)).fetchone()
        return public_document(saved)

    def links(self, session_id: str) -> list[dict]:
        with self.store.db() as db:
            rows = db.execute("""SELECT l.* FROM trial_links l
                JOIN trial_documents i ON i.id=l.invoice_document_id
                JOIN trial_documents p ON p.id=l.po_document_id
                WHERE l.session_id=? AND i.session_id=? AND p.session_id=?
                  AND l.revoked_ms IS NULL ORDER BY l.created_ms,l.id""",
                (session_id, session_id, session_id)).fetchall()
        return [dict(row) for row in rows]

    def add_link(self, session_id: str, value: object) -> tuple[dict, bool]:
        names = {"invoice_document_id", "invoice_item_id", "po_document_id", "po_item_id", "quantity"}
        if not isinstance(value, dict) or set(value) != names or any(
            not valid_uuid(value[name]) for name in names - {"quantity"}
        ):
            raise ValueError("TRIAL_LINK_INVALID")
        quantity = decimal_string(value["quantity"])
        if quantity is not None and Decimal(quantity) <= 0:
            raise ValueError("TRIAL_LINK_INVALID")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            invoice = db.execute("SELECT * FROM trial_documents WHERE id=? AND session_id=? AND kind='invoice'",
                                 (value["invoice_document_id"], session_id)).fetchone()
            order = db.execute("SELECT * FROM trial_documents WHERE id=? AND session_id=? AND kind='purchase_order'",
                               (value["po_document_id"], session_id)).fetchone()
            if not invoice or not order:
                raise LookupError("TRIAL_DOCUMENT_NOT_FOUND")
            invoice_item = self._row(invoice, value["invoice_item_id"])
            order_item = self._row(order, value["po_item_id"])
            if not invoice_item or not order_item:
                raise LookupError("TRIAL_ITEM_NOT_FOUND")
            old = db.execute("""SELECT * FROM trial_links WHERE session_id=? AND
                invoice_document_id=? AND invoice_item_id=? AND po_document_id=? AND po_item_id=?
                AND revoked_ms IS NULL""", (session_id, value["invoice_document_id"],
                value["invoice_item_id"], value["po_document_id"], value["po_item_id"])).fetchone()
            if old:
                if old["quantity"] != quantity:
                    raise ValueError("TRIAL_LINK_CONFLICT")
                return dict(old), False
            if quantity is not None:
                if (not invoice_item["unit"] or invoice_item["unit"] != order_item["unit"] or
                        invoice_item["quantity"] is None or order_item["quantity"] is None):
                    raise ValueError("TRIAL_LINK_CONFLICT")
                for side, document, item in (("invoice", invoice, invoice_item),
                                             ("po", order, order_item)):
                    found = db.execute(
                        f"SELECT quantity FROM trial_links WHERE session_id=? AND {side}_document_id=? "
                        f"AND {side}_item_id=? AND revoked_ms IS NULL AND quantity IS NOT NULL",
                        (session_id, document["id"], item["id"])).fetchall()
                    allocated = sum((Decimal(row["quantity"]) for row in found), Decimal("0"))
                    if allocated + Decimal(quantity) > Decimal(item["quantity"]):
                        raise ValueError("TRIAL_ALLOCATION_CONFLICT")
            link_id = str(uuid.uuid4())
            db.execute("""INSERT INTO trial_links
                (id,session_id,invoice_document_id,invoice_item_id,po_document_id,po_item_id,
                 quantity,created_ms) VALUES (?,?,?,?,?,?,?,?)""",
                (link_id, session_id, value["invoice_document_id"], value["invoice_item_id"],
                 value["po_document_id"], value["po_item_id"], quantity, now_ms()))
            saved = db.execute("SELECT * FROM trial_links WHERE id=?", (link_id,)).fetchone()
        return dict(saved), True

    def revoke_link(self, session_id: str, link_id: str) -> None:
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("UPDATE trial_links SET revoked_ms=? WHERE id=? AND session_id=? "
                                 "AND revoked_ms IS NULL", (now_ms(), link_id, session_id)).rowcount
            if not updated:
                raise LookupError("TRIAL_LINK_NOT_FOUND")

    def products(self, session_id: str) -> list[dict]:
        with self.store.db() as db:
            products = db.execute("SELECT * FROM trial_products WHERE session_id=? ORDER BY label,id",
                                  (session_id,)).fetchall()
            aliases = db.execute("SELECT product_id,company,code FROM trial_product_codes WHERE session_id=? "
                                 "ORDER BY product_id,company,code",
                                 (session_id,)).fetchall()
        by_product: dict[str, list[dict]] = {}
        for alias in aliases:
            by_product.setdefault(alias["product_id"], []).append(
                {"company": alias["company"], "code": alias["code"]})
        return [{"id": row["id"], "label": row["label"], "unit": row["unit"],
                 "revision": row["revision"],
                 "aliases": by_product.get(row["id"], [])} for row in products]

    def add_product(self, session_id: str, value: object) -> dict:
        label, unit, clean = validate_product(value)
        product_id = str(uuid.uuid4())
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO trial_products (id,session_id,label,unit,revision,created_ms) "
                       "VALUES (?,?,?,?,?,?)", (product_id, session_id, label, unit, 1, now_ms()))
            try:
                db.executemany("INSERT INTO trial_product_codes "
                               "(product_id,session_id,company,code) VALUES (?,?,?,?)",
                               [(product_id, session_id, company, code) for company, code in clean])
            except sqlite3.IntegrityError as error:
                raise ValueError("TRIAL_ALIAS_CONFLICT") from error
        return {"id": product_id, "label": label, "unit": unit, "revision": 1,
                "aliases": [{"company": company, "code": code} for company, code in clean]}

    def update_product(self, session_id: str, product_id: str, value: object) -> dict:
        if (not valid_uuid(product_id) or not isinstance(value, dict) or
                set(value) != {"revision", "label", "unit", "aliases"} or
                type(value["revision"]) is not int or value["revision"] < 1):
            raise ValueError("TRIAL_PRODUCT_INVALID")
        label, unit, clean = validate_product({key: value[key]
                                               for key in ("label", "unit", "aliases")})
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM trial_products WHERE id=? AND session_id=?",
                             (product_id, session_id)).fetchone()
            if not old:
                raise LookupError("TRIAL_PRODUCT_NOT_FOUND")
            current = db.execute("SELECT company,code FROM trial_product_codes "
                                 "WHERE product_id=? AND session_id=? ORDER BY company,code",
                                 (product_id, session_id)).fetchall()
            if (old["label"] == label and old["unit"] == unit and
                    [(row["company"], row["code"]) for row in current] == sorted(clean)):
                return {"id": product_id, "label": label, "unit": unit,
                        "revision": old["revision"],
                        "aliases": [{"company": company, "code": code} for company, code in clean]}
            if old["revision"] != value["revision"]:
                raise ValueError("TRIAL_PRODUCT_VERSION_CONFLICT")
            db.execute("UPDATE trial_products SET label=?,unit=?,revision=revision+1 "
                       "WHERE id=? AND session_id=? AND revision=?",
                       (label, unit, product_id, session_id, old["revision"]))
            db.execute("DELETE FROM trial_product_codes WHERE product_id=? AND session_id=?",
                       (product_id, session_id))
            try:
                db.executemany("INSERT INTO trial_product_codes "
                               "(product_id,session_id,company,code) VALUES (?,?,?,?)",
                               [(product_id, session_id, company, code) for company, code in clean])
            except sqlite3.IntegrityError as error:
                raise ValueError("TRIAL_ALIAS_CONFLICT") from error
        return {"id": product_id, "label": label, "unit": unit,
                "revision": old["revision"] + 1,
                "aliases": [{"company": company, "code": code} for company, code in clean]}

    def search(self, session_id: str, filters: dict[str, str]) -> list[dict]:
        docs = self.documents(session_id)
        result = []
        for doc in docs:
            header, rows = doc["confirmed"]["header"], doc["confirmed"]["rows"]
            if filters.get("company") and filters["company"].casefold() not in header["company"].casefold():
                continue
            if filters.get("number") and filters["number"].casefold() not in header["number"].casefold():
                continue
            if filters.get("date") and filters["date"] != header["date"]:
                continue
            if filters.get("code") and not any(filters["code"].casefold() in row["code"].casefold()
                                                  for row in rows):
                continue
            result.append(doc)
        return result

    @staticmethod
    def totals(documents: list[dict]) -> dict[str, dict[str, str]]:
        totals: dict[str, dict[str, Decimal]] = {"purchase_order": {}, "invoice": {}}
        for doc in documents:
            currency = doc["confirmed"]["header"]["currency"]
            if not currency:
                continue
            for row in doc["confirmed"]["rows"]:
                if row["amount"] is not None:
                    group = totals[doc["kind"]]
                    group[currency] = group.get(currency, Decimal("0")) + Decimal(row["amount"])
        return {kind: {currency: format(amount, ".2f") for currency, amount in sorted(group.items())}
                for kind, group in totals.items()}

    @staticmethod
    def statistics(documents: list[dict]) -> dict:
        return {"totals_by_currency": TrialBook.totals(documents),
                "missing_currency_documents": sum(not doc["confirmed"]["header"]["currency"]
                                                  for doc in documents),
                "missing_amount_documents": sum(any(row["amount"] is None for row in doc["confirmed"]["rows"])
                                                for doc in documents)}

    @staticmethod
    def csv_bytes(documents: list[dict]) -> bytes:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["kind", "company", "number", "date", "currency", "code",
                         "description", "quantity", "unit", "unit_price", "amount"])
        for doc in documents:
            header = doc["confirmed"]["header"]
            for item in doc["confirmed"]["rows"]:
                values = [doc["kind"], header["company"], header["number"],
                          header["date"], header["currency"], item["code"],
                          item["description"], item["quantity"], item["unit"],
                          item["unit_price"], item["amount"]]
                writer.writerow([("'" + value if value.startswith(FORMULA_PREFIX) else value)
                                 if isinstance(value, str) else "" for value in values])
        return ("\ufeff" + output.getvalue()).encode("utf-8")
