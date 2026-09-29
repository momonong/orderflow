"""Validation for human-confirmed purchase-order and invoice rows."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import uuid

KINDS = {"purchase_order", "invoice"}
TEXT_LIMITS = {
    "orderNo": 100, "invoiceNo": 100, "client": 200, "product": 200,
    "code": 100, "currency": 3, "date": 10, "incoterms": 60, "unit": 32,
}
FIELDS = ("id", "orderNo", "invoiceNo", "client", "product", "code", "qty",
          "unitPrice", "amount", "currency", "date", "incoterms", "unit",
          "status", "linked_order_row_id", "deleted")
DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SLASH_DATE_RE = re.compile(r"^([0-9]{4})/([0-9]{1,2})/([0-9]{1,2})$")
MAX_ROW_ERRORS = 20


def is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def decimal_string(value: object, *, positive: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise ValueError("RECORD_ROWS_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("RECORD_ROWS_INVALID") from None
    if positive and number <= 0:
        raise ValueError("RECORD_ROWS_INVALID")
    return value


class RecordRowsError(ValueError):
    """A bounded, value-free list of human-editable row errors."""

    def __init__(self, errors: list[dict]):
        super().__init__("RECORD_ROWS_INVALID")
        self.errors = errors[:MAX_ROW_ERRORS]


def canonical_record_date(value: str) -> str:
    """Accept exact ISO or explicit year/month/day; reject ambiguous dates."""
    if DATE_RE.fullmatch(value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("DATE_INVALID") from None
        if parsed.isoformat() != value:
            raise ValueError("DATE_FORMAT")
        return value
    match = SLASH_DATE_RE.fullmatch(value)
    if not match:
        raise ValueError("DATE_FORMAT")
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        raise ValueError("DATE_INVALID") from None


def validate_rows(value: object, kind: str) -> list[dict]:
    if kind not in KINDS or not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise RecordRowsError([{"row_id": None, "index": None, "field": "rows", "code": "ROW_COUNT"}])
    result: list[dict] = []
    errors: list[dict] = []
    ids: set[str] = set()

    def issue(index: int, raw: dict, field: str, code: str) -> None:
        if len(errors) < MAX_ROW_ERRORS:
            errors.append({"row_id": raw["id"] if is_uuid(raw.get("id")) else None,
                           "index": index, "field": field, "code": code})

    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or set(raw) != set(FIELDS) or not is_uuid(raw["id"]):
            issue(index, raw if isinstance(raw, dict) else {}, "row", "ROW_FORMAT")
            continue
        if raw["id"] in ids:
            issue(index, raw, "id", "ROW_ID_DUPLICATE")
        ids.add(raw["id"])
        if type(raw["deleted"]) is not bool:
            issue(index, raw, "deleted", "BOOLEAN_REQUIRED")
        row = {"id": raw["id"], "deleted": raw["deleted"]}
        for field, limit in TEXT_LIMITS.items():
            item = raw[field]
            row[field] = item
            if item is None:
                continue
            if not isinstance(item, str) or not item.strip() or "\x00" in item:
                issue(index, raw, field, "TEXT_INVALID")
            elif len(item) > limit:
                issue(index, raw, field, "TEXT_TOO_LONG")
        if isinstance(row["currency"], str) and row["currency"].strip() and len(row["currency"]) <= 3:
            if not CURRENCY_RE.fullmatch(row["currency"]):
                issue(index, raw, "currency", "CURRENCY_FORMAT")
        if isinstance(row["date"], str) and row["date"].strip() and len(row["date"]) <= 10:
            try:
                row["date"] = canonical_record_date(row["date"])
            except ValueError as exc:
                issue(index, raw, "date", str(exc))
        for field in ("qty", "unitPrice", "amount"):
            item = raw[field]
            row[field] = item
            if item is None:
                continue
            if not isinstance(item, str) or not DECIMAL_RE.fullmatch(item):
                issue(index, raw, field, "DECIMAL_FORMAT")
            elif field == "qty" and Decimal(item) <= 0:
                issue(index, raw, field, "POSITIVE_REQUIRED")
        if kind == "purchase_order":
            if row["invoiceNo"] is not None:
                issue(index, raw, "invoiceNo", "FIELD_NOT_ALLOWED")
            if raw["linked_order_row_id"] is not None:
                issue(index, raw, "linked_order_row_id", "FIELD_NOT_ALLOWED")
            if not isinstance(raw["status"], str) or raw["status"] not in {"確認中", "確定"}:
                issue(index, raw, "status", "STATUS_INVALID")
            row["status"] = raw["status"]
            row["linked_order_row_id"] = None
        else:
            if row["orderNo"] is not None:
                issue(index, raw, "orderNo", "FIELD_NOT_ALLOWED")
            if raw["status"] is not None:
                issue(index, raw, "status", "FIELD_NOT_ALLOWED")
            link = raw["linked_order_row_id"]
            if link is not None and not is_uuid(link):
                issue(index, raw, "linked_order_row_id", "ROW_ID_INVALID")
            row["status"] = None
            row["linked_order_row_id"] = link
        if not row["deleted"] and row["product"] is None and row["code"] is None:
            issue(index, raw, "product", "PRODUCT_OR_CODE_REQUIRED")
        result.append({field: row[field] for field in FIELDS})
    if errors:
        raise RecordRowsError(errors)
    return result


def comparable_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None
