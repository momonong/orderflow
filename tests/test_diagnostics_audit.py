"""Critical job outcomes survive routine audit saturation."""

from collections import deque
from contextlib import redirect_stderr
import io
import json
import unittest
import uuid
from unittest.mock import patch

from orderflow import diagnostics


class AuditQuotaTests(unittest.TestCase):
    def test_poll_flood_cannot_hide_job_failure_and_drops_are_visible(self):
        trace = str(uuid.uuid4())
        job = str(uuid.uuid4())
        buckets = {name: deque() for name in ("routine", "client", "critical", "terminal")}
        counts = {name: 0 for name in buckets}
        output = io.StringIO()
        with (patch.object(diagnostics, "_MAX_EVENTS", 9),
              patch.object(diagnostics, "_times", buckets),
              patch.object(diagnostics, "_drop_counts", counts),
              patch.object(diagnostics, "_drop_started", 0.0),
              redirect_stderr(output)):
            for _ in range(20):
                diagnostics.audit_event("http", str(uuid.uuid4()), "received",
                                        {"route": "management_job_get", "trace_id": trace})
            diagnostics.audit_event("real_job", job, "ai_start", {"trace_id": trace})
            diagnostics.audit_event("real_job", job, "ai_response", {"trace_id": trace})
            diagnostics.audit_event("real_job", job, "db_committed", {"trace_id": trace})
            diagnostics.audit_event("real_job", job, "result_format_invalid", {
                "trace_id": trace, "code": "RESULT_FORMAT_INVALID", "format_reason": "FIELD_TYPE",
                "item_index": 0, "field": "qty", "actual_type": "number",
                "raw_value": "PRIVATE_CUSTOMER_MARKER",
            })
            diagnostics.audit_event("real_job", str(uuid.uuid4()), "internal_unknown", {
                "trace_id": trace, "code": "INTERNAL_UNKNOWN"})
            diagnostics.audit_event("real_job", str(uuid.uuid4()), "failed", {"trace_id": trace})

        records = [json.loads(line.removeprefix("orderflow_audit "))
                   for line in output.getvalue().splitlines()]
        job_phases = [record["phase"] for record in records if record.get("id") == job]
        self.assertIn("db_committed", job_phases)
        self.assertIn("result_format_invalid", job_phases)
        failure = next(record for record in records
                       if record.get("id") == job and record["phase"] == "result_format_invalid")
        self.assertEqual((failure["format_reason"], failure["field"], failure["code"]),
                         ("FIELD_TYPE", "qty", "RESULT_FORMAT_INVALID"))
        self.assertEqual((failure["version"], failure["build_id"]),
                         ("0.3.1", "diag-20261005-01"))
        self.assertNotIn("PRIVATE_CUSTOMER_MARKER", output.getvalue())
        self.assertIn("routine", [record.get("category") for record in records
                                  if record["event"] == "audit_limit"])
        self.assertIn("critical", [record.get("category") for record in records
                                   if record["event"] == "audit_limit"])
        self.assertIn("terminal", [record.get("category") for record in records
                                   if record["event"] == "audit_limit"])


if __name__ == "__main__":
    unittest.main()
