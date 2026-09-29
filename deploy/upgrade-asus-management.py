#!/usr/bin/env python3
"""One-time ASUS management upgrade; preserves the live SQLite/PDF state.

Run only as root from a separately reviewed root-owned copy. The archive is a
pinned `git archive` of the proposed commit; no user key or PDF is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

OLD_COMMIT = "eb2f928ce213a6b4b23dca6b04f095f5adc98bb9"
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


def wait_app(expect_management: bool = True) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
            response = conn.getresponse()
            html = response.read()
            conn.close()
            expected_asset = b"manage.js" if expect_management else b"app.js"
            require(response.status == 200 and b"OrderFlow" in html and expected_asset in html,
                    "expected page not ready")
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/api/bootstrap", headers={"Host": "momonong.me"})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            if response.status != 401 or body.get("error_code") != "AUTH_REQUIRED":
                continue
            if not expect_management:
                return
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/api/management/bootstrap", headers={"Host": "momonong.me"})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            if response.status == 401 and body.get("error_code") == "AUTH_REQUIRED":
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("protected ASUS app did not become ready")


def management_writes_exist() -> bool:
    db = sqlite3.connect(f"file:{STATE / 'orderflow.sqlite3'}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if "purpose" not in columns:
            return False
        return (db.execute("SELECT EXISTS(SELECT 1 FROM documents WHERE purpose='management')").fetchone()[0] == 1
                or db.execute("SELECT EXISTS(SELECT 1 FROM management_drafts)").fetchone()[0] == 1)
    finally:
        db.close()


def validate_upgraded_state(snapshot_path: Path) -> None:
    before = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    after = sqlite3.connect(f"file:{STATE / 'orderflow.sqlite3'}?mode=ro", uri=True)
    try:
        require(after.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                "upgraded SQLite quick_check failed")
        for table in ("sessions", "session_auth", "documents", "jobs"):
            require(before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    == after.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    f"upgraded {table} row count changed")
        require(after.execute("SELECT COUNT(*) FROM documents WHERE purpose!='diagnostic'").fetchone()[0] == 0,
                "legacy document purpose changed")
        require(after.execute("SELECT COUNT(*) FROM management_drafts").fetchone()[0] == 0,
                "unexpected management draft during upgrade")
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
    # Stop and wait for the new process to exit before inspecting persistent writes.
    # The preflight pending-job check cannot close the post-switch race.
    run("systemctl", "stop", "orderflow", phase="rollback-stop-service")
    if switched:
        require(not management_writes_exist(),
                "management writes exist; old runtime rollback would expose them; fix forward")
        replace_current(OLD_COMMIT)
    # No backup restore: diagnostic sessions/documents/jobs written after switch
    # remain in the same live state and are readable by the old runtime.
    run("systemctl", "start", "orderflow", phase="rollback-start-service")
    wait_app(expect_management=False)


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
    backup = BACKUPS / f"before-management-shell-{args.commit[:12]}"
    try:
        require(pending_jobs() == 0, "queued/running work appeared; upgrade stopped")
        stopped = True
        run("systemctl", "stop", "orderflow", phase="stop-service")
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
            for table in ("sessions", "documents", "jobs"):
                require(source_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        == snapshot.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        "SQLite snapshot row count mismatch")
        finally:
            source_db.close()
            snapshot.close()
        run("runuser", "-u", "orderflow", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
            str(new_release / ".venv/bin/python"), "-c",
            "from pathlib import Path; from orderflow.app import Store; Store(Path('/var/lib/orderflow'))",
            cwd=new_release, phase="migrate-state")
        validate_upgraded_state(backup / "snapshot.sqlite3")
        replace_current(args.commit)
        switched = True
        run("systemctl", "start", "orderflow", phase="start-service")
        wait_app()
        require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow"]).returncode == 0,
                "ASUS app not active after upgrade")
        print("ASUS management upgrade and protected loopback health: PASS")
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
        print(f"ASUS management upgrade: FAIL ({detail})", file=sys.stderr)
        raise SystemExit(1) from None
