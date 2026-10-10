import unittest

from collector.sse_ipo_api import compact_date, document_type, parse_jsonp


class SseIpoApiTests(unittest.TestCase):
    def test_jsonp_and_file_metadata_helpers(self):
        self.assertEqual(parse_jsonp('cb({"result":[]})')["result"], [])
        self.assertEqual(compact_date("20260527224004"), "2026-05-27")
        self.assertEqual(document_type({"fileTypeMap": "I0013"}), "prospectus")
        self.assertEqual(document_type({"fileTypeMap": "I3010"}), "review_inquiry_or_response")


if __name__ == "__main__":
    unittest.main()
