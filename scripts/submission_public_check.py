"""Read-only public-delivery checks; report locations, never credential values.

Includes tracked files, document payloads, reachable Git history and Release
archives. Optional OCR checks image text, including images inside archives.
This is a technical check, not approval of licenses or data redistribution.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private_platform_domain': re.compile(r'\b[\w-]+\.pubs\.net\.cn\b', re.I),
    'deployment_location': re.compile('\u6821\u56ed|\\bcampus\\b', re.I),
    'private_ingress_description': re.compile('\u53cd\u5411\u96a7\u9053|\u516c\u7f51\u5165\u53e3\u673a'),
    'machine_working_path': re.compile(r'/(?:root|autodl-tmp)/(?!\.cache/|\.codex/)'),
    'provider_ssh_hostname': re.compile(r'connect\.[\w.-]+|\b[\w.-]+\.seetacloud\.com'),
    'online_credential_table': re.compile('\u7ebf\u4e0a\uff0f\u672c\u5730'),
    'live_inventory_association': re.compile('\u751f\u4ea7\u6570\u636e\u76d8\u70b9|\u751f\u4ea7\u5185\u5bb9\u6d4b\u8bd5\u526f\u672c'),
}

def inspect_text(name, data, protected=()):
    text = data.decode('utf-8', errors='replace')
    patterns = dict(PATTERNS)
    if data.startswith(b'SQLite format 3\x00'):
        # News about recruitment or a textbook's network example is data,
        # not disclosure of the project's actual deployment location.
        patterns['deployment_location'] = re.compile(
            '\u6821\u56ed(?:\u673a|\u670d\u52a1\u5668|\u90e8\u7f72)|\\bcampus\\s+(?:server|instance|deployment|single)', re.I)
    hits = [{'location': name, 'category': label, 'count': len(p.findall(text))}
            for label,p in patterns.items() if p.search(text)]
    count = sum(bool(value) and value in data for value in protected)
    if count:
        hits.append({'location': name, 'category': 'protected_reviewer_password', 'count': count})
    return hits

def payloads(name, data):
    yield name, data
    if name.endswith(('.zip','.docx','.pptx')):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    yield from payloads(name+'::'+entry.filename, archive.read(entry))
    elif name.endswith(('.tar.gz','.tgz')):
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for entry in archive:
                if entry.isfile():
                    yield from payloads(name+'::'+entry.name, archive.extractfile(entry).read())
    elif name.endswith('.pdf'):
        import fitz
        with fitz.open(stream=data,filetype='pdf') as pdf:
            text='\n'.join(p.get_text() for p in pdf)+json.dumps(pdf.metadata,ensure_ascii=False)
            yield name+'::text-and-metadata',text.encode()

def protected_passwords(root):
    values=[]
    path=root/'.private/REVIEWER_ACCESS.private.md'
    if path.exists():
        for line in path.read_text().splitlines():
            cols=[v.strip() for v in line.strip('|').split('|')]
            if len(cols)==4 and cols[0].startswith('competition-') and len(cols[2])>=8:
                values.append(cols[2].encode())
    return values

def scan_blobs(blobs, protected, ocr, approved_media=None):
    hits=[]; approved_hits=[]; images={}; files=0
    approved_media = approved_media or {}
    def record(findings, permitted):
        for found in findings:
            if permitted and found['category'] == 'private_platform_domain':
                approved_hits.append(found)
            else:
                hits.append(found)
    for name,data in blobs:
        permitted = approved_media.get(name) == hashlib.sha256(data).hexdigest()
        for inner,raw in payloads(name,data):
            files+=1
            record(inspect_text(inner,raw,protected), permitted)
            if inner.lower().endswith(('.png','.jpg','.jpeg','.webp')):
                key=(hashlib.sha256(raw).hexdigest(), permitted)
                images.setdefault(key,(inner,raw,permitted))
    if ocr:
        def image_check(item):
            name,data,permitted=item
            with tempfile.TemporaryDirectory(prefix='delivery-image-check-') as temporary:
                source=Path(temporary)/('image'+Path(name).suffix)
                source.write_bytes(data)
                result=subprocess.run(['tesseract',str(source),'stdout','-l','eng+chi_sim'],capture_output=True,check=True)
                return inspect_text(name+'::ocr',result.stdout,protected), permitted
        with ThreadPoolExecutor(max_workers=4) as pool:
            for found, permitted in pool.map(image_check,images.values()):record(found, permitted)
    return {'payloads_checked':files,'unique_images':len(images),'ocr_performed':ocr,'findings':hits,
            'owner_approved_platform_domain_findings':approved_hits,
            'video_review_scope':'Binary/text scan only; video duration, full decode and sampled frames recorded separately in media-manifest.json'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--ocr',action='store_true')
    parser.add_argument('--skip-history',action='store_true')
    args=parser.parse_args()
    media_path = ROOT / 'docs/submission/media-manifest.json'
    media = json.loads(media_path.read_text()) if media_path.exists() else {'assets': []}
    approved_media = {item['filename']:item['sha256'] for item in media['assets']
                      if item.get('owner_confirmed_final') and item.get('content_unchanged')}
    protected=protected_passwords(ROOT)
    names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    work=scan_blobs(((n,(ROOT/n).read_bytes()) for n in names if n and (ROOT/n).is_file()),protected,args.ocr)
    release=scan_blobs(((p.name,p.read_bytes()) for p in sorted(args.release_dir.iterdir()) if p.is_file()),protected,args.ocr,approved_media) if args.release_dir else None
    history=None
    if not args.skip_history:
        def history_blobs():
            rows=subprocess.check_output(['git','rev-list','--objects','--all'],cwd=ROOT).decode().splitlines()
            for row in rows:
                oid,sep,name=row.partition(' ')
                if not sep: continue
                if subprocess.check_output(['git','cat-file','-t',oid],cwd=ROOT).strip()!=b'blob':continue
                yield 'history::'+name,subprocess.check_output(['git','cat-file','blob',oid],cwd=ROOT)
        history=scan_blobs(history_blobs(),protected,False)
    checks=[v for v in (work,release,history) if v]
    report={'version':'v1.0','technical_checks_passed':not any(v['findings'] for v in checks),
            'working_tree':work,'release':release,'history':history,
            'protected_passwords_compared':len(protected),
            'public_release_authorized':False,
            'remaining_approval':'Final code/dependency licenses and third-party data redistribution; see PUBLICATION.md'}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'technical_checks_passed':report['technical_checks_passed'],
                      'findings':sum(len(v['findings']) for v in checks),'output':str(args.out)}))
    if not report['technical_checks_passed']: raise SystemExit(1)

if __name__=='__main__':main()
