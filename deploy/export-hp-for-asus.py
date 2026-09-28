#!/usr/bin/env python3
"""Stop HP OrderFlow and export one consistent, checked snapshot to private staging."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from http.cookies import SimpleCookie

from migration_checks import inspect_state

OLD_RELEASE = "09712b6d828259e416d4f857447544fab4b09304"
OLD_CADDY = "b3b6dd9bb5a0b9338fb407e4aea9e9990aa9dadefcd56fdade613e3493f9645c"
PDF = base64.b64decode(
    "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoK"
    "MiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVu"
    "ZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoK"
    "NCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAu"
    "MCAwLjAgMTAwIDEwMCBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAw"
    "MDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4g"
    "CjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9T"
    "aXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo="
)


def require(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError(label)


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def request(method: str, path: str, basic: str, body: bytes | None = None,
            token: str | None = None, extra: dict[str, str] | None = None) -> tuple[int, object, str | None]:
    headers = {"Host": "momonong.me", "X-Forwarded-Proto": "https",
               "Authorization": "Basic " + basic, **(extra or {})}
    if token:
        headers["Cookie"] = "of_session=" + token
    if method in {"POST", "DELETE"}:
        headers.update({"Origin": "https://momonong.me", "X-Orderflow-Request": "1"})
    conn = http.client.HTTPConnection("127.0.0.1", 18080, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        value = json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data
        return response.status, value, response.getheader("Set-Cookie")
    finally:
        conn.close()


def synthetic_probe(password: str) -> tuple[str, str, str]:
    basic = base64.b64encode(("orderflow:" + password).encode()).decode()
    status, bootstrap, header = request("GET", "/orderflow/api/bootstrap", basic)
    require(status == 200 and isinstance(bootstrap.get("documents"), list), "old app bootstrap failed")
    cookie = SimpleCookie()
    cookie.load(header or "")
    require("of_session" in cookie, "old app session cookie missing")
    token = cookie["of_session"].value
    upload_headers = {"Content-Type": "application/pdf", "X-File-Size": str(len(PDF)),
                      "X-File-SHA256": hashlib.sha256(PDF).hexdigest(),
                      "X-Request-Key": str(uuid.uuid4())}
    status, doc, _ = request("POST", "/orderflow/api/documents", basic, PDF, token, upload_headers)
    require(status == 201 and doc.get("page_count") == 1, "old app synthetic PDF upload failed")
    payload = json.dumps({"document_id": doc["id"], "request_key": str(uuid.uuid4()),
                          "scenario": "success"}).encode()
    status, job, _ = request("POST", "/orderflow/api/jobs", basic, payload, token,
                             {"Content-Type": "application/json"})
    require(status == 202, "old app synthetic mock job failed")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status, job, _ = request("GET", "/orderflow/api/jobs/" + job["id"], basic, token=token)
        if status == 200 and job.get("state") not in {"queued", "running"}:
            break
        time.sleep(0.1)
    require(status == 200 and job.get("state") == "done" and job.get("mode") == "mock",
            "old app synthetic mock result failed")
    return token, doc["id"], job["id"]


def sha256_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def main() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == "hp-ubuntu2604-server", "wrong host or user")
    require(len(sys.argv) == 3 and re.fullmatch(r"[0-9a-f]{64}", sys.argv[2]) is not None,
            "pass ASUS public key path and independently checked SHA-256")
    public_key = Path(sys.argv[1])
    require(public_key.is_file() and not public_key.is_symlink() and
            sha256_file(public_key) == sys.argv[2], "ASUS recipient public key changed")
    require(Path("/opt/orderflow/current").readlink() == Path("releases") / OLD_RELEASE,
            "HP release changed")
    require(sha256_file(Path("/etc/caddy/Caddyfile")) == OLD_CADDY, "HP Caddyfile changed")
    run("systemctl", "is-active", "--quiet", "orderflow.service")
    run("systemctl", "is-enabled", "--quiet", "orderflow.service")
    root_state = Path("/var/lib/orderflow")
    require(root_state.is_dir(), "HP StateDirectory missing")
    password = Path("/etc/caddy/secrets/orderflow-password").read_text().strip()
    require(len(password) == 48 and all(c in "0123456789abcdef" for c in password),
            "HP credential format changed")
    credential = Path("/etc/caddy/secrets/orderflow-auth.caddy")
    require(credential.is_file() and not credential.is_symlink(), "HP bcrypt credential missing")
    require(credential.stat().st_size <= 318, "credential exceeds RSA-3072 OAEP-SHA256 capacity")
    token, document_id, job_id = synthetic_probe(password)
    backup_parent = Path("/var/backups/orderflow")
    backup_parent.mkdir(mode=0o700, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="asus-migration-", dir=backup_parent))
    root_public = backup / "recipient-public.pem"
    shutil.copyfile(public_key, root_public)
    root_public.chmod(0o600)
    require(sha256_file(root_public) == sys.argv[2], "ASUS public key changed during root copy")
    transfer_parent = Path("/home/morris/orderflow-migration")
    transfer_parent.mkdir(mode=0o700, exist_ok=True)
    transfer = Path(tempfile.mkdtemp(prefix="export-", dir=transfer_parent))
    rollback_needed = False
    try:
        # Fail the recipient-key and OAEP checks before stopping the live HP app.
        root_cipher = backup / "login-auth.oaep"
        run("openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(root_public),
            "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256", "-in", str(credential), "-out", str(root_cipher))
        root_cipher.chmod(0o600)
        rollback_needed = True
        run("systemctl", "disable", "--now", "orderflow.service")
        with socket.socket() as probe:
            probe.settimeout(0.3)
            require(probe.connect_ex(("127.0.0.1", 18081)) != 0, "HP app still listening")
        raw = backup / "raw"
        raw.mkdir(mode=0o700)
        run("cp", "-a", str(root_state) + "/.", str(raw))
        snapshot = backup / "snapshot"
        snapshot.mkdir(mode=0o700)
        (snapshot / "files").mkdir(mode=0o700)
        run("cp", "-a", str(raw / "files") + "/.", str(snapshot / "files"))
        source = sqlite3.connect(str(raw / "orderflow.sqlite3"), timeout=10)
        target = sqlite3.connect(str(snapshot / "orderflow.sqlite3"), timeout=10)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        summary = inspect_state(snapshot)
        # Interrupted jobs remain recorded; v0.3 marks them unknown on startup and never replays them.
        require((snapshot / "files" / f"{document_id}.pdf").is_file(), "synthetic PDF absent from snapshot")
        with tarfile.open(transfer / "state.tar", "w") as archive:
            archive.add(snapshot, arcname=".")
        shutil.copyfile(root_cipher, transfer / "login-auth.oaep")
        token_path = backup / "continuity-token"
        token_path.write_text(token)
        token_path.chmod(0o600)
        receipt = {"document_id": document_id, "job_id": job_id,
                   "snapshot": summary, "transfer_path": str(transfer),
                   "state_sha256": sha256_file(transfer / "state.tar"),
                   "sqlite_sha256": sha256_file(snapshot / "orderflow.sqlite3"),
                   "credential_ciphertext_sha256": sha256_file(transfer / "login-auth.oaep"),
                   "recipient_public_key_sha256": sys.argv[2]}
        receipt_path = backup / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))
        receipt_path.chmod(0o600)
        public_meta = {key: receipt[key] for key in ("snapshot", "state_sha256", "sqlite_sha256", "credential_ciphertext_sha256", "recipient_public_key_sha256")}
        (transfer / "meta.json").write_text(json.dumps(public_meta, sort_keys=True))
        morris = pwd.getpwnam("morris")
        for path in (transfer_parent, transfer, transfer / "state.tar",
                     transfer / "login-auth.oaep", transfer / "meta.json"):
            os.chown(path, morris.pw_uid, morris.pw_gid)
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        print("HP OrderFlow stopped and disabled; consistent SQLite backup and PDF check: PASS")
        print(f"Backup: {backup}")
        print(f"Private transfer: {transfer}")
        print(f"Rows: sessions={summary['sessions']} documents={summary['documents']} jobs={summary['jobs']} pending_at_stop={summary['pending_jobs']}")
        print(f"HP state tar SHA-256: {receipt['state_sha256']}")
        print(f"HP SQLite backup SHA-256: {receipt['sqlite_sha256']}")
        print(f"Credential ciphertext SHA-256: {receipt['credential_ciphertext_sha256']}")
    except Exception:
        if rollback_needed:
            try:
                # disable --now can stop or disable even when its final status is failure.
                run("systemctl", "enable", "--now", "orderflow.service")
                require(subprocess.run(["systemctl", "is-active", "--quiet", "orderflow.service"]).returncode == 0,
                        "HP service did not become active")
                print("HP export failed; old app restarted behind unchanged Basic Auth.", file=sys.stderr)
            except Exception:
                print("ROLLBACK FAILED: HP app did not restart; inspect service before any switch.", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"HP export: FAIL ({exc})", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"HP export: FAIL ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
