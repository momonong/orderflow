"""Exercise management recovery through real loopback HTTP and synthetic AI.

Run with ``.venv/bin/python -m tools.management_fault_lab --output DIR``.
The only upstream is the loopback FakeGoogle in fault_lab.py. Reports contain
request metadata, audit events and DB state, never credentials or PDF bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pypdf import PdfWriter

from orderflow import app
from tools.fault_lab import Client, Lab


ROOT = "/orderflow/api/management"


def insist(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def pdf_for(kind: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=101 if kind == "purchase_order" else 102, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def upload(client: Client, kind: str) -> str:
    pdf = pdf_for(kind)
    response = client.request("POST", ROOT + "/documents", pdf,
                              {"Content-Type": "application/pdf", "X-Document-Kind": kind,
                               "X-File-Size": str(len(pdf)),
                               "X-File-SHA256": hashlib.sha256(pdf).hexdigest(),
                               "X-Request-Key": str(uuid.uuid4())})
    insist(response["status"] == 201 and response["marker"] == "APP",
           f"{kind} upload: {response['status']} {response['error']}")
    return response["value"]["id"]


def post_job(client: Client, document_id: str, request_key: str,
             scenario: str = "real") -> dict:
    return client.request("POST", ROOT + "/jobs",
                          json.dumps({"document_id": document_id, "request_key": request_key,
                                      "scenario": scenario}).encode(),
                          {"Content-Type": "application/json"})


def get_job(client: Client, job_id: str) -> dict:
    return client.request("GET", ROOT + "/jobs/" + job_id)


def final_job(client: Client, job_id: str, *, expected: str = "done") -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = get_job(client, job_id)
        if response["status"] == 200 and response["value"]["state"] not in {"queued", "running"}:
            insist(response["value"]["state"] == expected,
                   f"job {job_id} ended {response['value']['state']}, expected {expected}")
            return response["value"]
        time.sleep(0.04)
    raise AssertionError(f"job {job_id} did not finish")


def record_row(item: dict, kind: str) -> dict:
    return {"id": str(uuid.uuid4()), **item,
            "status": "確認中" if kind == "purchase_order" else None,
            "linked_order_row_id": None, "deleted": False}


def confirm(client: Client, document_id: str, job: dict, kind: str) -> dict:
    item = job["result"][0]
    insist(item["product"] == "Synthetic widget" and item["qty"] == "2", "AI row changed")
    insist((item["orderNo"] == "PO-SYN-1" and item["invoiceNo"] is None) if kind ==
           "purchase_order" else (item["invoiceNo"] == "INV-SYN-1" and item["orderNo"] is None),
           "document kind fields crossed")
    payload = {"source_job_id": job["id"], "revision": 0, "rows": [record_row(item, kind)]}
    saved = client.request("PUT", ROOT + "/record-sets/" + document_id,
                           json.dumps(payload, ensure_ascii=False).encode(),
                           {"Content-Type": "application/json"})
    insist(saved["status"] == 201, f"human confirmation: {saved['status']} {saved['value']}")
    again = client.request("PUT", ROOT + "/record-sets/" + document_id,
                           json.dumps(payload, ensure_ascii=False).encode(),
                           {"Content-Type": "application/json"})
    insist(again["status"] == 200 and again["value"]["revision"] == 1,
           "confirmation replay changed revision")
    read = client.request("GET", ROOT + "/record-sets/" + document_id)
    insist(read["status"] == 200 and read["value"]["source_job_id"] == job["id"],
           "record set not readable")
    return {"record_id": read["value"]["id"], "revision": read["value"]["revision"],
            "source_job_id": read["value"]["source_job_id"]}


def evidence(lab: Lab, clients: list[Client]) -> dict:
    with lab.proxy.lock:
        injected = [event for event in lab.proxy.events if event["injection"]]
        trigger_count = lab.proxy.trigger_count
    with lab.audit.lock:
        audit = list(lab.audit.events)
    with lab.fake.lock:
        calls = dict(lab.fake.calls_by_kind)
    return {"run_id": lab.run_id,
            "version": {"app": app.VERSION, "diagnostics": app.BUILD_ID},
            "injection_truth": {"trigger_count": trigger_count, "events": injected,
                                "fake_ai_calls": calls},
            "http_observations": [item for client in clients for item in client.observations],
            "backend_audit": audit, "db_state": lab.state()}


def group1_case(scenario: str, kind: str) -> dict:
    with Lab(scenario) as lab:
        client = Client(lab)
        client.setup()
        doc = upload(client, kind)
        key = str(uuid.uuid4())
        post = post_job(client, doc, key)
        lost_receipt = scenario == "management_receipt_lost_after_commit"
        lost_result = scenario == "management_result_lost_after_commit"
        insist(post["status"] is None if lost_receipt else post["status"] == 202,
               "POST symptom differs from injection")
        bootstrap = client.request("GET", ROOT + "/bootstrap")
        insist(bootstrap["status"] == 200, "bootstrap unavailable")
        jobs = [row for row in bootstrap["value"]["jobs"] if row["document_id"] == doc]
        insist(len(jobs) == 1, "job not discoverable or duplicated")
        job_id = jobs[0]["id"]
        if lost_result:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and lab.state()["jobs"][0]["state"] != "done":
                time.sleep(0.04)
            insist(lab.state()["jobs"][0]["state"] == "done",
                   "result-loss injection would precede saved result")
        first_get = get_job(client, job_id)
        insist(first_get["status"] is None if lost_result else first_get["status"] == 200,
               "GET symptom differs from injection")
        job = final_job(client, job_id)
        saved = confirm(client, doc, job, kind)
        data = evidence(lab, [client])
        insist(data["injection_truth"]["trigger_count"] == (1 if lost_receipt or lost_result else 0),
               "injection did not fire once")
        insist(data["injection_truth"]["fake_ai_calls"][kind] == 1 and
               data["db_state"]["job_count"] == 1, "job or AI call duplicated")
        if lost_receipt or lost_result:
            event = data["injection_truth"]["events"][0]
            insist(event["backend_status"] == (202 if lost_receipt else 200) and
                   event["client_delivery"] == "connection_closed_before_headers",
                   "response was not lost after backend completed")
        return {"case": scenario, "kind": kind, "hypothesis": "a lost response leaves one recoverable job",
                "client_symptom": {"post_status": post["status"], "first_get_status": first_get["status"]},
                "recovered_job_id": job_id, "final_state": job["state"],
                "confirmed_record": saved, "classification": "expected_recovery", **data}


def group2_parallel() -> dict:
    with Lab("normal") as lab:
        lab.fake.release.clear()
        client = Client(lab)
        client.setup()
        doc = upload(client, "purchase_order")
        key = str(uuid.uuid4())
        first = post_job(client, doc, key)
        insist(first["status"] == 202 and first["value"]["state"] == "queued", "queued snapshot missing")
        job_id = first["value"]["id"]
        insist(lab.fake.entered.wait(timeout=5), "AI request did not reach fake upstream")
        running = get_job(client, job_id)
        insist(running["status"] == 200 and running["value"]["state"] == "running", "running state missing")
        peers = [Client(lab), Client(lab)]
        for peer in peers:
            peer.cookie = client.cookie
        with ThreadPoolExecutor(max_workers=2) as pool:
            duplicates = list(pool.map(lambda peer: post_job(peer, doc, key), peers))
        insist(all(row["status"] == 200 and row["value"]["id"] == job_id for row in duplicates),
               "parallel replay made a new job")
        invalid = post_job(client, doc, key, "mock")
        insist(invalid["status"] == 400 and invalid["value"]["error_code"] == "BAD_JOB_REQUEST",
               "management scenario mismatch was not rejected")
        with lab.fake.lock:
            before_release_calls = lab.fake.call_count
        insist(before_release_calls == 1 and lab.state()["job_count"] == 1,
               "running replay duplicated work")
        lab.fake.release.set()
        done = final_job(client, job_id)
        replay = post_job(client, doc, key)
        insist(replay["status"] == 200 and replay["value"]["id"] == job_id and
               replay["value"]["state"] == "done", "done replay changed work")
        other_doc = upload(client, "invoice")
        other = post_job(client, other_doc, key)
        insist(other["status"] == 202 and other["value"]["id"] != job_id,
               "same key must be allowed for a different document")
        other_done = final_job(client, other["value"]["id"])
        data = evidence(lab, [client, *peers])
        insist(data["injection_truth"]["fake_ai_calls"] ==
               {"diagnostic": 0, "purchase_order": 1, "invoice": 1}, "AI count mismatch")
        return {"case": "same_key_parallel_and_scope", "hypothesis":
                "same session/document/key returns one job; same key on another document is allowed",
                "states_observed": [first["value"]["state"], running["value"]["state"],
                                    done["state"], other_done["state"]],
                "parallel_http": [row["status"] for row in duplicates],
                "mismatched_scenario_http": invalid["status"],
                "cross_document_http": other["status"], "classification": "contract_pass", **data}


def group2_unknown() -> dict:
    with Lab("management_ai_503") as lab:
        client = Client(lab)
        client.setup()
        doc = upload(client, "purchase_order")
        key = str(uuid.uuid4())
        first = post_job(client, doc, key)
        insist(first["status"] == 202, "unknown case did not create job")
        job = final_job(client, first["value"]["id"], expected="unknown")
        replay = post_job(client, doc, key)
        insist(replay["status"] == 200 and replay["value"]["id"] == job["id"] and
               replay["value"]["state"] == "unknown", "unknown replay started new job")
        data = evidence(lab, [client])
        insist(data["injection_truth"]["fake_ai_calls"]["purchase_order"] == 1 and
               data["db_state"]["job_count"] == 1 and job["error_code"] == "AI_HTTP_UNKNOWN",
               "unknown state or AI call count mismatch")
        return {"case": "unknown_replay", "hypothesis": "unknown is retained, not automatically resent",
                "job_id": job["id"], "error_code": job["error_code"],
                "replay_http": replay["status"], "classification": "correct_unknown", **data}


def group2_rate_limit(source: str) -> dict:
    scenario = "normal" if source == "local_slot" else "management_ai_429"
    with Lab(scenario) as lab:
        client = Client(lab)
        client.setup()
        doc = upload(client, "purchase_order")
        if source == "local_slot":
            lab.fake.release.clear()
            first = post_job(client, doc, str(uuid.uuid4()))
            insist(first["status"] == 202 and lab.fake.entered.wait(timeout=5),
                   "first job did not occupy the local AI slot")
        limited = post_job(client, doc, str(uuid.uuid4()))
        insist(limited["status"] == 202, "rate-limited job was not recorded")
        job = final_job(client, limited["value"]["id"], expected="failed")
        insist(job["error_code"] == "AI_RATE_LIMITED", "wrong rate-limit code")
        expected_upstream = None if source == "local_slot" else 429
        insist(job["steps"].get("upstream_http_status") == expected_upstream,
               "local slot and upstream 429 metadata not distinguished")
        if source == "local_slot":
            lab.fake.release.set()
            final_job(client, first["value"]["id"])
        data = evidence(lab, [client])
        failed_audit = [event for event in data["backend_audit"]
                        if event.get("event") == "real_job" and event.get("phase") == "failed"]
        insist(len(failed_audit) == 1 and
               failed_audit[0].get("upstream_http_status") == expected_upstream,
               "audit did not preserve rate-limit source")
        insist(data["injection_truth"]["fake_ai_calls"]["purchase_order"] == 1,
               "rate-limit case sent extra upstream request")
        return {"case": "rate_limit_" + source, "hypothesis":
                "local slot contention and upstream 429 share a code but have different metadata",
                "job_id": job["id"], "error_code": job["error_code"],
                "upstream_http_status": job["steps"].get("upstream_http_status"),
                "classification": "contract_pass", **data}


def group3_key_expiry() -> dict:
    old_ttl = app.KEY_TTL_SECONDS
    try:
        with Lab("normal") as lab:
            client = Client(lab)
            client.setup()
            doc = upload(client, "purchase_order")
            first = post_job(client, doc, str(uuid.uuid4()))
            insist(first["status"] == 202, "initial job not created")
            done = final_job(client, first["value"]["id"])
            # Finish normal work first; only the second key set gets the short
            # TTL. This avoids a race between setup/upload and initial POST.
            app.KEY_TTL_SECONDS = 0.1  # Isolated-process time acceleration.
            refreshed = client.request("POST", "/orderflow/api/key",
                                       json.dumps({"key": lab.key}).encode(),
                                       {"Content-Type": "application/json"})
            insist(refreshed["status"] == 200, "short-lived key was not set over HTTP")
            time.sleep(0.15)
            key_state = client.request("GET", "/orderflow/api/key")
            read = get_job(client, done["id"])
            new_job = post_job(client, doc, str(uuid.uuid4()))
            data = evidence(lab, [client])
            insist(key_state["status"] == 200 and not key_state["value"]["configured"],
                   "key expiry did not occur")
            insist(read["status"] == 200 and read["value"]["state"] == "done" and
                   read["value"]["result"] == done["result"], "saved result lost with key")
            insist(new_job["status"] == 409 and new_job["value"]["error_code"] == "KEY_REQUIRED",
                   "expired key allowed new AI work")
            insist(data["injection_truth"]["fake_ai_calls"]["purchase_order"] == 1 and
                   data["db_state"]["job_count"] == 1, "expired-key request created work")
            return {"case": "key_expired_result_still_readable", "hypothesis":
                    "an expired key blocks new work while completed results remain readable",
                    "ttl_seconds_test_override": 0.1, "existing_get_http": read["status"],
                    "new_post_http": new_job["status"], "classification": "contract_pass", **data}
    finally:
        app.KEY_TTL_SECONDS = old_ttl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = [lambda: group1_case("normal", "purchase_order"),
             lambda: group1_case("management_receipt_lost_after_commit", "invoice"),
             lambda: group1_case("management_result_lost_after_commit", "purchase_order"),
             group2_parallel, group2_unknown,
             lambda: group2_rate_limit("local_slot"),
             lambda: group2_rate_limit("upstream_429"), group3_key_expiry]
    summary = []
    for run in cases:
        result = run()
        name = result["case"]
        (args.output / f"{name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        summary.append({"case": name, "run_id": result["run_id"],
                        "classification": result["classification"], "passed": True})
        print(name, "PASS", flush=True)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Reports: {args.output}")


if __name__ == "__main__":
    main()
