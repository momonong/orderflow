#!/usr/bin/env python3
"""One-time ASUS record-date validation code upgrade; preserves the live SQLite/PDF state.

Run only as root from a separately reviewed root-owned copy. The archive is a
pinned `git archive` of the proposed commit; no user key or PDF is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

OLD_COMMIT = "ee41445565a73ea7e0b3444f0bb1bfad5e6ddd2a"
ROOT = Path("/opt/orderflow")
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
STATE = Path("/var/lib/orderflow")
BACKUPS = Path("/var/backups/orderflow")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(*command: str, phase: str, cwd: Path | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    require(result.returncode == 0, f"phase={phase} exit={result.returncode}")


def make_release_readable(stage: Path) -> None:
    # tarfile's data filter may yield 0700 directories under umask 077.
    # This tree contains only the reviewed git archive and copied venv.
    run("chmod", "-R", "a+rX,go-w", str(stage), phase="stage-permissions")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def pending_jobs() -> int:
    db = sqlite3.connect(f"file:{STATE / 'orderflow.sqlite3'}?mode=ro", uri=True)
    try:
        return db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
    finally:
        db.close()


def wait_app(expected_release: Path | None = None) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
            response = conn.getresponse()
            html = response.read()
            conn.close()
            require(response.status == 200 and b"OrderFlow" in html and b"manage.js" in html,
                    "management page not ready")
            if expected_release is not None:
                require(b"record-errors" in html and
                        hashlib.sha256(html).hexdigest() == digest(expected_release / "web/index.html"),
                        "new management HTML not served")
                conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
                conn.request("GET", "/orderflow/manage.js", headers={"Host": "momonong.me"})
                response = conn.getresponse()
                script = response.read()
                conn.close()
                require(response.status == 200 and b"normalizeRecordDate" in script and
                        hashlib.sha256(script).hexdigest() == digest(expected_release / "web/manage.js"),
                        "new management script not served")
                conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
                conn.request("GET", "/orderflow/manage.css", headers={"Host": "momonong.me"})
                response = conn.getresponse()
                style = response.read()
                conn.close()
                require(response.status == 200 and
                        hashlib.sha256(style).hexdigest() == digest(expected_release / "web/manage.css"),
                        "new management style not served")
            for route in ("/orderflow/api/bootstrap", "/orderflow/api/management/bootstrap"):
                conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
                conn.request("GET", route, headers={"Host": "momonong.me"})
                response = conn.getresponse()
                body = json.loads(response.read())
                conn.close()
                require(response.status == 401 and body.get("error_code") == "AUTH_REQUIRED",
                        "anonymous API protection missing")
            return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("protected ASUS app did not become ready")


def require_quiescent() -> None:
    # The managed service must be stopped and no separate process under its
    # dedicated account may still write the private StateDirectory.
    account_uid = pwd.getpwnam("orderflow").pw_uid
    for process in Path("/proc").iterdir():
        if process.name.isdecimal():
            try:
                if process.stat().st_uid == account_uid:
                    raise RuntimeError("orderflow account still has a process; stop and review writers")
            except FileNotFoundError:
                continue


def validate_unchanged_state(snapshot_path: Path) -> None:
    before = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    after = sqlite3.connect(f"file:{STATE / 'orderflow.sqlite3'}?mode=ro", uri=True)
    try:
        require(after.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                "live SQLite quick_check failed")
        tables = ("sessions", "session_auth", "documents", "jobs", "management_drafts",
                  "management_record_sets", "management_upload_keys")
        for table in tables:
            require(before.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    == after.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall(),
                    f"live {table} data changed before release switch")
    finally:
        before.close()
        after.close()


def replace_current(commit: str) -> None:
    temporary = ROOT / f".current-{os.getpid()}"
    try:
        temporary.symlink_to(Path("releases") / commit)
        os.replace(temporary, CURRENT)
    finally:
        temporary.unlink(missing_ok=True)


def recover_previous_release(switched: bool) -> None:
    # Stop first: a write may have completed after the preflight check.
    # Both releases share the same schema. Never restore an old data snapshot.
    run("systemctl", "stop", "orderflow", phase="rollback-stop-service")
    require_quiescent()
    if switched:
        replace_current(OLD_COMMIT)
    run("systemctl", "start", "orderflow", phase="rollback-start-service")
    wait_app()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, "root required")
    os.umask(0o077)
    require(HEX40.fullmatch(args.commit) is not None and args.commit != OLD_COMMIT,
            "invalid target commit")
    require(HEX64.fullmatch(args.archive_sha256) is not None, "invalid archive SHA")
    require(CURRENT.is_symlink() and os.readlink(CURRENT) == f"releases/{OLD_COMMIT}",
            "live release changed; stop for review")
    require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow"]).returncode == 0,
            "ASUS app is not active")
    require(STATE.is_dir() and (STATE / "orderflow.sqlite3").is_file(), "live state missing")
    require(pending_jobs() == 0, "queued/running work present; wait and review before upgrade")
    require(not (RELEASES / args.commit).exists(), "target release already exists")
    require(args.archive.is_file() and not args.archive.is_symlink(), "archive missing or symlinked")

    staged_release = RELEASES / f".upgrade-{args.commit}-{os.getpid()}"
    require(not staged_release.exists(), "temporary release already exists")
    try:
        with tempfile.TemporaryDirectory(prefix=".orderflow-upgrade-", dir=RELEASES) as private_name:
            private = Path(private_name)
            private.chmod(0o700)
            archive = private / "source.tar"
            shutil.copyfile(args.archive, archive)
            require(digest(archive) == args.archive_sha256, "archive checksum changed")
            stage = private / "release"
            stage.mkdir(mode=0o700)
            with tarfile.open(archive, "r:") as source:
                members = source.getmembers()
                require(bool(members), "empty release archive")
                for item in members:
                    name = Path(item.name)
                    require(not name.is_absolute() and ".." not in name.parts and
                            (item.isfile() or item.isdir()), "unsafe release archive entry")
                source.extractall(stage, filter="data")
            old = RELEASES / OLD_COMMIT
            for name in ("pyproject.toml", "uv.lock", "deploy/orderflow-asus.service"):
                require(digest(stage / name) == digest(old / name),
                        f"{name} changed; code-only upgrade gate cannot continue")
            shutil.copytree(old / ".venv", stage / ".venv", symlinks=True)
            run("chown", "-R", "root:root", str(stage), phase="stage-chown")
            make_release_readable(stage)
            stage.chmod(0o755)
            os.replace(stage, staged_release)
        run("runuser", "-u", "orderflow", "--", "test", "-x", str(staged_release / ".venv/bin/python"),
            phase="stage-python-access")
        run("runuser", "-u", "orderflow", "--", "test", "-r", str(staged_release / "orderflow/gemini.py"),
            phase="stage-code-access")
        run("runuser", "-u", "orderflow", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
            str(staged_release / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests", "-q",
            cwd=staged_release, phase="stage-unit-tests")
        os.replace(staged_release, RELEASES / args.commit)
    finally:
        if staged_release.exists():
            shutil.rmtree(staged_release)

    stopped = False
    switched = False
    new_release = RELEASES / args.commit
    backup = BACKUPS / f"before-record-date-validation-{args.commit[:12]}"
    try:
        require(pending_jobs() == 0, "queued/running work appeared; upgrade stopped")
        stopped = True
        run("systemctl", "stop", "orderflow", phase="stop-service")
        require_quiescent()
        require(pending_jobs() == 0, "queued/running work after stop; old app must be reviewed")
        require(not backup.exists(), "backup destination already exists")
        backup.mkdir(parents=True, mode=0o700)
        backup.chmod(0o700)
        # Preserve raw DB/WAL/PDF files and an independently consistent SQLite snapshot.
        raw = backup / "raw"
        shutil.copytree(STATE, raw, symlinks=False)
        for source in STATE.rglob("*"):
            if source.is_file():
                require(digest(source) == digest(raw / source.relative_to(STATE)),
                        "raw backup checksum mismatch")
        source_db = sqlite3.connect(f"file:{STATE / 'orderflow.sqlite3'}?mode=ro", uri=True)
        snapshot = sqlite3.connect(backup / "snapshot.sqlite3")
        try:
            source_db.backup(snapshot)
            require(snapshot.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                    "SQLite snapshot failed quick_check")
            for table in ("sessions", "session_auth", "documents", "jobs", "management_drafts",
                          "management_record_sets", "management_upload_keys"):
                require(source_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        == snapshot.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        "SQLite snapshot row count mismatch")
        finally:
            source_db.close()
            snapshot.close()
        validate_unchanged_state(backup / "snapshot.sqlite3")
        replace_current(args.commit)
        switched = True
        run("systemctl", "start", "orderflow", phase="start-service")
        wait_app(new_release)
        require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow"]).returncode == 0,
                "ASUS app not active after upgrade")
        print("ASUS record-date upgrade and protected loopback health: PASS")
        print(f"Runtime release: {args.commit}; pre-upgrade data backup: {backup}")
        print("SQLite/PDF/session preserved; in-memory AI keys cleared by restart")
    except Exception:
        recovered = not stopped
        if stopped:
            try:
                recover_previous_release(switched)
                recovered = True
                print("Upgrade failed; previous release restarted; state and backup retained", file=sys.stderr)
            except Exception:
                print("HARD STOP: ASUS service recovery uncertain; do not restart HP old app", file=sys.stderr)
        if (recovered and not switched and new_release.exists() and CURRENT.is_symlink()
                and os.readlink(CURRENT) == f"releases/{OLD_COMMIT}"):
            shutil.rmtree(new_release)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(f"ASUS record-date upgrade: FAIL ({detail})", file=sys.stderr)
        raise SystemExit(1) from None
