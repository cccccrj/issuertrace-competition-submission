"""Source names a reader recognises, instead of internal source codes.

``toms_hardware`` and ``cninfo_announcements`` are configuration keys; they were
printed on a dozen pages, in Word reports and in alert emails (I-06 platform-
wide review).  Every source already has a ``name`` in sources.yaml.
"""
from __future__ import annotations

import re

from processor.pipeline import load_config

#: Pseudo-sources the collector and processors write their own log lines under.
INTERNAL_SOURCES = {"system": "平台", "manual": "人工导入", "scheduler": "调度"}


_names: tuple[int, dict[str, str]] | None = None


def _configured_names() -> dict[str, str]:
    """source_id → display name, rebuilt only when the cached config object changes."""
    global _names
    config = load_config()
    if _names is None or _names[0] != id(config):
        names = {}
        for source in config.get("sources") or []:
            if source.get("source_id") and source.get("name"):
                # 「巨潮资讯网上市公司公告（证监会指定信息披露网站）」 — the remark is for
                # whoever maintains the list, not for the reader of a source line.
                name = str(source["name"])
                names[source["source_id"]] = re.sub(r"[（(][^）)]*[）)]\s*$", "", name).strip() or name
        _names = (id(config), names)
    return _names[1]


def source_name(source_id: str | None) -> str | None:
    """The configured name of a source; never the internal code itself."""
    if not source_id:
        return None
    if source_id in INTERNAL_SOURCES:
        return INTERNAL_SOURCES[source_id]
    return _configured_names().get(source_id, "其他来源")


def with_source_names(row: dict, *keys: str) -> dict:
    """Add ``<key>_name`` beside every source-id field present in ``row``."""
    for key in keys or ("source_id",):
        if key in row:
            stem = key[:-3] if key.endswith("_id") else key
            row[f"{stem}_name"] = source_name(row[key])
    return row


#: Fields that carry a source code, wherever they appear in a response.
SOURCE_KEYS = ("source_id", "lead_source", "representative_source", "root_source",
               "from_source", "to_source")


def name_sources(value):
    """Walk a response and put a readable name beside every source code.

    Done once, on the way out, rather than in thirty queries: a field added to
    one of them later would otherwise reach the page as ``toms_hardware`` again.
    """
    if isinstance(value, dict):
        named = {key: name_sources(item) for key, item in value.items()}
        for key in SOURCE_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item and f"{key}_name" not in value:
                stem = key[:-3] if key.endswith("_id") else key
                if f"{stem}_name" not in value:
                    named[f"{stem}_name"] = source_name(item)
        sources = value.get("sources")
        if isinstance(sources, list) and sources and all(isinstance(item, str) for item in sources):
            named.setdefault("source_names", [source_name(item) for item in sources])
        return named
    if isinstance(value, list):
        return [name_sources(item) for item in value]
    return value
