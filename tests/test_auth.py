import tempfile
import unittest
from pathlib import Path

import bcrypt

from orderflow.auth import load_caddy_hash, verify_password


class CredentialTests(unittest.TestCase):
    def test_fixed_account_hash_and_invalid_files(self):
        password = 'a valid test password'
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'credential'
            with self.assertRaises(FileNotFoundError):
                load_caddy_hash(path)
            path.write_bytes(b'basic_auth {\n\torderflow ' + hashed + b'\n}\n')
            self.assertEqual(load_caddy_hash(path), hashed)
            self.assertTrue(verify_password(password, hashed))
            self.assertFalse(verify_password('wrong', hashed))
            for bad in (
                b'basic_auth {\n\tother ' + hashed + b'\n}\n',
                b'basic_auth {\n\torderflow ' + hashed[:-1] + b'?\n}\n',
                b'basic_auth {\n\torderflow ' + hashed + b'\n}\nextra\n',
            ):
                path.write_bytes(bad)
                with self.assertRaises(ValueError):
                    load_caddy_hash(path)

    def test_password_limits(self):
        hashed = bcrypt.hashpw(b'valid', bcrypt.gensalt(rounds=4))
        for bad in (None, '', 'x' * 73, 'nul\x00byte', 123):
            self.assertFalse(verify_password(bad, hashed))
