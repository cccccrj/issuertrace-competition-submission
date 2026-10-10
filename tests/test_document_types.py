import inspect
import unittest

from collector.document_types import (ALIASES, AUDIT_TYPES, EVIDENCE_TYPES, KNOWN_TYPES,
                                      canonical, is_citable)
from processor.claim_index import EVIDENCE_DOCUMENT_TYPES
from processor.disclosure_diff import run as diff_run


class VocabularyTests(unittest.TestCase):
    def test_the_evidence_set_matches_what_the_index_selects(self):
        # If these drift, documents are collected and then quietly never cited.
        self.assertEqual(EVIDENCE_TYPES, frozenset(EVIDENCE_DOCUMENT_TYPES))

    def test_the_version_comparison_default_is_an_evidence_type(self):
        default = inspect.signature(diff_run).parameters["document_type"].default
        self.assertIn(default, EVIDENCE_TYPES)

    def test_every_alias_resolves_to_a_known_type(self):
        for alias, target in ALIASES.items():
            self.assertIn(target, KNOWN_TYPES, f"{alias} maps outside the vocabulary")
            self.assertNotIn(alias, KNOWN_TYPES, f"{alias} is both an alias and a name")

    def test_the_singular_forms_that_indexed_nothing_are_mapped(self):
        # The three that actually cost a collection round.
        self.assertEqual(canonical("announcement"), "announcements")
        self.assertEqual(canonical("review_inquiry_or_response"), "review_responses")
        self.assertEqual(canonical("listing_document"), "listing_documents")

    def test_an_unknown_type_is_returned_unchanged_rather_than_guessed(self):
        self.assertEqual(canonical("something_new"), "something_new")
        self.assertIsNone(canonical(None))

    def test_audit_trail_is_never_citable(self):
        for kind in AUDIT_TYPES:
            self.assertFalse(is_citable(kind))

    def test_an_announcement_is_citable_however_it_was_spelt(self):
        self.assertTrue(is_citable("announcements"))
        self.assertTrue(is_citable("announcement"))


class CollectorVocabularyTests(unittest.TestCase):
    def test_the_exchange_review_adapter_types_map_into_the_vocabulary(self):
        from collector.sse_ipo_api import document_type
        cases = [{"fileTypeMap": "I0011"}, {"fileTypeMap": "I3010"}, {"fileTypeMap": "I3020"},
                 {"fileTypeMap": "I2010"}, {"fileTypeMap": "I1010"}, {"fileTypeMap": "I1020"},
                 {"fileTypeMap": "other"}, {"fileType": 30}]
        for item in cases:
            resolved = canonical(document_type(item))
            self.assertIn(resolved, KNOWN_TYPES,
                          f"{item} produces {resolved!r}, which no processor selects")

    def test_the_cninfo_adapter_writes_citable_and_audit_types(self):
        from collector.cninfo_api import DOCUMENT_TYPE, QUERY_DOCUMENT_TYPE
        self.assertTrue(is_citable(DOCUMENT_TYPE))
        self.assertFalse(is_citable(QUERY_DOCUMENT_TYPE))


if __name__ == "__main__":
    unittest.main()
