#!/usr/bin/env python3
"""Pinned, data-preserving OrderFlow Broker migration gate.

Root-only, explicit --deploy required. No provider calls. The default action
stages the reviewed release and checks an isolated state copy and credential.
Run only from a reviewed root-owned packet; never rerun a completed deployment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

SPEC = importlib.util.spec_from_file_location(
    "shared_gate", Path(__file__).with_name("upgrade-asus-shared-trial.py"))
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)
require, digest, run = shared.require, shared.digest, shared.run
OLD_COMMIT = "12cbed79dc81cf2b266fbddd9cbf6e806d7ff80a"
UNIT = Path("/etc/systemd/system/orderflow.service")
DROPINS = Path("/etc/systemd/system/orderflow.service.d")
CONFIG = Path("/etc/orderflow/broker.json")
CREDENTIAL = Path("/etc/api-quota-broker/credentials/client_token.cred")
# The shared validator independently checks this fixed policy and actual host.
BROKER_READINESS_CONTRACT_PIN = "81725056364b9c88296a38c93a745e90000fa57e"


def private_regular(path: Path) -> None:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
            info.st_mode & 0o077 == 0, "root-only regular file required")


def schema(path: Path) -> set[tuple]:
    with shared.db_readonly(path) as db:
        return set(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"))


def compare_migration(before: Path, after: Path, expected: set[tuple]) -> None:
    """Every old row/schema is unchanged; only the exact new table is allowed."""
    shared.check_db(after)
    shared.existing_rows_unchanged(before, after, migrated=False)
    old = schema(before)
    require(not any(row[2] == "broker_jobs" for row in old),
            "Broker mapping already exists; fresh integration gate cannot resume it")
    require(schema(after) - old == expected, "unexpected additive schema")
    with shared.db_readonly(after) as db:
        require(db.execute("SELECT COUNT(*) FROM broker_jobs").fetchone()[0] == 0,
                "new Broker table is not empty")


def expected_broker_schema(release: Path) -> set[tuple]:
    # Read only the pinned candidate's schema function, without starting workers.
    code = ("import sqlite3,json; from orderflow.broker_jobs import migrate; "
            "d=sqlite3.connect(':memory:'); migrate(d); "
            "print(json.dumps(list(d.execute('SELECT type,name,tbl_name,sql FROM sqlite_master'))))")
    result = subprocess.run([str(release / ".venv/bin/python"), "-c", code],
                            cwd=release, capture_output=True)
    require(result.returncode == 0, "candidate schema extraction failed")
    rows = {tuple(row) for row in json.loads(result.stdout)}
    require(rows and all(row[2] == "broker_jobs" for row in rows), "invalid Broker schema")
    return rows


def file_manifest(directory: Path) -> dict[str, str]:
    result = {}
    require(directory.is_dir() and not directory.is_symlink(), "state directory invalid")
    for path in directory.rglob("*"):
        require(not path.is_symlink() and (path.is_dir() or path.is_file()),
                "unexpected state file type")
        if path.is_file():
            result[str(path.relative_to(directory))] = digest(path)
    return result


def check_document_files(directory: Path) -> None:
    with shared.db_readonly(directory / "orderflow.sqlite3") as db:
        for doc_id, size, sha in db.execute("SELECT id,size,sha256 FROM documents"):
            require(isinstance(doc_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", doc_id),
                    "unexpected stored document id")
            path = directory / "files" / (doc_id + ".pdf")
            require(path.is_file() and not path.is_symlink() and
                    path.stat().st_size == size and digest(path) == sha,
                    "stored PDF missing or changed")


def migrate_copy(release: Path, source: Path, expected: set[tuple]) -> None:
    with tempfile.TemporaryDirectory(prefix="orderflow-broker-trial-") as name:
        trial = Path(name)
        shutil.copytree(source, trial, dirs_exist_ok=True, symlinks=True)
        file_manifest(trial)
        account = pwd.getpwnam("orderflow")
        run("chown", "-R", f"{account.pw_uid}:{account.pw_gid}", str(trial), phase="trial-owner")
        shared.migrate_as_orderflow(release, trial)
        compare_migration(source / "orderflow.sqlite3", trial / "orderflow.sqlite3", expected)
        shared.existing_files_unchanged(source, trial)
        check_document_files(trial)


def copy_online_state(destination: Path) -> None:
    destination.mkdir(mode=0o700)
    shared.sqlite_snapshot(shared.STATE / "orderflow.sqlite3", destination / "orderflow.sqlite3")
    # Private file set may change during this preflight; failure stops before
    # service interruption. The stopped-writer gate repeats every comparison.
    for source in shared.STATE.iterdir():
        if source.name in {"orderflow.sqlite3", "orderflow.sqlite3-wal", "orderflow.sqlite3-shm"}:
            continue
        require(not source.is_symlink(), "unexpected state symlink")
        if source.is_dir():
            shutil.copytree(source, destination / source.name, symlinks=True)
        else:
            shutil.copy2(source, destination / source.name, follow_symlinks=False)
    file_manifest(destination)
    check_document_files(destination)


def check_installed_base() -> None:
    require(shared.CURRENT.is_symlink() and
            shared.CURRENT.resolve() == shared.RELEASES / OLD_COMMIT, "live release changed")
    require(digest(UNIT) == digest(shared.RELEASES / OLD_COMMIT / "deploy/orderflow-asus.service"),
            "installed base unit changed")
    result = subprocess.run(["systemctl", "show", "orderflow", "-p", "DropInPaths", "--value"],
                            capture_output=True, text=True, check=True)
    require(not result.stdout.strip() and (not DROPINS.exists() or not list(DROPINS.iterdir())),
            "unexpected existing service drop-in")
    for prop, expected in (("ActiveState", "active"), ("SubState", "running"), ("NeedDaemonReload", "no")):
        actual = subprocess.run(["systemctl", "show", "orderflow", "-p", prop, "--value"],
                                capture_output=True, text=True, check=True).stdout.strip()
        require(actual == expected, "OrderFlow effective unit state changed")
    require(not CONFIG.exists() and not CONFIG.is_symlink(), "existing Broker config requires review")
    private_regular(CREDENTIAL)
    shared.require_regular_state_tree()
    require(shared.pending_jobs() == 0, "queued/running jobs must finish first")
    require(not any(row[2] == "broker_jobs" for row in schema(shared.STATE / "orderflow.sqlite3")),
            "existing Broker table requires separate review")
    shared.check_app()


def require_backup_space() -> None:
    total = sum(path.stat().st_size for path in shared.STATE.rglob("*") if path.is_file())
    # Online copy, migration copy, raw backup and stopped snapshot can coexist.
    # The reserve also covers the candidate virtualenv and ordinary service I/O.
    require(shutil.disk_usage(shared.BACKUPS.parent).free >= total * 6 + 512 * 1024 * 1024,
            "insufficient space for preserved state copies")


def stage_release(archive: Path, sha: str, commit: str, *, publish: bool = True) -> Path:
    final = shared.RELEASES / (commit if publish else f".broker-preflight-{commit[:12]}-{os.getpid()}")
    require(not final.exists() and not final.is_symlink(), "target release exists; inspect previous run")
    require(re.fullmatch(r"[0-9a-f]{64}", sha), "invalid archive digest")
    with tempfile.TemporaryDirectory(prefix=".broker-stage-", dir=shared.RELEASES) as name:
        directory = Path(name)
        saved = directory / "source.tar"
        shutil.copyfile(archive, saved)
        require(digest(saved) == sha, "source archive hash mismatch")
        release = directory / "release"
        release.mkdir()
        with tarfile.open(saved, "r:") as tar:
            members = tar.getmembers()
            require(members, "empty source archive")
            for member in members:
                path = Path(member.name)
                require(not path.is_absolute() and ".." not in path.parts and
                        (member.isfile() or member.isdir()), "unsafe archive entry")
            tar.extractall(release, filter="data")
        old = shared.RELEASES / OLD_COMMIT
        for filename in ("pyproject.toml", "uv.lock", "deploy/orderflow-asus.service"):
            require(digest(old / filename) == digest(release / filename),
                    "runtime dependencies or base service changed")
        for path in [old, *old.rglob("*")]:
            info = path.lstat()
            require(info.st_uid == 0 and (path.is_symlink() or info.st_mode & 0o022 == 0),
                    "trusted runtime ownership or permissions changed")
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                for component in (resolved, *resolved.parents):
                    target = component.stat()
                    require(target.st_uid == 0 and target.st_mode & 0o022 == 0,
                            "trusted runtime symlink target is writable")
        shutil.copytree(old / ".venv", release / ".venv", symlinks=True)
        run("chown", "-R", "root:root", str(release), phase="release-owner")
        run("chmod", "-R", "a+rX,go-w", str(release), phase="release-permissions")
        directory.chmod(0o755)
        run("runuser", "-u", "orderflow", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
            str(release / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests",
            "-p", "test_broker*.py", "-q", phase="candidate-contract-tests", cwd=release)
        os.replace(release, final)
    return final


def check_config(release: Path, path: Path) -> None:
    code = ("import json,sys; from orderflow.broker import BrokerConfig; "
            "c=BrokerConfig.from_snapshot(json.load(open(sys.argv[1]))); "
            "valid=(c.token_file is None and c.endpoint=='http://127.0.0.1:18084' "
            "and c.model=='gemini-3.5-flash-lite' "
            "and (c.max_pdf_bytes,c.max_request_bytes,c.max_result_bytes,c.max_output_tokens)"
            "==(8388608,16777216,4194304,8192) and c.bound_ready()); "
            "sys.exit(0 if valid else 2)")
    run(str(release / ".venv/bin/python"), "-c", code, str(path),
        phase="configuration-and-bound", cwd=release)


def credential_check(release: Path) -> None:
    code = ("import os; from pathlib import Path; from orderflow.broker import _read_token; "
            "_read_token(Path(os.environ['CREDENTIALS_DIRECTORY'])/'client_token')")
    # systemd, not the caller, decrypts and exposes the bound credential to the
    # same service account. No credential values or provider calls are emitted.
    run("systemd-run", "--quiet", "--wait", "--collect", "--pipe",
        "-p", "User=orderflow", "-p", "Group=orderflow", "-p", "UMask=0077",
        "-p", f"WorkingDirectory={release}", "-p", "NoNewPrivileges=yes",
        "-p", "PrivateNetwork=yes", "-p", "ProtectSystem=strict", "-p", "ProtectHome=yes",
        "-p", f"LoadCredentialEncrypted=client_token:{CREDENTIAL}",
        str(release / ".venv/bin/python"), "-c", code, phase="credential-binding")


def verify_unit(release: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="orderflow-broker-unit-") as name:
        directory = Path(name)
        shutil.copyfile(UNIT, directory / "orderflow.service")
        dropins = directory / "orderflow.service.d"
        dropins.mkdir()
        shutil.copyfile(release / "deploy/orderflow-broker.service.conf.example", dropins / "broker.conf")
        run("systemd-analyze", "verify", str(directory / "orderflow.service"), phase="verify-unit")


def check_broker_pin(path: Path, expected: str) -> None:
    require(path.is_symlink() and str(path.resolve()) == expected, "Broker runtime changed")


def deploy(release: Path, config: Path, backup: Path, expected: set[tuple], broker_check) -> None:
    stopped = False
    try:
        broker_check()
        check_installed_base()
        stopped = True
        run("systemctl", "stop", "orderflow", phase="stop-orderflow")
        shared.require_quiescent()
        require(shared.pending_jobs() == 0, "pending jobs appeared before stop")
        before = file_manifest(shared.STATE)
        raw = backup / "raw"
        shutil.copytree(shared.STATE, raw, symlinks=True)
        require(file_manifest(raw) == before == file_manifest(shared.STATE), "raw backup mismatch")
        snapshot = backup / "stopped-state"
        copy_online_state(snapshot)
        shared.existing_rows_unchanged(snapshot / "orderflow.sqlite3",
                                      shared.STATE / "orderflow.sqlite3", migrated=False)
        migrate_copy(release, snapshot, expected)
        shared.migrate_as_orderflow(release, shared.STATE)
        compare_migration(snapshot / "orderflow.sqlite3", shared.STATE / "orderflow.sqlite3", expected)
        shared.existing_files_unchanged(raw, shared.STATE)
        check_document_files(shared.STATE)
        shutil.copyfile(UNIT, backup / "original.service")
        shutil.copyfile(config, backup / "broker.json")
        dropin = release / "deploy/orderflow-broker.service.conf.example"
        shutil.copyfile(dropin, backup / "broker.service.conf")
        # Config has no secret, but is readable only by root and the app group.
        shutil.copyfile(config, CONFIG)
        os.chown(CONFIG, 0, pwd.getpwnam("orderflow").pw_gid)
        CONFIG.chmod(0o640)
        # /etc/orderflow was root-only. Traverse-only group access is necessary
        # for broker.json; private login-auth permissions remain untouched.
        os.chown(CONFIG.parent, 0, pwd.getpwnam("orderflow").pw_gid)
        CONFIG.parent.chmod(0o710)
        DROPINS.mkdir(mode=0o755, exist_ok=True)
        installed = DROPINS / "broker.conf"
        shutil.copyfile(dropin, installed)
        installed.chmod(0o644)
        broker_check()
        shared.replace_current(release.name)
        run("systemctl", "daemon-reload", phase="reload-unit")
        run("systemctl", "start", "orderflow", phase="start-orderflow")
        shared.wait_app(release)
        require(digest(CONFIG) == digest(config) and digest(installed) == digest(dropin),
                "installed configuration differs")
        print("ASUS OrderFlow Broker upgrade: PASS")
        print(f"Runtime: {release.name}; root-only backup: {backup}")
        print("Old tables and files preserved; provider and human acceptance not performed")
    except BaseException:
        if stopped:
            subprocess.run(["systemctl", "stop", "orderflow"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            print("HARD STOP: preserve state, backup, mappings; no automatic rollback", file=sys.stderr)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--broker-current", type=Path, required=True)
    parser.add_argument("--broker-release", required=True)
    parser.add_argument("--readiness-receipt", type=Path, required=True)
    parser.add_argument("--readiness-sha256", required=True)
    parser.add_argument("--deploy", action="store_true")
    args = parser.parse_args()
    require(os.geteuid() == 0, "root required")
    readiness_spec = importlib.util.spec_from_file_location(
        "broker_readiness", Path(__file__).with_name("broker_readiness.py"))
    readiness = importlib.util.module_from_spec(readiness_spec)
    readiness_spec.loader.exec_module(readiness)
    require(readiness.SOURCE == BROKER_READINESS_CONTRACT_PIN, "readiness source contract mismatch")
    def broker_check():
        check_broker_pin(args.broker_current, args.broker_release)
        return readiness.validate_readiness(args.readiness_receipt, args.readiness_sha256)
    require(re.fullmatch(r"[0-9a-f]{40}", args.commit), "invalid source commit")
    require(re.fullmatch(r"[0-9a-f]{64}", args.config_sha256), "invalid config digest")
    os.umask(0o077)
    check_installed_base()
    require_backup_space()
    broker_ready = broker_check()
    release = stage_release(args.archive, args.archive_sha256, args.commit, publish=args.deploy)
    backup = shared.BACKUPS / ("before-broker-" + args.commit[:12])
    require(not backup.is_symlink(), "backup path is a symlink")
    if backup.exists():
        info = backup.stat()
        require(backup.is_dir() and info.st_uid == 0 and info.st_mode & 0o077 == 0,
                "backup directory permissions changed")
        private_regular(backup / "preflight.json")
        previous = json.loads((backup / "preflight.json").read_text())
        require(not previous.get("deploy_requested") and not (backup / "raw").exists(),
                "previous deployment attempted; inspect before recovery")
        require(previous["source"] == args.commit and previous["archive_sha256"] == args.archive_sha256
                and previous["config_sha256"] == args.config_sha256
                and previous["broker_release"] == args.broker_release, "preflight inputs changed")
    else:
        backup.mkdir(mode=0o700, parents=True)
    saved_config = backup / "reviewed-config.json"
    shutil.copyfile(args.config, saved_config)
    require(digest(saved_config) == args.config_sha256, "config hash mismatch")
    check_config(release, saved_config)
    configured = json.loads(saved_config.read_text())
    require(configured["input_bound_evidence"] == "broker-pdf-" + BROKER_READINESS_CONTRACT_PIN
            and datetime.fromisoformat(configured["input_bound_expires_at"]) <=
                datetime.fromisoformat(broker_ready["bound_valid_until"]),
            "configuration exceeds reviewed Broker evidence")
    verify_unit(release)
    credential_check(release)
    expected = expected_broker_schema(release)
    online = backup / ("online-state-" + str(os.getpid()))
    copy_online_state(online)
    migrate_copy(release, online, expected)
    receipt = {"source": args.commit, "archive_sha256": args.archive_sha256,
               "config_sha256": args.config_sha256, "old_runtime": OLD_COMMIT,
               "broker_current": str(args.broker_current), "broker_release": args.broker_release,
               "preflight_at": datetime.now(timezone.utc).isoformat(), "deploy_requested": args.deploy}
    (backup / "preflight.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("ASUS OrderFlow isolated migration and credential preflight: PASS")
    if args.deploy:
        deploy(release, saved_config, backup, expected, broker_check)
    else:
        print("No service or live-state change; staged artifacts retained for review")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ASUS OrderFlow Broker gate: FAIL (" +
              (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__) + ")", file=sys.stderr)
        raise SystemExit(1) from None
