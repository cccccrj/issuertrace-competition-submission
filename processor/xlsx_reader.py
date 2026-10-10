#!/usr/bin/env python3
"""Streaming reader for the .xlsx files of the competition's labelled dataset.

Written against the standard library on purpose.  ``openpyxl`` in
``read_only=True`` mode still materialises ``xl/sharedStrings.xml`` into a list
before it yields a single cell; in the unlabelled pool that part is 576 MB of
XML, which does not fit in this machine's 3 GB.  One reader has to serve both
the 8 MB labelled files and the 233 MB pool, so the size that cannot be loaded
decides the design.

Two access shapes follow from that:

``rows``          one pass, shared strings held in memory, for the small files.
``select_rows``   three passes and constant memory, for the pool: scan the
                  shared strings for the values a caller cares about, scan the
                  sheet to find which rows reference them, then scan the shared
                  strings once more and materialise only those rows.

Two details of the format are load-bearing and are easy to get wrong.  An empty
cell is *omitted* from its ``<row>`` rather than written as blank, so column
identity must be read from the cell's ``r="C7"`` reference and never inferred
from position.  And a cell with ``t="inlineStr"`` carries its text under ``<is>``
instead of ``<v>``, so a reader that only looks at ``<v>`` silently returns
nothing for it.
"""
from __future__ import annotations

import datetime as _dt
import re
import zipfile
from pathlib import Path
from typing import Callable, Iterator, Sequence
from xml.etree.ElementTree import iterparse

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# ``rows`` holds every shared string at once.  Above this the caller is asking
# for something the machine cannot do, and failing loudly beats being killed by
# the OOM reaper halfway through an import.
SHARED_STRINGS_LIMIT = 64 * 1024 * 1024

# Excel serial dates count from 1899-12-30 because the format reproduces a bug
# that treats 1900 as a leap year.  Serial 60 is that phantom 1900-02-29, so
# anything at or below it cannot be converted unambiguously and is rejected.
EXCEL_EPOCH = _dt.date(1899, 12, 30)
FIRST_UNAMBIGUOUS_SERIAL = 61

COLUMN_REF = re.compile(r"^([A-Z]+)")


def _column(ref: str) -> str:
    """Column letters of a cell reference such as ``C7``."""
    match = COLUMN_REF.match(ref)
    if not match:
        raise ValueError(f"unrecognised cell reference: {ref!r}")
    return match.group(1)


def sheet_names(path: Path | str) -> list[str]:
    """Worksheet names, in workbook order."""
    with zipfile.ZipFile(path) as archive:
        return [name for name, _ in _sheets(archive)]


