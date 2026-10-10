"""Read-only delivery audit; emits locations/counts, never credential values.

Scans working files, Git history and an existing Release's archives. Personal
information in images and redistribution rights still require human review.
Requires python-docx and PyMuPDF for portable report verification.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import re
import subprocess
import shutil
import os
from concurrent.futures import ThreadPoolExecutor
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import fitz

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
PATTERNS = {
    "api_key_candidate": re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{24,}"),
    "github_token_candidate": re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    "private_key_candidate": re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def known_secrets():
    # Compare local protected credentials without including them in any output.
    values = set()
    private = ROOT / ".private"
    for p in private.glob("*.env"):
        for line in p.read_text(errors="ignore").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("'\"")
            if sep and any(term in key.upper() for term in ("API_KEY", "AUTH_SECRET", "SECRET_KEY")) and len(value) >= 12:
                values.add(value.encode())
    signing = private / "production-signing-key"
    if signing.is_file() and signing.read_bytes().strip():
        values.add(signing.read_bytes().strip())
    return values


def payloads(name, data):
    yield name, data
    if name.endswith((".docx", ".zip", ".pptx")):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for item in z.infolist():
                    if not item.is_dir():
                        yield from payloads(name + "::" + item.filename, z.read(item))
        except zipfile.BadZipFile:
            pass
    elif name.endswith(".pdf"):
        with fitz.open(stream=data, filetype="pdf") as pdf:
            text = "\n".join(page.get_text() for page in pdf)
            text += json.dumps(pdf.metadata, ensure_ascii=False)
            yield name + "::text-and-metadata", text.encode()
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for item in archive:
                if item.isfile():
                    yield from payloads(name + "::" + item.name, archive.extractfile(item).read())


def inspect(name, data, secrets):
    matches = []
    for label, pattern in PATTERNS.items():
        count = len(pattern.findall(data))
        if count:
            matches.append({"location": name, "category": label, "count": count})
    count = sum(value in data for value in secrets)
    if count:
        matches.append({"location": name, "category": "known_production_secret", "count": count})
    return matches


def scan_history(secrets):
    objects = git("rev-list", "--objects", "--all").decode().splitlines()
    names = {}
    for row in objects:
        oid, _, name = row.partition(" ")
        if name:
            names[oid] = name
    process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    hits, access, count = [], [], 0
    try:
        for oid, name in names.items():
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise RuntimeError("Cannot read Git audit object")
            size = int(header[2])
            data = process.stdout.read(size)
            process.stdout.read(1)
            if header[1] != b"blob":
                continue
            count += 1
            if Path(name).name == "ACCESS.md":
                access.append({"path": name, "blob": oid, "policy": "private reviewer credentials; not public-safe"})
            for location, value in payloads(name, data):
                for hit in inspect(location, value, secrets):
                    hits.append(dict(hit, blob=oid))
    finally:
        process.stdin.close()
        process.wait()
    return {"blobs_checked": count, "credential_documents": access, "secret_candidates": hits}


def check_reports():
    sources = [ROOT / name for name in ("README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md") if (ROOT / name).exists()]
    sources += sorted((ROOT / "docs/submission").glob("*.md"))
    bad, checked = [], 0
    for source in sources:
        in_code = False
        for number, line in enumerate(source.read_text().splitlines(), 1):
            if line.startswith("```"):
                in_code = not in_code
            if in_code:
                continue
            for target in LINK.findall(line):
                checked += 1
                target = target.strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or not parsed.path:
                    continue
                dest = (source.parent / unquote(parsed.path)).resolve()
                if not dest.exists():
                    bad.append({"source": str(source.relative_to(ROOT)), "line": number, "target": target})
    reports = []
    ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    for source in sorted((ROOT / "docs/submission").glob("[0-9][0-9]-*.md")):
        with zipfile.ZipFile(source.with_suffix(".docx")) as archive:
            assert archive.testzip() is None
            xml = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
            expected = {unquote(r.attrib["Target"]) for r in xml if r.attrib.get("Type", "").endswith("/hyperlink")}
            body = ET.fromstring(archive.read("word/document.xml"))
            doc_text = "".join(body.itertext())
        with fitz.open(source.with_suffix(".pdf")) as pdf:
            actual = {unquote(link["uri"]) for page in pdf for link in page.get_links() if link.get("uri")}
            pdf_text = "".join(page.get_text() for page in pdf)
            missing_links = sorted(expected - actual)
            headings = [re.sub(r"[*`]", "", line.lstrip("# ")) for line in source.read_text().splitlines() if re.match(r"^#{1,4} ", line)]
            normalize = lambda value: re.sub(r"\s+", "", value)
            missing_headings = [h for h in headings if normalize(h) not in normalize(pdf_text)]
            reports.append({"source": source.name, "pages": len(pdf), "docx_hyperlinks": len(expected),
                            "pdf_hyperlinks": len(actual), "missing_pdf_links": missing_links,
                            "missing_pdf_headings": missing_headings,
                            "docx_title_present": normalize(headings[0]) in normalize(doc_text)})
    return {"links_checked": checked, "missing_local_targets": bad, "reports": reports,
            "passed": not bad and len(reports) == 9 and all(not r["missing_pdf_links"] and not r["missing_pdf_headings"] and r["docx_title_present"] for r in reports)}


def image_ocr(paths, secrets):
    if not shutil.which("tesseract"):
        return {"performed": False, "reason": "tesseract unavailable; visual review required"}
    languages = subprocess.check_output(["tesseract", "--list-langs"], stderr=subprocess.DEVNULL).decode().splitlines()
    language = "eng+chi_sim" if "chi_sim" in languages else "eng"
    ocr_env = dict(os.environ, OMP_NUM_THREADS="1", OMP_THREAD_LIMIT="1")
    def check(name):
        data = subprocess.check_output(["tesseract", str(ROOT / name), "stdout", "-l", language],
                                       stderr=subprocess.DEVNULL, timeout=60, env=ocr_env)
        return {"path": name, "ocr_sha256": digest(data), "characters": len(data.decode(errors="replace")),
                "secret_candidates": inspect(name + "::ocr", data, secrets),
                "email_candidates": len(re.findall(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", data)),
                "phone_candidates": len(re.findall(rb"(?<![0-9])1[3-9][0-9]{9}(?![0-9])", data))}
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(check, paths))
    return {"performed": True, "language": language, "images_checked": len(results), "results": results,
            "scope": "OCR candidate detection only; placeholders and identity context need visual review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ocr", action="store_true", help="scan tracked image text with local tesseract")
    args = parser.parse_args()
    secrets = known_secrets()
    paths = set(git("ls-files", "-z").decode().split("\0"))
    paths |= set(git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"))
    current, archives, screenshots, access = [], [], [], []
    for name in sorted(paths - {""}):
        p = ROOT / name
        if not p.is_file() or p.resolve() == args.out.resolve():
            continue
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            screenshots.append(name)
        if p.name == "ACCESS.md":
            access.append(name)
        for location, data in payloads(name, p.read_bytes()):
            current.extend(inspect(location, data, secrets))
    for p in sorted(args.release_dir.iterdir()):
        if p.suffix not in {".gz", ".zip"}:
            continue
        for location, data in payloads(p.name, p.read_bytes()):
            archives.extend(inspect(location, data, secrets))
    ocr = image_ocr(screenshots, secrets) if args.ocr else {"performed": False}
    current.extend(hit for item in ocr.get("results", []) for hit in item["secret_candidates"])
    report = {
        "source_base": git("rev-parse", "HEAD").decode().strip(),
        "revision_policy": "delivery materials revision; measured results retained; deployment identifiers may be sanitized in evidence copies",
        "reports": check_reports(),
        "privacy": {"known_secret_values_compared": len(secrets), "working_tree_candidates": current,
                    "release_candidates": archives, "history": scan_history(secrets),
                    "reviewer_credential_files": access, "image_files_for_visual_review": screenshots,
                    "image_ocr": ocr,
                    "public_ready": False,
                    "scope": "byte/text/metadata scan; image OCR, identity judgment and licensing need separate review"},
        "evidence_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes())
                            for base in (ROOT / "artifacts/evidence", ROOT / "docs/submission/evidence")
                            if base.exists() for p in sorted(base.rglob("*"))
                            if p.is_file() and p.resolve() != args.out.resolve()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report_links_passed": report["reports"]["passed"],
                      "working_tree_secret_candidates": len(current), "release_secret_candidates": len(archives),
                      "history_secret_candidates": len(report["privacy"]["history"]["secret_candidates"]),
                      "public_ready": False, "output": str(args.out)}, ensure_ascii=False))
    if not report["reports"]["passed"] or current or archives or report["privacy"]["history"]["secret_candidates"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
