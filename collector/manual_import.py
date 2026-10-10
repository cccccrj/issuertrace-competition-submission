#!/usr/bin/env python3
"""Import a lawfully obtained local file into the immutable evidence store."""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from collector.collect import (
    ROOT, RuntimeSource, audit, canonicalize, connect, load_sources, now_iso,
    record_document, save_raw, url_in_scope,
)
from collector.report import main as write_report

RIGHTS_BASES = ("public_manual_download", "authorized_export", "user_provided")
SOURCE_TIERS = ("S0", "S1", "S2", "S3")
EQUIVALENCE_STATUSES = (
    "exact_official_hash",
    "metadata_verified_mirror",
    "mirror_only",
    "authorized_export",
    "user_provided",
)
DEFAULT_MAX_BYTES = 200 * 1024 * 1024


def validate_request(source: RuntimeSource, path: Path, canonical_url: str,
                     company_id: str | None, document_type: str | None,
                     published_at: str | None, max_bytes: int) -> tuple[str, str | None, str]:
    if not path.is_file():
        raise ValueError(f"input file does not exist or is not a regular file: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("input file is empty")
    if max_bytes <= 0 or size > max_bytes:
        raise ValueError(f"input file is {size} bytes; configured maximum is {max_bytes}")
    parsed = urlparse(canonical_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--canonical-url must be an absolute HTTP(S) source URL")
    normalized = canonicalize(canonical_url)
    if not url_in_scope(source, normalized):
        raise ValueError("canonical URL is outside the selected source's configured host/path scope")
    scopes = source.raw.get("company_scope") or []
    if company_id and company_id not in scopes:
        raise ValueError(f"company {company_id!r} is outside source company_scope")
    if not company_id and len(scopes) > 1:
        raise ValueError("--company-id is required for a source covering multiple companies")
    company_id = company_id or (scopes[0] if len(scopes) == 1 else None)
    allowed_types = source.raw.get("document_types") or []
    document_type = document_type or (allowed_types[0] if allowed_types else "document")
    if allowed_types and document_type not in allowed_types:
        raise ValueError(f"document type {document_type!r} is not configured for this source")
    if published_at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[^\s]+)?", published_at):
        raise ValueError("--published-at must be ISO YYYY-MM-DD or an ISO datetime")
    return normalized, company_id, document_type


def content_type_for(path: Path, body: bytes) -> str:
    if body.startswith(b"%PDF"):
        return "application/pdf"
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "application/octet-stream"


def absolute_http_url(value: str, option: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{option} must be an absolute HTTP(S) URL")
    return canonicalize(value)


def ensure_provenance_table(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS manual_import_provenance (
        version_id TEXT PRIMARY KEY, acquisition_url TEXT NOT NULL,
        official_reference_url TEXT NOT NULL, rights_basis TEXT NOT NULL,
        acquisition_source_name TEXT NOT NULL, acquisition_source_tier TEXT NOT NULL,
        artifact_role TEXT, declared_sha256 TEXT, equivalence_status TEXT NOT NULL,
        imported_at TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending',
        review_note TEXT, reviewed_at TEXT,
        FOREIGN KEY(version_id) REFERENCES document_versions(version_id))""")


def ingest(args: argparse.Namespace, *, write_ledger: bool = True) -> dict[str, str | bool]:
    config, sources = load_sources(
        ROOT / "config" / "sources.yaml", {args.source_id}, None, include_manual=True)
    if not sources:
        raise ValueError(f"unknown source_id: {args.source_id}")
    source = sources[0]
    source.route = "MANUAL"
    input_path = Path(args.file).expanduser().resolve()
    canonical_url, company_id, document_type = validate_request(
        source, input_path, args.canonical_url, args.company_id,
        args.document_type, args.published_at, args.max_bytes)
    body = input_path.read_bytes()
    declared_sha256 = (getattr(args, "declared_sha256", None) or "").lower() or None
    if declared_sha256 and not re.fullmatch(r"[0-9a-f]{64}", declared_sha256):
        raise ValueError("--declared-sha256 must contain exactly 64 lowercase hex characters")
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if declared_sha256 and declared_sha256 != actual_sha256:
        raise ValueError(
            f"declared SHA-256 {declared_sha256} does not match file {actual_sha256}")
    content_type = content_type_for(input_path, body)
    if input_path.suffix.lower() == ".pdf" and content_type != "application/pdf":
        raise ValueError("file has a .pdf extension but does not contain a PDF signature")
    title = args.title or input_path.name
    acquisition_url = absolute_http_url(
        getattr(args, "acquisition_url", None) or canonical_url, "--acquisition-url")
    official_reference_url = absolute_http_url(
        getattr(args, "official_reference_url", None) or canonical_url,
        "--official-reference-url")
    acquisition_source_tier = (getattr(args, "acquisition_source_tier", None)
                               or source.raw["source_tier"])
    if acquisition_source_tier not in SOURCE_TIERS:
        raise ValueError(f"invalid acquisition source tier: {acquisition_source_tier}")
    if (urlparse(acquisition_url).hostname != urlparse(canonical_url).hostname and
            not getattr(args, "acquisition_source_tier", None)):
        raise ValueError("--acquisition-source-tier is required when acquisition and canonical hosts differ")
    equivalence_status = getattr(args, "equivalence_status", None) or (
        "authorized_export" if args.rights_basis == "authorized_export" else
        "user_provided" if args.rights_basis == "user_provided" else
        "exact_official_hash" if acquisition_url == canonical_url else "mirror_only")
    if equivalence_status not in EQUIVALENCE_STATUSES:
        raise ValueError(f"invalid equivalence status: {equivalence_status}")
    acquisition_source_name = (getattr(args, "acquisition_source_name", None)
                               or urlparse(acquisition_url).hostname or "unknown")
    legal_name = config["entities"][company_id]["canonical_name"] if company_id else None
    response = SimpleNamespace(
        headers={"Content-Type": content_type}, url=canonical_url, status_code=None)
    db = connect()
    try:
        ensure_provenance_table(db)
        existed = db.execute("SELECT 1 FROM documents WHERE source_id=? AND canonical_url=?",
                             (source.source_id, canonical_url)).fetchone() is not None
        db.execute("""INSERT INTO discovered_metadata
            (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,
             document_type,original_timezone,review_status)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
            title=excluded.title,published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
            company_id=excluded.company_id,matched_legal_entity=excluded.matched_legal_entity,
            document_type=excluded.document_type,original_timezone=excluded.original_timezone,
            review_status=excluded.review_status""",
                   (source.source_id, canonical_url, title, args.published_at, company_id,
                    legal_name, document_type, source.raw.get("original_timezone"),
                    "manual_import_pending_review"))
        digest, raw_path = save_raw(source.source_id, response, body)
        metadata = record_document(db, source, canonical_url, response, digest, raw_path,
                                   title, "manual_import_pending_review")
        version_id = hashlib.sha256(
            f"{source.source_id}\0{canonical_url}\0{digest}".encode()).hexdigest()
        # A mirror-acquired byte stream must retain its effective evidence tier
        # even when the official URL is kept as the canonical reference.
        db.execute("""UPDATE document_versions SET source_tier=?,collection_mode='manual',
            network_route='MANUAL' WHERE version_id=?""",
                   (acquisition_source_tier, version_id))
        db.execute("""INSERT INTO manual_import_provenance
            (version_id,acquisition_url,official_reference_url,rights_basis,
             acquisition_source_name,acquisition_source_tier,artifact_role,
             declared_sha256,equivalence_status,imported_at,review_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET
            acquisition_url=excluded.acquisition_url,
            official_reference_url=excluded.official_reference_url,
            rights_basis=excluded.rights_basis,
            acquisition_source_name=excluded.acquisition_source_name,
            acquisition_source_tier=excluded.acquisition_source_tier,
            artifact_role=excluded.artifact_role,
            declared_sha256=excluded.declared_sha256,
            equivalence_status=excluded.equivalence_status""",
                   (version_id, acquisition_url, official_reference_url,
                    args.rights_basis, acquisition_source_name,
                    acquisition_source_tier, getattr(args, "artifact_role", None),
                    declared_sha256, equivalence_status, now_iso(), "pending"))
        db.execute("""UPDATE crawl_frontier SET status='done',processed_at=?,last_error=NULL
            WHERE source_id=? AND url IN (?,?)""",
                   (now_iso(), source.source_id, args.canonical_url, canonical_url))
        db.commit()
    finally:
        db.close()
    audit({"source_id": source.source_id, "outcome": "manual_file_import_success",
           "url": canonical_url, "content_hash": digest, "raw_path": raw_path,
           "bytes": len(body), "content_type": content_type,
           "rights_basis": args.rights_basis, "company_id": company_id,
           "document_type": document_type, "created_new_url": not existed,
           "acquisition_url": acquisition_url,
           "official_reference_url": official_reference_url,
           "acquisition_source_name": acquisition_source_name,
           "acquisition_source_tier": acquisition_source_tier,
           "equivalence_status": equivalence_status})
    if write_ledger:
        write_report()
    return {"source_id": source.source_id, "canonical_url": canonical_url,
            "content_hash": digest, "raw_path": raw_path,
            "version_id": version_id, "review_status": str(metadata["review_status"]),
            "created_new_url": not existed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a public/authorized local evidence file")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--canonical-url", required=True,
                        help="canonical URL used to identify this evidence record")
    parser.add_argument("--acquisition-url",
                        help="actual public/authorized URL from which these bytes were obtained")
    parser.add_argument("--official-reference-url",
                        help="official URL this mirror/export refers to")
    parser.add_argument("--acquisition-source-name")
    parser.add_argument("--acquisition-source-tier", choices=SOURCE_TIERS)
    parser.add_argument("--artifact-role")
    parser.add_argument("--declared-sha256")
    parser.add_argument("--equivalence-status", choices=EQUIVALENCE_STATUSES)
    parser.add_argument("--rights-basis", required=True, choices=RIGHTS_BASES)
    parser.add_argument("--company-id")
    parser.add_argument("--title")
    parser.add_argument("--published-at")
    parser.add_argument("--document-type")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = ingest(args)
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
