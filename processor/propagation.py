#!/usr/bin/env python3
"""Link documents by how a story travelled between them.

A near-duplicate cluster answers one question — which documents are the same
text — and nothing else.  It cannot say that an article restates a prospectus,
that a company answered a report, or that a later piece corrected an earlier
one.  Those relations are what turns a pile of documents into a story with an
origin, and what stops twelve reprints from reading as twelve confirmations.

Every edge here is deterministic and carries the basis it was drawn from, so a
reader can disagree with a specific rule instead of with a black box.  Edge
kinds:

``REPOST`` 转载        the same text carried by another outlet
``REWRITE`` 改写       the same story rewritten
``RECURRING`` 同源重复 one source's own repeated template filing
``FROM_DISCLOSURE`` 源于披露  a claim the authoritative filing supports and predates
``FOLLOW_UP`` 跟进     a later, related report on the same issue
``RESPONSE`` 回应      an official or company document after a media report
``CLARIFICATION`` 澄清 a document denying or clarifying an earlier report
``CORRECTION`` 纠正    a document correcting an earlier report
``CONFLICT`` 冲突      a claim the authoritative filing contradicts
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterable

from collector.collect import audit, connect
from processor.pipeline import ISSUE_LABELS, add_column, configured_companies, tokenize, utc_now

# 边的说明是给人读的，写成中文（I-06）：此前印着「主张裁决 SUPPORTED」「同议题 EXPORT_CTRL」。
VERDICT_WORDS = {"SUPPORTED": "支持", "PARTIAL": "部分支持", "CONFLICT": "冲突",
                 "UNVERIFIABLE": "无法核验"}

EDGE_KINDS = ("FROM_DISCLOSURE", "REPOST", "REWRITE", "RECURRING", "FOLLOW_UP", "RESPONSE",
              "CLARIFICATION", "CORRECTION", "CONFLICT")
# Only copying propagates a story.  Grouping by 源于披露 instead would merge every
# article that quotes the prospectus into one component and erase the very
# independence the component count exists to measure.  Same-source recurrence
# belongs here too: a company filing the same daily return for a year is one
# source repeating itself, never a story spreading.
DERIVATION_KINDS = ("REPOST", "REWRITE", "RECURRING")
REPOST_SIMILARITY = 0.90
FOLLOW_UP_MIN_SIMILARITY = 0.30
# A denial or correction is recognised by its wording, so it needs far less
# textual overlap with what it answers than a follow-up does.
REACTION_MIN_SIMILARITY = 0.15
LOOKBACK_DAYS = 30
# Bounded so a 250-event issue cannot turn edge building into an O(n²) scan.
LOOKBACK_LIMIT = 40
CLARIFICATION_TERMS = ("澄清", "辟谣", "不实", "否认", "失实", "谣言")
CORRECTION_TERMS = ("更正", "勘误", "纠正", "撤稿", "报道有误")
# A company answering a report says so.  Without this, every quarterly earnings
# release that happens to follow a story about revenue reads as a 回应 to it,
# which invents a dialogue that never took place.
RESPONSE_TERMS = ("回应", "答复", "回复", "问询函", "予以说明", "作出说明", "特此说明",
                  "就相关报道", "针对报道", "针对上述报道", "声明")
OFFICIAL_TIERS = {"S0", "S1"}


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS propagation_edges(
        edge_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, edge_kind TEXT NOT NULL,
        from_kind TEXT NOT NULL, from_version_id TEXT NOT NULL, to_version_id TEXT NOT NULL,
        issue_code TEXT, similarity REAL, lag_hours REAL,
        basis TEXT NOT NULL, detected_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS propagation_edges_to ON propagation_edges(to_version_id)")
    db.execute("CREATE INDEX IF NOT EXISTS propagation_edges_from ON propagation_edges(from_version_id)")
    db.execute("CREATE INDEX IF NOT EXISTS propagation_edges_company ON propagation_edges(company_id,edge_kind)")
    db.execute("""CREATE TABLE IF NOT EXISTS propagation_components(
        component_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, root_version_id TEXT NOT NULL,
        member_count INTEGER NOT NULL, source_count INTEGER NOT NULL,
        first_published_at TEXT, last_published_at TEXT, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS propagation_component_members(
        component_id TEXT NOT NULL, version_id TEXT NOT NULL, company_id TEXT NOT NULL,
        depth INTEGER NOT NULL, PRIMARY KEY(component_id, version_id))""")
    db.execute("""CREATE INDEX IF NOT EXISTS propagation_members_version
        ON propagation_component_members(version_id)""")
    add_column(db, "issue_ledger", "independent_component_count", "INTEGER NOT NULL DEFAULT 0")


def as_moment(value: str | None) -> datetime | None:
    """Parse the publication timestamps the collector actually stores.

    Two shapes occur side by side: a bare ``2026-05-20`` from filing indexes and
    a full ``2026-08-24T02:28:41.759Z`` from news APIs.  A bare date is read as
    midnight UTC, which is only ever used for ordering and lag, never presented
    as a precise time.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lag_hours(earlier: str | None, later: str | None) -> float | None:
    start, end = as_moment(earlier), as_moment(later)
    if not start or not end:
        return None
    return round((end - start).total_seconds() / 3600, 2)


def similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def edge_id(kind: str, source: str, target: str, issue_code: str | None) -> str:
    return hashlib.sha256(f"{kind}\0{source}\0{target}\0{issue_code or ''}".encode()).hexdigest()[:32]


def has_term(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def reaction_kind(later: dict[str, Any], earlier: dict[str, Any], overlap: float) -> str | None:
    """Classify how a later document relates to an earlier one on the same issue.

    Wording decides in every case, and the channel only narrows it.  Coming from
    an official channel after a media report is not on its own evidence of a
    reply: routine results announcements would qualify, and the graph would fill
    with exchanges that never happened.
    """
    text = f"{later['title'] or ''}\n{later['summary'] or ''}"
    if overlap >= REACTION_MIN_SIMILARITY:
        if has_term(text, CORRECTION_TERMS):
            return "CORRECTION"
        if has_term(text, CLARIFICATION_TERMS):
            return "CLARIFICATION"
        if (overlap >= FOLLOW_UP_MIN_SIMILARITY and has_term(text, RESPONSE_TERMS)
                and later["source_tier"] in OFFICIAL_TIERS
                and earlier["source_tier"] not in OFFICIAL_TIERS):
            return "RESPONSE"
    return "FOLLOW_UP" if overlap >= FOLLOW_UP_MIN_SIMILARITY else None


def event_rows(db: sqlite3.Connection, companies: set[str] | None) -> list[dict[str, Any]]:
    """Load every event with the text used for relatedness.

    Summary and claims stand in for the article body on purpose: they are already
    in the database, they are what the event actually asserts, and using them
    keeps edge building free of file IO.
    """
    query = """SELECT e.event_id,e.version_id,e.company_id,e.summary,e.issue_codes_json,
                      e.production_mode,v.title,v.published_at,v.source_id,v.source_tier
               FROM event_records e JOIN document_versions v ON v.version_id=e.version_id
               WHERE e.company_id IS NOT NULL"""
    parameters: list[Any] = []
    if companies:
        query += f" AND e.company_id IN ({','.join('?' for _ in companies)})"
        parameters = sorted(companies)
    claims: dict[str, list[str]] = defaultdict(list)
    for event_id, text in db.execute("SELECT event_id,text FROM event_claims"):
        claims[event_id].append(text)
    rows = []
    for row in db.execute(query, parameters):
        item = dict(row)
        item["issue_codes"] = json.loads(item.pop("issue_codes_json") or "[]")
        item["tokens"] = tokenize("\n".join(
            [str(item["title"] or ""), str(item["summary"] or "")] + claims.get(item["event_id"], [])))
        rows.append(item)
    return rows


def copy_kind(score: float, same_source: bool) -> str:
    """Name a copy edge by who made the copy.

    Calling a company's own daily template filing a 转载 of its previous one is
    simply wrong, and it is the difference between a story that spread and a
    source that repeats itself.  Both still collapse into one component, because
    neither is independent corroboration.
    """
    if same_source:
        return "RECURRING"
    return "REPOST" if score >= REPOST_SIMILARITY else "REWRITE"


def duplicate_edges(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Draw carrier edges from the duplicate work the pipeline already did.

    Direction always runs from the version that came first, because a cluster
    representative is chosen as the earliest publication — the one the rest
    plausibly derive from.
    """
    edges: list[dict[str, Any]] = []
    published = dict(db.execute("SELECT version_id,published_at FROM document_versions"))
    sources = dict(db.execute("SELECT version_id,source_id FROM document_versions"))
    company = dict(db.execute(
        "SELECT version_id,company_id FROM document_versions WHERE company_id IS NOT NULL"))

    def add(source: str, target: str, score: float, company_id: str, basis: str) -> None:
        kind = copy_kind(score, sources.get(source) == sources.get(target))
        edges.append({"kind": kind, "from_kind": "media", "from": source, "to": target,
                      "issue_code": None, "similarity": round(score, 4),
                      "lag_hours": lag_hours(published.get(source), published.get(target)),
                      "basis": basis, "company_id": company_id})

    for version_id, target in db.execute(
            "SELECT version_id,exact_duplicate_of FROM processed_documents WHERE exact_duplicate_of IS NOT NULL"):
        if company.get(target) and company.get(target) == company.get(version_id):
            add(target, version_id, 1.0, company[target], "正文内容完全相同")
    for row in db.execute("""SELECT m.version_id,m.similarity,c.company_id,c.representative_version_id
            FROM duplicate_cluster_members m JOIN duplicate_clusters c ON c.cluster_id=m.cluster_id"""):
        member, score, company_id, representative = row
        if member == representative:
            continue
        add(representative, member, float(score), company_id,
            f"近重复簇代表文档相似度 {float(score):.2f}")
    return edges


def verdict_edges(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Link an article to the filing that supports or contradicts what it claims.

    ``FROM_DISCLOSURE`` additionally requires the filing to predate the article:
    a report cannot be derived from a document published after it, and drawing
    that edge would invent an origin the timeline contradicts.
    """
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    rows = db.execute("""SELECT DISTINCT w.verdict,w.evidence_version_id,w.evidence_page,e.version_id,
                                e.company_id,c.issue_code,ev.published_at AS evidence_published,
                                v.published_at AS article_published
        FROM claim_verdicts w
        JOIN event_claims c ON c.claim_id=w.claim_id
        JOIN event_records e ON e.event_id=c.event_id
        JOIN document_versions v ON v.version_id=e.version_id
        JOIN document_versions ev ON ev.version_id=w.evidence_version_id
        WHERE w.evidence_version_id IS NOT NULL AND w.verdict<>'UNVERIFIABLE'""").fetchall()
    for row in rows:
        kind = "CONFLICT" if row["verdict"] == "CONFLICT" else "FROM_DISCLOSURE"
        if kind == "FROM_DISCLOSURE":
            filing, article = as_moment(row["evidence_published"]), as_moment(row["article_published"])
            if filing and article and filing > article:
                continue
        key = (kind, str(row["evidence_version_id"]), str(row["version_id"]))
        if key in seen:
            continue
        seen.add(key)
        page = f"第{row['evidence_page']}页" if row["evidence_page"] else "未标注页码"
        edges.append({"kind": kind, "from_kind": "disclosure", "from": row["evidence_version_id"],
                      "to": row["version_id"], "issue_code": row["issue_code"], "similarity": None,
                      "lag_hours": lag_hours(row["evidence_published"], row["article_published"]),
                      "basis": f"核验结论：{VERDICT_WORDS.get(row['verdict'], row['verdict'])}，证据{page}", "company_id": row["company_id"]})
    return edges


def sequence_edges(rows: list[dict[str, Any]], linked: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Connect each event to the single earlier event it most plausibly follows.

    One antecedent per event, not all of them.  A dense mesh between every pair
    of related articles is unreadable and says nothing more than the issue
    ledger already does; the strongest earlier match is the claim worth making.
    """
    by_issue: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for issue in row["issue_codes"]:
            by_issue[(row["company_id"], issue)].append(row)
    edges: list[dict[str, Any]] = []
    for (company_id, issue), members in by_issue.items():
        ordered = sorted(members, key=lambda item: (str(item["published_at"] or "9999"), item["version_id"]))
        for index, later in enumerate(ordered):
            moment = as_moment(later["published_at"])
            best: tuple[float, dict[str, Any]] | None = None
            for earlier in reversed(ordered[max(0, index - LOOKBACK_LIMIT):index]):
                if earlier["version_id"] == later["version_id"]:
                    continue
                if (earlier["version_id"], later["version_id"]) in linked:
                    continue
                earlier_moment = as_moment(earlier["published_at"])
                if moment and earlier_moment and (moment - earlier_moment).days > LOOKBACK_DAYS:
                    break
                overlap = similarity(earlier["tokens"], later["tokens"])
                if best is None or overlap > best[0]:
                    best = (overlap, earlier)
            if best is None:
                continue
            overlap, earlier = best
            kind = reaction_kind(later, earlier, overlap)
            if not kind:
                continue
            edges.append({"kind": kind, "from_kind": "media", "from": earlier["version_id"],
                          "to": later["version_id"], "issue_code": issue,
                          "similarity": round(overlap, 4),
                          "lag_hours": lag_hours(earlier["published_at"], later["published_at"]),
                          "basis": f"同一专题「{ISSUE_LABELS.get(issue, issue)}」，与前序报道相似度 {overlap:.2f}",
                          "company_id": company_id})
    return edges


def components(edges: list[dict[str, Any]], versions: dict[str, str]
               ) -> tuple[list[list[str]], dict[str, set[str]]]:
    """Group versions into independent propagation components.

    A component is one story: a root plus everything that copied or rewrote it.
    Documents nothing copied form their own component, because a single original
    report is still an independent story and must be counted as one.  The copy
    adjacency is returned alongside so callers can measure depth from the root.
    """
    parent: dict[str, str] = {version_id: version_id for version_id in versions}
    adjacency: dict[str, set[str]] = defaultdict(set)

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for edge in edges:
        if edge["kind"] not in DERIVATION_KINDS:
            continue
        source, target = edge["from"], edge["to"]
        if source not in parent or target not in parent:
            continue
        adjacency[source].add(target)
        adjacency[target].add(source)
        left, right = find(source), find(target)
        if left != right:
            parent[left] = right
    grouped: dict[str, list[str]] = defaultdict(list)
    for version_id in versions:
        grouped[find(version_id)].append(version_id)
    return [sorted(members) for members in grouped.values()], adjacency


def depths(root: str, members: list[str], adjacency: dict[str, set[str]]) -> dict[str, int]:
    """Hops from the root along copy edges, so a reprint chain reads as a chain."""
    found = {root: 0}
    queue = [root]
    allowed = set(members)
    while queue:
        node = queue.pop(0)
        for neighbour in sorted(adjacency.get(node, ())):
            if neighbour in allowed and neighbour not in found:
                found[neighbour] = found[node] + 1
                queue.append(neighbour)
    return {member: found.get(member, 0) for member in members}


def build(db: sqlite3.Connection, companies: set[str] | None = None) -> dict[str, Any]:
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    rows = event_rows(db, companies)
    duplicates = duplicate_edges(db)
    linked = {(edge["from"], edge["to"]) for edge in duplicates}
    linked |= {(edge["to"], edge["from"]) for edge in duplicates}
    edges = duplicates + verdict_edges(db) + sequence_edges(rows, linked)
    if companies:
        edges = [edge for edge in edges if edge["company_id"] in companies]
    # A filing adjudicated against itself, or a document recorded as its own
    # duplicate, is not propagation; and the same relation found twice is still
    # one relation, so the stored count matches what was decided here.
    unique: dict[str, dict[str, Any]] = {}
    for edge in edges:
        if edge["from"] == edge["to"]:
            continue
        unique.setdefault(edge_id(edge["kind"], edge["from"], edge["to"], edge["issue_code"]), edge)
    edges = list(unique.values())

    if companies:
        placeholders = ",".join("?" for _ in companies)
        db.execute(f"DELETE FROM propagation_edges WHERE company_id IN ({placeholders})", sorted(companies))
    else:
        db.execute("DELETE FROM propagation_edges")
    now = utc_now()
    db.executemany("""INSERT OR REPLACE INTO propagation_edges
        (edge_id,company_id,edge_kind,from_kind,from_version_id,to_version_id,issue_code,
         similarity,lag_hours,basis,detected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [(key, edge["company_id"], edge["kind"], edge["from_kind"], edge["from"], edge["to"],
          edge["issue_code"], edge["similarity"], edge["lag_hours"], edge["basis"], now)
         for key, edge in unique.items()])

    version_company = {row["version_id"]: row["company_id"] for row in rows}
    for edge in edges:
        if edge["kind"] in DERIVATION_KINDS:
            version_company.setdefault(edge["from"], edge["company_id"])
            version_company.setdefault(edge["to"], edge["company_id"])
    published = dict(db.execute("SELECT version_id,published_at FROM document_versions"))
    sources = dict(db.execute("SELECT version_id,source_id FROM document_versions"))
    made, adjacency = components(edges, version_company)
    db.execute("DELETE FROM propagation_component_members")
    db.execute("DELETE FROM propagation_components")
    component_of: dict[str, str] = {}
    for members in made:
        company_id = version_company[members[0]]
        root = min(members, key=lambda item: (str(published.get(item) or "9999"), item))
        component_id = hashlib.sha256(f"component\0{company_id}\0{root}".encode()).hexdigest()[:24]
        dates = sorted(str(published[item]) for item in members if published.get(item))
        db.execute("""INSERT INTO propagation_components(component_id,company_id,root_version_id,
            member_count,source_count,first_published_at,last_published_at,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (component_id, company_id, root, len(members),
             len({sources.get(item) for item in members}),
             dates[0] if dates else None, dates[-1] if dates else None, now))
        for member, depth in depths(root, members, adjacency).items():
            db.execute("""INSERT INTO propagation_component_members(component_id,version_id,company_id,depth)
                VALUES(?,?,?,?)""", (component_id, member, company_id, depth))
            component_of[member] = component_id

    # 独立传播成分 is the corroboration figure the issue ledger should report:
    # twelve reprints of one report are one component, not twelve confirmations.
    per_issue: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        for issue in row["issue_codes"]:
            per_issue[(row["company_id"], issue)].add(component_of.get(row["version_id"], row["version_id"]))
    for (company_id, issue), found in per_issue.items():
        db.execute("UPDATE issue_ledger SET independent_component_count=? WHERE company_id=? AND issue_code=?",
                   (len(found), company_id, issue))

    kinds: dict[str, int] = defaultdict(int)
    for edge in edges:
        kinds[edge["kind"]] += 1
    return {"events": len(rows), "edges": len(edges), "edge_kinds": dict(sorted(kinds.items())),
            "components": len(made),
            "largest_component": max((len(members) for members in made), default=0)}


def run(companies: set[str] | None = None) -> dict[str, Any]:
    db = connect()
    db.row_factory = sqlite3.Row
    summary = build(db, companies)
    db.commit()
    db.close()
    audit({"source_id": "system", "outcome": "propagation_built",
           "edges": summary["edges"], "components": summary["components"]})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build propagation edges and independent components")
    parser.add_argument("--company", action="append", choices=configured_companies())
    args = parser.parse_args()
    print(json.dumps(run(set(args.company) if args.company else None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
