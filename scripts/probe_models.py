#!/usr/bin/env python3
"""Measure candidate models on a real document before committing to one.

Model choice for this workload is an empirical question: the corpus is mostly
Chinese disclosure text, volume is high, and every label must satisfy a JSON
schema.  This probe sends one preserved document to each candidate and reports
which ``response_format`` mode the gateway honours, how long it takes and
whether the output validates.  It never writes to the database.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time

from jsonschema import ValidationError, validate
from openai import OpenAI

from collector.collect import ROOT, connect
from processor.ai_label import locate_claims, SYSTEM_PROMPT, make_prompt, schema

CANDIDATES = ("qwen-plus", "qwen-turbo", "deepseek-chat", "deepseek-v4-flash", "glm-4-flash")


def sample_document(company: str | None) -> tuple[dict[str, str], str]:
    db = connect()
    db.row_factory = sqlite3.Row
    query = """SELECT v.company_id,v.title,v.published_at,v.document_type,p.text_path
               FROM processed_documents p JOIN document_versions v ON v.version_id=p.version_id
               WHERE p.inclusion_status='included' AND p.extraction_status='ok'
                 AND p.text_chars BETWEEN 1500 AND 8000 AND v.company_id IS NOT NULL"""
    params: list[str] = []
    if company:
        query += " AND v.company_id=?"
        params.append(company)
    query += " ORDER BY v.published_at DESC LIMIT 1"
    row = db.execute(query, params).fetchone()
    db.close()
    if row is None:
        raise SystemExit("no eligible document found; run processor.pipeline first")
    text = (ROOT / row["text_path"]).read_text(encoding="utf-8")
    return dict(row), text


def probe(client: OpenAI, model: str, prompt: str, mode: str,
          document_text: str) -> dict[str, object]:
    """Return one measurement row; never raises so the table always completes."""
    if mode == "json_schema":
        response_format = {"type": "json_schema",
                           "json_schema": {"name": "event_label", "strict": True, "schema": schema()}}
        system = SYSTEM_PROMPT
    else:
        response_format = {"type": "json_object"}
        system = f"{SYSTEM_PROMPT}\n\n输出必须是符合以下 JSON Schema 的单个 JSON 对象：\n{json.dumps(schema(), ensure_ascii=False)}"
    started = time.monotonic()
    try:
        completion = client.chat.completions.create(
            model=model, temperature=0, timeout=120,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            response_format=response_format,
        )
    except Exception as exc:  # noqa: BLE001 - the failure text is the measurement
        return {"model": model, "mode": mode, "ok": False, "seconds": round(time.monotonic() - started, 1),
                "detail": type(exc).__name__ + ": " + str(exc)[:110]}
    elapsed = round(time.monotonic() - started, 1)
    body = completion.choices[0].message.content or ""
    usage = getattr(completion, "usage", None)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"model": model, "mode": mode, "ok": False, "seconds": elapsed, "detail": "not valid JSON"}
    try:
        validate(parsed, schema())
    except ValidationError as exc:
        return {"model": model, "mode": mode, "ok": False, "seconds": elapsed,
                "detail": "schema violation: " + str(exc.message)[:90]}
    # Schema compliance is not usefulness.  A claim that is not a verbatim span
    # of the document is discarded by locate_claims(), so a model that writes
    # fluent paraphrase passes every check here and still yields nothing --
    # and the call was paid for either way.  Cost per *kept* claim is the
    # number that decides this, so it is measured rather than assumed.
    claims = parsed.get("claims", [])
    located, _ = locate_claims(dict(parsed, claims=list(claims)), document_text)
    kept = len(located.get("claims", []))
    tokens = getattr(usage, "total_tokens", None)
    return {"model": model, "mode": mode, "ok": True, "seconds": elapsed,
            "tokens": tokens,
            "claims": len(claims), "kept": kept,
            "verbatim_rate": round(kept / len(claims), 2) if claims else None,
            "tokens_per_kept": round(tokens / kept) if tokens and kept else None,
            "detail": f"{parsed['eventhood']}, {kept}/{len(claims)} 条主张可定位"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare candidate models on one preserved document")
    parser.add_argument("--company", choices=("cxmt", "wuxi_apptec"))
    parser.add_argument("--model", action="append", help="override candidate list; repeatable")
    args = parser.parse_args()

    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("LLM_API_KEY is not set; export it from .env first")
    client = OpenAI(api_key=key, base_url=os.environ.get("LLM_BASE_URL"))

    row, text = sample_document(args.company)
    prompt = make_prompt(row["company_id"], row["title"] or "", row["published_at"] or "",
                         row["document_type"] or "", text)
    print(f"document: {row['title']}\ncompany: {row['company_id']}  chars: {len(text)}\n")
    header = (f"{'model':<20} {'mode':<12} {'ok':<5} {'sec':>6} {'tokens':>7} "
              f"{'逐字率':>7} {'token/条':>9}  detail")
    print(header)
    print("-" * len(header))
    for model in (args.model or CANDIDATES):
        for mode in ("json_schema", "json_object"):
            result = probe(client, model, prompt, mode, text)
            print(f"{result['model']:<20} {result['mode']:<12} {str(result['ok']):<5} "
                  f"{result['seconds']:>6} {str(result.get('tokens') or '-'):>7} "
                  f"{str(result.get('verbatim_rate') if result.get('verbatim_rate') is not None else '-'):>7} "
                  f"{str(result.get('tokens_per_kept') or '-'):>9}  {result['detail']}")
            if result["ok"] and mode == "json_schema":
                break  # strict mode works; no need to measure the fallback


if __name__ == "__main__":
    main()
