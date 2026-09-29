"""Fixed Google Gemini REST adapter. Secrets and raw responses remain transient."""

from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
import urllib.error
import urllib.request

from .ai import AIError, AIUnknown

MODEL = "gemini-3.1-flash-lite"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BYTES = 4096
SAFE_400_REASONS = {"INVALID_ARGUMENT", "FAILED_PRECONDITION"}
MANAGEMENT_FIELDS = ("orderNo", "invoiceNo", "client", "product", "code", "qty",
                     "unitPrice", "amount", "currency", "date", "incoterms", "unit")
MANAGEMENT_SCHEMA = {"type": "OBJECT", "properties": {"items": {"type": "ARRAY", "items": {
    "type": "OBJECT", "properties": {field: {"type": "STRING"} for field in MANAGEMENT_FIELDS}
}}}, "required": ["items"]}
ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {"items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "description": {"type": "STRING"}, "quantity": {"type": "INTEGER"}
    }, "required": ["description", "quantity"]}}},
    "required": ["items"],
}


def _response_text(data: object) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise ValueError
        return text
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise AIError("AI_BAD_RESPONSE") from None


def _safe_400_reason(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read(MAX_ERROR_BYTES + 1)
        if len(raw) > MAX_ERROR_BYTES:
            return "UNCLASSIFIED"
        value = json.loads(raw)
        status = value.get("error", {}).get("status")
        return status if status in SAFE_400_REASONS else "UNCLASSIFIED"
    except (AttributeError, OSError, ValueError, TypeError):
        return "UNCLASSIFIED"


def _generate(key: str, parts: list[dict], *, timeout: float, structured: bool,
              schema: dict | None = None) -> str:
    body: dict = {"contents": [{"role": "user", "parts": parts}]}
    if structured:
        body["generationConfig"] = {"responseMimeType": "application/json", "responseSchema": schema or ITEM_SCHEMA}
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AIError("AI_AUTH_FAILED", upstream_http_status=exc.code) from None
        if exc.code == 429:
            raise AIError("AI_RATE_LIMITED", upstream_http_status=exc.code) from None
        if exc.code == 400:
            raise AIError("AI_BAD_REQUEST", upstream_http_status=400,
                          upstream_reason=_safe_400_reason(exc)) from None
        if exc.code == 404:
            raise AIError("AI_MODEL_UNAVAILABLE", upstream_http_status=exc.code) from None
        raise AIUnknown("AI_HTTP_UNKNOWN", upstream_http_status=exc.code) from None
    except http.client.HTTPException:
        # A partial or malformed upstream HTTP response can arrive after the
        # request was processed. Keep the outcome unknown and return safe JSON.
        raise AIUnknown("AI_HTTP_UNKNOWN") from None
    except (OSError, TimeoutError):
        raise AIUnknown("AI_TIMEOUT_UNKNOWN") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AIError("AI_BAD_RESPONSE")
    try:
        return _response_text(json.loads(raw))
    except (UnicodeError, json.JSONDecodeError):
        raise AIError("AI_BAD_RESPONSE") from None


class GeminiAdapter:
    def __init__(self, key: str, document_kind: str | None = None):
        self._key = key
        self.document_kind = document_kind

    def check_text(self) -> None:
        text = _generate(self._key, [{"text": "Reply with one short word: OK"}], timeout=12, structured=False)
        if not text.strip():
            raise AIError("AI_BAD_RESPONSE")

    def recognize(self, pdf_path: str, *, deadline_seconds: float) -> object:
        data = Path(pdf_path).read_bytes()
        if self.document_kind in {"purchase_order", "invoice"}:
            role = ("This is a purchase order. Find the buyer only if the document clearly identifies it."
                    if self.document_kind == "purchase_order" else
                    "This is an invoice. Find the billed customer only if the document clearly identifies it; "
                    "a Company label may identify the issuer instead.")
            prompt = (role + " Extract every line item into items with orderNo or invoiceNo, client, "
                      "product, code, qty, unitPrice, amount, currency, date, incoterms and unit. "
                      "Keep product spelling, special characters and internal whitespace. "
                      "Use decimal strings for quantities and money. Omit any field not supported "
                      "by the PDF; never infer customer, currency, date, zero, unit or shipment. "
                      "Reply in JSON only. Use an empty items array when no items are visible.")
            schema = MANAGEMENT_SCHEMA
        else:
            prompt = ("Extract line items from this PDF. Return an items array with each item's "
                      "description and nonnegative integer quantity. Do not invent missing items; "
                      "use an empty array if none are visible. Reply in JSON only.")
            schema = ITEM_SCHEMA
        text = _generate(self._key, [
            {"text": prompt},
            {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(data).decode("ascii")}},
        ], timeout=deadline_seconds, structured=True, schema=schema)
        try:
            return json.loads(text)
        except (UnicodeError, json.JSONDecodeError):
            raise AIError("AI_BAD_RESPONSE") from None
