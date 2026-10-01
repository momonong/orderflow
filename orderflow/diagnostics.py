"""Bounded, content-free operational events for the local OrderFlow service."""

from __future__ import annotations

from collections import deque
import json
import sys
import threading
import time
import uuid

EVENTS = {"http", "real_job", "key_check", "client_observation"}
APP_VERSION = "0.3.0"
BUILD_ID = "diag-20261002-01"
PHASES = {
    "received", "response_written", "response_write_unknown", "created",
    "start", "ai_start", "ai_response", "format_pass", "done", "failed",
    "unknown", "result_format_invalid", "internal_unknown", "db_committed",
    "job_fetched",
    "send", "http_received", "json_parsed", "marker_checked", "job_rendered",
    "request_unknown", "json_failed", "render_failed",
}
ROUTES = {
    "bootstrap", "management_bootstrap", "documents", "management_documents",
    "jobs", "management_jobs", "job_get", "management_job_get", "local_sources",
    "record_sets", "drafts", "key_check", "echo", "sample", "diagnostics", "other_api",
}
CLIENT_CODES = {
    "NONE", "HTTP_ERROR", "BAD_JSON_RESPONSE", "NETWORK_ERROR", "REQUEST_TIMEOUT",
    "REQUEST_ID_MISMATCH",
    "ECHO_MISMATCH", "SAMPLE_RENDER_FAILED", "RECEIPT_MISMATCH",
    "JOB_SUBMIT_FAILED", "QUERY_FAILED", "POLL_DEADLINE", "RENDER_FAILED",
    "AI_UNAVAILABLE", "AI_TIMEOUT_UNKNOWN", "AI_NOT_CONFIGURED", "AI_RATE_LIMITED",
    "AI_HTTP_ERROR", "AI_BAD_RESPONSE", "AI_AUTH_FAILED", "AI_MODEL_UNAVAILABLE",
    "AI_BAD_REQUEST", "AI_HTTP_UNKNOWN", "AI_RESULT_UNKNOWN", "AI_FAILURE",
    "KEY_REQUIRED", "JOB_LIMIT", "RESULT_FORMAT_INVALID", "INTERNAL_UNKNOWN",
}
SAFE_FIELDS = {
    "route", "trace_id", "request_id", "job_id", "http_status",
    "app_http_status", "upstream_http_status", "upstream_reason", "code",
    "duration_ms", "response_type", "marker", "source", "created",
    "transport_class",
    "format_reason", "item_index", "field", "actual_type", "length_bucket",
}
FORMAT_REASONS = {"ROOT_TYPE", "ITEMS_TYPE", "ITEMS_COUNT", "ITEM_TYPE",
                  "FIELD_TYPE", "FIELD_LENGTH", "FIELD_NUL", "FIELD_RANGE"}
FORMAT_FIELDS = {"description", "quantity", "orderNo", "invoiceNo", "client",
                 "product", "code", "qty", "unitPrice", "amount", "currency",
                 "date", "incoterms", "unit"}
FORMAT_TYPES = {"null", "boolean", "string", "object", "array", "number", "other"}
LENGTH_BUCKETS = {"201_500", "501_1000", "OVER_1000"}
REASONS = {"INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNCLASSIFIED"}
TYPES = {"JSON", "HTML", "TEXT", "OTHER", "MISSING"}
MARKERS = {"APP", "MISSING"}
SOURCES = {"server", "client"}
TRANSPORT_CLASSES = {"DNS", "TLS", "CONNECT_REFUSED", "TIMEOUT_UNKNOWN",
                     "IO_UNKNOWN", "HTTP_TRUNCATED", "HTTP_MALFORMED"}
_WINDOW = 60.0
_MAX_EVENTS = 600
_times: dict[str, deque[float]] = {name: deque() for name in
                                 ("routine", "client", "critical", "terminal")}
_drop_counts = {name: 0 for name in _times}
_drop_started = 0.0
_lock = threading.Lock()


def valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _safe_field(key: str, value: object) -> bool:
    if key in {"trace_id", "request_id", "job_id"}:
        return valid_uuid(value)
    if key == "route":
        return isinstance(value, str) and value in ROUTES
    if key in {"http_status", "app_http_status", "upstream_http_status"}:
        return type(value) is int and 100 <= value <= 599
    if key == "duration_ms":
        return type(value) is int and 0 <= value <= 120_000
    if key == "upstream_reason":
        return isinstance(value, str) and value in REASONS
    if key == "response_type":
        return isinstance(value, str) and value in TYPES
    if key == "marker":
        return isinstance(value, str) and value in MARKERS
    if key == "source":
        return isinstance(value, str) and value in SOURCES
    if key == "transport_class":
        return isinstance(value, str) and value in TRANSPORT_CLASSES
    if key == "code":
        return isinstance(value, str) and value in CLIENT_CODES
    if key == "format_reason":
        return isinstance(value, str) and value in FORMAT_REASONS
    if key == "item_index":
        return type(value) is int and 0 <= value < 100
    if key == "field":
        return isinstance(value, str) and value in FORMAT_FIELDS
    if key == "actual_type":
        return isinstance(value, str) and value in FORMAT_TYPES
    if key == "length_bucket":
        return isinstance(value, str) and value in LENGTH_BUCKETS
    if key == "created":
        return type(value) is bool
    return False


def audit_event(event: str, reference: str, phase: str,
                metadata: dict[str, object] | None = None) -> None:
    """Drop unreviewed labels or fields before they can reach the journal."""
    if event not in EVENTS or phase not in PHASES:
        return
    if event == "client_observation":
        category = "client"
    elif event == "real_job" and phase in {"db_committed", "done", "failed", "unknown",
                                           "result_format_invalid", "internal_unknown"}:
        category = "terminal"
    elif (event == "http" and phase == "response_write_unknown"
          or event == "key_check" and phase in {"failed", "internal_unknown"}):
        category = "terminal"
    elif (event in {"real_job", "key_check"} or event == "http" and
          (phase == "db_committed" or (metadata or {}).get("route") in {"jobs", "management_jobs"})):
        category = "critical"
    else:
        category = "routine"
    limits = {"terminal": max(1, _MAX_EVENTS // 3),
              "critical": max(1, _MAX_EVENTS // 5), "client": max(1, _MAX_EVENTS // 10)}
    limits["routine"] = max(1, _MAX_EVENTS - sum(limits.values()))
    now = time.monotonic()
    drop_record = None
    dropped = False
    with _lock:
        global _drop_started
        if now - _drop_started >= _WINDOW:
            for name in _drop_counts:
                _drop_counts[name] = 0
            _drop_started = now
        times = _times[category]
        while times and times[0] <= now - _WINDOW:
            times.popleft()
        if len(times) >= limits[category]:
            dropped = True
            _drop_counts[category] += 1
            count = _drop_counts[category]
            if count <= 1024 and count & (count - 1) == 0:
                drop_record = {"event": "audit_limit", "phase": "dropped",
                               "category": category, "dropped_count": count,
                               "version": APP_VERSION, "build_id": BUILD_ID,
                               "at_ms": int(time.time() * 1000)}
        else:
            times.append(now)
    if drop_record:
        _emit(drop_record)
    if dropped:
        return
    fields = {key: value for key, value in (metadata or {}).items()
              if key in SAFE_FIELDS and _safe_field(key, value)}
    record = {"event": event, "id": reference if valid_uuid(reference) else "invalid",
              "phase": phase, "version": APP_VERSION, "build_id": BUILD_ID,
              "at_ms": int(time.time() * 1000),
              **fields}
    _emit(record)


def _emit(record: dict[str, object]) -> None:
    try:
        print("orderflow_audit " + json.dumps(record, separators=(",", ":")),
              file=sys.stderr, flush=True)
    except OSError:
        # Journal failure must never change business state.
        pass


def client_report(value: object) -> list[dict[str, object]] | None:
    """Accept only a tiny fixed vocabulary, never free-form browser data."""
    if not isinstance(value, dict) or set(value) != {"events"}:
        return None
    events = value["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 12:
        return None
    allowed = {"phase", "route", "trace_id", "request_id", "http_status",
               "duration_ms", "response_type", "marker", "code"}
    result = []
    for item in events:
        if (not isinstance(item, dict) or not {"phase", "route", "trace_id"} <= set(item)
                or set(item) - allowed or not isinstance(item["phase"], str) or item["phase"] not in
                {"send", "http_received", "json_parsed", "marker_checked",
                 "job_rendered", "request_unknown", "json_failed", "render_failed"}
                or any(not _safe_field(key, field) for key, field in item.items()
                       if key != "phase")):
            return None
        result.append(item)
    return result
