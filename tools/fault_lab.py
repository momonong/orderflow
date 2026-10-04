"""Loopback-only, synthetic HTTP fault lab for the real OrderFlow application.

This is a local development tool, not a production entry point.  No request is
ever forwarded to an address supplied by a client or to the real Google API.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import secrets
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import bcrypt
from pypdf import PdfWriter

from orderflow import app, gemini


SCENARIOS = {
    "normal": (None, None),
    "browser_google_blocked": (None, None),
    "post_html_403": ("post_html_403", None),
    "post_html_502": ("post_html_502", None),
    "post_html_200": ("post_html_200", None),
    "post_missing_marker": ("post_missing_marker", None),
    "post_delay": ("post_delay", None),
    "post_disconnect": ("post_disconnect", None),
    "post_truncate": ("post_truncate", None),
    "receipt_lost_after_commit": ("receipt_lost_after_commit", None),
    "result_lost_after_commit": ("result_lost_after_commit", None),
    "diagnostics_blocked": ("diagnostics_blocked", None),
    "ai_401": (None, "401"),
    "ai_403": (None, "403"),
    "ai_429": (None, "429"),
    "ai_503": (None, "503"),
    "ai_timeout": (None, "timeout"),
    "ai_invalid": (None, "invalid"),
}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade"}


class AuditCapture:
    """Read records printed by the unmodified production audit formatter."""

    def __init__(self):
        self.events: list[dict] = []
        self.pending = ""
        self.lock = threading.Lock()
        self.other_lines = 0

    def write(self, value: str) -> int:
        with self.lock:
            self.pending += value
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line.startswith("orderflow_audit "):
                    try:
                        self.events.append(json.loads(line[len("orderflow_audit "):]))
                    except ValueError:
                        self.other_lines += 1
                elif line:
                    self.other_lines += 1
        return len(value)

    def flush(self) -> None:
        pass


class FakeGoogle(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fault: str | None):
        super().__init__(("127.0.0.1", 0), FakeGoogleHandler)
        self.fault = fault
        self.call_count = 0
        self.lock = threading.Lock()


class FakeGoogleHandler(BaseHTTPRequestHandler):
    server: FakeGoogle

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        # Bound the read; neither the PDF nor the request body is retained.
        if (self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}" or
                self.headers.get("Origin") is not None or
                self.headers.get("Sec-Fetch-Site") is not None):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if self.path != "/fake-google/generate" or not 0 < length < 12 * 1024 * 1024:
            self.send_error(404)
            return
        self.rfile.read(length)
        with self.server.lock:
            self.server.call_count += 1
        fault = self.server.fault
        if fault == "timeout":
            # The real job grants GeminiAdapter 25 seconds.  Exceed that bound
            # to exercise its actual socket timeout instead of an early close.
            time.sleep(26)
            return
        if fault in {"401", "403", "429", "503"}:
            payload = b'{"error":{"status":"UNCLASSIFIED"}}'
            self.send_response(int(fault))
        else:
            value = "not-json" if fault == "invalid" else json.dumps({"items": [
                {"description": "Synthetic item", "quantity": 2}]})
            payload = json.dumps({"candidates": [{"content": {"parts": [
                {"text": value}]}}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass


class Proxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, backend_port: int, scenario: str, password: str, key: str):
        super().__init__(("127.0.0.1", 0), ProxyHandler)
        self.backend_port = backend_port
        self.scenario = scenario
        self.fault = SCENARIOS[scenario][0]
        self.password = password
        self.key = key
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.trigger_count = 0

    def record(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)

    def claim_fault(self, method: str, path: str) -> str | None:
        fault = self.fault
        post = method == "POST" and path == "/orderflow/api/jobs"
        get = method == "GET" and path.startswith("/orderflow/api/jobs/")
        diagnostic = method == "POST" and path == "/orderflow/api/diagnostics"
        target = ((fault or "").startswith("post_") or fault == "receipt_lost_after_commit") and post
        target |= fault == "result_lost_after_commit" and get
        target |= fault == "diagnostics_blocked" and diagnostic
        if not target:
            return None
        with self.lock:
            if self.trigger_count:
                return None
            self.trigger_count += 1
        return fault


class ProxyHandler(BaseHTTPRequestHandler):
    server: Proxy
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def do_DELETE(self) -> None:
        self.forward()

    def reply(self, code: int, body: bytes, content_type: str, marker: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if marker:
            self.send_header("X-Orderflow-Origin", "app")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def backend(self, method: str, path: str, body: bytes, headers: dict[str, str]):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.backend_port, timeout=30)
        conn.request(method, path, body=body, headers={"Host": "127.0.0.1", **headers})
        response = conn.getresponse()
        data = response.read()
        status, response_headers = response.status, response.getheaders()
        conn.close()
        return status, response_headers, data

    def create_session(self) -> None:
        payload = json.dumps({"password": self.server.password}).encode()
        existing_cookie = self.headers.get("Cookie", "")
        status, headers, _ = self.backend("POST", "/orderflow/api/login", payload,
                                          {"Content-Type": "application/json",
                                           "X-Orderflow-Request": "1",
                                           "Cookie": existing_cookie})
        if status != 200:
            self.reply(502, b"lab setup failed", "text/plain")
            return
        cookie = next((value.split(";", 1)[0] for name, value in headers
                       if name.lower() == "set-cookie"), None)
        if not cookie:
            self.reply(502, b"lab setup failed", "text/plain")
            return
        key_body = json.dumps({"key": self.server.key}).encode()
        key_status, _, _ = self.backend("POST", "/orderflow/api/key", key_body,
                                         {"Content-Type": "application/json", "Cookie": cookie,
                                          "X-Orderflow-Request": "1"})
        if key_status != 200:
            self.reply(502, b"lab setup failed", "text/plain")
            return
        self.send_response(303)
        self.send_header("Location", "/orderflow/test/")
        self.send_header("Set-Cookie", f"{cookie}; Path=/orderflow/; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def forward(self) -> None:
        path = urlsplit(self.path).path
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host:
            self.reply(403, b"host rejected", "text/plain")
            return
        expected_origin = f"http://{expected_host}"
        if self.command in {"POST", "DELETE"} and self.headers.get("Origin") != expected_origin:
            self.reply(403, b"origin rejected", "text/plain")
            return
        if self.command == "GET" and path == "/__lab/session":
            if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") not in {None, "none"}:
                self.reply(403, b"session setup rejected", "text/plain")
                return
            self.create_session()
            return
        if self.command == "GET" and path == "/__lab/google-browser-probe":
            self.reply(403 if self.server.scenario == "browser_google_blocked" else 200,
                       b"synthetic browser probe", "text/plain")
            return
        if not path.startswith("/orderflow/") or self.path != path:
            self.reply(404, b"not found", "text/plain")
            return
        if self.headers.get("Transfer-Encoding"):
            self.reply(400, b"unsupported transfer encoding", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 9 * 1024 * 1024:
            self.reply(413, b"request too large", "text/plain")
            return
        body = self.rfile.read(length) if length else b""
        fault = self.server.claim_fault(self.command, path)
        event = {"method": self.command, "route": path, "request_id":
                 self.headers.get("X-Orderflow-Request-Id"), "trace_id":
                 self.headers.get("X-Orderflow-Trace-Id"), "injection": fault,
                 "backend_dispatched": False, "client_delivery": "pending"}
        if fault in {"post_html_403", "post_html_502", "post_html_200", "diagnostics_blocked"}:
            code = {"post_html_403": 403, "post_html_502": 502,
                    "post_html_200": 200, "diagnostics_blocked": 403}[fault]
            self.reply(code, b"<html><body>synthetic proxy fault</body></html>", "text/html")
            event.update(client_delivery="html", client_status=code)
            self.server.record(event)
            return
        if fault == "post_disconnect":
            event["client_delivery"] = "connection_closed_before_dispatch"
            self.server.record(event)
            self.close_connection = True
            return
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in HOP_HEADERS | {"host", "content-length"}}
        try:
            status, response_headers, data = self.backend(self.command, path, body, headers)
        except (OSError, http.client.HTTPException):
            event.update(backend_dispatched=True, backend_response="unknown",
                         client_delivery="backend_connection_failed")
            self.server.record(event)
            self.reply(502, b"backend unavailable", "text/plain")
            return
        event.update(backend_dispatched=True, backend_status=status)
        if path == "/orderflow/test/" and status == 200:
            # A visible lab label prevents the production page's Google wording
            # from being mistaken for a real external call in this experiment.
            banner = ('<aside role="note"><strong>OrderFlow 本機故障實驗：</strong>'
                      '僅使用合成 PDF 與本機假 Google；不會呼叫真實 Google API。</aside>').encode()
            data = data.replace(b"<body>", b"<body>" + banner, 1)
        if fault in {"receipt_lost_after_commit", "result_lost_after_commit"}:
            event["client_delivery"] = "connection_closed_before_headers"
            self.server.record(event)
            self.close_connection = True
            return
        if fault == "post_delay":
            time.sleep(6)
        self.send_response(status)
        for name, value in response_headers:
            lower = name.lower()
            if lower in HOP_HEADERS | {"content-length", "server", "date"}:
                continue
            if fault == "post_missing_marker" and lower == "x-orderflow-origin":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            sent = data[: max(1, len(data) // 2)] if fault == "post_truncate" else data
            self.wfile.write(sent)
            self.wfile.flush()
            event["client_delivery"] = "truncated" if fault == "post_truncate" else "written_to_socket"
            event["client_status"] = status
        except OSError:
            event["client_delivery"] = "write_failed_or_client_gone"
        self.server.record(event)
        self.close_connection = True


class Lab:
    def __init__(self, scenario: str):
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario")
        self.scenario = scenario
        self.run_id = str(uuid.uuid4())
        self.temp = tempfile.TemporaryDirectory(prefix="orderflow-fault-lab-")
        self.synthetic_pdf_path = Path(self.temp.name) / "synthetic.pdf"
        self.synthetic_pdf_path.write_bytes(synthetic_pdf())
        self.store = app.Store(Path(self.temp.name) / "synthetic-data")
        self.password = secrets.token_urlsafe(24)
        self.key = "lab-" + secrets.token_urlsafe(24)
        self.fake = FakeGoogle(SCENARIOS[scenario][1])
        self.backend = app.AppServer(("127.0.0.1", 0), self.store, None,
                                     bcrypt.hashpw(self.password.encode(), bcrypt.gensalt(rounds=4)))
        self.proxy = Proxy(self.backend.server_port, scenario, self.password, self.key)
        self.audit = AuditCapture()
        self.original_stderr = sys.stderr
        self.original_endpoint = gemini.ENDPOINT
        self.threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.proxy.server_port}"

    def __enter__(self):
        # The application still uses GeminiAdapter and urllib over real HTTP.
        # Only this dedicated process points its fixed endpoint to FakeGoogle.
        gemini.ENDPOINT = f"http://127.0.0.1:{self.fake.server_port}/fake-google/generate"

        # diagnostics.audit_event retains its whitelist and rate limits.  The
        # original _emit writes to this process-local stderr capture.
        sys.stderr = self.audit
        for server in (self.fake, self.backend, self.proxy):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def __exit__(self, *_args: object) -> None:
        for server in (self.proxy, self.backend, self.fake):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=3)
        sys.stderr = self.original_stderr
        gemini.ENDPOINT = self.original_endpoint
        self.temp.cleanup()

    def state(self) -> dict:
        with self.store.db() as db:
            jobs = [dict(row) for row in db.execute(
                "SELECT id,document_id,request_key,state,error_code,result FROM jobs ORDER BY created_ms")]
            documents = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return {"document_count": documents, "job_count": len(jobs),
                "jobs": [{"id": row["id"], "document_id": row["document_id"],
                          "request_key": row["request_key"], "state": row["state"],
                          "error_code": row["error_code"],
                          "result_saved": row["result"] is not None} for row in jobs]}


def synthetic_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class Client:
    def __init__(self, lab: Lab):
        self.lab = lab
        self.cookie = ""
        self.observations: list[dict] = []
        self.trace_id = str(uuid.uuid4())

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        request_id = str(uuid.uuid4())
        timeout = 5 if self.lab.scenario == "post_delay" and path == "/orderflow/api/jobs" else 8
        conn = http.client.HTTPConnection("127.0.0.1", self.lab.proxy.server_port, timeout=timeout)
        sent_headers = {"X-Orderflow-Request": "1", "X-Orderflow-Request-Id": request_id,
                        "X-Orderflow-Trace-Id": self.trace_id, **(headers or {})}
        if method in {"POST", "DELETE"}:
            sent_headers["Origin"] = self.lab.url
        if self.cookie:
            sent_headers["Cookie"] = self.cookie
        try:
            conn.request(method, path, body=body, headers=sent_headers)
            response = conn.getresponse()
            status = response.status
            marker = response.getheader("X-Orderflow-Origin")
            kind = response.getheader("Content-Type", "").split(";", 1)[0]
            issued = response.getheader("Set-Cookie")
            raw = response.read()
            if issued:
                self.cookie = issued.split(";", 1)[0]
            try:
                value = json.loads(raw) if kind == "application/json" else None
                parse_error = None if value is not None else "NON_JSON_RESPONSE"
            except (ValueError, UnicodeError):
                value, parse_error = None, "BAD_JSON_RESPONSE"
            result = {"status": status, "marker": "APP" if marker == "app" else "MISSING",
                      "response_type": "JSON" if kind == "application/json" else
                      "HTML" if kind == "text/html" else "OTHER", "value": value,
                      "error": parse_error, "request_id": request_id}
        except (OSError, http.client.HTTPException) as exc:
            result = {"status": None, "marker": None, "response_type": None,
                      "value": None, "error": type(exc).__name__, "request_id": request_id}
        finally:
            conn.close()
        self.observations.append({"method": method, "route": path,
                                  **{key: val for key, val in result.items() if key != "value"}})
        return result

    def setup(self) -> None:
        response = self.request("GET", "/__lab/session")
        if response["status"] != 303 or not self.cookie:
            raise RuntimeError("synthetic session setup failed")

    def upload(self) -> str:
        pdf = synthetic_pdf()
        response = self.request("POST", "/orderflow/api/documents", pdf,
                                {"Content-Type": "application/pdf", "X-File-Size": str(len(pdf)),
                                 "X-File-SHA256": hashlib.sha256(pdf).hexdigest(),
                                 "X-Request-Key": str(uuid.uuid4())})
        if response["status"] != 201:
            raise RuntimeError("synthetic PDF upload failed")
        return response["value"]["id"]


def exercise(lab: Lab) -> dict:
    client = Client(lab)
    client.setup()
    if lab.scenario == "browser_google_blocked":
        client.request("GET", "/__lab/google-browser-probe")
    document_id = client.upload()
    request_key = str(uuid.uuid4())
    post = client.request("POST", "/orderflow/api/jobs",
                          json.dumps({"document_id": document_id, "request_key": request_key,
                                      "scenario": "real"}).encode(),
                          {"Content-Type": "application/json"})
    if lab.scenario == "diagnostics_blocked":
        client.request("POST", "/orderflow/api/diagnostics",
                       json.dumps({"events": []}).encode(), {"Content-Type": "application/json"})
    deadline = time.monotonic() + (30 if lab.scenario == "ai_timeout" else 5)
    while time.monotonic() < deadline:
        state = lab.state()
        if not state["jobs"] or state["jobs"][0]["state"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
    bootstrap = client.request("GET", "/orderflow/api/bootstrap")
    recovered = []
    queried_jobs = []
    if bootstrap["status"] == 200:
        for job in bootstrap["value"]["jobs"]:
            if job["document_id"] == document_id:
                recovered.append(job["id"])
                first = client.request("GET", f"/orderflow/api/jobs/{job['id']}")
                query = first
                if first["error"] and lab.scenario == "result_lost_after_commit":
                    query = client.request("GET", f"/orderflow/api/jobs/{job['id']}")
                queried_jobs.append({"id": job["id"], "http_status": query["status"],
                                     "state": query["value"].get("state") if isinstance(
                                         query["value"], dict) else None,
                                     "same_job": isinstance(query["value"], dict) and
                                     query["value"].get("id") == job["id"],
                                     "result_readable": isinstance(query["value"], dict) and
                                     isinstance(query["value"].get("result"), list)})
    if lab.scenario == "post_delay":
        # The client times out at five seconds; the proxy finishes its delayed
        # write after six.  Wait for that actual event instead of racing a
        # one-second sleep before snapshotting the injection truth.
        event_deadline = time.monotonic() + 3
        while time.monotonic() < event_deadline:
            with lab.proxy.lock:
                recorded = any(event["injection"] == "post_delay" for event in lab.proxy.events)
            if recorded:
                break
            time.sleep(0.02)
    with lab.proxy.lock:
        proxy_events = list(lab.proxy.events)
        trigger_count = lab.proxy.trigger_count
    with lab.audit.lock:
        audit_events = list(lab.audit.events)
        other_stderr_lines = lab.audit.other_lines
    state = lab.state()
    # Do not infer browser reception from socket writes or database commits.
    result_recovered = any(job["http_status"] == 200 and job["same_job"] and
                           job["state"] == "done" and job["result_readable"]
                           for job in queried_jobs)
    verdict = {"job_creation_observed_by_script": post["status"] in {200, 202},
               "job_discovered_by_bootstrap": bool(recovered),
               "same_job_read_by_get": any(job["http_status"] == 200 and job["same_job"]
                                           for job in queried_jobs),
               "saved_result_read_by_get": result_recovered,
               "single_job": state["job_count"] <= 1,
               "single_fake_ai_call": lab.fake.call_count <= 1,
               "browser_acceptance": "UNTESTED"}
    expected_state = {
        "post_html_403": None, "post_html_502": None, "post_html_200": None,
        "post_disconnect": None,
        "ai_401": "failed", "ai_403": "failed", "ai_429": "failed",
        "ai_503": "unknown", "ai_timeout": "unknown", "ai_invalid": "failed",
    }.get(lab.scenario, "done")
    expected_calls = 0 if expected_state is None else 1
    actual_state = state["jobs"][0]["state"] if state["job_count"] == 1 else None
    verdict["scenario_expectation_met"] = (
        actual_state == expected_state and state["job_count"] == expected_calls and
        lab.fake.call_count == expected_calls and trigger_count ==
        (1 if SCENARIOS[lab.scenario][0] else 0))
    if expected_state == "done":
        verdict["scenario_expectation_met"] &= result_recovered
    if expected_state is not None:
        verdict["scenario_expectation_met"] &= verdict["same_job_read_by_get"]
    if lab.scenario == "result_lost_after_commit":
        queries = [item for item in client.observations if item["route"].startswith(
            "/orderflow/api/jobs/")]
        verdict["scenario_expectation_met"] &= (len(queries) == 2 and
                                                 queries[0]["status"] is None and
                                                 queries[1]["status"] == 200)
        fault_events = [event for event in proxy_events if event["injection"] ==
                        "result_lost_after_commit"]
        verdict["scenario_expectation_met"] &= (len(fault_events) == 1 and
                                                 fault_events[0].get("backend_status") == 200 and
                                                 fault_events[0]["client_delivery"] ==
                                                 "connection_closed_before_headers")
    if lab.scenario == "receipt_lost_after_commit":
        verdict["scenario_expectation_met"] &= post["status"] is None
    if lab.scenario == "browser_google_blocked":
        verdict["scenario_expectation_met"] &= any(
            item["route"] == "/__lab/google-browser-probe" and item["status"] == 403
            for item in client.observations)
    if lab.scenario == "diagnostics_blocked":
        verdict["scenario_expectation_met"] &= any(
            item["route"] == "/orderflow/api/diagnostics" and item["status"] == 403 and
            item["response_type"] == "HTML" and item["marker"] == "MISSING"
            for item in client.observations)
    post_symptoms = {
        "post_html_403": (403, "MISSING", "HTML", "NON_JSON_RESPONSE"),
        "post_html_502": (502, "MISSING", "HTML", "NON_JSON_RESPONSE"),
        "post_html_200": (200, "MISSING", "HTML", "NON_JSON_RESPONSE"),
        "post_missing_marker": (202, "MISSING", "JSON", None),
        "post_delay": (None, None, None, "TimeoutError"),
        "post_disconnect": (None, None, None, "RemoteDisconnected"),
        "post_truncate": (None, None, None, "IncompleteRead"),
        "receipt_lost_after_commit": (None, None, None, "RemoteDisconnected"),
    }
    if lab.scenario in post_symptoms:
        observed = (post["status"], post["marker"], post["response_type"], post["error"])
        verdict["scenario_expectation_met"] &= observed == post_symptoms[lab.scenario]
    if lab.scenario in {"post_truncate", "post_missing_marker", "post_delay"}:
        fault_events = [event for event in proxy_events if event["injection"] == lab.scenario]
        verdict["scenario_expectation_met"] &= len(fault_events) == 1 and bool(
            fault_events[0].get("backend_dispatched"))
        if lab.scenario == "post_truncate" and fault_events:
            verdict["scenario_expectation_met"] &= fault_events[0]["client_delivery"] == "truncated"
    if lab.scenario == "receipt_lost_after_commit":
        matching_proxy = [event for event in proxy_events if event["route"] == "/orderflow/api/jobs"]
        matching_audit = [event for event in audit_events if event.get("route") == "jobs" and
                          event.get("id") == post["request_id"]]
        verdict["scenario_expectation_met"] &= (
            len(matching_proxy) == 1 and matching_proxy[0].get("backend_status") == 202 and
            matching_proxy[0].get("client_delivery") == "connection_closed_before_headers" and
            {"received", "db_committed", "response_written"}.issubset(
                {event["phase"] for event in matching_audit}))
    if lab.scenario.startswith("ai_"):
        expected_code = {"ai_401": "AI_AUTH_FAILED", "ai_403": "AI_AUTH_FAILED",
                         "ai_429": "AI_RATE_LIMITED", "ai_503": "AI_HTTP_UNKNOWN",
                         "ai_timeout": "AI_TIMEOUT_UNKNOWN", "ai_invalid": "AI_BAD_RESPONSE"}[
                             lab.scenario]
        verdict["scenario_expectation_met"] &= (
            state["jobs"][0]["error_code"] == expected_code if state["jobs"] else False)
    if lab.scenario == "normal":
        verdict["scenario_expectation_met"] &= (
            post["status"], post["marker"], post["response_type"], post["error"]) == (
                202, "APP", "JSON", None)
    return {"scenario_id": lab.scenario, "run_id": lab.run_id,
            "version": {"app": app.VERSION, "diagnostics": app.BUILD_ID},
            "injection_truth": {"location": "loopback_proxy_or_fake_google",
                                "trigger_count": trigger_count,
                                "proxy_events": proxy_events,
                                "fake_ai_fault": SCENARIOS[lab.scenario][1],
                                "fake_ai_calls": lab.fake.call_count},
            "script_observations": client.observations,
            "backend_audit_output": audit_events,
            "other_stderr_lines": other_stderr_lines,
            "final_db_state": state,
            "recovery_job_ids": recovered,
            "queried_jobs": queried_jobs,
            "verdict": verdict,
            "unknowns": ["This run is synthetic and does not diagnose company DNS, TLS, proxy or policy.",
                         "Socket writes do not prove browser receipt.",
                         "Script observations are not browser UI acceptance."]}


def markdown_report(report: dict) -> str:
    state = report["final_db_state"]
    job = state["jobs"][0] if state["jobs"] else None
    truth = report["injection_truth"]
    lines = [f"# OrderFlow fault lab: {report['scenario_id']}", "",
             f"Run ID: `{report['run_id']}`; app `{report['version']['app']}`; "
             f"audit build `{report['version']['diagnostics']}`.", "",
             "## 注入真相（實驗器已知）", "",
             f"- 位置：{truth['location']}；代理觸發 {truth['trigger_count']} 次；"
             f"假 Google HTTP 收到 {truth['fake_ai_calls']} 次。",
             f"- 假上游故障：{truth['fake_ai_fault'] or 'none'}。"]
    for event in truth["proxy_events"]:
        if event["injection"]:
            lines.append(f"- {event['method']} {event['route']}：{event['injection']}；"
                         f"已轉送後端={event['backend_dispatched']}；"
                         f"後端 HTTP={event.get('backend_status', 'none')}；"
                         f"客戶端傳遞={event['client_delivery']}；"
                         f"request={event.get('request_id') or 'none'}；"
                         f"trace={event.get('trace_id') or 'none'}。")
    lines += ["", "## 客戶端腳本觀察（不是瀏覽器驗收）", ""]
    for item in report["script_observations"]:
        lines.append(f"- {item['method']} {item['route']}：HTTP={item['status']}；"
                     f"marker={item['marker']}；類型={item['response_type']}；"
                     f"錯誤={item['error']}；request={item['request_id']}。")
    lines += ["", "## 正式後端 audit 輸出", ""]
    audit = [event for event in report["backend_audit_output"] if event["event"] == "real_job" or
             event.get("route") in {"jobs", "job_get"}]
    for event in audit:
        lines.append(f"- {event['event']}/{event['phase']}：id={event['id']}；"
                     f"route={event.get('route', 'none')}；"
                     f"trace={event.get('trace_id', 'none')}；"
                     f"job={event.get('job_id', 'none')}；"
                     f"HTTP={event.get('http_status', 'none')}。")
    lines += ["", "## 持久狀態與恢復", "",
              f"- 合成文件 {state['document_count']} 筆，工作 {state['job_count']} 筆；"
              f"最終工作={job['id'] if job else 'none'}，狀態={job['state'] if job else 'none'}，"
              f"結果已保存={job['result_saved'] if job else False}。",
              f"- Bootstrap 找到的工作：{', '.join(report['recovery_job_ids']) or 'none'}。"]
    for query in report["queried_jobs"]:
        lines.append(f"- GET {query['id']}：HTTP={query['http_status']}；"
                     f"同一工作={query['same_job']}；狀態={query['state']}；"
                     f"結果可讀={query['result_readable']}。")
    lines += ["", "## 判定與未知", "",
              f"- 場景腳本驗證：{report['verdict']['scenario_expectation_met']}；"
              f"瀏覽器 UI 驗收：{report['verdict']['browser_acceptance']}。"]
    lines += [f"- {unknown}" for unknown in report["unknowns"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "run-all", "serve"))
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal")
    parser.add_argument("--output", type=Path, help="Report directory (run mode)")
    args = parser.parse_args()
    if args.mode == "run-all":
        output = args.output or Path(tempfile.mkdtemp(prefix="orderflow-fault-reports-"))
        output.mkdir(parents=True, exist_ok=True)
        summary = []
        for scenario in SCENARIOS:
            with Lab(scenario) as lab:
                report = exercise(lab)
            destination = output / scenario
            destination.mkdir()
            (destination / "report.json").write_text(json.dumps(report, indent=2,
                                                           ensure_ascii=False) + "\n")
            (destination / "report.md").write_text(markdown_report(report))
            summary.append({"scenario_id": scenario,
                            "scenario_expectation_met": report["verdict"]["scenario_expectation_met"],
                            "browser_acceptance": "UNTESTED"})
            print(scenario, "PASS" if summary[-1]["scenario_expectation_met"] else "FAIL",
                  flush=True)
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"Reports: {output}")
        if not all(row["scenario_expectation_met"] for row in summary):
            raise SystemExit(1)
        return
    with Lab(args.scenario) as lab:
        if args.mode == "serve":
            print(f"Run {lab.run_id}; scenario {args.scenario}; "
                  f"browser setup: {lab.url}/__lab/session", flush=True)
            print(f"Synthetic PDF: {lab.synthetic_pdf_path}", flush=True)
            print("Stop with Ctrl+C; all three listeners are loopback-only.", flush=True)
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                return
        report = exercise(lab)
    output = args.output or Path(tempfile.mkdtemp(prefix="orderflow-fault-report-"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (output / "report.md").write_text(markdown_report(report))
    print(f"Scenario {args.scenario}: {output / 'report.json'} ; {output / 'report.md'}")


if __name__ == "__main__":
    main()
