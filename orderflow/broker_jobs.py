"""Durable OrderFlow-to-Broker ownership and conservative submission claims.

The database commit before POST is intentionally conservative: a crash in that
gap may leave an unsent job unknown. It must never cause another provider call.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from dataclasses import replace

from .broker import BrokerClient, BrokerConfig, BrokerError, validate_outcome

TERMINAL = {"complete", "failed", "expired", "busy_paused"}
POLL_MS = 30_000
# Only the pinned exact work_busy rejection proves the prior POST was unsent.
# Count and start time persist in existing columns; no schema migration is needed.
BUSY_RETRY_DELAYS_MS = (1_000, 2_000, 4_000) + (8_000,) * 29
BUSY_RETRY_WINDOW_MS = 240_000
KEY = re.compile(r"[A-Za-z0-9._~-]{1,120}\Z")
BUSY_PHASE = re.compile(r"busy_(0|[1-9]|[12][0-9]|3[0-2])\Z")
SUBMITTING_PHASE = re.compile(r"submitting_(0|[1-9]|[12][0-9]|3[0-2])\Z")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False)


def migrate(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS broker_jobs (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id),
        session_id TEXT NOT NULL REFERENCES sessions(id),
        document_id TEXT NOT NULL REFERENCES documents(id),
        request_key TEXT NOT NULL UNIQUE,
        config_json TEXT NOT NULL, task_json TEXT NOT NULL, task_sha256 TEXT NOT NULL,
        payload_version TEXT NOT NULL, prompt_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        phase TEXT NOT NULL, execution_key TEXT, gateway_state TEXT,
        first_submit_ms INTEGER, expires_ms INTEGER,
        next_poll_ms INTEGER NOT NULL DEFAULT 0,
        finish_reason TEXT, response_truncated INTEGER, usage_unknown INTEGER NOT NULL DEFAULT 1
    )""")


