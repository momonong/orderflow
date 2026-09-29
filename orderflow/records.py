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


def nullable_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError("RECORD_ROWS_INVALID")
    return value


def validate_rows(value: object, kind: str) -> list[dict]:
    if kind not in KINDS or not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("RECORD_ROWS_INVALID")
    result: list[dict] = []
    ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != set(FIELDS) or not is_uuid(raw["id"]):
            raise ValueError("RECORD_ROWS_INVALID")
        if raw["id"] in ids or type(raw["deleted"]) is not bool:
            raise ValueError("RECORD_ROWS_INVALID")
        ids.add(raw["id"])
        row = {"id": raw["id"], "deleted": raw["deleted"]}
        for field, limit in TEXT_LIMITS.items():
            row[field] = nullable_text(raw[field], limit)
        if row["currency"] is not None and not CURRENCY_RE.fullmatch(row["currency"]):
            raise ValueError("RECORD_ROWS_INVALID")
        if row["date"] is not None:
            if not DATE_RE.fullmatch(row["date"]):
                raise ValueError("RECORD_ROWS_INVALID")
            try:
                if date.fromisoformat(row["date"]).isoformat() != row["date"]:
                    raise ValueError
            except ValueError:
                raise ValueError("RECORD_ROWS_INVALID") from None
        for field in ("qty", "unitPrice", "amount"):
            row[field] = decimal_string(raw[field], positive=field == "qty")
        if kind == "purchase_order":
            if row["invoiceNo"] is not None or raw["linked_order_row_id"] is not None:
                raise ValueError("RECORD_ROWS_INVALID")
            if raw["status"] not in {"確認中", "確定"}:
                raise ValueError("RECORD_ROWS_INVALID")
            row["status"] = raw["status"]
            row["linked_order_row_id"] = None
        else:
            if row["orderNo"] is not None or raw["status"] is not None:
                raise ValueError("RECORD_ROWS_INVALID")
            link = raw["linked_order_row_id"]
            if link is not None and not is_uuid(link):
                raise ValueError("RECORD_ROWS_INVALID")
            row["status"] = None
            row["linked_order_row_id"] = link
        if not row["deleted"] and row["product"] is None and row["code"] is None:
            raise ValueError("RECORD_ROWS_INVALID")
        result.append({field: row[field] for field in FIELDS})
    return result


def comparable_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None
