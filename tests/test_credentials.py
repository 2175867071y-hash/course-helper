"""本机 Windows DPAPI 密钥存储合同；只使用随机生成的测试数据。"""

import base64
import importlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    credentials = importlib.import_module('credentials')
except ModuleNotFoundError as exc:
    if exc.name != 'credentials':
        raise
    credentials = None


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(credentials, 'credentials.py 尚未实现')

    def test_rejects_empty_or_non_string_key_without_echoing_value(self):
        for value in ('', '   ', None, 123, b'synthetic-key'):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    credentials.protect_api_key(value)

    def test_rejects_oversized_or_unencodable_key_without_echoing_value(self):
        for value in ('x' * 20000, 'synthetic-\ud800-key'):
            with self.assertRaises(ValueError) as caught:
                credentials.protect_api_key(value)
            self.assertNotIn(value, str(caught.exception))

    def test_rejects_unknown_or_invalid_ciphertext_format(self):
        for value in ('', None, 123, 'synthetic-plaintext-key',
                      'dpapi:v2:AAAA', 'dpapi:v1:', 'dpapi:v1:????',
                      'dpapi:v1:AA\nAA', 'dpapi:v1:中文',
                      'dpapi:v1:' + 'A' * 100000):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    credentials.unprotect_api_key(value)

    @unittest.skipUnless(os.name == 'nt', '实际 Windows DPAPI 测试')
    def test_random_unicode_key_round_trips_without_plaintext_in_blob(self):
        key = 'synthetic-密钥-' + secrets.token_urlsafe(48) + '\0tail'
        saved = credentials.protect_api_key(key)
        self.assertTrue(saved.startswith('dpapi:v1:'))
        self.assertNotIn(key, saved)
        blob = base64.b64decode(saved[len('dpapi:v1:'):], validate=True)
        self.assertNotIn(key.encode('utf-8'), blob)
        self.assertEqual(credentials.unprotect_api_key(saved), key)

    @unittest.skipUnless(os.name == 'nt', '实际 Windows DPAPI 测试')
    def test_saved_key_can_be_decrypted_in_a_new_process(self):
        key = 'synthetic-' + secrets.token_urlsafe(48)
        saved = credentials.protect_api_key(key)
        code = ('import sys; from credentials import unprotect_api_key; '
                'sys.stdout.write(unprotect_api_key(sys.stdin.read()))')
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-B', '-c', code],
            cwd=Path(__file__).resolve().parents[1], input=saved,
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, key)

    @unittest.skipUnless(os.name == 'nt', '实际 Windows DPAPI 测试')
    def test_corrupt_ciphertext_fails_without_echoing_data(self):
        key = 'synthetic-' + secrets.token_urlsafe(48)
        saved = credentials.protect_api_key(key)
        blob = bytearray(base64.b64decode(saved[len('dpapi:v1:'):]))
        blob[-1] ^= 1
        corrupt = 'dpapi:v1:' + base64.b64encode(blob).decode('ascii')
        with self.assertRaises((OSError, ValueError)) as caught:
            credentials.unprotect_api_key(corrupt)
        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn(corrupt, str(caught.exception))

    @unittest.skipUnless(os.name == 'nt', '实际 Windows DPAPI 测试')
    def test_valid_base64_that_is_not_dpapi_fails_without_echoing_data(self):
        saved = 'dpapi:v1:' + base64.b64encode(b'synthetic-invalid-data').decode('ascii')
        with self.assertRaises((OSError, ValueError)) as caught:
            credentials.unprotect_api_key(saved)
        self.assertNotIn(saved, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
