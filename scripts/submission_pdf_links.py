"""Restore DOCX hyperlinks when a headless PDF exporter omits link annotations.

Uses exact hyperlink labels and the renderer's dedicated blue text runs.
Only annotations change: PDF text, page layout, tables and images stay intact.
Ambiguous/missing labels fail closed before the PDF is replaced.
"""
from __future__ import annotations
import argparse
import re
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
import fitz

ROOT = Path(__file__).resolve().parents[1]
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
BLUE = int("0563C1", 16)


def normalize(text):
    return re.sub(r"\s+", "", text)


def expected_links(docx):
    with zipfile.ZipFile(docx) as archive:
        relations = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in relations if r.attrib["Type"].endswith("/hyperlink")}
        document = ET.fromstring(archive.read("word/document.xml"))
    return [(normalize("".join(t.text or "" for t in link.iter(W + "t"))),
             targets[link.attrib[R + "id"]])
            for link in document.iter(W + "hyperlink")]


def blue_groups(pdf):
    group = []
    for page in pdf:
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    chars = [(page.number, c["c"], fitz.Rect(c["bbox"])) for c in span["chars"] if not c["c"].isspace()]
                    if span["color"] == BLUE:
                        group.extend(chars)
                    elif chars and group:
                        yield group
                        group = []
        if group:
            yield group
            group = []


def rectangles(chars):
    result = []
    for page, _, rect in chars:
        if result and result[-1][0] == page and abs(result[-1][1].y0 - rect.y0) < .5:
            result[-1] = (page, result[-1][1] | rect)
        else:
            result.append((page, rect))
    return result


def restore(pdf_path):
    expected = expected_links(pdf_path.with_suffix(".docx"))
    with fitz.open(pdf_path) as pdf:
        existing = Counter(link.get("uri") for page in pdf for link in page.get_links())
        if all(existing[url] >= count for url, count in Counter(url for _, url in expected).items()):
            return 0
        remaining = expected.copy()
        annotations = []
        # Table-cell labels can wrap around intervening black text in the PDF
        # reading order. Only dedicated hyperlink-colored glyphs participate.
        for group in [[char for group in blue_groups(pdf) for char in group]]:
            text = "".join(c for _, c, _ in group)
            offset = 0
            while offset < len(text):
                matches = [(i, label, url) for i, (label, url) in enumerate(remaining)
                           if label and text[offset:].startswith(label)]
                if not matches:
                    raise ValueError("PDF hyperlink label does not match DOCX: " + text[offset:])
                # Longest label handles labels that are prefixes of another;
                # repeated identical labels follow DOCX reading order.
                i, label, url = sorted(matches, key=lambda item: (-len(item[1]), item[0]))[0]
                remaining.pop(i)
                for page, rect in rectangles(group[offset:offset + len(label)]):
                    annotations.append((page, rect, url))
                offset += len(label)
        if remaining:
            raise ValueError("PDF hyperlink labels missing: " + repr([label for label, _ in remaining]))
        for page, rect, url in annotations:
            pdf[page].insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url})
        original_text = [page.get_text() for page in pdf]
        temporary = pdf_path.with_suffix(".linked.pdf")
        pdf.save(temporary, garbage=4, deflate=True)
    try:
        with fitz.open(temporary) as result:
            assert [page.get_text() for page in result] == original_text
            actual = {link.get("uri") for page in result for link in page.get_links()}
            assert {url for _, url in expected} <= actual
        temporary.replace(pdf_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return len(annotations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.pdfs or sorted((ROOT / "docs/submission").glob("[0-9][0-9]-*.pdf"))
    for path in paths:
        print(path.name, "link annotations added:", restore(path))


if __name__ == "__main__":
    main()
