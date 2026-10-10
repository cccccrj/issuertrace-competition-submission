#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests

from collector import safety
import yaml

from collector.config_validation import validate_config

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_DB = DATA / "state" / "collector.sqlite3"
AUDIT_LOG = DATA / "logs" / "collection.jsonl"
SHANGHAI = timezone(timedelta(hours=8))
CHALLENGE_MARKERS = (
    "cloudflare challenge", "acw_sc__v2", "document.location.reload()",
    "abuse-detection-apology", "excessive-requests-apology",
)
CHALLENGE_PROMPTS = (
    "verify you are human", "your request has been flagged as potentially automated",
    "complete the captcha", "enter the captcha", "solve the captcha", "captcha verification",
    "访问验证", "安全验证", "请输入验证码", "验证码以继续", "完成验证码",
)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.title = ""
        self._in_title = False
        self._in_head = False
        self._title_seen = False
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self.link_titles: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        lowered = tag.lower()
        if lowered == "head":
            self._in_head = True
        elif lowered == "a" and values.get("href"):
            self.links.append(values["href"] or "")
            self._active_href = values["href"] or ""
            self._active_text = []
        elif lowered == "title" and self._in_head and not self._title_seen:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "head":
            self._in_head = False
        elif lowered == "title" and self._in_title:
            self._in_title = False
            self._title_seen = True
        elif lowered == "a" and self._active_href is not None:
            self.link_titles.append((self._active_href, re.sub(r"\s+", " ", " ".join(self._active_text)).strip()))
            self._active_href = None
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._active_href is not None:
            self._active_text.append(data)


@dataclass
class RuntimeSource:
    raw: dict
    route: str
    profile: dict

    @property
    def source_id(self) -> str:
        return self.raw["source_id"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def challenge_marker(body: bytes) -> str | None:
    """Return a challenge marker without flagging normal pages that load CAPTCHA libraries."""
    text = body[:250000].decode("utf-8", errors="ignore").lower()
    for marker in CHALLENGE_MARKERS:
        if marker in text:
            return marker
    if len(body) < 100000:
        visible = re.sub(r"<(?:script|style)\b.*?</(?:script|style)>", " ", text,
                         flags=re.IGNORECASE | re.DOTALL)
        visible = html.unescape(re.sub(r"<[^>]+>", " ", visible))
        visible = re.sub(r"\s+", " ", visible)
        for prompt in CHALLENGE_PROMPTS:
            if prompt in visible:
                return prompt
    return None


def canonicalize(url: str) -> str:
    p = urlparse(url)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    query_parts = [q for q in p.query.split("&") if q and not q.lower().startswith(("utm_", "spm=", "from="))]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "&".join(sorted(query_parts)), ""))


def redirected_to_landing(requested_url: str, final_url: str, base_url: str) -> bool:
    """Identify stale deep links that resolve to the configured source landing page."""
    return (canonicalize(requested_url) != canonicalize(base_url) and
            canonicalize(final_url) == canonicalize(base_url))


