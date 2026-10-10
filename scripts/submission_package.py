#!/usr/bin/env python3
"""Package sanitized runtime, reports and GPU evidence; never overwrite releases."""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_media_assets(manifest_path, media_dir):
    """Validate exact final payloads; larger files remain Release assets."""
    media = json.loads(manifest_path.read_text())
    names = set()
    files = []
    for item in media['assets']:
        name = item['filename']
        if Path(name).name != name or name.startswith('.') or name in names:
            raise ValueError('Invalid or duplicate media filename')
        if (item['kind'], Path(name).suffix) not in {('presentation', '.pptx'), ('demo_video', '.mp4')}:
            raise ValueError('Unexpected media kind or extension')
        path = media_dir / name
        if path.is_symlink() or path.resolve().parent != media_dir.resolve():
            raise ValueError('Media must be a regular file in the final-media directory')
        if path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError('Media bytes differ from the final manifest')
        if item['kind'] == 'demo_video' and not 180 <= item['duration_seconds'] <= 300:
            raise ValueError('Demo video must be 3-5 minutes')
        names.add(name)
        files.append(path)
    if {item['kind'] for item in media['assets']} != {'presentation', 'demo_video'}:
        raise ValueError('Both final presentation and video are required')
    return media, files


def reuse_verified_archive(source, destination, expected):
    """Reuse an unchanged archive only after checking every member digest."""
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        if len(members) != len(expected) or {m.name for m in members} != set(expected):
            raise ValueError('Reusable archive member list differs')
        for member in members:
            if not member.isfile() or member.size != expected[member.name]['bytes']:
                raise ValueError('Reusable archive member type or size differs')
            if hashlib.sha256(archive.extractfile(member).read()).hexdigest() != expected[member.name]['sha256']:
                raise ValueError('Reusable archive content differs')
    shutil.copyfile(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--reuse-assets-from', type=Path,
                        help='Reuse unchanged runtime/GPU archives after full member digest checks')
    args = parser.parse_args()
    if not re.fullmatch(r'v\d+\.\d+(?:\.\d+)?', args.version):
        raise SystemExit('Version must be vMAJOR.MINOR or vMAJOR.MINOR.PATCH.')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise SystemExit('Commit source and reports before packaging a frozen release.')
    out = ROOT / 'artifacts/release' / args.version
    if out.exists():
        raise SystemExit(f'Release directory already exists; refusing overwrite: {out}')
    state = ROOT / 'data/state'
    with sqlite3.connect(f'file:{state}/collector.sqlite3?mode=ro', uri=True) as db:
        accounts = [r[0] for r in db.execute('SELECT username FROM platform_users')]
        if len(accounts) != 5 or any(not x.startswith('competition-') or 'load-' in x for x in accounts):
            raise SystemExit('Runtime must contain exactly five fresh competition identities.')
        for table in ('review_tasks', 'review_decisions', 'alert_dispositions', 'platform_actions',
                      'alert_subscriptions', 'alert_deliveries', 'notification_reads'):
            if db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:
                raise SystemExit(f'Unexpected human/test records in {table}.')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('Database integrity check failed.')
    files = [state / 'collector.sqlite3', state / 'benchmark.sqlite3', ROOT / 'ACCESS.md']
    files += sorted((ROOT / 'web/dist').rglob('*'))
    files += sorted((state / 'dense').rglob('*'))
    files = [p for p in files if p.is_file()]
    if not (ROOT / 'web/dist/index.html').is_file():
        raise SystemExit('Frontend is missing.')
    reports = sorted(p for p in (ROOT / 'docs/submission').glob('[0-9][0-9]-*')
                     if p.suffix in ('.md', '.docx', '.pdf'))
    if len(reports) != 27:
        raise SystemExit('Expected nine reports in Markdown, DOCX and PDF (27 files).')
    supporting = [ROOT / 'docs/submission/处理后数据交付范围.md', ROOT / 'NOTICE.md', ROOT / 'THIRD_PARTY_NOTICES.md', ROOT / 'PUBLICATION.md']
    media_path = ROOT / 'docs/submission/media-manifest.json'
    media, media_files = load_media_assets(media_path, ROOT / '.private/final-media')
    if media['version'] != args.version:
        raise SystemExit('Final media version differs from the release.')
    supporting += [ROOT / 'docs/submission/演示材料.md', media_path]
    evidence = ROOT / 'docs/submission/evidence/gpu-timing'
    benchmark = json.loads((evidence / 'benchmark.json').read_text())
    acceptance = json.loads((evidence / 'application-acceptance.json').read_text())
    if not benchmark['complete'] or not acceptance['passed']:
        raise SystemExit('GPU evidence must be complete and accepted.')
    out.mkdir(parents=True)
    manifest = {str(p.relative_to(ROOT)): {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files}
    manifest_path = out / 'RUNTIME-MANIFEST.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    runtime = out / f'issuertrace-runtime-{args.version}.tar.gz'
    if args.reuse_assets_from:
        expected = dict(manifest)
        expected[manifest_path.name] = {'bytes': manifest_path.stat().st_size, 'sha256': digest(manifest_path)}
        reuse_verified_archive(args.reuse_assets_from / runtime.name, runtime, expected)
    else:
        with tarfile.open(runtime, 'w:gz') as bundle:
            for path in files:
                bundle.add(path, arcname=str(path.relative_to(ROOT)))
            bundle.add(manifest_path, arcname=manifest_path.name)
    with zipfile.ZipFile(out / f'issuertrace-reports-{args.version}.zip', 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in reports:
            bundle.write(path, str(path.relative_to(ROOT)))
        for path in supporting:
            bundle.write(path, str(path.relative_to(ROOT)))
    gpu_archive = out / f'issuertrace-gpu-evidence-{args.version}.tar.gz'
    evidence_files = sorted(p for p in evidence.rglob('*') if p.is_file())
    if args.reuse_assets_from:
        expected = {str(p.relative_to(ROOT)): {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in evidence_files}
        reuse_verified_archive(args.reuse_assets_from / gpu_archive.name, gpu_archive, expected)
    else:
        with tarfile.open(gpu_archive, 'w:gz') as bundle:
            for path in evidence_files:
                bundle.add(path, arcname=str(path.relative_to(ROOT)))
    for path in media_files:
        shutil.copyfile(path, out / path.name)
    # The GPU-only extension does not change UI source. Preserve its frozen
    # complete screenshot set as a companion asset, rather than retest claims.
    ui = ROOT / '.private/final-ui-evidence.tar.gz'
    if ui.is_file():
        shutil.copyfile(ui, out / f'issuertrace-ui-evidence-{args.version}.tar.gz')
    metadata = {
        'version': args.version,
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'application_baseline': '58c47073c5cf3ab943c91900d8bf23cc133c59c7',
        'gpu_benchmark_commit': benchmark['source_commit'],
        'reports': [str(p.relative_to(ROOT)) for p in reports],
        'supporting_reports': [str(p.relative_to(ROOT)) for p in supporting],
        'media': media,
        'runtime_reused_after_file_verification': bool(args.reuse_assets_from),
        'frozen_runtime_neighbors_replaced': False,
        'frontend_source': 'unchanged application; prebuilt assets covered by runtime SHA256 manifest',
        'ui_evidence': 'frozen application screenshots; measured environment retained in evidence',
        'assets': {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)}
                   for p in sorted(out.iterdir()) if p.is_file()},
        'restrictions': 'ACCESS.md contains local fictional demo credentials only. Live reviewer credentials, instance signing keys and provider credentials are independently managed. Public redistribution of third-party data requires separate authorization.',
    }
    (out / 'SUBMISSION-MANIFEST.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    (out / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.name}\n'
                                         for p in sorted(out.iterdir()) if p.is_file()))
    print(json.dumps({'directory': str(out), 'runtime_bytes': runtime.stat().st_size, 'reports': len(reports)}))


if __name__ == '__main__':
    main()
