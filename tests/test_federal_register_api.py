import unittest

from collector.federal_register_api import result_metadata


class FederalRegisterApiTests(unittest.TestCase):
    def test_result_metadata_normalizes_document_type(self):
        item = {"title": "A notice", "publication_date": "2025-01-07", "type": "Proposed Rule"}
        self.assertEqual(result_metadata(item), ("A notice", "2025-01-07", "proposed_rule"))


if __name__ == "__main__":
    unittest.main()
