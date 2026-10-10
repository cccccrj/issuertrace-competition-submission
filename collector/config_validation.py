#!/usr/bin/env python3
"""Validation for the machine-readable source and entity inventory."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    pass


def validate_config(config: dict) -> None:
    errors: list[str] = []
    entities = config.get("entities") or {}
    sources = config.get("sources") or []
    enums = config.get("enums") or {}
    defaults = config.get("collection_defaults") or {}
    scheduling = defaults.get("scheduling") or {}
    priorities = set((enums.get("priority") or {}).keys())
    tiers = set((enums.get("source_tier") or {}).keys())
    modes = {"crawl", "search", "api", "manual"}
    cadences = {"hourly", "daily", "weekly"}
    try:
        scope_start = date.fromisoformat(config["scope"]["backfill_start"])
        scope_end = date.fromisoformat(config["scope"]["snapshot_as_of"])
        if scope_start > scope_end:
            errors.append("scope.backfill_start must not be after snapshot_as_of")
    except (KeyError, TypeError, ValueError):
        errors.append("scope requires valid ISO backfill_start and snapshot_as_of dates")
    focus_company = scheduling.get("focus_company")
    if focus_company is not None and focus_company not in entities:
        errors.append(f"collection_defaults.scheduling.focus_company: unknown entity {focus_company!r}")
    if "paused" in scheduling and not isinstance(scheduling.get("paused"), bool):
        errors.append("collection_defaults.scheduling.paused must be boolean")
    adapter_jobs = scheduling.get("adapter_jobs_per_source_per_cycle", 1)
    if (not isinstance(adapter_jobs, int) or isinstance(adapter_jobs, bool)
            or not 1 <= adapter_jobs <= 6):
        errors.append("scheduling.adapter_jobs_per_source_per_cycle must be between 1 and 6")
    if ("drain_discovered_details_first" in scheduling and
            not isinstance(scheduling.get("drain_discovered_details_first"), bool)):
        errors.append("scheduling.drain_discovered_details_first must be boolean")
    completion_gate = scheduling.get("completion_gate")
    if completion_gate is not None:
        if not isinstance(completion_gate, dict):
            errors.append("collection_defaults.scheduling.completion_gate must be a mapping")
        else:
            gate_company = completion_gate.get("company_id")
            if gate_company not in entities:
                errors.append(f"completion_gate.company_id: unknown entity {gate_company!r}")
            minimum = completion_gate.get("minimum_valid_documents")
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum <= 0:
                errors.append("completion_gate.minimum_valid_documents must be a positive integer")
            gate_types = completion_gate.get("document_types")
            if (not isinstance(gate_types, list) or not gate_types or
                    any(not isinstance(value, str) or not value for value in gate_types)):
                errors.append("completion_gate.document_types must be a non-empty string list")
            if not isinstance(completion_gate.get("stop_daemon"), bool):
                errors.append("completion_gate.stop_daemon must be boolean")

    source_ids = [source.get("source_id") for source in sources]
    duplicates = sorted({sid for sid in source_ids if sid and source_ids.count(sid) > 1})
    if duplicates:
        errors.append("duplicate source_id: " + ", ".join(duplicates))
    active_source_ids = scheduling.get("active_source_ids") or []
    if not isinstance(active_source_ids, list):
        errors.append("collection_defaults.scheduling.active_source_ids must be a list")
        active_source_ids = []
    unknown_active = sorted(set(active_source_ids) - set(source_ids))
    if unknown_active:
        errors.append("scheduling active_source_ids reference unknown sources: " +
                      ", ".join(unknown_active))
    duplicate_active = sorted({sid for sid in active_source_ids
                               if active_source_ids.count(sid) > 1})
    if duplicate_active:
        errors.append("duplicate scheduling active_source_ids: " + ", ".join(duplicate_active))
    for index, source in enumerate(sources):
        label = source.get("source_id") or f"sources[{index}]"
        missing = [key for key in (
            "source_id", "name", "base_url", "company_scope", "priority", "source_tier",
            "languages", "document_types", "collection_mode", "backfill_start", "cadence",
            "paywall", "evidence_eligible", "evidence_role", "terms_and_robots_note",
        ) if key not in source]
        if missing:
            errors.append(f"{label}: missing {', '.join(missing)}")
        parsed = urlparse(str(source.get("base_url") or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            errors.append(f"{label}: base_url must be an absolute HTTP(S) URL")
        if source.get("priority") not in priorities:
            errors.append(f"{label}: invalid priority {source.get('priority')!r}")
        if source.get("source_tier") not in tiers:
            errors.append(f"{label}: invalid source_tier {source.get('source_tier')!r}")
        if source.get("collection_mode") not in modes:
            errors.append(f"{label}: invalid collection_mode {source.get('collection_mode')!r}")
        if source.get("cadence") not in cadences:
            errors.append(f"{label}: invalid cadence {source.get('cadence')!r}")
        if source.get("original_timezone"):
            try:
                ZoneInfo(source["original_timezone"])
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                errors.append(f"{label}: invalid original_timezone {source.get('original_timezone')!r}")
        unknown_entities = sorted(set(source.get("company_scope") or []) - set(entities))
        if unknown_entities:
            errors.append(f"{label}: unknown company_scope {', '.join(unknown_entities)}")
        if "first_party_entity" in source:
            if source.get("first_party_entity") is not True:
                errors.append(f"{label}: first_party_entity must be true when present")
            if len(source.get("company_scope") or []) != 1:
                errors.append(f"{label}: first_party_entity requires exactly one company_scope")
            if source.get("collection_mode") != "crawl":
                errors.append(f"{label}: first_party_entity requires crawl collection_mode")
            if source.get("evidence_role") != "company_statement":
                errors.append(f"{label}: first_party_entity requires company_statement evidence_role")
        if source.get("attachment_filename_date_pattern"):
            try:
                pattern = re.compile(str(source["attachment_filename_date_pattern"]))
                if "date" not in pattern.groupindex:
                    errors.append(f"{label}: attachment_filename_date_pattern requires named date group")
            except re.error:
                errors.append(f"{label}: invalid attachment_filename_date_pattern")
        try:
            source_start = date.fromisoformat(source["backfill_start"])
            if "scope_start" in locals() and source_start < scope_start:
                errors.append(f"{label}: backfill_start precedes configured scope")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label}: invalid backfill_start")

    routing = defaults.get("network_routing") or {}
    direct = set(routing.get("direct_source_ids") or [])
    proxy = set(routing.get("proxy_source_ids") or [])
    overlap = sorted(direct & proxy)
    if overlap:
        errors.append("sources assigned to both DIRECT and PROXY: " + ", ".join(overlap))
    unknown_routing = sorted((direct | proxy) - set(source_ids))
    if unknown_routing:
        errors.append("routing references unknown sources: " + ", ".join(unknown_routing))

    access = defaults.get("access_control") or {}
    profiles = access.get("profiles") or {}
    assigned: dict[str, str] = {}
    for profile, ids in (access.get("profile_source_ids") or {}).items():
        if profile not in profiles:
            errors.append(f"unknown access profile: {profile}")
        for sid in ids or []:
            if sid in assigned:
                errors.append(f"{sid}: assigned to multiple access profiles")
            assigned[sid] = profile
            if sid not in source_ids:
                errors.append(f"access profile references unknown source: {sid}")
    for source in sources:
        if source.get("collection_mode") == "manual" and assigned.get(source.get("source_id")) != "manual_only":
            errors.append(f"{source.get('source_id')}: manual source must use manual_only profile")

    if errors:
        raise ConfigError("invalid source configuration:\n- " + "\n- ".join(errors))


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Validate source inventory and collection policy")
    parser.add_argument("path", nargs="?", type=Path, default=Path("config/sources.yaml"))
    args = parser.parse_args()
    config = yaml.safe_load(args.path.read_text(encoding="utf-8"))
    validate_config(config)
    print(f"valid sources={len(config['sources'])} entities={len(config['entities'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