def _sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(name, archive path) for every worksheet.

    The workbook lists sheets by relationship id, so the name a human uses and
    the part that holds the cells are only connected through the .rels file.
    """
    import xml.etree.ElementTree as ElementTree

    rels = {
        node.get("Id"): node.get("Target", "")
        for node in ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    }
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheets = []
    for node in workbook.iter(MAIN_NS + "sheet"):
        target = rels[node.get(REL_NS + "id")].lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets.append((node.get("name", ""), target))
    return sheets


def _sheet_part(archive: zipfile.ZipFile, sheet: str) -> str:
    for name, target in _sheets(archive):
        if name == sheet:
            return target
    raise KeyError(f"no such worksheet: {sheet!r} (have {sheet_names(archive.filename)})")


def _si_text(element) -> str:
    """Text of one shared-string entry, with rich-text runs concatenated."""
    return "".join(node.text or "" for node in element.iter(MAIN_NS + "t"))


def _iter_shared_strings(archive: zipfile.ZipFile) -> Iterator[tuple[int, str]]:
    """(index, text) for each shared string, without holding the file."""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return
    with archive.open("xl/sharedStrings.xml") as handle:
        index = -1
        for _, element in iterparse(handle, ("end",)):
            if element.tag != MAIN_NS + "si":
                continue
            index += 1
            yield index, _si_text(element)
            element.clear()


def _cell_values(row_element) -> dict[str, tuple[str, str]]:
    """Raw cells of a row as column letter -> (kind, payload).

    ``kind`` is ``"shared"`` with a shared-string index as payload, or
    ``"text"`` with the literal value.  Resolving shared strings is left to the
    caller because the two access shapes resolve them at different times.
    """
    cells: dict[str, tuple[str, str]] = {}
    for cell in row_element.iter(MAIN_NS + "c"):
        column = _column(cell.get("r", "A1"))
        kind = cell.get("t")
        if kind == "inlineStr":
            inline = cell.find(MAIN_NS + "is")
            cells[column] = ("text", _si_text(inline) if inline is not None else "")
            continue
        value = cell.find(MAIN_NS + "v")
        if value is None:
            cells[column] = ("text", "")
        elif kind == "s":
            cells[column] = ("shared", value.text or "0")
        else:
            cells[column] = ("text", value.text or "")
    return cells


def _iter_rows(archive: zipfile.ZipFile, part: str) -> Iterator[dict[str, tuple[str, str]]]:
    with archive.open(part) as handle:
        for _, element in iterparse(handle, ("end",)):
            if element.tag != MAIN_NS + "row":
                continue
            yield _cell_values(element)
            element.clear()


def header(path: Path | str, sheet: str) -> dict[str, str]:
    """Column letter -> header name, from the sheet's first row.

    Resolved with a partial shared-string scan: header cells sit at low indices,
    so this stops long before the end of a 576 MB part.
    """
    with zipfile.ZipFile(path) as archive:
        part = _sheet_part(archive, sheet)
        first: dict[str, tuple[str, str]] = {}
        for cells in _iter_rows(archive, part):
            first = cells
            break
        if not first:
            return {}
        wanted = {int(payload) for kind, payload in first.values() if kind == "shared"}
        resolved: dict[int, str] = {}
        if wanted:
            ceiling = max(wanted)
            for index, text in _iter_shared_strings(archive):
                if index in wanted:
                    resolved[index] = text
                if index >= ceiling:
                    break
        return {
            column: (resolved.get(int(payload), "") if kind == "shared" else payload)
            for column, (kind, payload) in first.items()
        }


def _named(cells: dict[str, tuple[str, str]], columns: dict[str, str],
           strings: dict[int, str] | list[str]) -> dict[str, str]:
    out: dict[str, str] = {name: "" for name in columns.values() if name}
    for column, (kind, payload) in cells.items():
        name = columns.get(column)
        if not name:
            continue
        out[name] = strings[int(payload)] if kind == "shared" else payload
    return out


def rows(path: Path | str, sheet: str) -> Iterator[dict[str, str]]:
    """Every data row of ``sheet`` as header-name -> value, header row excluded.

    Holds all shared strings, so it refuses parts above ``SHARED_STRINGS_LIMIT``
    rather than exhausting memory.  Use ``select_rows`` for those.
    """
    with zipfile.ZipFile(path) as archive:
        info = archive.NameToInfo.get("xl/sharedStrings.xml")
        if info is not None and info.file_size > SHARED_STRINGS_LIMIT:
            raise MemoryError(
                f"{Path(path).name}: sharedStrings.xml is {info.file_size} bytes, "
                f"over the {SHARED_STRINGS_LIMIT} byte limit; use select_rows()")
        part = _sheet_part(archive, sheet)
        strings = [text for _, text in _iter_shared_strings(archive)]
        columns: dict[str, str] = {}
        for cells in _iter_rows(archive, part):
            if not columns:
                columns = {
                    column: (strings[int(payload)] if kind == "shared" else payload)
                    for column, (kind, payload) in cells.items()
                }
                continue
            yield _named(cells, columns, strings)


def select_rows(path: Path | str, sheet: str, *,
                predicate: Callable[[str], bool],
                probe_columns: Sequence[str],
                limit: int | None = None) -> Iterator[dict[str, str]]:
    """Rows whose ``probe_columns`` hold a shared string accepted by ``predicate``.

    Three passes, constant memory in the size of the file:

    A. scan the shared strings, keeping only the *indices* ``predicate`` accepts;
    B. scan the sheet, noting which rows reference one of those indices in a
       probe column, and collecting the string indices those rows need;
    C. scan the shared strings again, materialising only what step B asked for.

    ``predicate`` only ever sees shared strings.  A probe cell written inline
    cannot be matched, which is safe for these files — every text cell in them
    is shared — but would silently under-select elsewhere, so
    ``inline_probe_cells`` is counted and reported through ``ValueError`` if any
    are found.
    """
    with zipfile.ZipFile(path) as archive:
        part = _sheet_part(archive, sheet)
        columns = header(path, sheet)
        by_name = {name: column for column, name in columns.items()}
        missing = [name for name in probe_columns if name not in by_name]
        if missing:
            raise KeyError(f"{sheet!r} has no column named {missing!r}")
        probes = {by_name[name] for name in probe_columns}

        # Pass A -- which shared strings does the caller want?
        matching = {index for index, text in _iter_shared_strings(archive) if predicate(text)}
        if not matching:
            return

        # Pass B -- which rows reference them, and what strings do those rows need?
        selected: list[dict[str, tuple[str, str]]] = []
        needed: set[int] = set()
        inline_probe_cells = 0
        for position, cells in enumerate(_iter_rows(archive, part)):
            if position == 0:
                continue
            hit = False
            for column in probes:
                kind, payload = cells.get(column, ("text", ""))
                if kind == "shared":
                    if int(payload) in matching:
                        hit = True
                        break
                elif payload:
                    inline_probe_cells += 1
            if not hit:
                continue
            selected.append(cells)
            needed.update(int(payload) for kind, payload in cells.values() if kind == "shared")
            if limit is not None and len(selected) >= limit:
                break
        if inline_probe_cells:
            raise ValueError(
                f"{Path(path).name}/{sheet}: {inline_probe_cells} probe cells are inline "
                "strings, which this reader cannot match; widen probe_columns or use rows()")
        if not selected:
            return

        # Pass C -- materialise just those strings.
        ceiling = max(needed) if needed else -1
        resolved: dict[int, str] = {}
        for index, text in _iter_shared_strings(archive):
            if index in needed:
                resolved[index] = text
            if index >= ceiling:
                break
        for cells in selected:
            yield _named(cells, columns, resolved)


def excel_serial_to_iso(value: str | float | None) -> str | None:
    """Excel serial date -> ISO-8601, or None if it is not a usable serial.

    Returns a date when the serial has no time part and a datetime when it does,
    so a value that carried no clock reading does not acquire a spurious
    midnight.
    """
    if value is None or value == "":
        return None
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if serial < FIRST_UNAMBIGUOUS_SERIAL:
        return None
    days = int(serial)
    seconds = round((serial - days) * 86400)
    if seconds >= 86400:  # rounded up past midnight
        days += 1
        seconds -= 86400
    day = EXCEL_EPOCH + _dt.timedelta(days=days)
    if seconds == 0:
        return day.isoformat()
    return _dt.datetime.combine(day, _dt.time()).replace(
        hour=seconds // 3600, minute=seconds % 3600 // 60, second=seconds % 60).isoformat()
