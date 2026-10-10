"""Final attachment contracts use synthetic fixtures, not private payloads."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_media', ROOT / 'scripts/submission_package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class MediaTests(unittest.TestCase):
    def fixture(self, folder):
        assets = []
        for kind, name in [('presentation', 'demo.pptx'), ('demo_video', 'demo.mp4')]:
            data = b'fixture-' + kind.encode()
            (folder / name).write_bytes(data)
            assets.append({'filename': name, 'kind': kind, 'bytes': len(data),
                           'sha256': hashlib.sha256(data).hexdigest(), 'duration_seconds': 193.577})
        path = folder / 'manifest.json'
        path.write_text(json.dumps({'version': 'v1.0', 'assets': assets}))
        return path, assets

    def test_exact_bytes_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); manifest, _ = self.fixture(folder)
            self.assertEqual(2, len(package.load_media_assets(manifest, folder)[1]))

    def test_changed_content_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); manifest, _ = self.fixture(folder)
            (folder / 'demo.mp4').write_bytes(b'changed')
            with self.assertRaises(ValueError): package.load_media_assets(manifest, folder)

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); manifest, assets = self.fixture(folder)
            assets[0]['filename'] = '../demo.pptx'
            manifest.write_text(json.dumps({'assets': assets}))
            with self.assertRaises(ValueError): package.load_media_assets(manifest, folder)

    def test_duration_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); manifest, assets = self.fixture(folder)
            assets[1]['duration_seconds'] = 179
            manifest.write_text(json.dumps({'assets': assets}))
            with self.assertRaises(ValueError): package.load_media_assets(manifest, folder)

    def test_archive_reuse_requires_identical_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); payload = folder / 'data'; payload.write_bytes(b'original')
            archive = folder / 'before.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle: bundle.add(payload, arcname='data')
            expected = {'data': {'bytes': 8, 'sha256': package.digest(payload)}}
            package.reuse_verified_archive(archive, folder / 'after.tar.gz', expected)
            self.assertEqual(archive.read_bytes(), (folder / 'after.tar.gz').read_bytes())
            expected['data']['sha256'] = '0' * 64
            with self.assertRaises(ValueError): package.reuse_verified_archive(archive, folder / 'bad.tar.gz', expected)


if __name__ == '__main__':
    unittest.main()
