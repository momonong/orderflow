"""Bounded queue client for the pinned API quota Broker contract.

Only protected credentials are read; token values and upstream error text never
become configuration, diagnostics, or persisted completion metadata.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from urllib.parse import urlsplit

from .gemini import recognition_prompt

MIB = 1024 * 1024
KEY_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,120}\Z")
MODEL_PATTERN = re.compile(r"gemini-[A-Za-z0-9._-]{1,96}\Z")
COMPLETED_STATES = frozenset({"completed", "completed_usage_unknown"})
SAFE_CODES = frozenset({
    "BROKER_NOT_CONFIGURED", "BROKER_CONFIG_INVALID", "BROKER_CONFIG_CHANGED", "BROKER_INPUT_BOUND_REQUIRED",
    "BROKER_PDF_TOO_LARGE", "BROKER_REQUEST_TOO_LARGE", "BROKER_RESULT_TOO_LARGE",
    "BROKER_BAD_REQUEST", "BROKER_BAD_RESPONSE", "BROKER_HTTP_UNKNOWN",
    "BROKER_UNAUTHORIZED", "BROKER_CONFLICT", "BROKER_REJECTED", "BROKER_NOT_FOUND",
    "BROKER_RESULT_EXPIRED", "BROKER_PENDING", "BROKER_RESULT_INCOMPLETE", "BROKER_WORK_BUSY",
    "BROKER_BUSY_RESUME_NOT_ALLOWED",
})


class BrokerError(Exception):
    def __init__(self, code: str):
        self.code = code if code in SAFE_CODES else "BROKER_BAD_RESPONSE"
        super().__init__(self.code)


def _key(value: object) -> str:
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value):
        raise BrokerError("BROKER_BAD_REQUEST")
    return value


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BrokerError("BROKER_BAD_REQUEST") from None


def _json_load(raw: bytes | str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def invalid_constant(_value):
        raise ValueError
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BrokerError("BROKER_BAD_RESPONSE") from None


@dataclass(frozen=True)
class BrokerConfig:
    token_file: Path | None = None
    endpoint: str = "http://127.0.0.1:18084"
    model: str = "gemini-3.5-flash-lite"
    max_pdf_bytes: int = MIB
    max_request_bytes: int = 2 * MIB
    max_result_bytes: int = 4 * MIB
    max_output_tokens: int = 8192
    input_bound_verified: bool = False
    input_bound_evidence: str | None = None
    input_bound_expires_at: str | None = None
    timeout_seconds: float = 12.0

    def __post_init__(self):
        try:
            endpoint = urlsplit(self.endpoint)
            valid_endpoint = (endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1"
                              and endpoint.netloc == f"127.0.0.1:{endpoint.port}"
                              and 1 <= endpoint.port <= 65535 and not endpoint.path
                              and not endpoint.query and not endpoint.fragment)
        except (ValueError, TypeError, AttributeError):
            valid_endpoint = False
        bounds = ((self.max_pdf_bytes, 8 * MIB), (self.max_request_bytes, 16 * MIB),
                  (self.max_result_bytes, 32 * MIB), (self.max_output_tokens, 8192))
        if (not valid_endpoint or not isinstance(self.model, str)
                or not MODEL_PATTERN.fullmatch(self.model)
                or any(type(value) is not int or not 1 <= value <= cap for value, cap in bounds)
                or type(self.input_bound_verified) is not bool
                or type(self.timeout_seconds) not in {int, float}
                or not 0 < self.timeout_seconds <= 60):
            raise BrokerError("BROKER_CONFIG_INVALID")
        if self.token_file is not None:
            if not isinstance(self.token_file, (Path, str)):
                raise BrokerError("BROKER_CONFIG_INVALID")
            object.__setattr__(self, "token_file", Path(self.token_file))
            if not self.token_file.is_absolute():
                raise BrokerError("BROKER_CONFIG_INVALID")
        if self.input_bound_evidence is not None and (
                not isinstance(self.input_bound_evidence, str)
                or not KEY_PATTERN.fullmatch(self.input_bound_evidence)):
            raise BrokerError("BROKER_CONFIG_INVALID")
        if self.input_bound_expires_at is not None:
            self._bound_expiry()

    def _bound_expiry(self) -> datetime | None:
        if self.input_bound_expires_at is None:
            return None
        try:
            expiry = datetime.fromisoformat(self.input_bound_expires_at)
            if expiry.tzinfo is None:
                raise ValueError
            return expiry
        except (ValueError, TypeError):
            raise BrokerError("BROKER_CONFIG_INVALID") from None

    def bound_ready(self) -> bool:
        expiry = self._bound_expiry()
        return bool(self.input_bound_verified and self.input_bound_evidence and expiry
                    and expiry > datetime.now(timezone.utc))

    def public_status(self) -> dict:
        code = None
        if not self.bound_ready():
            code = "BROKER_INPUT_BOUND_REQUIRED"
        else:
            try:
                _read_token(self.token_file)
            except BrokerError as exc:
                code = exc.code
        return {"mode": "broker", "model": self.model, "ready": code is None,
                "max_pdf_bytes": self.max_pdf_bytes, "max_output_tokens": self.max_output_tokens,
                "unavailable_code": code}

    def snapshot(self) -> dict:
        value = {field.name: getattr(self, field.name) for field in fields(self)}
        value["token_file"] = str(self.token_file) if self.token_file is not None else None
        return value

    @classmethod
    def from_snapshot(cls, value: object) -> BrokerConfig:
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise BrokerError("BROKER_CONFIG_INVALID")
        try:
            return cls(**value)
        except (TypeError, ValueError):
            raise BrokerError("BROKER_CONFIG_INVALID") from None


def _read_token(path: Path | None) -> str:
    if path is None:
        raise BrokerError("BROKER_NOT_CONFIGURED")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise BrokerError("BROKER_NOT_CONFIGURED")
        with os.fdopen(descriptor, "rb") as credential:
            descriptor = None
            raw = credential.read(4097)
        token = raw.decode("ascii").strip()
        if not token or len(raw) > 4096 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise BrokerError("BROKER_NOT_CONFIGURED")
        return token
    except (OSError, UnicodeError, ValueError, TypeError):
        raise BrokerError("BROKER_NOT_CONFIGURED") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _lower_schema(value: object) -> object:
    if isinstance(value, dict):
        return {key: (item.lower() if key == "type" and isinstance(item, str)
                      else _lower_schema(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_lower_schema(item) for item in value]
    return value


def make_task(config: BrokerConfig, pdf_bytes: bytes, document_kind: str | None,
              request_key: str) -> dict:
    _key(request_key)
    if document_kind is not None and not isinstance(document_kind, str):
        raise BrokerError("BROKER_BAD_REQUEST")
    if not config.bound_ready():
        raise BrokerError("BROKER_INPUT_BOUND_REQUIRED")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise BrokerError("BROKER_BAD_REQUEST")
    if len(pdf_bytes) > config.max_pdf_bytes:
        raise BrokerError("BROKER_PDF_TOO_LARGE")
    prompt, schema = recognition_prompt(document_kind)
    task = {"request_key": request_key, "capability": "text_generation", "provider": "google",
            "model": config.model, "input": {"messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "document", "mime_type": "application/pdf",
                 "data": base64.b64encode(pdf_bytes).decode("ascii")},
            ]}]}, "options": {"json_schema": _lower_schema(schema)},
            "requirements": {"features": ["document_input", "structured_output"]},
            "max_output_tokens": config.max_output_tokens, "max_attempts": 1,
            "wait_policy": "wait"}
    if len(_json_bytes(task)) > config.max_request_bytes:
        raise BrokerError("BROKER_REQUEST_TOO_LARGE")
    return task


class BrokerClient:
    def __init__(self, config: BrokerConfig):
        self.config = config

    def submit(self, task: dict) -> dict:
        if not isinstance(task, dict):
            raise BrokerError("BROKER_BAD_REQUEST")
        key = _key(task.get("request_key"))
        if not self.config.bound_ready():
            raise BrokerError("BROKER_INPUT_BOUND_REQUIRED")
        result = self._request("POST", "/v1/queue", task)
        if result.get("request_key") != key:
            raise BrokerError("BROKER_BAD_RESPONSE")
        return result

    def status(self, request_key: str) -> dict:
        key = _key(request_key)
        result = self._request("GET", f"/v1/queue/{key}")
        if result.get("request_key") != key:
            raise BrokerError("BROKER_BAD_RESPONSE")
        return result

    def result(self, request_key: str) -> dict:
        return self._request("GET", f"/v1/queue/{_key(request_key)}/result")

    def _request(self, method: str, path: str, task: dict | None = None) -> dict:
        body = _json_bytes(task) if task is not None else None
        if body is not None and len(body) > self.config.max_request_bytes:
            raise BrokerError("BROKER_REQUEST_TOO_LARGE")
        token = _read_token(self.config.token_file)
        endpoint = urlsplit(self.config.endpoint)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port,
                                                timeout=self.config.timeout_seconds)
        deadline = time.monotonic() + self.config.timeout_seconds
        deadline_timer = None
        try:
            # HTTPConnection has no proxy, redirect, or automatic request retry.
            connection.connect()
            wire_socket = connection.sock
            def stop_wire():
                # A socket timeout alone can be renewed by trickling HTTP headers.
                try:
                    wire_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            remaining = max(0.001, deadline - time.monotonic())
            wire_socket.settimeout(remaining)
            deadline_timer = threading.Timer(remaining, stop_wire)
            deadline_timer.daemon = True
            deadline_timer.start()
            send_failed = False
            try:
                connection.request(method, path, body=body, headers={
                    "Authorization": "Bearer " + token, "Content-Type": "application/json",
                    "Accept": "application/json", "Connection": "close",
                })
            except OSError:
                # A large body may hit BrokenPipe after the server has already
                # returned a pre-body busy rejection. Read that same socket once;
                # only the complete exact marker can prove zero acceptance.
                send_failed = True
            if time.monotonic() >= deadline:
                raise TimeoutError
            wire_socket.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            raw = bytearray()
            # The busy contract closes the connection. Read its actual bytes
            # through EOF instead of letting Content-Length hide an extra tail.
            closed_error = response.status == 503 and response.getheader("Connection", "").lower() == "close"
            reader = response.fp if closed_error else response
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                wire_socket.settimeout(remaining)
                part = reader.read1(min(65536, self.config.max_result_bytes + 1 - len(raw)))
                raw.extend(part)
                if len(raw) > self.config.max_result_bytes:
                    raise BrokerError("BROKER_RESULT_TOO_LARGE")
                if not part:
                    if not closed_error and response.length not in (None, 0):
                        raise BrokerError("BROKER_HTTP_UNKNOWN")
                    break
                if not closed_error and response.isclosed():
                    break
            if not closed_error and response.length not in (None, 0):
                raise BrokerError("BROKER_HTTP_UNKNOWN")
            if send_failed:
                try:
                    self._http_error(response.status, bytes(raw), response.getheader("Content-Type", ""),
                                     response.getheader("Content-Length"), response.getheader("Transfer-Encoding"),
                                     closed_response=closed_error)
                except BrokerError as exc:
                    if exc.code == "BROKER_WORK_BUSY":
                        raise
                raise BrokerError("BROKER_HTTP_UNKNOWN")
            if response.status != 200:
                self._http_error(response.status, bytes(raw), response.getheader("Content-Type", ""),
                                 response.getheader("Content-Length"), response.getheader("Transfer-Encoding"),
                                 closed_response=closed_error)
            value = _json_load(bytes(raw))
            if not isinstance(value, dict):
                raise BrokerError("BROKER_BAD_RESPONSE")
            return value
        except BrokerError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise BrokerError("BROKER_HTTP_UNKNOWN") from None
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            connection.close()

    @staticmethod
    def _http_error(status: int, raw: bytes, content_type: str,
                    content_length: str | None = None, transfer_encoding: str | None = None,
                    *, closed_response: bool = False) -> None:
        codes = {401: "BROKER_UNAUTHORIZED", 403: "BROKER_UNAUTHORIZED",
                 409: "BROKER_CONFLICT", 400: "BROKER_REJECTED", 404: "BROKER_NOT_FOUND"}
        if status in codes:
            raise BrokerError(codes[status])
        if status == 503:
            try:
                value = _json_load(raw)
                error = value.get("error") if isinstance(value, dict) else None
            except BrokerError:
                value = None
                error = None
            # This exact pre-body rejection alone proves zero queue acceptance.
            # A proxy page, extra fields, duplicate keys or truncated response
            # never grants another POST, even if it contains the same wording.
            if (closed_response and content_type.split(";", 1)[0].strip().lower() == "application/json"
                    and isinstance(content_length, str) and re.fullmatch(r"[0-9]{1,10}", content_length)
                    and int(content_length) == len(raw) and transfer_encoding is None
                    and isinstance(value, dict) and set(value) == {"error"} and error == "work_busy"):
                raise BrokerError("BROKER_WORK_BUSY")
            if error == "queue_result_pending":
                raise BrokerError("BROKER_PENDING")
            if error == "queue_content_expired":
                raise BrokerError("BROKER_RESULT_EXPIRED")
            if isinstance(error, str) and error in {"queue_disabled", "unavailable", "no_route", "quota_exhausted"}:
                raise BrokerError("BROKER_REJECTED")
        raise BrokerError("BROKER_HTTP_UNKNOWN")


def validate_outcome(outcome: object, execution_key: str, gateway_state: str) -> tuple[object, dict]:
    _key(execution_key)
    if (not isinstance(outcome, dict) or outcome.get("request_key") != execution_key
            or not isinstance(gateway_state, str) or gateway_state not in COMPLETED_STATES
            or outcome.get("state") != gateway_state):
        raise BrokerError("BROKER_BAD_RESPONSE")
    if outcome.get("finish_reason") != "stop" or outcome.get("response_truncated") is not False:
        raise BrokerError("BROKER_RESULT_INCOMPLETE")
    result = outcome.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise BrokerError("BROKER_BAD_RESPONSE")
    parsed = _json_load(result["text"])
    metadata = {"gateway_state": gateway_state, "finish_reason": "stop", "response_truncated": False,
                "usage_unknown": gateway_state == "completed_usage_unknown"}
    return parsed, metadata
