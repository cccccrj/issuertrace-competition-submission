import tempfile
import unittest
import zipfile
from pathlib import Path

from processor import xlsx_reader
from processor.xlsx_reader import (
    excel_serial_to_iso, header, rows, select_rows, sheet_names,
)

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
</Types>"""

PACKAGE_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def workbook_xml(names):
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{sheets}</sheets></workbook>')


def workbook_rels(names):
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i, _ in enumerate(names, start=1))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{rels}</Relationships>')


def shared_strings_xml(values):
    items = "".join(f"<si><t>{v}</t></si>" for v in values)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            f' count="{len(values)}" uniqueCount="{len(values)}">{items}</sst>')


def sheet_xml(rows_spec):
    """rows_spec: list of {column: (kind, payload)} with kind s / n / inline."""
    body = []
    for index, cells in enumerate(rows_spec, start=1):
        rendered = []
        for column, (kind, payload) in cells.items():
            ref = f'{column}{index}'
            if kind == "s":
                rendered.append(f'<c r="{ref}" t="s"><v>{payload}</v></c>')
            elif kind == "inline":
                rendered.append(f'<c r="{ref}" t="inlineStr"><is><t>{payload}</t></is></c>')
            else:
                rendered.append(f'<c r="{ref}"><v>{payload}</v></c>')
        body.append(f'<row r="{index}">{"".join(rendered)}</row>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>')


def build_workbook(path, sheets, strings):
    """sheets: list of (name, rows_spec)."""
    names = [name for name, _ in sheets]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", PACKAGE_RELS)
        archive.writestr("xl/workbook.xml", workbook_xml(names))
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels(names))
        archive.writestr("xl/sharedStrings.xml", shared_strings_xml(strings))
        for index, (_, spec) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml(spec))


class WorkbookTestCase(unittest.TestCase):
    """A two-sheet workbook exercising shared strings, inline text and a gap.

    Strings: 0 title, 1 source, 2 note, 3 alpha, 4 新浪财经, 5 beta, 6 腾讯网.
    """

    STRINGS = ["title", "source", "note", "alpha", "新浪财经", "beta", "腾讯网"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "book.xlsx"
        build_workbook(self.path, [
            ("首页", [
                {"A": ("s", 0), "B": ("s", 1), "C": ("s", 2)},
                {"A": ("s", 3), "B": ("s", 4), "C": ("n", "46024")},
                # B is omitted entirely: an empty cell is absent, not blank.
                {"A": ("s", 5), "C": ("n", "46025")},
                {"A": ("inline", "gamma"), "B": ("s", 6), "C": ("n", "46026")},
            ]),
            ("次页", [
                {"A": ("s", 0)},
                {"A": ("s", 3)},
            ]),
        ], self.STRINGS)


class SheetDiscoveryTests(WorkbookTestCase):
    def test_sheet_names_follow_workbook_order(self):
        self.assertEqual(sheet_names(self.path), ["首页", "次页"])

    def test_header_maps_column_letters_to_names(self):
        self.assertEqual(header(self.path, "首页"),
                         {"A": "title", "B": "source", "C": "note"})

    def test_unknown_sheet_is_rejected(self):
        with self.assertRaises(KeyError):
            list(rows(self.path, "不存在"))


class RowReadingTests(WorkbookTestCase):
    def test_rows_are_keyed_by_header_and_exclude_it(self):
        got = list(rows(self.path, "首页"))
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0], {"title": "alpha", "source": "新浪财经", "note": "46024"})

    def test_a_skipped_empty_cell_does_not_shift_column_identity(self):
        got = list(rows(self.path, "首页"))
        # Row 3 omits B.  Reading positionally would slide 46025 into "source".
        self.assertEqual(got[1], {"title": "beta", "source": "", "note": "46025"})

    def test_inline_strings_are_read_from_the_is_element(self):
        got = list(rows(self.path, "首页"))
        self.assertEqual(got[2]["title"], "gamma")

    def test_every_header_column_is_present_on_every_row(self):
        for row in rows(self.path, "首页"):
            self.assertEqual(set(row), {"title", "source", "note"})

    def test_shared_strings_over_the_limit_are_refused(self):
        original = xlsx_reader.SHARED_STRINGS_LIMIT
        xlsx_reader.SHARED_STRINGS_LIMIT = 10
        self.addCleanup(setattr, xlsx_reader, "SHARED_STRINGS_LIMIT", original)
        with self.assertRaises(MemoryError) as caught:
            list(rows(self.path, "首页"))
        self.assertIn("select_rows", str(caught.exception))


class SelectRowsTests(WorkbookTestCase):
    def test_only_rows_matching_the_predicate_come_back(self):
        got = list(select_rows(self.path, "首页",
                               predicate=lambda text: text == "新浪财经",
                               probe_columns=("source",)))
        self.assertEqual([row["title"] for row in got], ["alpha"])

    def test_the_header_row_is_never_selected(self):
        # "source" is both a header cell and a candidate value.
        got = list(select_rows(self.path, "首页",
                               predicate=lambda text: text == "source",
                               probe_columns=("source",)))
        self.assertEqual(got, [])

    def test_limit_caps_the_selection(self):
        got = list(select_rows(self.path, "首页",
                               predicate=lambda text: text in {"新浪财经", "腾讯网"},
                               probe_columns=("source",), limit=1))
        self.assertEqual(len(got), 1)

    def test_no_match_yields_nothing(self):
        self.assertEqual(list(select_rows(self.path, "首页",
                                          predicate=lambda text: False,
                                          probe_columns=("source",))), [])

    def test_unknown_probe_column_is_rejected(self):
        with self.assertRaises(KeyError):
            list(select_rows(self.path, "首页", predicate=lambda text: True,
                             probe_columns=("nope",)))

    def test_an_inline_probe_cell_raises_instead_of_under_selecting(self):
        with self.assertRaises(ValueError) as caught:
            list(select_rows(self.path, "首页", predicate=lambda text: text == "腾讯网",
                             probe_columns=("title",)))
        self.assertIn("inline", str(caught.exception))

    def test_selection_agrees_with_a_full_read(self):
        expected = [row for row in rows(self.path, "首页") if row["source"] == "腾讯网"]
        got = list(select_rows(self.path, "首页",
                               predicate=lambda text: text == "腾讯网",
                               probe_columns=("source",)))
        self.assertEqual(got, expected)


class ExcelSerialTests(unittest.TestCase):
    def test_known_anchors_convert_exactly(self):
        # Excel's own numbering: 44927 is 2023-01-01, 45658 is 2025-01-01.
        self.assertEqual(excel_serial_to_iso(44927), "2023-01-01")
        self.assertEqual(excel_serial_to_iso("45658"), "2025-01-01")

    def test_a_whole_serial_stays_a_date(self):
        self.assertEqual(excel_serial_to_iso("46024"), "2026-01-02")

    def test_a_fractional_serial_becomes_a_datetime(self):
        self.assertEqual(excel_serial_to_iso(46024.4497916667), "2026-01-02T10:47:42")

    def test_rounding_up_to_midnight_advances_the_day(self):
        self.assertEqual(excel_serial_to_iso(46024 + 0.9999999), "2026-01-03")

    def test_the_phantom_leap_day_is_refused(self):
        # Serial 60 is Excel's non-existent 1900-02-29; below 61 nothing is safe.
        self.assertIsNone(excel_serial_to_iso(60))
        self.assertEqual(excel_serial_to_iso(61), "1900-03-01")

    def test_unusable_values_return_none(self):
        for value in (None, "", "abc", "2026-01-01"):
            self.assertIsNone(excel_serial_to_iso(value), value)


if __name__ == "__main__":
    unittest.main()
