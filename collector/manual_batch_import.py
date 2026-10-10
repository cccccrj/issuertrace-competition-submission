#!/usr/bin/env python3
"""Batch-import the validated CXMT IPO manifest without network access."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from collector.collect import ROOT, canonicalize, connect
from collector.manual_import import DEFAULT_MAX_BYTES, ensure_provenance_table, ingest
from collector.manual_review import review_version
from collector.report import main as write_report

DEFAULT_MANIFEST = ROOT / "data" / "manual_inbox" / "cxmt_ipo" / "manifest.json"
CORE_STEMS = {
    "2025-12-30_申报稿_招股说明书",
    "2026-05-20_上会稿_招股说明书",
    "2026-05-27_注册稿_招股说明书",
    "2025-12-30_首轮问询回复",
    "2025-12-30_第二轮问询回复",
    "2026-05-20_审核中心意见落实函回复",
    "2026-06-12_证监会注册批复",
    "2026-07-09_招股意向书",
    "2026-07-22_最终招股说明书",
    "2026-07-15_发行公告",
    "2026-07-22_发行结果公告",
    "2026-07-24_上市公告书",
}


def classify(filename: str, official_url: str) -> tuple[str, str]:
    if "/disclosure/listedinfo/announcement/" in official_url:
        return "sse_announcements", "announcements"
    if "招股说明书" in filename:
        return "sse_ipo_review", "prospectus"
    if "问询" in filename or "落实函" in filename:
        return "sse_ipo_review", "review_responses"
    return "sse_ipo_review", "listing_documents"


def selected_artifacts(manifest: dict, scope: str) -> list[dict]:
    artifacts = list(manifest.get("artifacts") or [])
    if scope == "core":
        selected = [item for item in artifacts
                    if Path(str(item.get("filename"))).suffix.lower() == ".pdf"
                    and Path(str(item.get("filename"))).stem in CORE_STEMS]
        stems = {Path(str(item["filename"])).stem for item in selected}
        missing = sorted(CORE_STEMS - stems)
        if missing:
            raise ValueError("core manifest artifacts missing: " + ", ".join(missing))
        if len(selected) != len(CORE_STEMS):
            raise ValueError(f"expected {len(CORE_STEMS)} core PDFs, found {len(selected)}")
        return selected
    if scope == "primary":
        return [item for item in artifacts if item.get("role") == "primary"]
    return artifacts


def validate_artifact(base: Path, item: dict) -> tuple[Path, str]:
    required = ("filename", "mirror_url", "official_sse_url", "bytes", "sha256")
    missing = [key for key in required if not item.get(key)]
    if missing:
        raise ValueError(f"manifest artifact missing {', '.join(missing)}: {item}")
    path = (base / str(item["filename"])).resolve()
    if path.parent != base.resolve() or not path.is_file():
        raise ValueError(f"manifest file is missing or outside inbox: {path}")
    body = path.read_bytes()
    if len(body) != int(item["bytes"]):
        raise ValueError(f"byte count mismatch for {path.name}")
    digest = hashlib.sha256(body).hexdigest()
    if digest != str(item["sha256"]).lower():
        raise ValueError(f"SHA-256 mismatch for {path.name}")
    if path.suffix.lower() == ".pdf" and not body.startswith(b"%PDF"):
        raise ValueError(f"PDF signature missing for {path.name}")
    validation = item.get("validation") or {}
    if validation.get("title_match") is not True:
        raise ValueError(f"title validation missing for {path.name}")
    return path, digest


def import_manifest(manifest_path: Path, scope: str, dry_run: bool) -> list[dict]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("company_id") != "cxmt":
        raise ValueError("this batch workflow accepts only the cxmt manifest")
    selected = selected_artifacts(manifest, scope)
    prepared = []
    for item in selected:
        path, digest = validate_artifact(manifest_path.parent, item)
        official_url = str(item["official_sse_url"])
        source_id, document_type = classify(path.name, official_url)
        prepared.append({"path": path, "digest": digest, "item": item,
                         "source_id": source_id, "document_type": document_type})
    if dry_run:
        return [{"filename": row["path"].name, "sha256": row["digest"],
                 "source_id": row["source_id"], "document_type": row["document_type"],
                 "status": "validated"} for row in prepared]
    db = connect()
    try:
        ensure_provenance_table(db)
        existing_references = {row[0] for row in db.execute(
            "SELECT DISTINCT official_reference_url FROM manual_import_provenance")}
    finally:
        db.close()
    results = []
    for row in prepared:
        item = row["item"]
        path = row["path"]
        official_reference = canonicalize(str(item["official_sse_url"]))
        if official_reference in existing_references:
            results.append({"filename": path.name, "source_id": row["source_id"],
                            "official_reference_url": official_reference,
                            "status": "skipped_existing_reference"})
            continue
        args = SimpleNamespace(
            source_id=row["source_id"], file=str(path),
            canonical_url=str(item["official_sse_url"]),
            acquisition_url=str(item["mirror_url"]),
            official_reference_url=str(item["official_sse_url"]),
            acquisition_source_name="新浪财经公告镜像",
            acquisition_source_tier="S3", artifact_role=item.get("role"),
            declared_sha256=row["digest"],
            equivalence_status="metadata_verified_mirror",
            rights_basis="public_manual_download", company_id="cxmt",
            title=path.stem.split("_", 1)[-1], published_at=path.name[:10],
            document_type=row["document_type"], max_bytes=DEFAULT_MAX_BYTES)
        imported = ingest(args, write_ledger=False)
        reviewed = review_version(
            str(imported["version_id"]), "verified-mirror",
            "manifest_automation",
            "local bytes match declared SHA-256/size/PDF signature and manifest title validation; official URL retained as reference, byte source remains S3 mirror",
            write_ledger=False)
        results.append({**imported, **reviewed, "filename": path.name})
        existing_references.add(official_reference)
    write_report()
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-import a validated CXMT IPO manifest")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--scope", choices=("core", "primary", "all"), default="core")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = import_manifest(args.manifest, args.scope, args.dry_run)
    for result in results:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"processed={len(results)} scope={args.scope} dry_run={str(args.dry_run).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
