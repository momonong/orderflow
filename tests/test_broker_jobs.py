"""Offline durable queue, ownership, restart and human-confirmation contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from orderflow.app import AppServer, Store, validate_result, validate_management_result
from orderflow.broker import BrokerConfig, BrokerError, make_task
from orderflow.broker_jobs import BrokerJobs, BUSY_RETRY_DELAYS_MS, BUSY_RETRY_WINDOW_MS
import test_public_ai as helpers
import test_management_records as record_helpers


class QueueFixture:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.results = []
        self.lost_submit = False
        self.fail_get = False
        self.busy_submissions = 0
        self.busy_until_ms = 0
        self.busy_gets = 0
        self.busy_results = 0
        self.acceptances = []
        self.generations = []
        self.status_value = {"state": "queued", "execution_key": None, "result_available": False,
                             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        self.outcome = {"request_key": "q-fixture", "state": "completed",
                        "result": {"text": '{"items":[{"description":"fixture","quantity":2}]}'},
                        "finish_reason": "stop", "response_truncated": False}

    def __call__(self, config):
        return self

    def submit(self, task):
        self.posts.append(task)
        if self.busy_submissions or int(time.time() * 1000) < self.busy_until_ms:
            self.busy_submissions = max(0, self.busy_submissions - 1)
            raise BrokerError("BROKER_WORK_BUSY")
        self.acceptances.append(task["request_key"])
        if self.status_value["state"] == "completed":
            self.generations.append(task["request_key"])
        self.status_value["request_key"] = task["request_key"]
        if self.lost_submit:
            raise BrokerError("BROKER_HTTP_UNKNOWN")
        return dict(self.status_value)

    def status(self, key):
        self.gets.append(key)
        if self.busy_gets:
            self.busy_gets -= 1
            raise BrokerError("BROKER_WORK_BUSY")
        if self.fail_get:
            raise BrokerError("BROKER_NOT_FOUND")
        return dict(self.status_value)

    def result(self, key):
        self.results.append(key)
        if self.busy_results:
            self.busy_results -= 1
            raise BrokerError("BROKER_WORK_BUSY")
        return dict(self.outcome)

    def complete(self, *, usage_unknown=False):
        state = "completed_usage_unknown" if usage_unknown else "completed"
        self.status_value.update(state="completed", execution_key="q-fixture",
                                 result_available=True, gateway_state=state)
        self.outcome["state"] = state


class DurableBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.owner, self.token, _ = self.store.authenticate(None)
        self.token_path = self.root / "credential"
        self.token_path.write_text("fixture-client-token")
        self.token_path.chmod(0o600)
        self.config = BrokerConfig(token_file=self.token_path, input_bound_verified=True,
                                   input_bound_evidence="offline-fixture-only",
                                   input_bound_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.fixture = QueueFixture()
        self.runner = self.new_runner()

    def tearDown(self):
        self.tmp.cleanup()

    def new_runner(self, config=None):
        return BrokerJobs(self.store, config or self.config,
                          lambda raw, kind: validate_management_result(raw) if kind else validate_result(raw),
                          client_factory=self.fixture)

    def new_job(self, kind=None):
        purpose = "management" if kind else "diagnostic"
        doc = self.store.add_document(self.owner, str(uuid.uuid4()), helpers.PDF,
                                     hashlib.sha256(helpers.PDF).hexdigest(), 1, 1, purpose, kind)
        key = str(uuid.uuid4())
        task = make_task(self.config, helpers.PDF, kind, "orderflow-" + uuid.uuid4().hex)
        job, created = self.store.add_job(self.owner, doc["id"], key, "real", purpose,
                                          broker_plan=(task, self.config))
        self.assertTrue(created)
        return doc, job, key, task

    def view(self, job, purpose="diagnostic"):
        return self.store.job(self.owner, job["id"], purpose)

    def step(self, job_id, runner=None, *, at_ms=None):
        # Advance the fixture clock to the persisted poll, without real sleeps.
        with self.store.db() as db:
            due = db.execute("SELECT next_poll_ms FROM broker_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        now = max(int(time.time() * 1000), due) if at_ms is None else at_ms
        with patch("orderflow.broker_jobs.time.time", return_value=now / 1000):
            (runner or self.runner).step(job_id)

    def test_persisted_before_post_and_pending_does_not_submit_again(self):
        doc, job, _, task = self.new_job()
        with self.store.db() as db:
            row = db.execute("SELECT * FROM broker_jobs").fetchone()
            self.assertEqual(json.loads(row["task_json"]), task)
            self.assertEqual(row["task_sha256"], hashlib.sha256(row["task_json"].encode()).hexdigest())
            self.assertEqual(row["phase"], "prepared")
        self.step(job["id"])
        self.assertEqual(self.view(job)["broker"]["phase"], "pending")
        self.step(job["id"])
        self.assertEqual(len(self.fixture.posts), 1)
        self.assertEqual(self.fixture.gets, [task["request_key"]])
        self.assertEqual((self.store.files / f'{doc["id"]}.pdf').read_bytes(), helpers.PDF)

    def test_management_result_remains_draft_until_human_confirmation(self):
        doc, job, _, _ = self.new_job("invoice")
        self.fixture.complete(usage_unknown=True)
        self.fixture.outcome["result"] = {"text": '{"items":[{"product":"fixture","qty":"2"}]}'}
        self.step(job["id"])
        result = self.view(job, "management")
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["result"][0]["qty"], "2")
        self.assertEqual(self.store.record_sets(self.owner), [])
        confirmed = record_helpers.row("invoice", product="human corrected", qty="2")
        saved, created = self.store.save_record_set(self.owner, doc["id"], job["id"], 0, [confirmed])
        self.assertTrue(created)
        self.assertEqual(saved["rows"][0]["product"], "human corrected")
        self.store.deauthenticate(self.token)
        self.store = Store(self.root / "state")
        owner, _, _ = self.store.authenticate(self.token)
        self.assertEqual(self.store.record_sets(owner)[0]["rows"][0]["product"], "human corrected")

    def test_two_jobs_busy_then_accept_same_payload_once_each_after_low_frequency_wait(self):
        _, first, _, first_task = self.new_job()
        _, second, _, second_task = self.new_job()
        self.fixture.busy_until_ms = int(time.time() * 1000) + 10_000
        self.fixture.complete()
        self.step(first["id"])
        self.step(second["id"])
        self.assertEqual(self.fixture.acceptances, [])
        for job, task in ((first, first_task), (second, second_task)):
            with self.store.db() as db:
                row = dict(db.execute("SELECT * FROM broker_jobs WHERE job_id=?", (job["id"],)).fetchone())
            self.assertEqual(row["phase"], "busy_1")
            self.assertEqual(self.view(job)["broker"]["phase"], "prepared")
            self.assertEqual(row["next_poll_ms"] - row["first_submit_ms"], 1_000)
            before = len(self.fixture.posts)
            self.step(job["id"], at_ms=row["next_poll_ms"] - 1)
            self.assertEqual(len(self.fixture.posts), before)
            self.step(job["id"], at_ms=row["first_submit_ms"] + 7_001)
            self.assertEqual(self.view(job)["broker"]["phase"], "prepared")
            for _ in range(4):
                self.step(job["id"])
                if self.view(job)["state"] == "done":
                    break
            self.assertEqual(self.view(job)["state"], "done")
            sent = [sent for sent in self.fixture.posts if sent["request_key"] == task["request_key"]]
            self.assertGreaterEqual(len(sent), 3)
            self.assertEqual(sent, [task] * len(sent))
            self.assertEqual(self.fixture.generations.count(task["request_key"]), 1)
            self.assertEqual(self.fixture.acceptances.count(task["request_key"]), 1)

    def test_restart_busy_keeps_key_payload_budget_and_claim_commits_before_retry(self):
        _, job, _, task = self.new_job()
        self.fixture.busy_submissions = 1
        self.step(job["id"])
        with self.store.db() as db:
            persisted = dict(db.execute("SELECT * FROM broker_jobs").fetchone())
            schema_before = list(db.execute("PRAGMA table_info(broker_jobs)"))
        self.store = Store(self.root / "state")
        self.runner = self.new_runner()
        with self.store.db() as db:
            self.assertEqual(list(db.execute("PRAGMA table_info(broker_jobs)")), schema_before)
        original_submit = self.fixture.submit
        def check_claim(task):
            with self.store.db() as db:
                row = db.execute("SELECT * FROM broker_jobs").fetchone()
                self.assertEqual(row["phase"], "submitting_1")
                self.assertEqual(row["first_submit_ms"], persisted["first_submit_ms"])
                self.assertEqual(row["task_json"], persisted["task_json"])
                self.assertEqual(row["config_json"], persisted["config_json"])
            return original_submit(task)
        with patch.object(self.fixture, "submit", side_effect=check_claim):
            self.step(job["id"], at_ms=persisted["next_poll_ms"] - 1)
            self.assertEqual(len(self.fixture.posts), 1)
            self.step(job["id"], at_ms=persisted["next_poll_ms"])
        self.assertEqual(self.fixture.posts, [task, task])

    def test_busy_maximum_retries_is_durable_and_terminal(self):
        _, job, _, task = self.new_job()
        self.fixture.busy_submissions = 99
        for attempt in range(len(BUSY_RETRY_DELAYS_MS) + 1):
            self.store = Store(self.root / "state")
            self.runner = self.new_runner()
            self.step(job["id"])
            self.assertEqual(len(self.fixture.posts), attempt + 1)
        self.assertEqual(self.view(job)["state"], "failed")
        self.assertEqual(self.view(job)["error_code"], "BROKER_WORK_BUSY")
        self.assertEqual(self.view(job)["broker"]["phase"], "busy_paused")
        self.step(job["id"])
        self.assertEqual(self.fixture.posts, [task] * 33)
        self.assertEqual(self.fixture.acceptances, [])

    def test_explicit_busy_resume_renews_only_unsent_window_with_identical_mapping(self):
        _, job, _, task = self.new_job()
        self.fixture.busy_submissions = 1
        self.step(job["id"])
        with self.store.db() as db:
            before = dict(db.execute("SELECT * FROM broker_jobs").fetchone())
        self.step(job["id"], at_ms=before["first_submit_ms"] + BUSY_RETRY_WINDOW_MS)
        self.assertEqual(self.view(job)["broker"]["phase"], "busy_paused")
        self.store = Store(self.root / "state")
        self.runner = self.new_runner()
        with patch("orderflow.broker_jobs.time.time", return_value=(before["first_submit_ms"] + BUSY_RETRY_WINDOW_MS + 1) / 1000):
            self.runner.resume_busy(job["id"])
        with self.store.db() as db:
            renewed = dict(db.execute("SELECT * FROM broker_jobs").fetchone())
        self.assertEqual(renewed["phase"], "busy_0")
        for field in ("job_id", "request_key", "task_json", "task_sha256", "config_json", "expires_ms"):
            self.assertEqual(renewed[field], before[field])
        self.assertGreater(renewed["first_submit_ms"], before["first_submit_ms"])
        with self.assertRaises(BrokerError) as rejected:
            self.runner.resume_busy(job["id"])
        self.assertEqual(rejected.exception.code, "BROKER_BUSY_RESUME_NOT_ALLOWED")
        self.fixture.complete()
        self.step(job["id"])
        self.assertEqual(self.fixture.posts, [task, task])
        self.assertEqual(self.fixture.generations, [task["request_key"]])
        for phase in ("submitting", "submitting_1", "unknown", "complete", "pending"):
            with self.subTest(phase=phase):
                with self.store.db() as db:
                    db.execute("UPDATE broker_jobs SET phase=?", (phase,))
                with self.assertRaises(BrokerError):
                    self.runner.resume_busy(job["id"])

    def test_busy_total_deadline_or_result_expiry_never_posts_again(self):
        for ttl_expired in (False, True):
            with self.subTest(ttl_expired=ttl_expired):
                _, job, _, task = self.new_job()
                self.fixture.busy_submissions = 1
                self.step(job["id"])
                with self.store.db() as db:
                    first = db.execute("SELECT first_submit_ms FROM broker_jobs WHERE job_id=?", (job["id"],)).fetchone()[0]
                    if ttl_expired:
                        db.execute("UPDATE broker_jobs SET expires_ms=? WHERE job_id=?", (first + 1, job["id"]))
                before = len(self.fixture.posts)
                self.step(job["id"], at_ms=first + BUSY_RETRY_WINDOW_MS)
                self.assertEqual(len(self.fixture.posts), before)
                self.assertEqual(self.view(job)["error_code"], "BROKER_RESULT_EXPIRED" if ttl_expired else "BROKER_WORK_BUSY")
                self.assertEqual(self.view(job)["state"], "failed")

    def test_last_busy_post_uses_remaining_wire_deadline_without_changing_binding(self):
        for original_ttl_shorter in (False, True):
            with self.subTest(original_ttl_shorter=original_ttl_shorter):
                _, job, _, task = self.new_job()
                self.fixture.busy_submissions = 1
                self.step(job["id"])
                with self.store.db() as db:
                    saved = dict(db.execute("SELECT * FROM broker_jobs WHERE job_id=?", (job["id"],)).fetchone())
                    deadline = saved["first_submit_ms"] + (10_000 if original_ttl_shorter else BUSY_RETRY_WINDOW_MS)
                    if original_ttl_shorter:
                        db.execute("UPDATE broker_jobs SET expires_ms=? WHERE job_id=?", (deadline, job["id"]))
                observed = []
                def factory(config):
                    observed.append(config)
                    return self.fixture
                self.fixture.complete()
                runner = BrokerJobs(self.store, self.config, lambda raw, kind: validate_result(raw), client_factory=factory)
                self.step(job["id"], runner, at_ms=deadline - 500)
                self.assertAlmostEqual(observed[0].timeout_seconds, 0.5)
                self.assertEqual((observed[0].endpoint, observed[0].model, observed[0].token_file),
                                 (self.config.endpoint, self.config.model, self.config.token_file))
                with self.store.db() as db:
                    self.assertEqual(db.execute("SELECT config_json FROM broker_jobs WHERE job_id=?", (job["id"],)).fetchone()[0], saved["config_json"])
                sent = [sent for sent in self.fixture.posts if sent["request_key"] == task["request_key"]]
                self.assertEqual(sent, [task, task])

    def test_busy_retry_claim_crash_queries_original_key_without_reposting(self):
        _, job, _, task = self.new_job()
        self.fixture.busy_submissions = 1
        self.step(job["id"])
        with self.store.db() as db:
            db.execute("UPDATE broker_jobs SET phase='submitting_1'")
        self.store = Store(self.root / "state")
        self.runner = self.new_runner()
        self.fixture.fail_get = True
        self.step(job["id"])
        self.assertEqual(self.fixture.posts, [task])
        self.assertEqual(self.fixture.gets, [task["request_key"]])
        self.assertEqual(self.view(job)["state"], "unknown")

    def test_ambiguous_submit_and_busy_get_or_result_never_schedule_post_retry(self):
        _, job, _, task = self.new_job()
        self.fixture.lost_submit = True
        self.step(job["id"])
        self.fixture.busy_gets = 1
        self.step(job["id"])
        self.assertEqual(self.view(job)["broker"]["phase"], "unknown")
        self.fixture.complete()
        self.fixture.busy_results = 1
        self.step(job["id"])
        self.assertEqual(self.view(job)["state"], "unknown")
        self.step(job["id"])
        self.assertEqual(self.view(job)["state"], "done")
        self.assertEqual(self.fixture.posts, [task])
        self.assertEqual(self.fixture.gets, [task["request_key"]] * 3)
        self.assertEqual(self.fixture.results, [task["request_key"]] * 2)

    def test_busy_get_preserves_known_acceptance_and_busy_result_preserves_completion_metadata(self):
        _, job, _, task = self.new_job()
        self.step(job["id"])
        self.fixture.busy_gets = 1
        self.step(job["id"])
        accepted = self.view(job)
        self.assertEqual((accepted["state"], accepted["broker"]["phase"], accepted["error_code"]), ("running", "pending", None))
        self.fixture.complete(usage_unknown=True)
        self.fixture.busy_results = 1
        self.step(job["id"])
        held = self.view(job)
        self.assertEqual((held["state"], held["broker"]["phase"], held["error_code"]), ("running", "pending", None))
        self.assertEqual(held["broker"]["gateway_state"], "completed_usage_unknown")
        self.step(job["id"])
        self.assertEqual(self.view(job)["state"], "done")
        self.assertEqual(self.fixture.posts, [task])

    def test_lost_post_get_failure_and_restart_use_original_key(self):
        _, job, _, task = self.new_job()
        self.fixture.lost_submit = True
        self.step(job["id"])
        self.assertEqual(self.view(job)["state"], "unknown")
        self.fixture.fail_get = True
        self.step(job["id"])
        self.store = Store(self.root / "state")
        self.runner = self.new_runner()
        self.fixture.fail_get = False
        self.fixture.complete(usage_unknown=True)
        self.step(job["id"])
        result = self.view(job)
        self.assertEqual(result["state"], "done")
        self.assertTrue(result["broker"]["usage_unknown"])
        self.assertEqual(result["result"], [{"description": "fixture", "quantity": 2}])
        self.assertEqual(len(self.fixture.posts), 1)
        self.assertEqual(set(self.fixture.gets + self.fixture.results), {task["request_key"]})

    def test_crash_between_claim_and_send_never_reposts(self):
        _, job, _, task = self.new_job()
        with self.store.db() as db:
            db.execute("UPDATE broker_jobs SET phase='submitting'")
        self.store = Store(self.root / "state")
        self.runner = self.new_runner()
        self.fixture.fail_get = True
        self.step(job["id"])
        self.assertEqual(self.fixture.posts, [])
        self.assertEqual(self.fixture.gets, [task["request_key"]])
        self.assertEqual(self.view(job)["state"], "unknown")

    def test_duplicate_keys_and_concurrent_new_keys_return_same_job(self):
        doc, job, key, task = self.new_job()
        def duplicate(_):
            return self.store.add_job(self.owner, doc["id"], str(uuid.uuid4()), "real",
                                      broker_plan=(task, self.config))
        with ThreadPoolExecutor(max_workers=4) as pool:
            duplicates = list(pool.map(duplicate, range(6)))
        self.assertTrue(all(not created and value["id"] == job["id"] for value, created in duplicates))
        self.assertEqual(self.store.existing_job(self.owner, doc["id"], key, "diagnostic")["id"], job["id"])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM broker_jobs").fetchone()[0], 1)

    def test_owner_isolation_logout_login_and_private_mapping(self):
        doc, job, key, task = self.new_job()
        other, _, _ = self.store.authenticate(None)
        self.assertIsNone(self.store.job(other, job["id"]))
        self.assertIsNone(self.store.existing_job(other, doc["id"], key, "diagnostic"))
        self.assertEqual(self.store.jobs(other), [])
        with self.assertRaises(LookupError):
            self.store.add_job(other, doc["id"], str(uuid.uuid4()), "real", broker_plan=(task, self.config))
        self.store.deauthenticate(self.token)
        owner, _, _ = self.store.authenticate(self.token)
        self.assertEqual(owner, self.owner)
        public = json.dumps(self.store.jobs(owner))
        for private in (task["request_key"], str(self.token_path), "fixture-client-token", "127.0.0.1"):
            self.assertNotIn(private, public)
        self.assertEqual(self.view(job)["execution_mode"], "broker")

    def test_completed_job_requires_explicit_new_attempt_and_serializes_competitors(self):
        doc, job, key, task = self.new_job()
        self.fixture.complete()
        self.step(job["id"])
        existing, created = self.store.add_job(self.owner, doc["id"], str(uuid.uuid4()), "real",
                                               broker_plan=(task, self.config))
        self.assertFalse(created)
        self.assertEqual(existing["id"], job["id"])
        original, created = self.store.add_job(self.owner, doc["id"], key, "real",
                                               broker_plan=(task, self.config), new_attempt=True)
        self.assertFalse(created)
        self.assertEqual(original["id"], job["id"])
        def explicit(_):
            new_task = make_task(self.config, helpers.PDF, None, "orderflow-" + uuid.uuid4().hex)
            return self.store.add_job(self.owner, doc["id"], str(uuid.uuid4()), "real",
                                      broker_plan=(new_task, self.config), new_attempt=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            attempts = list(pool.map(explicit, range(4)))
        self.assertEqual(sum(created for _, created in attempts), 1)
        self.assertEqual(len({item["id"] for item, _ in attempts}), 1)
        self.assertNotEqual(attempts[0][0]["id"], job["id"])

    def test_restart_in_direct_mode_marks_broker_job_unknown_without_dispatch(self):
        _, job, _, _ = self.new_job()
        self.step(job["id"])
        self.store = Store(self.root / "state")
        server = AppServer(("127.0.0.1", 0), self.store, helpers.ORIGIN, helpers.TEST_HASH)
        try:
            self.assertEqual(self.view(job)["error_code"], "BROKER_MODE_INACTIVE")
            self.assertEqual(self.view(job)["state"], "unknown")
            self.assertIsNone(server.broker_worker)
        finally:
            server.server_close()
        self.fixture.complete()
        self.step(job["id"], self.new_runner())
        self.assertEqual(self.view(job)["state"], "done")
        self.assertEqual(len(self.fixture.posts), 1)

    def test_unknown_or_expired_cannot_create_new_attempt_or_direct_fallback(self):
        doc, job, _, _ = self.new_job()
        self.fixture.lost_submit = True
        self.step(job["id"])
        retry, created = self.store.add_job(self.owner, doc["id"], str(uuid.uuid4()), "real")
        self.assertFalse(created)
        self.assertEqual(retry["id"], job["id"])
        with self.store.db() as db:
            db.execute("UPDATE broker_jobs SET expires_ms=1")
        self.step(job["id"])
        self.assertEqual(self.view(job)["error_code"], "BROKER_RESULT_EXPIRED")
        self.step(job["id"])
        self.assertEqual(len(self.fixture.posts), 1)

    def test_truncation_missing_finish_and_schema_are_terminal(self):
        for finish, truncated, text, expected in (
            ("max_tokens", True, '{"items":[]}', "BROKER_RESULT_INCOMPLETE"),
            (None, False, '{"items":[]}', "BROKER_RESULT_INCOMPLETE"),
            ("stop", None, '{"items":[]}', "BROKER_RESULT_INCOMPLETE"),
            ("stop", False, '{"items":"invalid"}', "RESULT_FORMAT_INVALID"),
        ):
            with self.subTest(finish=finish, truncated=truncated, text=text):
                _, job, _, _ = self.new_job()
                self.fixture.complete()
                self.fixture.outcome.update(finish_reason=finish, response_truncated=truncated,
                                            result={"text": text})
                self.step(job["id"])
                self.assertEqual(self.view(job)["error_code"], expected)
                self.assertEqual(self.view(job)["state"], "failed")
                calls = len(self.fixture.gets)
                self.step(job["id"])
                self.assertEqual(len(self.fixture.gets), calls)

    def test_config_change_does_not_contact_another_instance(self):
        _, job, _, _ = self.new_job()
        runner = self.new_runner(replace(self.config, endpoint="http://127.0.0.1:18085"))
        self.step(job["id"], runner)
        self.assertEqual(self.fixture.posts + self.fixture.gets, [])
        self.assertEqual(self.view(job)["error_code"], "BROKER_CONFIG_CHANGED")

    def test_failure_transaction_rolls_back_both_tables_on_crash(self):
        _, job, _, _ = self.new_job()
        with self.store.db() as db:
            db.execute("CREATE TRIGGER fixture_crash BEFORE UPDATE OF state ON jobs "
                       "BEGIN SELECT RAISE(ABORT, 'fixture crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.runner._failure(job["id"], "BROKER_RESULT_EXPIRED", phase="expired")
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT phase FROM broker_jobs").fetchone()[0], "prepared")
            db.execute("DROP TRIGGER fixture_crash")
        self.store = Store(self.root / "state")
        self.runner = self.new_runner()
        self.fixture.complete()
        self.step(job["id"])
        self.assertEqual(self.view(job)["state"], "done")

    def test_additive_restart_preserves_legacy_and_pdf(self):
        doc, job, _, _ = self.new_job()
        self.store = Store(self.root / "state")
        self.assertEqual(self.view(job)["state"], "queued")
        self.assertEqual((self.store.files / f'{doc["id"]}.pdf').read_bytes(), helpers.PDF)
        with self.store.db() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_migration_from_pre_broker_schema_preserves_every_old_row_and_file(self):
        doc = self.store.add_document(self.owner, str(uuid.uuid4()), helpers.PDF,
                                     hashlib.sha256(helpers.PDF).hexdigest(), 1, 1)
        job, _ = self.store.add_job(self.owner, doc["id"], str(uuid.uuid4()), "success")
        self.store.set_job(job["id"], state="done", result=[{"description": "old", "quantity": 1}])
        def snapshot(db):
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                  "AND name!='broker_jobs' ORDER BY name")]
            return {table: hashlib.sha256(repr([tuple(row) for row in db.execute(
                'SELECT * FROM "' + table + '"')]).encode()).hexdigest() for table in tables}
        with self.store.db() as db:
            db.execute("DROP TABLE broker_jobs")
            before = snapshot(db)
        old_bytes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.store.files.iterdir()}
        self.store = Store(self.root / "state")
        with self.store.db() as db:
            self.assertEqual(snapshot(db), before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM broker_jobs").fetchone()[0], 0)
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in self.store.files.iterdir()}, old_bytes)


class BrokerHttpTests(unittest.TestCase):
    request = helpers.PublicAiTests.request
    login = helpers.PublicAiTests.login
    post_json = helpers.PublicAiTests.post_json
    upload = helpers.PublicAiTests.upload

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state")
        token = Path(self.tmp.name) / "token"
        token.write_text("fixture-client-token")
        token.chmod(0o600)
        self.config = BrokerConfig(token_file=token, input_bound_verified=True,
                                   input_bound_evidence="fixture-only",
                                   input_bound_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.fixture = QueueFixture()
        self.server = AppServer(("127.0.0.1", 0), self.store, helpers.ORIGIN, helpers.TEST_HASH,
                                broker_config=self.config, broker_client_factory=self.fixture)
        self.server.broker_worker.close()  # Tests explicitly advance the persisted state machine.
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie, _ = self.login()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_no_key_required_auth_isolation_and_original_job_after_lost_receipt(self):
        status, bootstrap, _ = self.request("GET", "/orderflow/api/bootstrap", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertFalse(bootstrap["ai_key_configured"])
        self.assertTrue(bootstrap["recognition"]["ready"])
        status, doc, _ = self.upload()
        self.assertEqual(status, 201)
        body = {"document_id": doc["id"], "request_key": str(uuid.uuid4()), "scenario": "real"}
        status, job, _ = self.post_json("/orderflow/api/jobs", body)
        self.assertEqual(status, 202)
        self.fixture.lost_submit = True
        self.server.broker_worker.step(job["id"])
        body["request_key"] = str(uuid.uuid4())
        status, recovered, _ = self.post_json("/orderflow/api/jobs", body)
        self.assertEqual((status, recovered["id"]), (200, job["id"]))
        other, _ = self.login()
        status, _, _ = self.request("GET", "/orderflow/api/jobs/" + job["id"], cookie=other)
        self.assertEqual(status, 404)
        status, data, _ = self.post_json("/orderflow/api/key/check", {})
        self.assertEqual((status, data["error_code"]), (409, "BROKER_MODE_ACTIVE"))
        self.assertEqual(len(self.fixture.posts), 1)

    def test_upload_preserved_when_bound_unavailable(self):
        self.server.broker_config = replace(self.config, input_bound_verified=False)
        status, doc, _ = self.upload()
        self.assertEqual(status, 201)
        status, data, _ = self.post_json("/orderflow/api/jobs", {
            "document_id": doc["id"], "request_key": str(uuid.uuid4()), "scenario": "real"})
        self.assertEqual((status, data["error_code"]), (409, "BROKER_INPUT_BOUND_REQUIRED"))
        self.assertEqual(self.fixture.posts, [])

    def test_busy_resume_endpoint_keeps_original_job_and_enforces_owner_purpose_body_and_state(self):
        _, doc, _ = self.upload()
        _, job, _ = self.post_json("/orderflow/api/jobs", {
            "document_id": doc["id"], "request_key": str(uuid.uuid4()), "scenario": "real"})
        self.fixture.busy_submissions = 1
        self.server.broker_worker.step(job["id"])
        with self.store.db() as db:
            original = dict(db.execute("SELECT * FROM broker_jobs").fetchone())
        with patch("orderflow.broker_jobs.time.time", return_value=(original["first_submit_ms"] + BUSY_RETRY_WINDOW_MS) / 1000):
            self.server.broker_worker.step(job["id"])
        path = f'/orderflow/api/jobs/{job["id"]}/resume'
        other, _ = self.login()
        status, _, _ = self.request("POST", path, json.dumps({}).encode(), {"Content-Type": "application/json"}, other)
        self.assertEqual(status, 404)
        status, _, _ = self.post_json(f'/orderflow/api/management/jobs/{job["id"]}/resume', {})
        self.assertEqual(status, 404)
        status, _, _ = self.post_json(path, {"new_attempt": True})
        self.assertEqual(status, 400)
        status, resumed, _ = self.post_json(path, {})
        self.assertEqual((status, resumed["id"], resumed["state"]), (202, job["id"], "queued"))
        self.assertEqual(len(self.fixture.posts), 1, 'HTTP resume commits intent without inline dispatch')
        status, data, _ = self.post_json(path, {})
        self.assertEqual((status, data["error_code"]), (409, "BROKER_BUSY_RESUME_NOT_ALLOWED"))
        with self.store.db() as db:
            renewed = dict(db.execute("SELECT * FROM broker_jobs").fetchone())
        for field in ("job_id", "request_key", "task_json", "config_json", "expires_ms"):
            self.assertEqual(renewed[field], original[field])

    def test_eight_mib_upload_one_mib_recognition_cap_rejects_before_post(self):
        pdf = helpers.PDF + b" " * (8 * 1024 * 1024 - len(helpers.PDF))
        # Keep the PDF end marker within pypdf's tail scan window.
        pdf = pdf[:-6] + b"%%EOF\n"
        status, doc, _ = self.request("POST", "/orderflow/api/documents", pdf, {
            "Content-Type": "application/pdf", "X-File-Size": str(len(pdf)),
            "X-File-SHA256": hashlib.sha256(pdf).hexdigest(), "X-Request-Key": str(uuid.uuid4())}, self.cookie)
        self.assertEqual(status, 201)
        status, result, _ = self.post_json("/orderflow/api/jobs", {
            "document_id": doc["id"], "request_key": str(uuid.uuid4()), "scenario": "real"})
        self.assertEqual((status, result["error_code"]), (409, "BROKER_PDF_TOO_LARGE"))
        self.assertEqual(self.fixture.posts, [])
        self.assertTrue((self.store.files / f'{doc["id"]}.pdf').exists())

    def test_completed_broker_then_explicit_direct_job_uses_new_route_only_after_confirmation(self):
        _, doc, _ = self.upload()
        body = {"document_id": doc["id"], "request_key": str(uuid.uuid4()), "scenario": "real"}
        status, original, _ = self.post_json("/orderflow/api/jobs", body)
        self.assertEqual(status, 202)
        self.fixture.complete()
        self.server.broker_worker.step(original["id"])
        self.server.broker_config = None  # Operator's explicit direct configuration.
        owner = self.store.session(self.cookie.split("=", 1)[1])
        self.server.set_key(owner, helpers.FAKE_KEY)
        body["request_key"] = str(uuid.uuid4())
        status, recovered, _ = self.post_json("/orderflow/api/jobs", body)
        self.assertEqual((status, recovered["id"]), (200, original["id"]))
        body["new_attempt"] = True
        with patch.object(helpers.GeminiAdapter, "recognize", return_value={"items": []}) as direct:
            status, new_job, _ = self.post_json("/orderflow/api/jobs", body)
            self.assertEqual(status, 202)
            self.assertEqual(new_job["execution_mode"], "direct")
            self.assertNotEqual(new_job["id"], original["id"])
            until = time.monotonic() + 2
            while time.monotonic() < until and self.store.job(owner, new_job["id"])["state"] != "done":
                time.sleep(0.01)
            self.assertEqual(self.store.job(owner, new_job["id"])["state"], "done")
            direct.assert_called_once()
        self.assertEqual(len(self.fixture.posts), 1)
