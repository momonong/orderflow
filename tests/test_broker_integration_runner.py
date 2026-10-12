"""The deployment runner must preserve old rows and never invent a second job."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("integration_runner", Path(__file__).resolve().parents[1] / "deploy/run-broker-integration.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class IntegrationRunnerTests(unittest.TestCase):
    def test_old_rows_must_remain_with_synthetic_additions(self):
        with tempfile.TemporaryDirectory() as name:
            before, after = Path(name) / "before", Path(name) / "after"
            for path in (before, after):
                with sqlite3.connect(path) as db:
                    db.execute("CREATE TABLE private(value TEXT)")
                    db.execute("INSERT INTO private VALUES ('retained')")
                    db.execute("CREATE TABLE session_auth(session_id TEXT PRIMARY KEY)")
                    db.execute("INSERT INTO session_auth VALUES ('existing-owner')")
            with sqlite3.connect(after) as db:
                db.execute("INSERT INTO private VALUES ('synthetic')")
                db.execute("DELETE FROM session_auth")
            runner.preserved_rows(before, after)
            with sqlite3.connect(after) as db:
                db.execute("UPDATE private SET value='legitimate update' WHERE value='retained'")
            runner.preserved_rows(before, after)
            with sqlite3.connect(after) as db:
                db.execute("DELETE FROM private WHERE value='legitimate update'")
            with self.assertRaisesRegex(RuntimeError, "existing rows missing"):
                runner.preserved_rows(before, after)

    def test_failed_readiness_does_not_stop_or_prepare(self):
        with tempfile.TemporaryDirectory() as name, patch.object(runner.gate, "run") as run, patch.object(runner, "smoke_command") as prepare:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                runner.one_smoke(Path(name), Path(name), {}, Path(name), lambda: (_ for _ in ()).throw(RuntimeError("not ready")))
            run.assert_not_called()
            prepare.assert_not_called()

    def test_preparation_failure_stops_writer_and_preserves_intent(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            def fail(*args):
                (root / "synthetic-intent.json").write_text("retained")
                raise RuntimeError("fixture preparation failure")
            with patch.object(runner.gate, "run") as run, patch.object(runner.gate.shared, "pending_jobs", return_value=0), patch.object(runner.gate.shared, "require_quiescent"), patch.object(runner, "smoke_command", side_effect=fail), patch.object(runner.subprocess, "run") as stop:
                with self.assertRaisesRegex(RuntimeError, "fixture preparation"):
                    runner.one_smoke(root, root, {}, root, lambda: None)
                run.assert_called_once_with("systemctl", "stop", "orderflow", phase="synthetic-stop")
                stop.assert_called_once()
                self.assertEqual((root / "synthetic-intent.json").read_text(), "retained")

    def test_existing_intent_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as name, patch.object(runner.gate, "run") as run:
            root = Path(name)
            (root / "synthetic-intent.json").write_text("retained")
            with self.assertRaisesRegex(RuntimeError, "original-job recovery"):
                runner.one_smoke(root, root, {}, root, lambda: None)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
