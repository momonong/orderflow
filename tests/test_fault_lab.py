"""Real loopback HTTP and persistence checks for the synthetic fault lab."""

import http.client
import json
import unittest

from tools.fault_lab import Client, Lab, exercise


class FaultLabTests(unittest.TestCase):
    def test_lost_create_receipt_recovers_saved_job_without_resending_ai(self):
        with Lab("receipt_lost_after_commit") as lab:
            report = exercise(lab)
        self.assertTrue(report["verdict"]["scenario_expectation_met"])
        self.assertEqual(report["injection_truth"]["fake_ai_calls"], 1)
        self.assertEqual(report["final_db_state"]["job_count"], 1)
        self.assertEqual(report["final_db_state"]["jobs"][0]["state"], "done")
        self.assertTrue(report["verdict"]["saved_result_read_by_get"])

    def test_lost_result_response_recovers_same_job(self):
        with Lab("result_lost_after_commit") as lab:
            report = exercise(lab)
        self.assertTrue(report["verdict"]["scenario_expectation_met"])
        self.assertEqual(report["injection_truth"]["fake_ai_calls"], 1)
        queries = [item for item in report["script_observations"]
                   if item["route"].startswith("/orderflow/api/jobs/")]
        self.assertEqual([item["status"] for item in queries], [None, 200])

    def test_proxy_preserves_application_csrf_guard_and_rejects_foreign_sites(self):
        with Lab("normal") as lab:
            client = Client(lab)
            client.setup()
            port = lab.proxy.server_port

            def request(method, path, headers, body=b""):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                status = response.status
                marker = response.getheader("X-Orderflow-Origin")
                response.read()
                conn.close()
                return status, marker

            self.assertEqual(request("GET", "/__lab/session", {"Host": "evil.example"})[0], 403)
            self.assertEqual(request("GET", "/__lab/session", {
                "Host": f"127.0.0.1:{port}", "Sec-Fetch-Site": "cross-site"})[0], 403)
            body = json.dumps({"document_id": "x", "request_key": "y", "scenario": "real"}).encode()
            self.assertEqual(request("POST", "/orderflow/api/jobs", {
                "Host": f"127.0.0.1:{port}", "Origin": "https://evil.example",
                "X-Orderflow-Request": "1", "Cookie": client.cookie,
                "Content-Type": "application/json"}, body)[0], 403)
            # With the right Origin but no custom header, the real backend rejects
            # a plain cross-site form; the proxy must not add the header for it.
            self.assertEqual(request("POST", "/orderflow/api/jobs", {
                "Host": f"127.0.0.1:{port}", "Origin": lab.url,
                "Cookie": client.cookie, "Content-Type": "application/json"}, body),
                (403, "app"))
            self.assertEqual(lab.state()["job_count"], 0)
            fake = http.client.HTTPConnection("127.0.0.1", lab.fake.server_port, timeout=3)
            fake.request("POST", "/fake-google/generate", body=b"{}", headers={
                "Origin": "https://evil.example", "Content-Type": "application/json"})
            response = fake.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            fake.close()
            self.assertEqual(lab.fake.call_count, 0)

    def test_synthetic_session_refresh_preserves_document_and_report_excludes_secrets(self):
        with Lab("normal") as lab:
            client = Client(lab)
            client.setup()
            document_id = client.upload()
            client.setup()
            bootstrap = client.request("GET", "/orderflow/api/bootstrap")
            self.assertEqual(bootstrap["status"], 200)
            self.assertIn(document_id, [item["id"] for item in bootstrap["value"]["documents"]])
            self.assertTrue(bootstrap["value"]["ai_key_configured"])
            page = client.request("GET", "/orderflow/test/")
            self.assertEqual(page["status"], 200)
            report = exercise(lab)
            serialized = json.dumps(report)
            self.assertNotIn(lab.password, serialized)
            self.assertNotIn(lab.key, serialized)
            self.assertNotIn("Synthetic item", serialized)


if __name__ == "__main__":
    unittest.main()
