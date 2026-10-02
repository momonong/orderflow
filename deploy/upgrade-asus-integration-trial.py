#!/usr/bin/env python3
"""ASUS integration-trial upgrade with a stopped-writer SQLite backup.

Run only as root from a separately reviewed, root-owned copy. The source tar
must be a git archive of --commit and match the supplied SHA-256. This gate
does not change shared ingress, restore old data, or contact Google.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

OLD_COMMIT = "9182d8fd56becfee07651b99c6ab09a9e13e1fa4"
ROOT = Path("/opt/orderflow")
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
STATE = Path("/var/lib/orderflow")
BACKUPS = Path("/var/backups/orderflow")
STATIC_ROUTES = {
    "index.html": "/orderflow/", "test.html": "/orderflow/test/",
    "integration.html": "/orderflow/integration/",
    **{name: "/orderflow/" + name for name in (
        "app.js", "style.css", "manage.js", "manage.css", "diagnostics.js",
        "local-pdf-core.mjs", "local-pdf-worker.mjs",
        "vendor/pdfjs/pdf.min.mjs", "vendor/pdfjs/pdf.worker.min.mjs",
        "integration.js", "integration.css", "integration-xlsx.mjs",
        "integration-comparison.mjs", "integration-duplicates.mjs",
        "integration-activity.mjs")},
}
TRIAL_TABLES = ("trial_documents", "trial_links", "trial_products", "trial_product_codes")


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def run(*command: str, phase: str, cwd: Path | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    require(result.returncode == 0, f"phase={phase} exit={result.returncode}")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


@contextmanager
def db_readonly(path: Path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def check_db(path: Path) -> None:
    with db_readonly(path) as db:
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "SQLite integrity_check failed")
        require(db.execute("PRAGMA foreign_key_check").fetchone() is None,
                "SQLite foreign_key_check failed")


def pending_jobs() -> int:
    with db_readonly(STATE / "orderflow.sqlite3") as db:
        return db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]


def existing_rows_unchanged(before_path: Path, after_path: Path, *, migrated: bool) -> None:
    with db_readonly(before_path) as before, db_readonly(after_path) as after:
        schema = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        old_schema = {tuple(row) for row in before.execute(schema)}
        new_schema = {tuple(row) for row in after.execute(schema)}
        require(old_schema <= new_schema, "existing SQLite schema changed")
        require(before.execute("PRAGMA user_version").fetchone() ==
                after.execute("PRAGMA user_version").fetchone(), "SQLite user_version changed")
        tables = [row[0] for row in before.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            old_rows = Counter(tuple(row) for row in before.execute(f"SELECT * FROM {quoted}"))
            new_rows = Counter(tuple(row) for row in after.execute(f"SELECT * FROM {quoted}"))
            require(old_rows == new_rows, f"SQLite rows changed in {table}")
        if migrated:
            for table in TRIAL_TABLES:
                require(after.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                      (table,)).fetchone() is not None,
                        f"missing trial table {table}")


def existing_files_unchanged(before_dir: Path, after_dir: Path) -> None:
    for source in before_dir.rglob("*"):
        require(not source.is_symlink(), "backup contains a symlink")
        if source.is_file() and source.name not in (
                "orderflow.sqlite3", "orderflow.sqlite3-wal", "orderflow.sqlite3-shm"):
            target = after_dir / source.relative_to(before_dir)
            require(target.is_file() and digest(source) == digest(target),
                    "existing non-database file changed")


def require_regular_state_tree() -> None:
    for item in STATE.rglob("*"):
        require(not item.is_symlink() and (item.is_dir() or item.is_file()),
                "live state contains an unexpected file type")


def sqlite_snapshot(source: Path, target: Path) -> None:
    with db_readonly(source) as db, closing(sqlite3.connect(target)) as snapshot:
        db.backup(snapshot)
    check_db(target)


def require_quiescent() -> None:
    uid = pwd.getpwnam("orderflow").pw_uid
    for process in Path("/proc").iterdir():
        if process.name.isdecimal():
            try:
                if process.stat().st_uid == uid:
                    raise RuntimeError("orderflow account still has a process")
            except FileNotFoundError:
                continue


def migrate_as_orderflow(release: Path, data_dir: Path) -> None:
    code = "from pathlib import Path; from orderflow.app import Store; Store(Path(__import__('sys').argv[1]))"
    run("runuser", "-u", "orderflow", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
        str(release / ".venv/bin/python"), "-c", code, str(data_dir),
        phase="migrate-schema", cwd=release)


def trial_migration(release: Path, snapshot: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="orderflow-migration-trial-") as name:
        trial = Path(name)
        trial_db = trial / "orderflow.sqlite3"
        shutil.copyfile(snapshot, trial_db)
        uid = pwd.getpwnam("orderflow").pw_uid
        gid = pwd.getpwnam("orderflow").pw_gid
        os.chown(trial, uid, gid)
        os.chown(trial_db, uid, gid)
        trial.chmod(0o700)
        trial_db.chmod(0o600)
        migrate_as_orderflow(release, trial)
        check_db(trial_db)
        existing_rows_unchanged(snapshot, trial_db, migrated=True)


def stage_release(archive_path: Path, archive_sha: str, commit: str) -> Path:
    final = RELEASES / commit
    require(not final.exists(), "target release already exists")
    require(archive_path.is_file() and not archive_path.is_symlink(), "archive missing")
    require(re.fullmatch(r"[0-9a-f]{64}", archive_sha) is not None,
            "invalid archive SHA")
    staged = RELEASES / f".integration-trial-{commit[:12]}-{os.getpid()}"
    require(not staged.exists(), "temporary release already exists")
    with tempfile.TemporaryDirectory(prefix=".integration-trial-stage-", dir=RELEASES) as name:
        private = Path(name)
        private.chmod(0o700)
        archive = private / "source.tar"
        shutil.copyfile(archive_path, archive)
        require(digest(archive) == archive_sha, "archive checksum mismatch")
        release = private / "release"
        release.mkdir(mode=0o700)
        with tarfile.open(archive, "r:") as source:
            members = source.getmembers()
            require(bool(members), "empty release archive")
            for item in members:
                parts = Path(item.name)
                require(not parts.is_absolute() and ".." not in parts.parts and
                        (item.isfile() or item.isdir()), "unsafe release archive entry")
            source.extractall(release, filter="data")
        old = RELEASES / OLD_COMMIT
        for name in ("pyproject.toml", "uv.lock", "deploy/orderflow-asus.service"):
            require(digest(release / name) == digest(old / name),
                    f"runtime dependency or service changed: {name}")
        require(digest(Path("/etc/systemd/system/orderflow.service")) ==
                digest(old / "deploy/orderflow-asus.service"),
                "installed OrderFlow unit differs from reviewed release")
        for name in STATIC_ROUTES:
            require((release / "web" / name).is_file(), f"static file missing: {name}")
        shutil.copytree(old / ".venv", release / ".venv", symlinks=True)
        run("chown", "-R", "root:root", str(release), phase="stage-owner")
        run("chmod", "-R", "a+rX,go-w", str(release), phase="stage-access")
        release.chmod(0o755)
        os.replace(release, staged)
    try:
        run("runuser", "-u", "orderflow", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
            str(staged / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests", "-q",
            cwd=staged, phase="release-unit-tests")
        os.replace(staged, final)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return final


def replace_current(commit: str) -> None:
    temporary = ROOT / f".current-{os.getpid()}"
    try:
        temporary.symlink_to(Path("releases") / commit)
        os.replace(temporary, CURRENT)
    finally:
        temporary.unlink(missing_ok=True)


def get(path: str) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", 18081, timeout=3)
    try:
        connection.request("GET", path, headers={"Host": "momonong.me"})
        response = connection.getresponse()
        body = response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, body, headers
    finally:
        connection.close()


def check_app(release: Path | None = None) -> None:
    status, body, _ = get("/orderflow/")
    require(status == 200 and b"OrderFlow" in body, "login page unavailable")
    if release is not None:
        for filename, route in STATIC_ROUTES.items():
            status, body, headers = get(route)
            require(status == 200 and digest(release / "web" / filename)
                    == hashlib.sha256(body).hexdigest(), f"served static file mismatch: {filename}")
            require(headers.get("content-type", "").startswith("text/javascript") if filename.endswith((".js", ".mjs")) else True,
                    f"module MIME mismatch: {filename}")
            require("worker-src 'self'" in headers.get("content-security-policy", ""),
                    f"Worker CSP missing: {filename}")
    routes = ["/orderflow/api/bootstrap", "/orderflow/api/management/bootstrap",
              "/orderflow/api/health"]
    if release is not None:
        routes.append("/orderflow/api/integration/bootstrap")
    for route in routes:
        status, body, _ = get(route)
        require(status == 401 and json.loads(body).get("error_code") == "AUTH_REQUIRED",
                "anonymous API protection missing")


def wait_app(release: Path) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            check_app(release)
            return
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.3)
    raise RuntimeError("upgraded app did not become healthy")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, "root required")
    require(re.fullmatch(r"[0-9a-f]{40}", args.commit) is not None,
            "invalid commit")
    require(args.commit != OLD_COMMIT, "target is already live")
    os.umask(0o077)
    require(CURRENT.is_symlink() and os.readlink(CURRENT) == f"releases/{OLD_COMMIT}",
            "live release changed")
    require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow"]).returncode == 0,
            "ASUS app is not active")
    require(STATE.is_dir() and (STATE / "orderflow.sqlite3").is_file(), "live state missing")
    require_regular_state_tree()
    require(pending_jobs() == 0, "queued or running jobs present")
    check_app()
    release = stage_release(args.archive, args.archive_sha256, args.commit)
    backup = BACKUPS / f"before-integration-trial-{args.commit[:12]}"
    require(not backup.exists(), "backup already exists")
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    stopped = False
    try:
        # An online snapshot allows a migration trial before interrupting traffic.
        preflight = backup / "preflight.sqlite3"
        sqlite_snapshot(STATE / "orderflow.sqlite3", preflight)
        trial_migration(release, preflight)
        require(pending_jobs() == 0, "queued or running jobs appeared")
        stopped = True
        run("systemctl", "stop", "orderflow", phase="stop-service")
        require_quiescent()
        require(pending_jobs() == 0, "queued or running jobs remained after stop")
        require_regular_state_tree()
        raw = backup / "raw"
        shutil.copytree(STATE, raw, symlinks=False)
        for source in STATE.rglob("*"):
            if source.is_file():
                require(digest(source) == digest(raw / source.relative_to(STATE)),
                        "raw backup checksum mismatch")
        snapshot = backup / "snapshot.sqlite3"
        sqlite_snapshot(STATE / "orderflow.sqlite3", snapshot)
        existing_rows_unchanged(snapshot, STATE / "orderflow.sqlite3", migrated=False)
        trial_migration(release, snapshot)
        migrate_as_orderflow(release, STATE)
        check_db(STATE / "orderflow.sqlite3")
        existing_rows_unchanged(snapshot, STATE / "orderflow.sqlite3", migrated=True)
        existing_files_unchanged(raw, STATE)
        replace_current(args.commit)
        run("systemctl", "start", "orderflow", phase="start-service")
        wait_app(release)
        require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow"]).returncode == 0,
                "ASUS app not active after upgrade")
        print("ASUS integration trial upgrade: PASS")
        print(f"Runtime release: {args.commit}; root-only backup: {backup}")
        print("Existing SQLite rows and files preserved; authenticated browser trial still required")
    except Exception:
        if stopped:
            # New schema may already exist, and the public app may have accepted
            # a write. Never restore a snapshot or restart the old app blindly.
            subprocess.run(["systemctl", "stop", "orderflow"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            print("HARD STOP: preserve live state and backup; inspect before recovery", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(f"ASUS integration trial upgrade: FAIL ({detail})", file=sys.stderr)
        raise SystemExit(1) from None