def connect() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS documents (
        source_id TEXT NOT NULL, canonical_url TEXT NOT NULL, final_url TEXT,
        fetched_at TEXT NOT NULL, status INTEGER, content_type TEXT, content_hash TEXT,
        raw_path TEXT, title TEXT, etag TEXT, last_modified TEXT, network_route TEXT,
        review_status TEXT, PRIMARY KEY(source_id, canonical_url))""")
    db.execute("""CREATE TABLE IF NOT EXISTS source_state (
        source_id TEXT PRIMARY KEY, last_checked_at TEXT, cooldown_until TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'closed',
        last_error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS document_versions (
        version_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_tier TEXT NOT NULL,
        company_id TEXT, matched_legal_entity TEXT, title TEXT, author TEXT, published_at TEXT,
        fetched_at TEXT NOT NULL, original_timezone TEXT, canonical_url TEXT NOT NULL,
        final_url TEXT, language TEXT, document_type TEXT, content_hash TEXT NOT NULL,
        attachment_hash TEXT, parent_version_id TEXT, paywall_status TEXT,
        collection_mode TEXT, network_route TEXT, raw_path TEXT, review_status TEXT NOT NULL)""")
    version_columns = {row[1] for row in db.execute("PRAGMA table_info(document_versions)")}
    for name in ("page_updated_at", "published_basis"):
        if name not in version_columns:
            db.execute(f"ALTER TABLE document_versions ADD COLUMN {name} TEXT")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_versions_source_url
        ON document_versions(source_id, canonical_url, fetched_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS crawl_frontier (
        source_id TEXT NOT NULL, url TEXT NOT NULL, discovered_from TEXT, depth INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', discovered_at TEXT NOT NULL, processed_at TEXT,
        last_error TEXT, PRIMARY KEY(source_id, url))""")
    frontier_columns = {row[1] for row in db.execute("PRAGMA table_info(crawl_frontier)")}
    if "priority" not in frontier_columns:
        db.execute("ALTER TABLE crawl_frontier ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        rows = db.execute("SELECT source_id,url FROM crawl_frontier").fetchall()
        db.executemany("UPDATE crawl_frontier SET priority=? WHERE source_id=? AND url=?",
                       [(frontier_priority(url), sid, url) for sid, url in rows])
    db.execute("""CREATE TABLE IF NOT EXISTS discovered_metadata (
        source_id TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT, published_at TEXT,
        company_id TEXT, matched_legal_entity TEXT, document_type TEXT, original_timezone TEXT,
        PRIMARY KEY(source_id, canonical_url))""")
    discovered_columns = {row[1] for row in db.execute("PRAGMA table_info(discovered_metadata)")}
    if "review_status" not in discovered_columns:
        db.execute("ALTER TABLE discovered_metadata ADD COLUMN review_status TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS host_state (
        source_id TEXT NOT NULL, host TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
        updated_at TEXT NOT NULL, PRIMARY KEY(source_id, host))""")
    db.execute("""CREATE TABLE IF NOT EXISTS domain_rate_state (
        host TEXT PRIMARY KEY, next_allowed_at TEXT NOT NULL, last_source_id TEXT,
        updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS robots_cache (
        robots_url TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status INTEGER NOT NULL,
        body TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS maintenance_state (
        job_id TEXT PRIMARY KEY, last_run_at TEXT NOT NULL, status TEXT NOT NULL,
        details TEXT)""")
    db.commit()
    return db


def audit(event: dict) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now_iso(), **event}, ensure_ascii=False) + "\n")


def load_sources(config_path: Path, selected: set[str], priority: str | None,
                 include_manual: bool = False) -> tuple[dict, list[RuntimeSource]]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    validate_config(config)
    defaults = config["collection_defaults"]
    routing = defaults["network_routing"]
    direct = set(routing["direct_source_ids"])
    proxy = set(routing["proxy_source_ids"])
    profile_by_source = {}
    for profile_name, ids in defaults["access_control"]["profile_source_ids"].items():
        for source_id in ids or []:
            profile_by_source[source_id] = profile_name
    result = []
    for source in config["sources"]:
        sid = source["source_id"]
        if selected and sid not in selected:
            continue
        if priority and source["priority"] != priority:
            continue
        if source.get("collection_mode") == "manual" and not include_manual:
            continue
        route = "DIRECT" if sid in direct else "PROXY" if sid in proxy else routing["unknown_source_route"].upper()
        profile_name = profile_by_source.get(sid, defaults["access_control"]["default_profile"])
        profile = defaults["access_control"]["profiles"][profile_name]
        scopes = source.get("company_scope", [])
        source["_matched_legal_entity"] = config["entities"][scopes[0]]["canonical_name"] if len(scopes) == 1 else None
        source["_canonical_names"] = {company_id: config["entities"][company_id]["canonical_name"] for company_id in scopes}
        source["_entity_alias_map"] = {
            company_id: list(config["entities"][company_id].get("include_aliases", []))
            + [str(item["code"]) for item in config["entities"][company_id].get("stock_codes", [])]
            for company_id in scopes
        }
        result.append(RuntimeSource(source, route, profile))
    return config, result


def session_for(source: RuntimeSource, user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/json,application/pdf;q=0.9,*/*;q=0.5",
        "Referer": source.raw["base_url"],
    })
    if source.route == "DIRECT":
        session.trust_env = False
        session.proxies = {}
    return session


def reserve_host_slot(db: sqlite3.Connection, host: str, source_id: str, min_interval: float,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      jitter_ratio: float = 0.20) -> float:
    """Atomically reserve a cross-process request slot for a target host."""
    db.commit()
    db.execute("""CREATE TABLE IF NOT EXISTS domain_rate_state (
        host TEXT PRIMARY KEY, next_allowed_at TEXT NOT NULL, last_source_id TEXT,
        updated_at TEXT NOT NULL)""")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    now = datetime.now(timezone.utc)
    row = db.execute("SELECT next_allowed_at FROM domain_rate_state WHERE host=?", (host,)).fetchone()
    next_allowed = datetime.fromisoformat(row[0]) if row else now
    slot = max(now, next_allowed)
    if slot > now and jitter_ratio > 0:
        slot += timedelta(seconds=random.uniform(0, max(min_interval, 0) * jitter_ratio))
    following = slot + timedelta(seconds=max(min_interval, 0))
    db.execute("""INSERT INTO domain_rate_state(host,next_allowed_at,last_source_id,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(host) DO UPDATE SET
        next_allowed_at=excluded.next_allowed_at,last_source_id=excluded.last_source_id,
        updated_at=excluded.updated_at""",
        (host, following.isoformat(timespec="microseconds"), source_id, now_iso()))
    db.commit()
    delay = max((slot - datetime.now(timezone.utc)).total_seconds(), 0.0)
    if delay:
        sleep_fn(delay)
    return delay


def robots_body_allows(body: str, target_url: str, user_agent: str) -> bool:
    """Evaluate robots rules including the widely used ``*`` and ``$`` syntax.

    ``urllib.robotparser`` treats several wildcard query rules as literal text.
    Sites in the registry use those rules, so relying on it can accidentally
    permit a URL the publisher expressly disallowed.
    """
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    rules_started = False
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.casefold()
        if key == "user-agent":
            if rules_started:
                groups.append((agents, rules))
                agents, rules = [], []
                rules_started = False
            agents.append(value.casefold())
        elif key in {"allow", "disallow"} and agents:
            rules_started = True
            if key == "disallow" and not value:
                continue
            rules.append((key == "allow", value))
    if agents:
        groups.append((agents, rules))

    ua = user_agent.casefold()
    matches: list[tuple[int, list[tuple[bool, str]]]] = []
    for group_agents, group_rules in groups:
        specificity = max(
            (0 if token == "*" else len(token)
             for token in group_agents if token == "*" or token in ua),
            default=-1)
        if specificity >= 0:
            matches.append((specificity, group_rules))
    if not matches:
        return True
    best = max(item[0] for item in matches)
    selected = [rule for specificity, group_rules in matches if specificity == best
                for rule in group_rules]
    parsed = urlparse(target_url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    matching: list[tuple[int, bool]] = []
    for allow, pattern in selected:
        anchored = pattern.endswith("$")
        source = pattern[:-1] if anchored else pattern
        expression = "^" + re.escape(source).replace(r"\*", ".*")
        if anchored:
            expression += "$"
        if re.search(expression, target):
            matching.append((len(source.replace("*", "")), allow))
    if not matching:
        return True
    longest = max(item[0] for item in matching)
    # Allow wins when equally specific rules both match.
    return any(allow for length, allow in matching if length == longest)


def robots_decision(status: int, body: str, robots_url: str, target_url: str,
                    user_agent: str, cached: bool = False) -> tuple[bool, str]:
    marker = "cached" if cached else "checked"
    if status in (401, 403):
        return False, f"robots_{'cached_' if cached else ''}http_{status}"
    if status >= 500:
        return False, f"robots_unavailable_{status}"
    if status == 404:
        return True, "robots_cached_absent" if cached else "robots_absent"
    if status >= 400:
        return False, f"robots_{'cached_' if cached else ''}http_{status}"
    allowed = robots_body_allows(body, target_url, user_agent)
    if allowed:
        return True, f"robots_{marker}"
    return False, "robots_cached_disallowed" if cached else "robots_disallowed"


def safe_get(session: requests.Session, url: str, headers: dict, timeout: int,
             form_data: dict | None = None) -> tuple[requests.Response, bytes]:
    """Fetch a URL, checking every redirect hop before following it.

    ``allow_redirects=True`` decides where to go next from the response, which
    means a public URL can hand back a Location pointing at localhost or the
    cloud metadata service and requests will dutifully fetch it.  Following the
    chain by hand is the only way the check happens *before* each request.

    The body is read through the cap for the same reason: by the time
    ``response.content`` has told you the body was too big, it is in memory.

    ``form_data`` posts a form instead of issuing a GET.  It exists because some
    disclosure interfaces answer only to POST, and the alternative -- letting an
    adapter call requests directly -- would put those requests outside every
    guard in this function.  Only the first hop is a POST: a redirect after a
    form submission is followed as a GET, which is what every browser does and
    what the 303 status means.
    """
    target = url
    for hop in range(safety.MAX_REDIRECTS + 1):
        safety.assert_public_url(target)
        if form_data is not None and hop == 0:
            response = session.post(target, data=form_data, headers=headers, timeout=timeout,
                                    allow_redirects=False, stream=True)
        else:
            response = session.get(target, headers=headers, timeout=timeout,
                                   allow_redirects=False, stream=True)
        location = response.headers.get("Location")
        if response.is_redirect and location:
            response.close()
            target = urljoin(target, location)
            continue
        try:
            return response, safety.read_capped(response)
        finally:
            response.close()
    raise safety.UnsafeRequest(f"more than {safety.MAX_REDIRECTS} redirects from {url}")


def robot_allowed(session: requests.Session, url: str, user_agent: str, timeout: int,
                  request_gate: Callable[[str], None] | None = None,
                  db: sqlite3.Connection | None = None,
                  cache_ttl_seconds: int = 86400) -> tuple[bool, str]:
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    if db is not None:
        db.execute("""CREATE TABLE IF NOT EXISTS robots_cache (
            robots_url TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status INTEGER NOT NULL,
            body TEXT NOT NULL)""")
        row = db.execute("SELECT fetched_at,status,body FROM robots_cache WHERE robots_url=?",
                         (robots_url,)).fetchone()
        if row:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()
            if 0 <= age <= cache_ttl_seconds:
                return robots_decision(int(row[1]), row[2], robots_url, url, user_agent, True)
    try:
        if request_gate:
            request_gate(robots_url)
        response = session.get(robots_url, timeout=timeout, allow_redirects=True)
        if db is not None and response.status_code < 500:
            db.execute("""INSERT INTO robots_cache(robots_url,fetched_at,status,body)
                VALUES(?,?,?,?) ON CONFLICT(robots_url) DO UPDATE SET
                fetched_at=excluded.fetched_at,status=excluded.status,body=excluded.body""",
                       (robots_url, now_iso(), response.status_code, response.text[:1_000_000]))
            db.commit()
        return robots_decision(response.status_code, response.text, robots_url, url, user_agent)
    except requests.RequestException as exc:
        return False, f"robots_error:{type(exc).__name__}"


def retry_after_seconds(value: str | None, fallback: int = 3600) -> int:
    if not value:
        return fallback
    try:
        return max(int(value), 0)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return max(int((dt - datetime.now(dt.tzinfo or timezone.utc)).total_seconds()), 0)
        except (TypeError, ValueError, OverflowError):
            return fallback


def save_raw(source_id: str, response: requests.Response, body: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    day = datetime.now(SHANGHAI).strftime("%Y/%m/%d")
    content_type = response.headers.get("Content-Type", "").lower()
    ext = ".pdf" if "pdf" in content_type or body.startswith(b"%PDF") else ".html" if "html" in content_type else ".bin"
    path = DATA / "raw" / source_id / day / f"{digest}{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(body)
    return digest, str(path.relative_to(ROOT))


def record_document(db: sqlite3.Connection, source: RuntimeSource, url: str, response: requests.Response, digest: str, raw_path: str, title: str, review: str) -> dict[str, str | None]:
    canonical = canonicalize(url)
    content_type = response.headers.get("Content-Type", "").lower()
    body_path = ROOT / raw_path
    body = body_path.read_bytes() if "html" in content_type else b""
    metadata = extract_metadata(source, response.url, body, title)
    hint = db.execute("""SELECT title,published_at,company_id,matched_legal_entity,document_type,original_timezone,review_status
        FROM discovered_metadata WHERE source_id=? AND canonical_url=?""", (source.source_id, canonical)).fetchone()
    hint_review = hint[6] if hint else None
    if hint:
        for key, value in zip(("title", "published_at", "company_id", "matched_legal_entity", "document_type", "original_timezone"), hint[:6]):
            if (key == "published_at" and metadata.get("published_at") and
                    metadata.get("published_basis") in {
                        "article_dateline", "document_signature", "structured_data", "labeled_page_field",
                    }):
                continue
            if value is not None:
                metadata[key] = value
    effective_review = hint_review or metadata.get("review_suggestion") or review
    if metadata["published_at"]:
        published_day = str(metadata["published_at"])[:10].replace("/", "-")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_day) and published_day < str(source.raw.get("backfill_start", "0000-00-00")):
            effective_review = "outside_backfill_window"
    metadata["review_status"] = effective_review
    if hint and not hint_review and metadata.get("company_id") and effective_review != "outside_backfill_window":
        effective_review = "auto_matched_strong_identifier"
        metadata["review_status"] = effective_review
    previous = db.execute("SELECT version_id,content_hash FROM document_versions WHERE source_id=? AND canonical_url=? ORDER BY fetched_at DESC LIMIT 1", (source.source_id, canonical)).fetchone()
    parent_version = previous[0] if previous and previous[1] != digest else None
    version_id = hashlib.sha256(f"{source.source_id}\0{canonical}\0{digest}".encode()).hexdigest()
    db.execute("""INSERT OR IGNORE INTO document_versions
        (version_id,source_id,source_tier,company_id,matched_legal_entity,title,author,published_at,
         fetched_at,original_timezone,canonical_url,final_url,language,document_type,content_hash,
         attachment_hash,parent_version_id,paywall_status,collection_mode,network_route,raw_path,review_status,
         page_updated_at,published_basis)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (version_id, source.source_id, source.raw["source_tier"], metadata["company_id"],
         metadata["matched_legal_entity"], metadata["title"], metadata["author"], metadata["published_at"],
         now_iso(), metadata["original_timezone"], canonical, response.url, metadata["language"],
         metadata["document_type"], digest, digest if "pdf" in content_type else None, parent_version,
         str(source.raw.get("paywall", False)).lower(), source.raw.get("collection_mode"), source.route,
         raw_path, effective_review, metadata["page_updated_at"], metadata["published_basis"]))
    db.execute("""INSERT INTO documents
        (source_id, canonical_url, final_url, fetched_at, status, content_type, content_hash, raw_path,
         title, etag, last_modified, network_route, review_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, canonical_url) DO UPDATE SET
         final_url=excluded.final_url, fetched_at=excluded.fetched_at, status=excluded.status,
         content_type=excluded.content_type, content_hash=excluded.content_hash, raw_path=excluded.raw_path,
         title=excluded.title, etag=excluded.etag, last_modified=excluded.last_modified,
         network_route=excluded.network_route, review_status=excluded.review_status""",
        (source.source_id, canonical, response.url, now_iso(), response.status_code,
         response.headers.get("Content-Type"), digest, raw_path, metadata["title"],
         response.headers.get("ETag"), response.headers.get("Last-Modified"), source.route, effective_review))
    db.commit()
    return metadata


# A meta attribute value runs to the quote that opened it.  The class
# ``["']([^"']+)`` stops at *either* quote, so content="China's banned
# memory-maker CXMT…" was stored as the title "China" — 14 Tom's Hardware
# articles, one of them the only independent source on a risk dossier (I-05).
_QUOTED = r"""(?:"([^"]*)"|'([^']*)')"""


def meta_content(text: str, name: str) -> str | None:
    """The content of ``<meta name|property=name>``, whichever attribute comes first."""
    patterns = (
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content={_QUOTED}',
        rf'<meta[^>]+content={_QUOTED}[^>]+(?:name|property)=["\']{re.escape(name)}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1) if match.group(1) is not None else match.group(2)
            value = re.sub(r"\s+", " ", html.unescape(value)).strip()
            if value:
                return value
    return None


def extract_metadata(source: RuntimeSource, url: str, body: bytes, fallback_title: str) -> dict[str, str | None]:
    text = body.decode("utf-8", errors="ignore")
    plain_text = html.unescape(re.sub(r"<[^>]+>", "", text))
    def meta_value(names: tuple[str, ...]) -> str | None:
        for name in names:
            value = meta_content(text, name)
            if value:
                return value
        return None
    # Search engines hand back titles wrapped in their own highlight markup.
    # Stored as they arrive, they surface as literal <font color=...> in an
    # alert headline, a notification and a Word report, and every consumer has
    # to strip them again.
    title = safety.strip_markup(meta_value(("og:title", "twitter:title")) or fallback_title) or None
    author = safety.strip_markup(meta_value(("author", "article:author"))) or None
    published = meta_value(("article:published_time", "publishdate", "date"))
    published_basis = "metadata" if published else None
    updated_match = re.search(r'"dateModified"\s*:\s*"([^"]+)"', text, re.I)
    page_updated = updated_match.group(1) if updated_match else None
    parsed_path = urlparse(url).path.lower()
    path_parts = [part for part in parsed_path.split("/") if part]
    # CXMT's Chinese detail pages use /news/info_69.html (only two path
    # segments), while /news.html and /news-2.html are archive pages.
    is_cxmt_detail = re.fullmatch(r"/news/info[_-]\d+\.html", parsed_path) is not None
    government_sections = {
        "media", "library", "release", "hearing", "hearings", "report", "reports", "letter", "letters",
    }
    is_article = (("news" in path_parts and len(path_parts) >= 3) or is_cxmt_detail or
                  bool(government_sections.intersection(path_parts)) or
                  parsed_path.endswith("/content.shtml") or
                  parsed_path.endswith(".pdf"))
    if source.source_id == "csrc" and is_article:
        signed_dates = re.findall(r'([12]\d{3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', plain_text)
        if signed_dates:
            year, month, day = signed_dates[-1]
            published = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            published_basis = "document_signature"
    if is_article:
        description = meta_value(("description", "og:description")) or ""
        cn_date = re.search(r'([12]\d{3})年(\d{1,2})月(\d{1,2})日', description)
        if cn_date:
            published = f"{int(cn_date.group(1)):04d}-{int(cn_date.group(2)):02d}-{int(cn_date.group(3)):02d}"
            published_basis = "article_dateline"
        else:
            en_date = re.search(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+([12]\d{3})\b', description, re.I)
            if en_date:
                parsed = datetime.strptime(" ".join(en_date.groups()), "%B %d %Y")
                published = parsed.strftime("%Y-%m-%d")
                published_basis = "article_dateline"
    if not published and is_article:
        visible_date = re.search(r'\b([12]\d{3})/(\d{1,2})/(\d{1,2})\b', plain_text)
        if visible_date:
            published = f"{int(visible_date.group(1)):04d}-{int(visible_date.group(2)):02d}-{int(visible_date.group(3)):02d}"
            published_basis = "visible_date"
    if not published and is_article:
        match = re.search(r'"(?:datePublished|publishedAt|publishDate)"\s*:\s*"([^"T]+(?:T[^"]+)?)"', text, re.I)
        published = match.group(1) if match else None
        published_basis = "structured_data" if match else None
    if not published and is_article:
        match = re.search(
            r'(?:发布时间|发布日期|发文日期|发布于|publish(?:ed)?\s*(?:date)?)[：:\s]*'
            r'([12]\d{3})[-/.年](\d{1,2})[-/.月](\d{1,2})日?', plain_text, re.I)
        published = (f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
                     if match else None)
        published_basis = "labeled_page_field" if match else None
    filename_date_pattern = source.raw.get("attachment_filename_date_pattern")
    if not published and parsed_path.endswith(".pdf") and filename_date_pattern:
        filename_match = re.search(str(filename_date_pattern), unquote(Path(parsed_path).name))
        if filename_match:
            try:
                published = datetime.strptime(
                    filename_match.group("date"), "%Y%m%d").strftime("%Y-%m-%d")
                published_basis = "filename_date"
            except (TypeError, ValueError):
                published = None
                published_basis = None
    if not is_article and not url.lower().endswith(".pdf"):
        published = None
        published_basis = None
    lang_match = re.search(r'<html[^>]+lang=["\']([^"\']+)', text, re.I)
    configured_languages = source.raw.get("languages") or [None]
    if source.source_id in {"cxmt_en", "wuxi_en", "wuxi_ir_materials", "wuxi_announcements"}:
        language = "en"
    else:
        language = lang_match.group(1) if lang_match else configured_languages[0]
    scopes = source.raw.get("company_scope", [])
    company_id = scopes[0] if len(scopes) == 1 and source.raw.get("collection_mode") != "search" else None
    legal_entity = source.raw.get("_matched_legal_entity") if company_id else None
    review_suggestion = None
    if source.raw.get("collection_mode") == "search":
        haystack = (url + " " + (title or "") + " " + text).casefold()
        hits = []
        for candidate, aliases in source.raw.get("_entity_alias_map", {}).items():
            if any(len(alias.strip()) >= 4 and alias.casefold() in haystack for alias in aliases):
                hits.append(candidate)
        if len(hits) == 1:
            company_id = hits[0]
            legal_entity = source.raw.get("_canonical_names", {}).get(company_id)
            review_suggestion = "candidate_entity_match"
        else:
            review_suggestion = "no_entity_match" if not hits else "ambiguous_entity_match"
    document_types = source.raw.get("document_types", [])
    lowered = url.lower()
    if canonicalize(url) == canonicalize(source.raw["base_url"]):
        doc_type = "landing_page"
        if source.raw.get("collection_mode") == "search":
            review_suggestion = "source_landing"
    elif "news" in path_parts:
        doc_type = next((d for d in document_types if "news" in d or d == "press_releases"), "news")
    elif "media" in path_parts or "release" in path_parts:
        doc_type = next(
            (d for d in document_types if d.rstrip("s") in lowered and d != "press_releases"),
            "press_releases")
    elif source.source_id == "csrc" and title and "批复" in title:
        doc_type = "administrative_permission"
    else:
        doc_type = "pdf" if lowered.endswith(".pdf") else next((d for d in document_types if d.rstrip("s") in lowered), document_types[0] if len(document_types) == 1 else "landing_page")
    if source.source_id == "miit" and parsed_path == "/search-front-server/api/search/info":
        doc_type = "search_response"
        review_suggestion = "search_response"
    if source.source_id == "cnipa" and parsed_path.startswith("/jsearchfront/search"):
        doc_type = "search_response"
        review_suggestion = "search_response"
    if source.source_id == "us_fda" and parsed_path == "/search":
        doc_type = "search_response"
        review_suggestion = "search_response"
    if (source.raw.get("collection_mode") == "search" and
            canonicalize(url) == canonicalize(source.raw["base_url"]) and
            urlparse(source.raw["base_url"]).query):
        doc_type = "search_response"
        review_suggestion = "search_response"
    query_shell_title = (title or "").casefold()
    if (source.raw.get("collection_mode") == "search" and urlparse(url).query and
            any("search" in part.casefold() for part in path_parts) and
            ("search" in query_shell_title or "搜索" in query_shell_title or
             "检索" in query_shell_title)):
        doc_type = "search_response"
        review_suggestion = "search_response"
    if (review_suggestion is None and source.raw.get("first_party_entity") and
            company_id and (is_article or parsed_path.endswith(".pdf")) and
            doc_type not in {"landing_page", "search_response"}):
        # A detail page on an explicitly configured, single-company official
        # site establishes who is speaking. It does not independently verify
        # the company's claim, which remains represented by source_tier/role.
        review_suggestion = "auto_matched_first_party"
    return {
        "company_id": company_id,
        "matched_legal_entity": legal_entity,
        "title": title, "author": author, "published_at": published,
        "original_timezone": source.raw.get("original_timezone"), "language": language, "document_type": doc_type,
        "page_updated_at": page_updated, "published_basis": published_basis,
        "review_suggestion": review_suggestion,
    }


def set_source_state(db: sqlite3.Connection, sid: str, failures: int, state: str, error: str | None, cooldown_seconds: int = 0) -> None:
    cooldown = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)).isoformat(timespec="seconds") if cooldown_seconds else None
    db.execute("""INSERT INTO source_state(source_id,last_checked_at,cooldown_until,consecutive_failures,state,last_error)
        VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET last_checked_at=excluded.last_checked_at,
        cooldown_until=excluded.cooldown_until, consecutive_failures=excluded.consecutive_failures,
        state=excluded.state,last_error=excluded.last_error""", (sid, now_iso(), cooldown, failures, state, error))
    db.commit()


def active_cooldown(db: sqlite3.Connection, sid: str) -> str | None:
    row = db.execute("SELECT cooldown_until FROM source_state WHERE source_id=?", (sid,)).fetchone()
    if row and row[0] and datetime.fromisoformat(row[0]) > datetime.now(timezone.utc):
        return row[0]
    return None


def previous_failures(db: sqlite3.Connection, sid: str) -> int:
    row = db.execute("SELECT consecutive_failures FROM source_state WHERE source_id=?", (sid,)).fetchone()
    return int(row[0]) if row else 0


def backoff_seconds(kind: str, failures: int) -> int:
    schedule = [30, 120] if kind == "transport" else [30, 120, 600]
    return schedule[min(max(failures - 1, 0), len(schedule) - 1)]


def api_job_outcome(job_id: str, succeeded: bool) -> str:
    job_class = "incremental" if job_id.startswith("incremental:") else "backfill"
    return f"api_{job_class}_{'success' if succeeded else 'error'}"


def prior_headers(db: sqlite3.Connection, sid: str, url: str) -> dict[str, str]:
    row = db.execute("SELECT etag,last_modified FROM documents WHERE source_id=? AND canonical_url=?", (sid, canonicalize(url))).fetchone()
    if not row:
        return {}
    result = {}
    if row[0]: result["If-None-Match"] = row[0]
    if row[1]: result["If-Modified-Since"] = row[1]
    return result


def url_in_scope(source: RuntimeSource, url: str) -> bool:
    parsed = urlparse(url)
    registered_host = urlparse(source.raw["base_url"]).hostname or ""
    allowed_hosts = set(source.raw.get("allowed_hosts", [registered_host]))
    if parsed.hostname not in allowed_hosts:
        return False
    path = parsed.path or "/"
    allow_prefixes = source.raw.get("path_allow_prefixes", [])
    exclude_prefixes = source.raw.get("path_exclude_prefixes", [])
    if allow_prefixes and not any(path.startswith(prefix) for prefix in allow_prefixes):
        return False
    if any(path.startswith(prefix) for prefix in exclude_prefixes):
        return False
    return True


def relevant_links(base: str, body: bytes, source: RuntimeSource | None = None, limit: int = 500) -> tuple[str, list[str]]:
    parser = LinkParser()
    parser.feed(body.decode("utf-8", errors="ignore"))
    host = urlparse(base).netloc.lower()
    keywords = ("news", "notice", "announcement", "investor", "report", "press", "disclosure", "公告", "新闻", "投资者")
    excluded_assets = ("terms_of_use", "privacy_policy", "cookie", "legal_notice")
    anchor_titles = {canonicalize(urljoin(base, href)): title.lower() for href, title in parser.link_titles}
    links = []
    for href in parser.links:
        url = canonicalize(urljoin(base, href))
        p = urlparse(url)
        in_scope = url_in_scope(source, url) if source else p.netloc.lower() == host
        discovery_prefixes = source.raw.get("discovery_path_prefixes", []) if source else []
        discovery_path_allowed = (not discovery_prefixes or
                                  any(p.path.startswith(prefix) for prefix in discovery_prefixes))
        search_seed = bool(source and source.raw.get("discover_search_results") and
                           canonicalize(base) == canonicalize(source.raw["base_url"]))
        result_prefixes = source.raw.get("search_result_path_prefixes", []) if source else []
        search_result_allowed = (not result_prefixes or
                                 any(p.path.startswith(prefix) for prefix in result_prefixes))
        result_query_parameter = source.raw.get("search_result_query_parameter") if source else None
        if result_query_parameter:
            search_result_allowed = (search_result_allowed and
                                     result_query_parameter in parse_qs(p.query, keep_blank_values=True))
        asset_label = anchor_titles.get(url, "")
        excluded_label = any(term in asset_label for term in (
            "privacy policy", "privacy statement", "terms of use", "cookie policy",
            "隐私政策", "隱私政策", "私隱政策", "隱私聲明", "使用条款", "使用條款"))
        is_attachment = p.path.lower().endswith(".pdf") and not excluded_label and not any(term in p.path.lower() for term in excluded_assets)
        if search_seed:
            link_is_relevant = search_result_allowed
        elif source and source.raw.get("detail_discovery_attachments_only"):
            link_is_relevant = is_attachment
        else:
            link_is_relevant = is_attachment or any(k in url.lower() for k in keywords)
        if (p.scheme in ("http", "https") and in_scope and discovery_path_allowed and
                link_is_relevant):
            if url not in links and url != canonicalize(base):
                links.append(url)
        if len(links) >= limit:
            break
    # Some archive pages render pagination controls from data attributes rather
    # than anchors. Expanding the declared public path keeps this deterministic.
    text = body.decode("utf-8", errors="ignore")
    for match in re.finditer(r'class=["\']paging["\'][^>]*data-total=["\'](\d+)["\'][^>]*data-path=["\']([^"\']*\$[^"\']*)["\']', text, re.I):
        total, template = int(match.group(1)), match.group(2)
        for page in range(2, min(total, 200) + 1):
            candidate = canonicalize(urljoin(base, template.replace("$", str(page))))
            if candidate not in links and (source is None or url_in_scope(source, candidate)):
                links.append(candidate)
    return re.sub(r"\s+", " ", parser.title).strip(), links


def link_title_hints(base: str, body: bytes, source: RuntimeSource) -> dict[str, str]:
    parser = LinkParser()
    parser.feed(body.decode("utf-8", errors="ignore"))
    hints: dict[str, str] = {}
    for href, title in parser.link_titles:
        url = canonicalize(urljoin(base, href))
        if title and url_in_scope(source, url):
            if title.strip().lower() in {
                "read more", "download", "view", "here", "click here", "阅读更多", "下载",
            } and urlparse(url).path.lower().endswith(".pdf"):
                filename = Path(urlparse(url).path).stem
                filename = re.sub(r"_[0-9a-f]{8,}$", "", filename, flags=re.I)
                title = re.sub(r"[_-]+", " ", filename).strip()
            hints[url] = title
    return hints


def attachment_metadata_hints(base: str, body: bytes, source: RuntimeSource) -> dict[str, tuple[str | None, str | None]]:
    text = body.decode("utf-8", errors="ignore")
    results: dict[str, tuple[str | None, str | None]] = {}
    fallback_titles = link_title_hints(base, body, source)
    for match in re.finditer(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', text, re.I):
        url = canonicalize(urljoin(base, html.unescape(match.group(1))))
        if not url_in_scope(source, url):
            continue
        item_start = text.rfind('<div class="list-item"', max(0, match.start() - 2500), match.start())
        context = text[item_start if item_start >= 0 else max(0, match.start() - 1200):match.start()]
        titles = re.findall(r'<h[1-6][^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)</h[1-6]>', context, re.I | re.S)
        dates = re.findall(r'([12]\d{3})[/-](\d{1,2})[/-](\d{1,2})', context)
        title = re.sub(r'<[^>]+>', ' ', titles[-1]).strip() if titles else fallback_titles.get(url)
        title = html.unescape(re.sub(r"\s+", " ", title)) if title else None
        published = f"{int(dates[-1][0]):04d}-{int(dates[-1][1]):02d}-{int(dates[-1][2]):02d}" if dates else None
        results[url] = (title, published)
    return results


def store_discovered_hint(db: sqlite3.Connection, source: RuntimeSource, url: str,
                          title: str | None, published_at: str | None = None,
                          review_status: str | None = None,
                          parent_url: str | None = None) -> None:
    scopes = source.raw.get("company_scope", [])
    company_id = scopes[0] if len(scopes) == 1 else None
    matched_legal_entity = source.raw.get("_matched_legal_entity")
    if parent_url:
        parent = db.execute("""SELECT company_id,matched_legal_entity
            FROM discovered_metadata WHERE source_id=? AND canonical_url=?""",
                            (source.source_id, canonicalize(parent_url))).fetchone()
        if parent:
            company_id = parent[0] or company_id
            matched_legal_entity = parent[1] or matched_legal_entity
    document_types = source.raw.get("document_types", [])
    document_type = document_types[0] if len(document_types) == 1 else (
        "announcements" if "announcement" in source.source_id else "attachment")
    if review_status and not urlparse(url).path.lower().endswith(".pdf"):
        # Official search result hints establish entity scope, while the detail
        # page parser remains responsible for classifying the content type.
        document_type = None
    db.execute("""INSERT INTO discovered_metadata
        (source_id,canonical_url,title,published_at,company_id,matched_legal_entity,document_type,review_status)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET
        title=COALESCE(NULLIF(excluded.title,''),discovered_metadata.title),
        published_at=COALESCE(excluded.published_at,discovered_metadata.published_at),
        company_id=COALESCE(excluded.company_id,discovered_metadata.company_id),
        matched_legal_entity=COALESCE(excluded.matched_legal_entity,discovered_metadata.matched_legal_entity),
        document_type=COALESCE(excluded.document_type,discovered_metadata.document_type),
        review_status=COALESCE(excluded.review_status,discovered_metadata.review_status)""",
        (source.source_id, canonicalize(url), title, published_at, company_id,
         matched_legal_entity, document_type, review_status))
    db.commit()


def enqueue_frontier(db: sqlite3.Connection, sid: str, url: str, discovered_from: str | None, depth: int) -> None:
    canonical = canonicalize(url)
    db.execute("""INSERT INTO crawl_frontier
        (source_id,url,discovered_from,depth,status,discovered_at,priority) VALUES(?,?,?,?, 'pending', ?,?)
        ON CONFLICT(source_id,url) DO UPDATE SET
        priority=excluded.priority,depth=MIN(crawl_frontier.depth,excluded.depth)""",
        (sid, canonical, discovered_from, depth, now_iso(), frontier_priority(canonical)))
    db.commit()


def frontier_priority(url: str) -> int:
    parsed = urlparse(url)
    page_values = parse_qs(parsed.query).get("page", [])
    if page_values and str(page_values[0]).isdigit():
        return int(page_values[0])
    match = re.search(r"-(\d+)\.html$", parsed.path, re.I)
    return int(match.group(1)) if match else 0


def discovery_depth(parent_depth: int, url: str, parent_url: str | None = None) -> int:
    """Pagination walks an archive horizontally; content links descend a level."""
    if frontier_priority(url) > 0:
        return parent_depth
    if parent_url and frontier_priority(parent_url) > 0:
        return min(parent_depth + 1, 3)
    return parent_depth + 1


def listing_page_dates(url: str, body: bytes) -> list[str]:
    if frontier_priority(url) <= 0:
        return []
    text = body.decode("utf-8", errors="ignore")
    dates = []
    for year, month, day in re.findall(r'class=["\'][^"\']*date[^"\']*["\'][^>]*>\s*([12]\d{3})[年/.-](\d{1,2})[月/.-](\d{1,2})', text, re.I):
        value = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if value not in dates:
            dates.append(value)
    return dates


def prune_archive_after_boundary(db: sqlite3.Connection, sid: str, boundary_url: str) -> int:
    """Close already-discovered older pages from the same archive after its date boundary."""
    boundary_page = frontier_priority(boundary_url)
    if boundary_page <= 0:
        return 0
    boundary = urlparse(canonicalize(boundary_url))
    boundary_query = parse_qs(boundary.query, keep_blank_values=True)
    boundary_query.pop("page", None)
    candidates = db.execute("""SELECT url FROM crawl_frontier
        WHERE source_id=? AND status='pending' AND priority>?""", (sid, boundary_page)).fetchall()
    pruned = []
    for (candidate_url,) in candidates:
        candidate = urlparse(candidate_url)
        candidate_query = parse_qs(candidate.query, keep_blank_values=True)
        candidate_query.pop("page", None)
        if ((candidate.scheme, candidate.netloc, candidate.path) ==
                (boundary.scheme, boundary.netloc, boundary.path) and
                candidate_query == boundary_query):
            pruned.append((now_iso(), sid, candidate_url))
    if pruned:
        db.executemany("""UPDATE crawl_frontier SET status='skipped_scope',processed_at=?,
            last_error='archive page beyond backfill boundary' WHERE source_id=? AND url=?""", pruned)
        db.commit()
    return len(pruned)


def next_frontier(db: sqlite3.Connection, sid: str, limit: int) -> list[tuple[str, int]]:
    return [(row[0], int(row[1])) for row in db.execute(
        "SELECT url,depth FROM crawl_frontier WHERE source_id=? AND status='pending' ORDER BY depth,priority,discovered_at,url LIMIT ?",
        (sid, limit)).fetchall()]


def finish_frontier(db: sqlite3.Connection, sid: str, url: str, status: str, error: str | None = None) -> None:
    db.execute("UPDATE crawl_frontier SET status=?,processed_at=?,last_error=? WHERE source_id=? AND url=?",
               (status, now_iso(), error, sid, canonicalize(url)))
    db.commit()


def blocked_host(db: sqlite3.Connection, sid: str, host: str | None) -> str | None:
    row = db.execute("SELECT reason FROM host_state WHERE source_id=? AND host=? AND state='manual_review'", (sid, host)).fetchone()
    return row[0] if row else None


def block_host(db: sqlite3.Connection, sid: str, host: str | None, reason: str) -> None:
    db.execute("""INSERT INTO host_state(source_id,host,state,reason,updated_at) VALUES(?,?,'manual_review',?,?)
        ON CONFLICT(source_id,host) DO UPDATE SET state='manual_review',reason=excluded.reason,updated_at=excluded.updated_at""",
        (sid, host, reason, now_iso()))
    db.commit()


def collect_source(db: sqlite3.Connection, source: RuntimeSource, user_agent: str, max_pages: int,
                   discover: bool, start_url: str | None = None,
                   form_data: dict | None = None) -> None:
    sid = source.source_id
    cooldown = active_cooldown(db, sid)
    if cooldown:
        audit({"source_id": sid, "outcome": "skipped_cooldown", "cooldown_until": cooldown, "route": source.route})
        return
    session = session_for(source, user_agent)
    timeout = int(source.profile["request_timeout_seconds"])
    min_interval = float(source.profile["min_interval_seconds"])
    request_gate = lambda target_url: reserve_host_slot(
        db, urlparse(target_url).hostname or "", sid, min_interval)
    base_url = start_url or source.raw["base_url"]
    base_host = urlparse(base_url).hostname
    base_block_reason = blocked_host(db, sid, base_host)
    if base_block_reason:
        audit({"source_id": sid, "outcome": "skipped_host_manual_review", "url": base_url,
               "route": source.route, "error": base_block_reason})
        if base_host == urlparse(source.raw["base_url"]).hostname:
            set_source_state(db, sid, previous_failures(db, sid), "manual_review", base_block_reason)
        return
    allowed, robots_note = robot_allowed(session, base_url, user_agent, timeout, request_gate, db)
    audit({"source_id": sid, "outcome": robots_note, "url": base_url, "route": source.route, "allowed": allowed})
    if not allowed:
        set_source_state(db, sid, 0, "manual_review", robots_note)
        return
    robots_checked_hosts = {urlparse(base_url).hostname}
    if discover:
        enqueue_frontier(db, sid, base_url, None, 0)
        queue: list[tuple[str, int]] = next_frontier(db, sid, 1)
    else:
        queue = [(base_url, 0)]
    seen: set[str] = set()
    failures = previous_failures(db, sid)
    while len(seen) < max_pages:
        if not queue:
            if discover:
                queue = [item for item in next_frontier(db, sid, 1) if item[0] not in seen]
            if not queue:
                break
        url, depth = queue.pop(0)
        if canonicalize(url) in seen:
            continue
        seen.add(canonicalize(url))
        target_host = urlparse(url).hostname
        if not url_in_scope(source, url):
            audit({"source_id": sid, "outcome": "skipped_out_of_scope", "url": url, "route": source.route})
            if discover: finish_frontier(db, sid, url, "skipped_scope", "outside configured host/path scope")
            continue
        host_reason = blocked_host(db, sid, target_host)
        if host_reason:
            audit({"source_id": sid, "outcome": "skipped_host_manual_review", "url": url, "route": source.route, "error": host_reason})
            if discover: finish_frontier(db, sid, url, "manual_review", host_reason)
            continue
        if target_host not in robots_checked_hosts:
            host_allowed, host_note = robot_allowed(session, url, user_agent, timeout, request_gate, db)
            audit({"source_id": sid, "outcome": host_note, "url": url, "route": source.route, "allowed": host_allowed})
            robots_checked_hosts.add(target_host)
            if not host_allowed:
                block_host(db, sid, target_host, host_note)
                if discover: finish_frontier(db, sid, url, "manual_review", host_note)
                continue
        request_gate(url)
        started = time.monotonic()
        try:
            # A conditional request is meaningless for a form submission: the
            # interface has no entity tag for a query it has not been asked yet.
            conditional = {} if form_data is not None else prior_headers(db, sid, url)
            response, body = safe_get(session, url, conditional, timeout, form_data)
            elapsed_ms = round((time.monotonic() - started) * 1000)
        except safety.UnsafeRequest as exc:
            # Not a transport failure and not worth retrying: the target is not
            # something this collector may fetch, and that is a fact about the
            # target, so it goes to manual review with the reason.
            audit({"source_id": sid, "outcome": "refused_unsafe_request", "url": url,
                   "route": source.route, "error": str(exc)[:180]})
            if discover: finish_frontier(db, sid, url, "manual_review", str(exc)[:180])
            continue
        except requests.RequestException as exc:
            failures += 1
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
            cooldown_s = backoff_seconds("transport", failures)
            state = "manual_review" if failures > int(source.profile["max_transport_retries"]) else "cooldown"
            audit({"source_id": sid, "outcome": "transport_error", "url": url, "route": source.route, "error": error, "cooldown_seconds": cooldown_s})
            if discover: finish_frontier(db, sid, url, "pending", error)
            set_source_state(db, sid, failures, state, error, cooldown_s)
            return
        status = response.status_code
        base_event = {"source_id": sid, "url": url, "final_url": response.url, "http_status": status, "route": source.route, "elapsed_ms": elapsed_ms}
        if status == 304:
            audit({**base_event, "outcome": "not_modified"})
            if discover: finish_frontier(db, sid, url, "done")
            failures = 0
            continue
        if status in (401, 403):
            cooldown_s = 86400 if status == 403 else 0
            audit({**base_event, "outcome": "manual_review_access_control", "cooldown_seconds": cooldown_s})
            if discover: finish_frontier(db, sid, url, "manual_review", f"HTTP {status}")
            set_source_state(db, sid, failures + 1, "manual_review", f"HTTP {status}", cooldown_s)
            return
        if status == 429:
            cooldown_s = retry_after_seconds(response.headers.get("Retry-After"))
            audit({**base_event, "outcome": "rate_limited", "cooldown_seconds": cooldown_s})
            if discover: finish_frontier(db, sid, url, "pending", "HTTP 429")
            set_source_state(db, sid, failures + 1, "cooldown", "HTTP 429", cooldown_s)
            return
        if status == 408 or status >= 500:
            failures += 1
            cooldown_s = 3600 if failures >= 5 else backoff_seconds("server", failures)
            state = "circuit_open" if failures >= 5 else "cooldown"
            audit({**base_event, "outcome": "http_error", "cooldown_seconds": cooldown_s, "retry_scheduled": True})
            if discover: finish_frontier(db, sid, url, "pending", f"HTTP {status}")
            set_source_state(db, sid, failures, state, f"HTTP {status}", cooldown_s)
            return
        if (status in (404, 410) and
                source.raw.get("store_not_found_response") and
                canonicalize(url) != canonicalize(source.raw["base_url"])):
            digest, raw_path = save_raw(sid, response, body)
            metadata = record_document(
                db, source, url, response, digest, raw_path, "", "search_response")
            audit({**base_event, "outcome": "not_found_response_stored",
                   "content_hash": digest, "raw_path": raw_path,
                   "bytes": len(body), "title": metadata["title"]})
            if discover:
                finish_frontier(db, sid, url, "done")
            failures = 0
            set_source_state(db, sid, 0, "closed", None)
            continue
        if status in (404, 410) and canonicalize(url) != canonicalize(source.raw["base_url"]):
            audit({**base_event, "outcome": "not_found", "retry_scheduled": False})
            if discover:
                finish_frontier(db, sid, url, "skipped_missing", f"HTTP {status}")
            failures = 0
            set_source_state(db, sid, 0, "closed", None)
            continue
        if status >= 400:
            failures += 1
            audit({**base_event, "outcome": "http_error", "retry_scheduled": False})
            if discover: finish_frontier(db, sid, url, "manual_review", f"HTTP {status}")
            set_source_state(db, sid, failures, "manual_review", f"HTTP {status}")
            return
        if redirected_to_landing(url, response.url, source.raw["base_url"]):
            audit({**base_event, "outcome": "redirected_to_landing", "retry_scheduled": False})
            if discover:
                finish_frontier(db, sid, url, "skipped_missing", "deep link redirected to source landing page")
            failures = 0
            set_source_state(db, sid, 0, "closed", None)
            continue
        detected_challenge = challenge_marker(body)
        if detected_challenge:
            digest, diagnostic_path = save_raw(sid, response, body)
            audit({**base_event, "outcome": "manual_review_challenge", "content_hash": digest,
                   "diagnostic_path": diagnostic_path, "challenge_marker": detected_challenge})
            block_host(db, sid, target_host, f"challenge marker {detected_challenge} detected")
            if discover: finish_frontier(db, sid, url, "manual_review", "challenge")
            if target_host == urlparse(source.raw["base_url"]).hostname:
                set_source_state(db, sid, failures + 1, "manual_review",
                                 f"challenge marker {detected_challenge} detected")
            return
        if urlparse(response.url).path.lower().endswith(".pdf") and not body.startswith(b"%PDF"):
            digest, diagnostic_path = save_raw(sid, response, body)
            audit({**base_event, "outcome": "manual_review_content_mismatch", "content_hash": digest,
                   "diagnostic_path": diagnostic_path, "error": "PDF URL did not return a PDF signature"})
            block_host(db, sid, target_host, "PDF content mismatch")
            if discover: finish_frontier(db, sid, url, "manual_review", "PDF content mismatch")
            if target_host == urlparse(source.raw["base_url"]).hostname:
                set_source_state(db, sid, failures + 1, "manual_review", "PDF content mismatch")
            return
        digest, raw_path = save_raw(sid, response, body)
        is_html = "html" in response.headers.get("Content-Type", "").lower()
        title, links = relevant_links(response.url, body, source) if is_html else ("", [])
        title_hints = link_title_hints(response.url, body, source) if is_html else {}
        attachment_hints = attachment_metadata_hints(response.url, body, source) if is_html else {}
        visible_dates = listing_page_dates(response.url, body) if is_html else []
        boundary_reached = bool(visible_dates and max(visible_dates) < str(source.raw.get("backfill_start", "0000-00-00")))
        metadata = record_document(db, source, url, response, digest, raw_path, title,
                                   "outside_backfill_window" if boundary_reached else "pending_entity_review")
        audit({**base_event, "outcome": "success", "content_hash": digest, "raw_path": raw_path,
               "bytes": len(body), "title": metadata["title"]})
        if boundary_reached:
            pruned_pages = prune_archive_after_boundary(db, sid, response.url)
            audit({"source_id": sid, "outcome": "backfill_boundary_reached", "url": url,
                   "newest_visible_date": max(visible_dates), "backfill_start": source.raw.get("backfill_start"),
                   "pruned_pending_pages": pruned_pages})
        failures = 0
        set_source_state(db, sid, 0, "closed", None)
        if discover:
            finish_frontier(db, sid, url, "done")
            if metadata.get("review_status") != "outside_backfill_window" and not boundary_reached:
                search_seed_page = bool(
                    source.raw.get("discover_search_results") and
                    canonicalize(url) == canonicalize(source.raw["base_url"]))
                for link in links:
                    child_depth = discovery_depth(depth, link, response.url)
                    if child_depth > 3:
                        continue
                    attachment_date = None
                    if search_seed_page:
                        store_discovered_hint(
                            db, source, link, None, None,
                            "candidate_entity_match")
                    if link.lower().endswith(".pdf"):
                        attachment_title, attachment_date = attachment_hints.get(link, (title_hints.get(link), None))
                        attachment_date = attachment_date or metadata.get("published_at")
                        store_discovered_hint(
                            db, source, link, attachment_title, attachment_date,
                            "candidate_entity_match" if source.raw.get("collection_mode") == "search" else None,
                            parent_url=url)
                    enqueue_frontier(db, sid, link, canonicalize(url), child_depth)
                    if attachment_date and attachment_date < str(source.raw.get("backfill_start", "0000-00-00")):
                        finish_frontier(db, sid, link, "skipped_scope", "attachment predates backfill window")
            remaining = max_pages - len(seen)
            if remaining > 0:
                queued_urls = {item[0] for item in queue}
                queue.extend(item for item in next_frontier(db, sid, 1) if item[0] not in queued_urls and item[0] not in seen)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compliance-first collection of registered public sources")
    p.add_argument("--config", type=Path, default=ROOT / "config" / "sources.yaml")
    p.add_argument("--source", action="append", default=[], help="source_id; repeatable")
    p.add_argument("--priority", choices=["P0", "P1", "P2"])
    p.add_argument("--max-pages", type=int, default=1, help="hard cap per source (default: 1)")
    p.add_argument("--discover", action="store_true", help="follow a small number of same-domain relevant links")
    p.add_argument("--url", help="explicit public URL for one selected source; host must match its registered domain")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_pages < 1 or args.max_pages > 20:
        raise SystemExit("--max-pages must be between 1 and 20")
    contact = os.environ.get("COLLECTOR_CONTACT", "project-owner-not-configured")
    user_agent = f"ListedCompanyEventIntelligence/0.1 (+{contact})"
    _, sources = load_sources(args.config, set(args.source), args.priority)
    if args.source and len(sources) != len(set(args.source)):
        found = {s.source_id for s in sources}
        missing = sorted(set(args.source) - found)
        raise SystemExit(f"unknown or manual-only source(s): {', '.join(missing)}")
    db = connect()
    if args.url:
        if len(sources) != 1:
            raise SystemExit("--url requires exactly one --source")
        registered = urlparse(sources[0].raw["base_url"]).hostname or ""
        requested = urlparse(args.url).hostname or ""
        allowed_hosts = set(sources[0].raw.get("allowed_hosts", [registered]))
        if requested not in allowed_hosts:
            raise SystemExit("--url host must be explicitly registered in allowed_hosts")
    for source in sources:
        collect_source(db, source, user_agent, args.max_pages, args.discover, args.url)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
