"""The code-only ASUS gate must preserve every existing table and schema."""

from pathlib import Path
import runpy
import shutil
import sqlite3
import tempfile
import unittest


GATE = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                          "deploy/upgrade-asus-diagnostics.py"))


class DiagnosticsGateTests(unittest.TestCase):
    def test_existing_local_sources_and_other_rows_must_survive(self):
        with tempfile.TemporaryDirectory() as name:
            before = Path(name) / "before.sqlite3"
            after = Path(name) / "after.sqlite3"
            with sqlite3.connect(before) as db:
                db.execute("CREATE TABLE management_local_sources (id INTEGER PRIMARY KEY, note TEXT)")
                db.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, value BLOB)")
                db.execute("INSERT INTO management_local_sources VALUES (7, 'existing')")
                db.execute("INSERT INTO sessions VALUES (2, X'000102')")
            shutil.copyfile(before, after)
            GATE["db_unchanged"](before, after)
            with sqlite3.connect(after) as db:
                db.execute("UPDATE management_local_sources SET note='changed' WHERE id=7")
            with self.assertRaisesRegex(RuntimeError, "management_local_sources"):
                GATE["db_unchanged"](before, after)

    def test_code_only_gate_rejects_new_schema(self):
        with tempfile.TemporaryDirectory() as name:
            before = Path(name) / "before.sqlite3"
            after = Path(name) / "after.sqlite3"
            with sqlite3.connect(before) as db:
                db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
            shutil.copyfile(before, after)
            with sqlite3.connect(after) as db:
                db.execute("CREATE TABLE unexpected (id INTEGER PRIMARY KEY)")
            with self.assertRaisesRegex(RuntimeError, "schema changed"):
                GATE["db_unchanged"](before, after)


if __name__ == "__main__":
    unittest.main()