def insert(db, job_id: str, session_id: str, document_id: str,
           task: dict, config: BrokerConfig) -> None:
    payload = canonical(task)
    db.execute("""INSERT INTO broker_jobs
        (job_id,session_id,document_id,request_key,config_json,task_json,task_sha256,
         payload_version,prompt_version,schema_version,phase)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, session_id, document_id, task["request_key"], canonical(config.snapshot()),
         payload, hashlib.sha256(payload.encode()).hexdigest(),
         "broker-typed-pdf-v1", "orderflow-pdf-v1", "orderflow-items-v1", "prepared"))


def public_metadata(db, job_id: str) -> dict | None:
    row = db.execute("SELECT phase,gateway_state,finish_reason,response_truncated,usage_unknown "
                     "FROM broker_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    if BUSY_PHASE.fullmatch(result["phase"]):
        result["phase"] = "prepared"
    elif SUBMITTING_PHASE.fullmatch(result["phase"]):
        result["phase"] = "submitting"
    result["response_truncated"] = (bool(result["response_truncated"])
                                     if result["response_truncated"] is not None else None)
    result["usage_unknown"] = bool(result["usage_unknown"])
    return result


def timestamp_ms(value: object) -> int:
    if not isinstance(value, str) or len(value) > 64:
        raise BrokerError("BROKER_BAD_RESPONSE")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError):
        raise BrokerError("BROKER_BAD_RESPONSE") from None


class BrokerJobs:
    """Single background reconciler; tests can advance one job without sleeping."""

    def __init__(self, store, config: BrokerConfig, validate, *, client_factory=BrokerClient):
        self.store = store
        self.config = config
        self.validate = validate
        self.client_factory = client_factory
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="broker-reconcile")

    def start(self):
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.wakeup.set()
        if self.thread.is_alive():
            self.thread.join(timeout=self.config.timeout_seconds * 2 + 2)

    def _run(self):
        while not self.stopping.is_set():
            with self.store.db() as db:
                rows = db.execute("SELECT job_id FROM broker_jobs WHERE phase NOT IN "
                                  "('complete','failed','expired','busy_paused') AND next_poll_ms<=? "
                                  "ORDER BY next_poll_ms LIMIT 32", (int(time.time() * 1000),)).fetchall()
            for row in rows:
                if self.stopping.is_set():
                    break
                self.step(row["job_id"])
            self.wakeup.wait(2)
            self.wakeup.clear()

    def _update(self, job_id, **fields):
        with self.store.db() as db:
            db.execute("UPDATE broker_jobs SET " + ",".join(f"{name}=?" for name in fields)
                       + " WHERE job_id=?", (*fields.values(), job_id))

    def _failure(self, job_id, code, *, phase="unknown"):
        state = "unknown" if phase == "unknown" else "failed"
        now = int(time.time() * 1000)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE broker_jobs SET phase=?,next_poll_ms=? WHERE job_id=?",
                       (phase, now + POLL_MS, job_id))
            db.execute("UPDATE jobs SET state=?,error_code=?,steps=?,finished_ms=? WHERE id=?",
                       (state, code, canonical({"ai": "unknown" if state == "unknown" else "fail",
                                               "format": "not_run"}), now, job_id))

    def step(self, job_id: str) -> None:
        # In-process serialization also keeps fixture/manual reconciliation from
        # racing the daemon. The DB transition owns the irreversible POST claim.
        with self.lock:
            self._step(job_id)

    def _busy(self, job_id: str, claimed_phase: str, first_submit_ms: int, rejected_count: int) -> None:
        now = int(time.time() * 1000)
        if rejected_count > len(BUSY_RETRY_DELAYS_MS) or now >= first_submit_ms + BUSY_RETRY_WINDOW_MS:
            self._failure(job_id, "BROKER_WORK_BUSY", phase="busy_paused")
            return
        next_poll = min(now + BUSY_RETRY_DELAYS_MS[rejected_count - 1], first_submit_ms + BUSY_RETRY_WINDOW_MS)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE broker_jobs SET phase=?,next_poll_ms=? "
                                 "WHERE job_id=? AND phase=?",
                                 (f"busy_{rejected_count}", next_poll, job_id, claimed_phase)).rowcount
            if changed:
                db.execute("UPDATE jobs SET state='running',error_code=?,finished_ms=NULL,steps=? WHERE id=?",
                           ("BROKER_WORK_BUSY", canonical({"ai": "pending", "format": "not_run"}), job_id))

    def resume_busy(self, job_id: str) -> None:
        """Explicitly renew only a proven unaccepted job's bounded busy window."""
        with self.lock, self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT b.*,j.state,j.error_code FROM broker_jobs b JOIN jobs j ON j.id=b.job_id WHERE b.job_id=?", (job_id,)).fetchone()
            if (not row or row["phase"] != "busy_paused" or row["state"] != "failed"
                    or row["error_code"] != "BROKER_WORK_BUSY" or row["execution_key"] is not None
                    or row["gateway_state"] is not None):
                raise BrokerError("BROKER_BUSY_RESUME_NOT_ALLOWED")
            now = int(time.time() * 1000)
            if row["expires_ms"] is not None and now >= row["expires_ms"]:
                raise BrokerError("BROKER_RESULT_EXPIRED")
            config = BrokerConfig.from_snapshot(json.loads(row["config_json"]))
            current = self.config.snapshot()
            if any(config.snapshot()[key] != current[key] for key in ("endpoint", "token_file")):
                raise BrokerError("BROKER_CONFIG_CHANGED")
            if not config.bound_ready():
                raise BrokerError("BROKER_INPUT_BOUND_REQUIRED")
            if hashlib.sha256(row["task_json"].encode()).hexdigest() != row["task_sha256"]:
                raise BrokerError("BROKER_CONFLICT")
            # No remote acceptance occurred in this phase. Keep its task, key and
            # original TTL, while renewing the explicitly requested wait budget.
            db.execute("UPDATE broker_jobs SET phase='busy_0',first_submit_ms=?,next_poll_ms=? WHERE job_id=?", (now, now, job_id))
            db.execute("UPDATE jobs SET state='queued',error_code=?,finished_ms=NULL,steps=? WHERE id=?",
                       ("BROKER_WORK_BUSY", canonical({"ai": "pending", "format": "not_run"}), job_id))
        self.wakeup.set()

    def _step(self, job_id: str) -> None:
        now = int(time.time() * 1000)
        with self.store.db() as db:
            row = db.execute("SELECT b.*,d.document_kind FROM broker_jobs b "
                             "JOIN documents d ON d.id=b.document_id WHERE b.job_id=?",
                             (job_id,)).fetchone()
        if not row or row["phase"] in TERMINAL:
            return
        if row["expires_ms"] is not None and now >= row["expires_ms"]:
            self._failure(job_id, "BROKER_RESULT_EXPIRED", phase="expired")
            return
        busy = BUSY_PHASE.fullmatch(row["phase"])
        if busy and (row["first_submit_ms"] is None or now >= row["first_submit_ms"] + BUSY_RETRY_WINDOW_MS):
            self._failure(job_id, "BROKER_WORK_BUSY", phase="busy_paused")
            return
        if now < row["next_poll_ms"]:
            return
        try:
            snapshot = json.loads(row["config_json"])
            # A changed instance or credential binding must not redirect an old
            # request. A future configuration may resume it on its original binding.
            current = self.config.snapshot()
            if any(snapshot.get(key) != current.get(key) for key in ("endpoint", "token_file")):
                raise BrokerError("BROKER_CONFIG_CHANGED")
            config = BrokerConfig.from_snapshot(snapshot)
            task_json = row["task_json"]
            if hashlib.sha256(task_json.encode()).hexdigest() != row["task_sha256"]:
                raise BrokerError("BROKER_CONFLICT")
            if busy:
                # The last wire request is bounded by the original retry window,
                # while the persisted provider/model/payload binding stays fixed.
                deadline = min(row["first_submit_ms"] + BUSY_RETRY_WINDOW_MS,
                               row["expires_ms"] or row["first_submit_ms"] + BUSY_RETRY_WINDOW_MS)
                remaining = (deadline - now) / 1000
                config = replace(config, timeout_seconds=min(config.timeout_seconds, remaining))
            client = self.client_factory(config)
            if row["phase"] == "prepared" or busy:
                rejected_count = int(busy[1]) if busy else 0
                first_submit = row["first_submit_ms"] if busy else now
                claimed_phase = f"submitting_{rejected_count}" if busy else "submitting"
                expires_ms = row["expires_ms"] or first_submit + 86_400_000
                with self.store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    claimed = db.execute("UPDATE broker_jobs SET phase=?,first_submit_ms=?,"
                                         "expires_ms=?,next_poll_ms=? WHERE job_id=? AND phase=?",
                                         (claimed_phase, first_submit, expires_ms, now + POLL_MS, job_id, row["phase"])).rowcount
                    if not claimed:
                        return
                    db.execute("UPDATE jobs SET state='running',started_ms=COALESCE(started_ms,?),error_code=NULL WHERE id=?", (now, job_id))
                # Every sending claim commits before POST. A restart in that gap
                # only queries; durable busy_N is the sole permission to retry.
                try:
                    metadata = client.submit(json.loads(task_json))
                except BrokerError as exc:
                    if exc.code == "BROKER_WORK_BUSY":
                        self._busy(job_id, claimed_phase, first_submit, rejected_count + 1)
                        return
                    raise
            else:
                metadata = client.status(row["request_key"])
            if not isinstance(metadata, dict) or metadata.get("request_key") != row["request_key"]:
                raise BrokerError("BROKER_BAD_RESPONSE")
            expires = timestamp_ms(metadata.get("expires_at"))
            # The Broker TTL starts at submit. A reported earlier expiry wins;
            # never extend an already persisted expiry after a replay or restart.
            expires = min(expires, row["expires_ms"] or now + 86_400_000)
            self._update(job_id, expires_ms=expires)
            if expires <= now:
                self._failure(job_id, "BROKER_RESULT_EXPIRED", phase="expired")
                return
            state = metadata.get("state")
            execution = metadata.get("execution_key")
            gateway = metadata.get("gateway_state")
            if execution is not None and (not isinstance(execution, str) or not KEY.fullmatch(execution)):
                raise BrokerError("BROKER_BAD_RESPONSE")
            if row["execution_key"] and execution != row["execution_key"]:
                raise BrokerError("BROKER_CONFLICT")
            safe_gateway = gateway if gateway in {"completed", "completed_usage_unknown", "unknown",
                                                 "failed", "rejected", "quota_exhausted"} else None
            self._update(job_id, execution_key=execution, gateway_state=safe_gateway,
                         next_poll_ms=now + POLL_MS)
            if state in {"queued", "waiting", "running"}:
                retry_at = metadata.get("next_retry_at")
                next_poll = max(now + POLL_MS, timestamp_ms(retry_at)) if retry_at else now + POLL_MS
                self._update(job_id, phase="pending", next_poll_ms=min(next_poll, expires))
                self.store.set_job(job_id, state="running", error_code=None, finished_ms=None,
                                   steps={"ai": "pending", "format": "not_run"})
                return
            if state == "expired":
                self._failure(job_id, "BROKER_RESULT_EXPIRED", phase="expired")
                return
            if state == "unknown":
                raise BrokerError("BROKER_HTTP_UNKNOWN")
            if state in {"failed", "rejected", "cancelled", "quota_exhausted"}:
                self._failure(job_id, "BROKER_REJECTED", phase="failed")
                return
            if state != "completed" or metadata.get("result_available") is not True or not execution:
                raise BrokerError("BROKER_BAD_RESPONSE")
            outcome = client.result(row["request_key"])
            try:
                raw, completion = validate_outcome(outcome, execution, gateway)
            except BrokerError as exc:
                self._failure(job_id, exc.code, phase="failed")
                return
            try:
                rows = self.validate(raw, row["document_kind"])
            except ValueError:
                self._failure(job_id, "RESULT_FORMAT_INVALID", phase="failed")
                return
            # Draft result and completion metadata are one transaction, so a
            # restart cannot claim completion without the validated local result.
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE jobs SET state='done',error_code=NULL,result=?,steps=?,finished_ms=? WHERE id=?",
                           (canonical(rows), canonical({"ai": "pass", "format": "pass"}), now, job_id))
                db.execute("UPDATE broker_jobs SET phase='complete',finish_reason=?,response_truncated=?,"
                           "usage_unknown=? WHERE job_id=?",
                           (completion["finish_reason"], completion["response_truncated"],
                            completion["usage_unknown"], job_id))
        except BrokerError as exc:
            if exc.code == "BROKER_WORK_BUSY":
                # A busy GET says nothing about submission acceptance. Preserve
                # the saved phase, state and completion metadata; only defer GET.
                self._update(job_id, next_poll_ms=min(now + POLL_MS, row["expires_ms"] or now + POLL_MS))
                return
            terminal = {"BROKER_RESULT_INCOMPLETE", "BROKER_CONFLICT", "BROKER_REJECTED"}
            phase = "expired" if exc.code == "BROKER_RESULT_EXPIRED" else "failed" if exc.code in terminal else "unknown"
            self._failure(job_id, exc.code, phase=phase)
        except Exception:
            # No raw upstream content or credentials in logs/errors.
            self._failure(job_id, "BROKER_HTTP_UNKNOWN")
