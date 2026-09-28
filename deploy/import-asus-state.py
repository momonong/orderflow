#!/usr/bin/env python3
"""Import a checked HP snapshot into an ASUS-only, loopback OrderFlow service."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

from migration_checks import inspect_state

SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def require(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError(label)


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_manifest(directory: Path, expected: str) -> None:
    manifest = directory / "release-manifest.sha256"
    require(manifest.is_file() and digest(manifest) == expected, "release manifest changed")
    listed = set()
    for line in manifest.read_text().splitlines():
        require(len(line) >= 67 and line[64:66] == "  ", "invalid manifest entry")
        raw_name = line[66:]
        name = Path(raw_name).as_posix()
        file = directory / raw_name
        require(name not in listed and not Path(raw_name).is_absolute() and ".." not in Path(raw_name).parts
                and file.is_file() and not file.is_symlink(), "unexpected manifest path")
        listed.add(name)
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
              if path.is_file() and path != manifest
              and ".venv" not in path.relative_to(directory).parts}
    require(actual == listed, "staged source inventory differs from manifest")
    subprocess.run(["sha256sum", "-c", "release-manifest.sha256", "--status"],
                   cwd=directory, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def private_copy(source: Path, dest: Path, expected: str) -> None:
    require(source.is_file() and not source.is_symlink(), "transfer file missing or symlinked")
    shutil.copyfile(source, dest)
    dest.chmod(0o600)
    require(digest(dest) == expected, "transfer checksum mismatch")


def unpack_state(archive_path: Path, dest: Path) -> None:
    with tarfile.open(archive_path, "r") as archive:
        for member in archive.getmembers():
            name = Path(member.name)
            require(not name.is_absolute() and ".." not in name.parts and
                    (member.isfile() or member.isdir()), "unexpected state archive entry")
        archive.extractall(dest, filter="data")


def wait_app() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/", headers={"Host": "momonong.me"})
            page = conn.getresponse()
            page.read()
            conn.close()
            if page.status != 200:
                raise RuntimeError("login page not ready")
            conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
            conn.request("GET", "/orderflow/api/bootstrap", headers={"Host": "momonong.me"})
            api = conn.getresponse()
            data = json.loads(api.read())
            conn.close()
            if api.status == 401 and data.get("error_code") == "AUTH_REQUIRED":
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    raise RuntimeError("ASUS app did not become ready with protected API")


def main() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == "asus-ubuntu2604-server", "wrong host or user")
    require(len(sys.argv) == 5, "pass exact app commit, manifest, HP state tar and SQLite digests")
    commit, manifest_sha, expected_state_sha, expected_sqlite_sha = sys.argv[1:]
    require(COMMIT.fullmatch(commit) is not None and all(
            SHA.fullmatch(value) is not None for value in (manifest_sha, expected_state_sha, expected_sqlite_sha)),
            "invalid commit or digest")
    stage = Path("/home/morris/orderflow-staging") / commit
    transfer = Path("/home/morris/orderflow-migration/import")
    release = Path("/opt/orderflow/releases") / commit
    current = Path("/opt/orderflow/current")
    unit = Path("/etc/systemd/system/orderflow.service")
    state = Path("/var/lib/orderflow")
    credential = Path("/etc/orderflow/login-auth.caddy")
    require(not release.exists() and not current.exists() and not current.is_symlink()
            and not unit.exists() and not state.exists() and not credential.exists(),
            "ASUS OrderFlow target already exists")
    require(subprocess.run(["id", "orderflow"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode != 0, "service account already exists")
    with socket.socket() as probe:
        probe.settimeout(0.3)
        require(probe.connect_ex(("127.0.0.1", 18081)) != 0, "ASUS app port is busy")
    verify_manifest(stage, manifest_sha)
    metadata_path = transfer / "meta.json"
    require(metadata_path.is_file() and not metadata_path.is_symlink(), "transfer metadata missing")
    metadata = json.loads(metadata_path.read_text())
    state_sha, sqlite_sha, auth_sha = (metadata[key] for key in
                                      ("state_sha256", "sqlite_sha256", "auth_sha256"))
    stable_sha = metadata["snapshot"]["stable_digest"]
    require(state_sha == expected_state_sha and sqlite_sha == expected_sqlite_sha,
            "ASUS transfer differs from independently reviewed HP export")
    require(all(SHA.fullmatch(item) for item in (state_sha, sqlite_sha, auth_sha, stable_sha)),
            "invalid transfer digest")
    private = Path(tempfile.mkdtemp(prefix="orderflow-import-", dir="/tmp"))
    service_transition_attempted = False
    try:
        private_copy(transfer / "state.tar", private / "state.tar", state_sha)
        private_copy(transfer / "login-auth.caddy", private / "login-auth.caddy", auth_sha)
        lines = [line.strip() for line in (private / "login-auth.caddy").read_bytes().splitlines() if line.strip()]
        require(len(lines) == 3 and lines[0] == b"basic_auth {" and
                lines[1].startswith(b"orderflow $2") and lines[2] == b"}",
                "bcrypt credential format changed")
        extracted = private / "data"
        extracted.mkdir(mode=0o700)
        unpack_state(private / "state.tar", extracted)
        require(digest(extracted / "orderflow.sqlite3") == sqlite_sha,
                "SQLite backup bytes changed during transfer")
        summary = inspect_state(extracted)
        require(summary["stable_digest"] == stable_sha, "snapshot rows or ownership changed")
        run("useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
            "--shell", "/usr/sbin/nologin", "orderflow")
        owner = pwd.getpwnam("orderflow")
        Path("/opt/orderflow/releases").mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, release, symlinks=True)
        run("chown", "-R", "root:root", str(release))
        run("chmod", "-R", "go-w", str(release))
        verify_manifest(release, manifest_sha)
        current.symlink_to(Path("releases") / commit)
        state.mkdir(mode=0o700)
        run("cp", "-a", str(extracted) + "/.", str(state))
        run("chown", "-R", "orderflow:orderflow", str(state))
        state.chmod(0o700)
        credential.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(private / "login-auth.caddy", credential)
        credential.chmod(0o600)
        shutil.copyfile(release / "deploy/orderflow-asus.service", unit)
        unit.chmod(0o644)
        run("systemd-analyze", "verify", str(unit))
        run("systemctl", "daemon-reload")
        service_transition_attempted = True
        run("systemctl", "enable", "--now", "orderflow.service")
        wait_app()
        after = inspect_state(state)
        require(after["stable_digest"] == stable_sha and
                all(after[key] == summary[key] for key in ("sessions", "documents", "jobs")),
                "migrated rows or PDFs changed after ASUS app start")
        require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow.service"]).returncode == 0,
                "ASUS app service not active")
        print("ASUS OrderFlow imported; protected loopback app and data integrity: PASS")
        print(f"Rows: sessions={summary['sessions']} documents={summary['documents']} jobs={summary['jobs']}")
        print(f"Pending jobs at HP stop: {summary['pending_jobs']} (not replayed)")
    except Exception:
        if service_transition_attempted:
            try:
                run("systemctl", "disable", "--now", "orderflow.service")
                require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow.service"]).returncode != 0,
                        "ASUS service remained active after stop")
                with socket.socket() as probe:
                    probe.settimeout(0.3)
                    require(probe.connect_ex(("127.0.0.1", 18081)) != 0,
                            "ASUS app still listens after stop")
            except Exception:
                print("HARD STOP: ASUS service may still be active; do not restart HP app or switch Caddy.", file=sys.stderr)
        print("ASUS import failed; staged transfer and all created state retained for diagnosis.", file=sys.stderr)
        raise
    finally:
        shutil.rmtree(private)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ASUS import: FAIL ({exc})", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"ASUS import: FAIL ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
