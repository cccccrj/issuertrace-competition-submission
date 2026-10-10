import unittest

from processor.repair_claims import find_span


class RepairClaimsTests(unittest.TestCase):
    def test_find_span_accepts_pdf_whitespace_and_returns_raw_text(self):
        text = "One sentence split\nacross a PDF line."
        self.assertEqual(find_span(text, "One sentence split across a PDF line."),
                         (0, len(text), text))

    def test_find_span_rejects_unsupported_claim(self):
        self.assertIsNone(find_span("source", "invented claim"))


if __name__ == "__main__":
    unittest.main()
