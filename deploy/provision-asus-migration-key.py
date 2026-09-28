#!/usr/bin/env python3
"""Create a one-time ASUS root key; publish only its RSA-OAEP public key."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == "asus-ubuntu2604-server", "wrong host or user")
    directory = Path("/etc/orderflow")
    private = directory / "migration-private.pem"
    public = Path("/home/morris/orderflow-migration/asus-public.pem")
    require(not private.exists() and not public.exists() and
            not Path("/etc/orderflow/login-auth.caddy").exists() and
            not Path("/opt/orderflow/current").exists(), "ASUS migration target already provisioned")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    public.parent.mkdir(mode=0o700, exist_ok=True)
    public.parent.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="key-check-", dir=directory) as temp:
        work = Path(temp)
        secret = work / "private.pem"
        candidate = work / "public.pem"
        run("openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
            "-out", str(secret))
        secret.chmod(0o600)
        run("openssl", "pkey", "-in", str(secret), "-pubout", "-out", str(candidate))
        probe = work / "probe"
        probe.write_bytes(b"synthetic one-time transfer self-test")
        cipher = work / "probe.oaep"
        plain = work / "probe.copy"
        run("openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(candidate),
            "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256", "-in", str(probe), "-out", str(cipher))
        run("openssl", "pkeyutl", "-decrypt", "-inkey", str(secret),
            "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256", "-in", str(cipher), "-out", str(plain))
        require(plain.read_bytes() == probe.read_bytes(), "RSA-OAEP self-test failed")
        os.replace(secret, private)
        private.chmod(0o600)
        shutil.copyfile(candidate, public)
        owner = pwd.getpwnam("morris")
        os.chown(public.parent, owner.pw_uid, owner.pw_gid)
        os.chown(public, owner.pw_uid, owner.pw_gid)
        public.chmod(0o644)
    print("ASUS root-only migration private key and synthetic OAEP self-test: PASS")
    print(f"Public key SHA-256: {hashlib.sha256(public.read_bytes()).hexdigest()}")
    print(f"Public key: {public}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ASUS transfer key: FAIL ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
