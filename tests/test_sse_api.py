import unittest

from collector.sse_api import flatten_items


class SseApiTests(unittest.TestCase):
    def test_nested_announcement_items_are_flattened(self):
        payload = [[{"SECURITY_CODE": "603259", "URL": "/a.pdf", "TITLE": "A"}], {"ignored": True}]
        self.assertEqual([item["TITLE"] for item in flatten_items(payload)], ["A"])


if __name__ == "__main__":
    unittest.main()
