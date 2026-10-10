import json
import unittest
from pathlib import Path

from processor.benchmark import (
    E1_LAYERS, GOLD_DIR, TAXONOMY_MAP_PATH, load_taxonomy_map, mapped_codes,
    multilabel_scores, schema_candidate_baseline,
)
from processor.pipeline import EVENT_TYPES, ISSUE_RULES

COMPANY_TAXONOMY = GOLD_DIR / "事件类型_标的.json"
INDUSTRY_TAXONOMY = GOLD_DIR / "事件类型_行业.json"


def official_names(path, key):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {definition["event_name"]
            for entry in payload for definition in entry.get("event_schema", [])}


@unittest.skipUnless(COMPANY_TAXONOMY.exists(),
                     "gold taxonomy not extracted; run `benchmark extract`")
class TaxonomyMapTests(unittest.TestCase):
    """The map is the one subjective step in the benchmark, so it is tested.

    Every failure here is a report that would have been quietly wrong: a new
    official event name silently scoring as nothing, a typo'd code inventing a
    class, or an empty mapping with no stated reason.
    """

    @classmethod
    def setUpClass(cls):
        cls.taxonomy = load_taxonomy_map()
        cls.company = official_names(COMPANY_TAXONOMY, "company")
        cls.industry = official_names(INDUSTRY_TAXONOMY, "industry")

    def tables(self, layer):
        company_key, industry_key = E1_LAYERS[layer]
        return self.taxonomy[company_key], self.taxonomy[industry_key]

    def test_every_official_event_name_has_a_mapping(self):
        for layer in E1_LAYERS:
            company, industry = self.tables(layer)
            self.assertEqual(self.company - set(company), set(), layer)
            self.assertEqual(self.industry - set(industry), set(), layer)

    def test_the_map_invents_no_event_name(self):
        for layer in E1_LAYERS:
            company, industry = self.tables(layer)
            self.assertEqual(set(company) - self.company, set(), layer)
            self.assertEqual(set(industry) - self.industry, set(), layer)

    def test_issue_codes_are_real_issue_codes(self):
        company, industry = self.tables("issue_codes")
        for table in (company, industry):
            for name, codes in table.items():
                for code in codes:
                    self.assertIn(code, ISSUE_RULES, f"{name} -> {code}")

    def test_event_types_are_real_event_types(self):
        company, industry = self.tables("event_types")
        for table in (company, industry):
            for name, codes in table.items():
                for code in codes:
                    self.assertIn(code, EVENT_TYPES, f"{name} -> {code}")

    def test_every_empty_mapping_states_why(self):
        rationale = self.taxonomy["unmappable_rationale"]
        company, industry = self.tables("issue_codes")
        for table in (company, industry):
            for name, codes in table.items():
                if not codes:
                    self.assertIn(name, rationale, name)
                    self.assertTrue(rationale[name].strip(), name)

    def test_no_rationale_is_left_behind_by_a_later_mapping(self):
        # If a name gains a code, its rationale must go, or the file starts
        # explaining a gap that no longer exists.
        company, industry = self.tables("issue_codes")
        mapped = {**company, **industry}
        for name in self.taxonomy["unmappable_rationale"]:
            self.assertIn(name, mapped, name)
            self.assertEqual(mapped[name], [], name)

    def test_the_map_records_the_taxonomy_files_it_was_written_against(self):
        import hashlib
        for name, expected in self.taxonomy["dataset_sha256"].items():
            actual = hashlib.sha256((GOLD_DIR / name).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, name)

    def test_the_version_label_travels_with_the_file_hash(self):
        label = self.taxonomy["_version_label"]
        self.assertTrue(label.startswith(f"v{self.taxonomy['version']}+"), label)
        self.assertIn(self.taxonomy["_file_sha256"][:8], label)

    def test_the_frozen_map_still_hashes_to_what_reports_cite(self):
        # Not pinned to a literal: the point is that the label changes when the
        # file does, so a report's map_version cannot silently go stale.
        first = load_taxonomy_map()["_version_label"]
        second = load_taxonomy_map(TAXONOMY_MAP_PATH)["_version_label"]
        self.assertEqual(first, second)


class MappedCodeTests(unittest.TestCase):
    TAXONOMY = {
        "event_name_to_issue_codes": {"产品技术创新": ["TECH_LEVEL"], "公司战略调整": []},
        "industry_event_name_to_issue_codes": {"行业政策": []},
        "event_name_to_event_types": {"产品技术创新": ["PRODUCT_TECH_IP"]},
        "industry_event_name_to_event_types": {"行业政策": ["REGULATORY_COMPLIANCE"]},
    }

    def test_a_company_row_reads_the_company_table(self):
        self.assertEqual(
            mapped_codes(self.TAXONOMY, "issue_codes", "个股新闻", "产品技术创新"),
            ["TECH_LEVEL"])

    def test_an_industry_row_reads_the_industry_table(self):
        self.assertEqual(
            mapped_codes(self.TAXONOMY, "event_types", "行业新闻", "行业政策"),
            ["REGULATORY_COMPLIANCE"])

    def test_an_unmappable_name_returns_empty_not_none(self):
        # Empty means "this vocabulary has no such class"; None means the map is
        # broken.  The two are scored very differently and must not be confused.
        self.assertEqual(
            mapped_codes(self.TAXONOMY, "issue_codes", "个股新闻", "公司战略调整"), [])

    def test_an_unknown_name_returns_none(self):
        self.assertIsNone(
            mapped_codes(self.TAXONOMY, "issue_codes", "个股新闻", "查无此事件"))


class MultilabelScoreTests(unittest.TestCase):
    def test_a_perfect_prediction_scores_one(self):
        scores = multilabel_scores([({"A", "B"}, {"A", "B"}), ({"C"}, {"C"})])
        self.assertEqual(scores["micro_f1"], 1.0)
        self.assertEqual(scores["macro_f1"], 1.0)
        self.assertEqual(scores["exact_set_match"], 1.0)

    def test_over_emitting_costs_precision_not_recall(self):
        scores = multilabel_scores([({"A", "B", "C"}, {"A"})])
        self.assertEqual(scores["micro_recall"], 1.0)
        self.assertEqual(scores["micro_precision"], round(1 / 3, 4))
        self.assertEqual(scores["any_overlap"], 1.0)
        self.assertEqual(scores["exact_set_match"], 0.0)

    def test_a_class_with_no_gold_support_is_left_out_of_the_macro_average(self):
        scores = multilabel_scores([({"A", "Z"}, {"A"})])
        self.assertEqual(scores["per_class"]["Z"]["support"], 0)
        self.assertEqual(scores["macro_f1"], scores["per_class"]["A"]["f1"])

    def test_micro_and_macro_diverge_on_a_skewed_vocabulary(self):
        # Nine easy majority rows and one rare class we always miss: micro looks
        # fine, macro says the rare class does not work.  That divergence is the
        # reason §11.2 asks for both.
        pairs = [({"A"}, {"A"})] * 9 + [(set(), {"B"})]
        scores = multilabel_scores(pairs)
        self.assertGreater(scores["micro_f1"], 0.9)
        self.assertLess(scores["macro_f1"], 0.6)

    def test_empty_input_does_not_divide_by_zero(self):
        scores = multilabel_scores([])
        self.assertEqual(scores["micro_f1"], 0.0)
        self.assertIsNone(scores["macro_f1"])
        self.assertIsNone(scores["exact_set_match"])


if __name__ == "__main__":
    unittest.main()
