#!/usr/bin/env python3
"""One reviewed deployment and one durable synthetic job, run as root.

No provider HTTP is made here. The formal OrderFlow worker owns submission.
Once a deployment has started this command is not a restart/recovery command.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("integration_upgrade", "upgrade-asus-broker.py")


def preserved_rows(before: Path, after: Path):
    """After reopening, retain old identities while allowing normal row updates.

    Full row/schema equality is enforced by the stopped-writer deployment gate.
    Here a legitimate login may have rotated session_auth while smoke ran.
    """
    with gate.shared.db_readonly(before) as old, gate.shared.db_readonly(after) as new:
        for (table,) in old.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table == "session_auth":
                # A normal logout deletes this authorization row. Its owning
                # session and all document/business identities are still checked.
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            keys = sorted((row[5], row[1]) for row in old.execute(f"PRAGMA table_info({quoted})") if row[5])
            columns = ','.join('"' + name.replace('"', '""') + '"' for _, name in keys) or 'rowid'
            rows = Counter(tuple(row) for row in old.execute(f"SELECT {columns} FROM {quoted}"))
            actual = Counter(tuple(row) for row in new.execute(f"SELECT {columns} FROM {quoted}"))
            gate.require(not (rows - actual), "existing rows missing during smoke")


def smoke_command(release: Path, packet: Path, manifest: dict, intent: Path) -> dict:
    command = [str(release / ".venv/bin/python"), str(release / "deploy/orderflow-broker-smoke.py"),
               "--source-sha", manifest["source"], "--intent", str(intent),
               "--readiness-receipt", str(packet / "ready.json"),
               "--readiness-sha256", manifest["files"]["ready.json"]]
    result = subprocess.run(command, cwd=release, env={**os.environ, "PYTHONPATH": str(release),
                            "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=90)
    gate.require(result.returncode == 0 and len(result.stdout) <= 65536,
                 "synthetic preparation/read failed; retain original intent")
    return json.loads(result.stdout)


def one_smoke(release: Path, packet: Path, manifest: dict, backup: Path, broker_check) -> dict:
    intent = backup / "synthetic-intent.json"
    gate.require(not intent.exists(), "existing synthetic intent requires original-job recovery")
    broker_check()
    gate.require(gate.shared.pending_jobs() == 0, "pending jobs before synthetic stop")
    stopped = False
    try:
        stopped = True
        gate.run("systemctl", "stop", "orderflow", phase="synthetic-stop")
        gate.shared.require_quiescent()
        gate.require(gate.shared.pending_jobs() == 0, "pending jobs after synthetic stop")
        receipt = smoke_command(release, packet, manifest, intent)
        gate.require(receipt["state"] == "queued" and receipt["phase"] == "prepared"
                     and receipt["max_output_tokens"] == 2048, "unexpected initial synthetic state")
        gate.run("systemctl", "start", "orderflow", phase="synthetic-worker-start")
        gate.shared.wait_app(release)
        # Bounded observation of the same persistent job. A timeout does not
        # cancel it, change its key, clear holds, resume or generate another.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            receipt = smoke_command(release, packet, manifest, intent)
            if receipt["state"] in {"done", "failed"}:
                break
            time.sleep(3)
        preserved_rows(backup / "stopped-state/orderflow.sqlite3",
                       gate.shared.STATE / "orderflow.sqlite3")
        gate.shared.existing_files_unchanged(backup / "raw", gate.shared.STATE)
        gate.check_document_files(gate.shared.STATE)
        gate.shared.wait_app(release)
        return receipt
    except BaseException:
        if stopped:
            subprocess.run(["systemctl", "stop", "orderflow"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            print("HARD STOP: preserve original synthetic intent/job; do not rerun deployment", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    args = parser.parse_args()
    gate.require(os.geteuid() == 0, "root required")
    os.umask(0o077)
    packet = args.packet.resolve(strict=True)
    info = packet.stat()
    gate.require(info.st_uid == 0 and info.st_mode & 0o077 == 0, "private root packet required")
    gate.private_regular(packet / "manifest.json")
    manifest = json.loads((packet / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        gate.require(Path(name).name == name, "invalid packet member")
        gate.private_regular(packet / name)
        gate.require(gate.digest(packet / name) == expected, "packet hash mismatch")
    marker = packet / "started.json"
    with marker.open("x") as stream:
        stream.write(json.dumps({"source": manifest["source"], "started_at": time.time()}))
        stream.flush()
        os.fsync(stream.fileno())
    command = [sys.executable, str(packet / "upgrade-asus-broker.py"),
               "--archive", str(packet / "source.tar"), "--archive-sha256", manifest["files"]["source.tar"],
               "--commit", manifest["source"], "--config", str(packet / "broker.json"),
               "--config-sha256", manifest["files"]["broker.json"],
               "--broker-current", "/opt/api-quota-broker/current", "--broker-release", manifest["broker_release"],
               "--readiness-receipt", str(packet / "ready.json"),
               "--readiness-sha256", manifest["files"]["ready.json"], "--deploy"]
    gate.run(*command, phase="preserved-deployment")
    release = gate.shared.RELEASES / manifest["source"]
    backup = gate.shared.BACKUPS / ("before-broker-" + manifest["source"][:12])
    readiness = load("integration_readiness", "broker_readiness.py")
    receipt = one_smoke(release, packet, manifest, backup,
                        lambda: readiness.validate_readiness(packet / "ready.json", manifest["files"]["ready.json"]))
    (packet / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"deployment": "passed", "old_rows_and_files": "preserved", "synthetic": receipt}))
    if receipt["state"] != "done" or receipt["expected_items_match"] is not True:
        print("Synthetic acceptance incomplete; inspect original job only, no second submission")
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Integration gate failed: " + (str(exc) if isinstance(exc, RuntimeError)
                                             else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1) from None
