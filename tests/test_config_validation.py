import copy
import unittest
from pathlib import Path

import yaml

from collector.config_validation import ConfigError, validate_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))

    def test_repository_source_inventory_is_valid(self):
        validate_config(self.config)

    def test_duplicate_source_ids_are_rejected(self):
        broken = copy.deepcopy(self.config)
        broken["sources"].append(copy.deepcopy(broken["sources"][0]))
        with self.assertRaisesRegex(ConfigError, "duplicate source_id"):
            validate_config(broken)

    def test_unknown_company_scope_is_rejected(self):
        broken = copy.deepcopy(self.config)
        broken["sources"][0]["company_scope"] = ["not_a_company"]
        with self.assertRaisesRegex(ConfigError, "unknown company_scope"):
            validate_config(broken)

    def test_invalid_timezone_is_rejected(self):
        broken = copy.deepcopy(self.config)
        broken["sources"][0]["original_timezone"] = "Mars/Olympus_Mons"
        with self.assertRaisesRegex(ConfigError, "invalid original_timezone"):
            validate_config(broken)

    def test_first_party_entity_requires_a_single_company_crawl_source(self):
        broken = copy.deepcopy(self.config)
        source = broken["sources"][0]
        source["company_scope"] = ["cxmt", "wuxi_apptec"]
        with self.assertRaisesRegex(ConfigError, "first_party_entity requires exactly one"):
            validate_config(broken)

    def test_attachment_filename_date_pattern_requires_named_group(self):
        broken = copy.deepcopy(self.config)
        broken["sources"][0]["attachment_filename_date_pattern"] = r"[0-9]{8}"
        with self.assertRaisesRegex(ConfigError, "requires named date group"):
            validate_config(broken)

    def test_completion_gate_requires_known_company_and_positive_target(self):
        broken = copy.deepcopy(self.config)
        gate = broken["collection_defaults"]["scheduling"]["completion_gate"]
        gate["company_id"] = "unknown"
        gate["minimum_valid_documents"] = 0
        with self.assertRaisesRegex(ConfigError, "completion_gate.company_id"):
            validate_config(broken)

    def test_adapter_batch_size_has_conservative_hard_cap(self):
        broken = copy.deepcopy(self.config)
        broken["collection_defaults"]["scheduling"]["adapter_jobs_per_source_per_cycle"] = 7
        with self.assertRaisesRegex(ConfigError, "between 1 and 6"):
            validate_config(broken)

    def test_detail_drain_switch_must_be_boolean(self):
        broken = copy.deepcopy(self.config)
        broken["collection_defaults"]["scheduling"]["drain_discovered_details_first"] = "yes"
        with self.assertRaisesRegex(ConfigError, "must be boolean"):
            validate_config(broken)


if __name__ == "__main__":
    unittest.main()
