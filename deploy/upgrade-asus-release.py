#!/usr/bin/env python3
"""One-time ASUS code-only upgrade; preserves the live SQLite/PDF state.

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

OLD_COMMIT = "a6e619fca9c23bd7a36e6ed5bfde346a97b485b0"
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


def run(*command: str) -> None:
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def wait_app() -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
            response = conn.getresponse()
            response.read()
            conn.close()
            require(response.status == 200, "login page not ready")
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/api/bootstrap", headers={"Host": "momonong.me"})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            if response.status == 401 and body.get("error_code") == "AUTH_REQUIRED":
                return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.3)
    raise RuntimeError("protected ASUS app did not become ready")


def replace_current(commit: str) -> None:
    temporary = ROOT / f".current-{os.getpid()}"
    try:
        temporary.symlink_to(Path("releases") / commit)
        os.replace(temporary, CURRENT)
    finally:
        temporary.unlink(missing_ok=True)


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
            run("chown", "-R", "root:root", str(stage))
            run("chmod", "-R", "go-w", str(stage))
            stage.chmod(0o755)
            os.replace(stage, staged_release)
        run("runuser", "-u", "orderflow", "--", "test", "-x", str(staged_release / ".venv/bin/python"))
        run("runuser", "-u", "orderflow", "--", "test", "-r", str(staged_release / "orderflow/gemini.py"))
        subprocess.run(["runuser", "-u", "orderflow", "--", "env", "PYTHONDONTWRITEBYTECODE=1",
                        str(staged_release / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests", "-q"],
                       cwd=staged_release, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(staged_release, RELEASES / args.commit)
    finally:
        if staged_release.exists():
            shutil.rmtree(staged_release)

    stopped = False
    switched = False
    new_release = RELEASES / args.commit
    backup = BACKUPS / f"before-google-text-fix-{args.commit[:12]}"
    try:
        require(pending_jobs() == 0, "queued/running work appeared; upgrade stopped")
        stopped = True
        run("systemctl", "stop", "orderflow")
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
        replace_current(args.commit)
        switched = True
        run("systemctl", "start", "orderflow")
        wait_app()
        require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow"]).returncode == 0,
                "ASUS app not active after upgrade")
        print("ASUS code-only upgrade and protected loopback health: PASS")
        print(f"Runtime release: {args.commit}; pre-upgrade data backup: {backup}")
        print("SQLite/PDF/session preserved; in-memory AI keys cleared by restart")
    except Exception:
        recovered = not stopped
        if stopped:
            try:
                run("systemctl", "stop", "orderflow")
                if switched:
                    replace_current(OLD_COMMIT)
                run("systemctl", "start", "orderflow")
                wait_app()
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
        print(f"ASUS code-only upgrade: FAIL ({detail})", file=sys.stderr)
        raise SystemExit(1) from None
