#!/usr/bin/env python3
"""Read-only archive/digest checks; does not extract or alter runtime databases."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile


def sha(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()


def safe(name):
    path = Path(name)
    return not path.is_absolute() and '..' not in path.parts and '.private' not in path.parts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    out = args.directory
    meta = json.loads((out / 'SUBMISSION-MANIFEST.json').read_text())
    version = meta['version']
    checked = 0
    for line in (out / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        assert Path(name).name == name
        with (out / name).open('rb') as stream:
            assert sha(stream) == expected, name
        checked += 1
    media = meta.get('media')
    if media:
        assert media['version'] == version
        for item in media['assets']:
            path = out / item['filename']
            assert safe(item['filename']) and path.name == item['filename']
            assert path.stat().st_size == item['bytes']
            with path.open('rb') as stream:
                assert sha(stream) == item['sha256']
            if item['kind'] == 'presentation':
                with zipfile.ZipFile(path) as slides:
                    assert slides.testzip() is None
            elif item['kind'] == 'demo_video':
                assert 180 <= item['duration_seconds'] <= 300
                assert item['full_decode_passed']
    with tarfile.open(out / f'issuertrace-runtime-{version}.tar.gz') as bundle:
        members = bundle.getmembers()
        assert all(m.isfile() and safe(m.name) for m in members)
        manifest = json.load(bundle.extractfile('RUNTIME-MANIFEST.json'))
        assert set(manifest) | {'RUNTIME-MANIFEST.json'} == {m.name for m in members}
        for name, item in manifest.items():
            member = bundle.getmember(name)
            assert member.size == item['bytes']
            assert sha(bundle.extractfile(member)) == item['sha256'], name
    with zipfile.ZipFile(out / f'issuertrace-reports-{version}.zip') as bundle:
        assert bundle.testzip() is None
        assert set(bundle.namelist()) == set(meta['reports']) | set(meta.get('supporting_reports', []))
        assert len(meta['reports']) == 27 and all(safe(n) for n in bundle.namelist())
        for name in bundle.namelist():
            if name.endswith('.pdf'):
                assert bundle.read(name).startswith(b'%PDF')
            elif name.endswith('.docx'):
                assert bundle.read(name).startswith(b'PK')
    prefix = 'docs/submission/evidence/gpu-timing/'
    with tarfile.open(out / f'issuertrace-gpu-evidence-{version}.tar.gz') as bundle:
        assert all(m.isfile() and safe(m.name) and m.name.startswith(prefix) for m in bundle.getmembers())
        assert not any(Path(m.name).name in ('paragraphs.jsonl', 'queries.jsonl') for m in bundle.getmembers())
        benchmark = json.load(bundle.extractfile(prefix + 'benchmark.json'))
        acceptance = json.load(bundle.extractfile(prefix + 'application-acceptance.json'))
        assert benchmark['complete'] and benchmark['gpu_execution_observed'] and acceptance['passed']
        assert len(benchmark['runs']) == 3 and acceptance['queries'] == 3922
        for number in range(1, 4):
            path = prefix + f'run-{number}/'
            manifest = json.load(bundle.extractfile(path + 'manifest.json'))
            assert sha(bundle.extractfile(path + 'neighbors.jsonl')) == manifest['neighbors_sha256']
    print(json.dumps({'passed': True, 'version': version, 'asset_checksums': checked,
                      'runtime_files': len(members), 'report_files': 27, 'gpu_repeats': 3}))


if __name__ == '__main__':
    main()
