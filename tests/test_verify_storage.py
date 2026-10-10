import hashlib
import tempfile
import unittest
from pathlib import Path

from collector.verify_storage import sha256_file


class VerifyStorageTests(unittest.TestCase):
    def test_sha256_file_streams_exact_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "raw.bin"
            content = b"immutable evidence\x00" * 100000
            path.write_bytes(content)
            self.assertEqual(sha256_file(path), hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
