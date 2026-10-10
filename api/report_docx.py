"""Word reports, written with the standard library.

A .docx is a zip of XML parts, and the subset a report needs — headings,
paragraphs, bullets and a table — is small.  Writing it directly keeps the
platform's dependency list where it is and, more to the point, keeps the export
working on a machine with no network: the alternative was a rendering library
plus a CJK font, and a demo that fails offline because a font is missing is the
kind of failure this project cannot afford.

Word opens the result directly and exports it to PDF, which is the other half of
"PDF/Word" the brief asks for.
"""
from __future__ import annotations

import zipfile
from io import BytesIO
from typing import Any, Iterable
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}</w:body></w:document>"""

# Half-points, as Word counts them.
SIZES = {"title": 32, "h1": 26, "h2": 22, "p": 21, "small": 18}


def text_run(text: str, *, bold: bool = False, size: int = SIZES["p"],
             grey: bool = False) -> str:
    # East Asian text needs the font named for its own script too, or Word falls
    # back per-character and the report comes out in two typefaces.
    properties = (f'<w:rPr><w:rFonts w:ascii="Calibri" w:eastAsia="Microsoft YaHei"/>'
                  f'{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/>'
                  f'{"<w:color w:val=\"666666\"/>" if grey else ""}</w:rPr>')
    return f'<w:r>{properties}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def paragraph(text: str, *, bold: bool = False, size: int = SIZES["p"], grey: bool = False,
              bullet: bool = False, spacing: int = 60) -> str:
    indent = '<w:ind w:left="360" w:hanging="180"/>' if bullet else ""
    body = text_run(("• " if bullet else "") + text, bold=bold, size=size, grey=grey)
    return f'<w:p><w:pPr><w:spacing w:after="{spacing}"/>{indent}</w:pPr>{body}</w:p>'


def table(rows: list[list[str]]) -> str:
    """A plain grid; the first row is the header."""
    if not rows:
        return ""
    width = 9360 // max(1, len(rows[0]))
    cells = []
    for index, row in enumerate(rows):
        line = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'
            f'{paragraph(str(value), bold=index == 0, size=SIZES["small"], spacing=20)}</w:tc>'
            for value in row)
        cells.append(f"<w:tr>{line}</w:tr>")
    borders = "".join(f'<w:{edge} w:val="single" w:sz="4" w:color="CCCCCC"/>'
                      for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
    return (f'<w:tbl><w:tblPr><w:tblBorders>{borders}</w:tblBorders></w:tblPr>'
            + "".join(cells) + "</w:tbl>" + paragraph("", spacing=0))


def render(blocks: Iterable[tuple[str, Any]]) -> bytes:
    body = []
    for kind, value in blocks:
        if kind == "title":
            body.append(paragraph(value, bold=True, size=SIZES["title"], spacing=160))
        elif kind == "h1":
            body.append(paragraph(value, bold=True, size=SIZES["h1"], spacing=120))
        elif kind == "h2":
            body.append(paragraph(value, bold=True, size=SIZES["h2"], spacing=80))
        elif kind == "note":
            body.append(paragraph(value, size=SIZES["small"], grey=True))
        elif kind == "bullet":
            body.append(paragraph(value, bullet=True))
        elif kind == "table":
            body.append(table(value))
        else:
            body.append(paragraph(value))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", DOCUMENT.format(body="".join(body)))
    return buffer.getvalue()
