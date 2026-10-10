import unittest

from processor.normalize import (
    compare_quantities, extract_quantities, load_organizations, normalize_amount, numeric_check, normalize_ratio, normalize_time,
    resolve_organization, resolve_subject, resolve_subject_with_basis,
)


ENTITIES = {
    "cxmt": {
        "canonical_name": "长鑫科技集团股份有限公司",
        "include_aliases": ["长鑫科技集团股份有限公司", "长鑫科技", "长鑫存储", "CXMT",
                            "ChangXin Memory Technologies"],
        "stock_codes": [{"exchange": "SSE STAR", "code": "688825"}],
        "exclude_or_review": [{"term": "长江存储", "reason": "独立主体"},
                              {"term": "长鑫", "reason": "简称需上下文"}],
    },
    "wuxi_apptec": {
        "canonical_name": "无锡药明康德新药开发股份有限公司",
        "include_aliases": ["药明康德", "WuXi AppTec"],
        "stock_codes": [{"exchange": "SSE", "code": "603259"}],
        "exclude_or_review": [{"term": "药明生物", "reason": "独立上市主体"}],
    },
}


class TimeNormalizationTests(unittest.TestCase):
    def test_explicit_date_resolves_to_the_day(self):
        result = normalize_time("2026年7月27日")
        self.assertEqual((result["value"], result["precision"], result["basis"]),
                         ("2026-07-27", "day", "explicit"))

    def test_iso_and_slash_formats_are_accepted(self):
        for raw in ("2026-07-27", "2026/7/27"):
            self.assertEqual(normalize_time(raw)["value"], "2026-07-27", raw)

    def test_month_becomes_an_interval(self):
        result = normalize_time("2026年2月")
        self.assertEqual((result["value"], result["precision"]), ("2026-02-01/2026-02-28", "month"))

    def test_half_year_and_quarter_become_intervals(self):
        self.assertEqual(normalize_time("2026年上半年")["value"], "2026-01-01/2026-06-30")
        self.assertEqual(normalize_time("2025年下半年")["value"], "2025-07-01/2025-12-31")
        self.assertEqual(normalize_time("2026年第三季度")["value"], "2026-07-01/2026-09-30")

    def test_relative_expressions_use_the_publication_date_and_say_so(self):
        result = normalize_time("近日", "2026-07-15")
        self.assertEqual((result["value"], result["precision"], result["basis"]),
                         ("2026-07-01/2026-07-31", "month", "relative_to_publication"))
        self.assertEqual(normalize_time("去年", "2026-07-15")["value"], "2025-01-01/2025-12-31")
        self.assertEqual(normalize_time("昨日", "2026-07-15")["value"], "2026-07-14")

    def test_bare_month_day_borrows_the_publication_year(self):
        self.assertEqual(normalize_time("7月27日", "2026-08-02")["value"], "2026-07-27")

    def test_unresolvable_mentions_return_none_rather_than_a_guess(self):
        for raw in ("", "未来某个时候", "近日"):  # 近日 without a publication date
            self.assertIsNone(normalize_time(raw), raw)

    def test_impossible_date_is_rejected(self):
        self.assertIsNone(normalize_time("2026年2月30日"))

    def test_leap_year_february_end_is_correct(self):
        self.assertEqual(normalize_time("2024年2月")["value"], "2024-02-01/2024-02-29")

    def test_chinese_dates_survive_pdf_inserted_whitespace(self):
        # PDF text extraction splits 2025年12月31日 across the unit characters.
        self.assertEqual(normalize_time("2025 年12 月31 日")["value"], "2025-12-31")


class EnglishTimeNormalizationTests(unittest.TestCase):
    """The HK filings arrive in English, and must land on the same scale."""

    def test_day_first_and_month_first_forms_resolve(self):
        for raw in ("26 June 2025", "June 26, 2025", "June 26 2025", "26th June 2025"):
            self.assertEqual(normalize_time(raw)["value"], "2025-06-26", raw)

    def test_weekday_clock_time_and_parenthetical_are_ignored(self):
        for raw in ("Tuesday, April 28, 2026", "April 28, 2026 at 2:00 p.m.",
                    "As at April 28, 2026", "April 28, 2026 (before trading hours)"):
            self.assertEqual(normalize_time(raw)["value"], "2026-04-28", raw)

    def test_month_and_year_stays_a_month_not_a_day(self):
        # "May 2025" must not be read as day 20 of May by consuming the year.
        result = normalize_time("May 2025")
        self.assertEqual((result["value"], result["precision"]), ("2025-05-01/2025-05-31", "month"))

    def test_ranges_span_both_endpoints(self):
        self.assertEqual(normalize_time("from 26 June 2025 to 28 July 2025")["value"],
                         "2025-06-26/2025-07-28")
        self.assertEqual(normalize_time("1 March 2024 to 31 March 2024")["value"],
                         "2024-03-01/2024-03-31")

    def test_range_left_endpoint_borrows_the_right_endpoint_year(self):
        self.assertEqual(normalize_time("1 June to 16 June 2026")["value"], "2026-06-01/2026-06-16")

    def test_quarters_and_halves_resolve_with_their_year(self):
        self.assertEqual(normalize_time("Q1 2026")["value"], "2026-01-01/2026-03-31")
        self.assertEqual(normalize_time("first quarter of 2024")["value"], "2024-01-01/2024-03-31")
        self.assertEqual(normalize_time("fourth quarter of 2023")["value"], "2023-10-01/2023-12-31")
        self.assertEqual(normalize_time("2026H1")["value"], "2026-01-01/2026-06-30")
        self.assertEqual(normalize_time("first half of 2024")["value"], "2024-01-01/2024-06-30")

    def test_reporting_window_ends_on_its_stated_date(self):
        # The trailing date closes the window; it is not the event's own day.
        self.assertEqual(normalize_time("six months ended June 30, 2025")["value"],
                         "2025-01-01/2025-06-30")
        self.assertEqual(normalize_time("first quarter ending March 31, 2024")["value"],
                         "2024-01-01/2024-03-31")
        self.assertEqual(normalize_time("the first three quarters of 2024")["value"],
                         "2024-01-01/2024-09-30")

    def test_bare_year_resolves_only_when_it_stands_alone(self):
        self.assertEqual(normalize_time("2025")["value"], "2025-01-01/2025-12-31")
        self.assertEqual(normalize_time("the full year of 2025")["value"], "2025-01-01/2025-12-31")

    def test_english_relatives_use_the_publication_date_and_say_so(self):
        result = normalize_time("recently", "2026-07-15")
        self.assertEqual((result["value"], result["basis"]),
                         ("2026-07-01/2026-07-31", "relative_to_publication"))
        self.assertEqual(normalize_time("today", "2026-07-15")["value"], "2026-07-15")
        self.assertEqual(normalize_time("last month", "2026-07-15")["value"], "2026-06-01/2026-06-30")
        self.assertEqual(normalize_time("two months ago", "2026-07-15")["value"], "2026-05-01/2026-05-31")
        self.assertEqual(normalize_time("last year", "2026-07-15")["value"], "2025-01-01/2025-12-31")

    def test_date_of_this_announcement_is_the_publication_day(self):
        for raw in ("as at the date of this announcement", "截至本报告披露日"):
            self.assertEqual(normalize_time(raw, "2026-07-15")["value"], "2026-07-15", raw)

    def test_year_less_english_date_needs_a_publication_date(self):
        self.assertEqual(normalize_time("December 10th", "2026-06-20")["value"], "2026-12-10")
        self.assertIsNone(normalize_time("December 10th"))

    def test_a_clock_time_alone_carries_no_date(self):
        # 截至11:00 states a cutoff hour, not a day — resolving it would invent one.
        for raw in ("截至11:00", "截至09:50"):
            self.assertIsNone(normalize_time(raw, "2026-07-15"), raw)

    def test_a_weekday_alone_is_not_resolved(self):
        for raw in ("Monday", "Late Monday"):
            self.assertIsNone(normalize_time(raw, "2026-07-15"), raw)

    def test_narrative_mentions_without_a_date_stay_unresolved(self):
        for raw in ("unknown", "报告期各期", "上市首日", "公司成立25周年之际"):
            self.assertIsNone(normalize_time(raw, "2026-07-15"), raw)


class QuantityTests(unittest.TestCase):
    """I-02: 708.15亿股 was once converted to 7,081,500,000 股 by the adjudicator."""

    def _one(self, text):
        found = extract_quantities(text)
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_scales_are_exact_powers_of_ten(self):
        self.assertEqual(self._one("1万元")["value"], 10_000)
        self.assertEqual(self._one("1亿元")["value"], 100_000_000)
        self.assertEqual(self._one("1.2万亿元")["value"], 1_200_000_000_000)

    def test_the_i02_figures_convert_exactly(self):
        self.assertEqual(self._one("708.15亿股")["value"], 70_815_000_000)
        self.assertEqual(self._one("7,081,505.7468 万股")["value"], 70_815_057_468)

    def test_decimal_arithmetic_has_no_float_residue(self):
        # As floats, 0.1亿 + 0.2亿 would not equal 0.3亿 exactly.
        values = [self._one(text)["value"] for text in ("0.1亿元", "0.2亿元", "0.3亿元")]
        self.assertEqual(values[0] + values[1], values[2])

    def test_thousands_separators_are_read_as_one_number(self):
        self.assertEqual(self._one("294,040股")["value"], 294_040)

    def test_shares_are_not_money(self):
        self.assertEqual(self._one("708.15亿股")["unit"], "股")
        self.assertEqual(self._one("人民币67.8亿元")["unit"], "元")

    def test_bare_numbers_are_not_quantities(self):
        self.assertEqual(extract_quantities("2025年第3页共12人"), [])

    def test_precision_follows_the_digits_written(self):
        self.assertEqual(self._one("708.15亿股")["step"], 10**6)
        self.assertEqual(self._one("10亿元")["step"], 10**8)

    def test_the_i02_pair_agrees_within_the_stated_precision(self):
        check = numeric_check("发行后总股本不超708.15亿股", "发行后总股本不超过7,081,505.7468 万股")
        self.assertEqual(check["status"], "agree")
        figure = check["figures"][0]
        self.assertEqual(figure["claim_value"], "70,815,000,000股")
        self.assertEqual(figure["evidence_value"], "70,815,057,468股")
        self.assertEqual(figure["difference"], "57,468股")
        self.assertEqual(figure["relative"], "0.000081%")

    def test_a_power_of_ten_apart_is_flagged_as_a_unit_slip(self):
        result = compare_quantities(self._one("70.815亿股"), self._one("7,081,505.7468万股"))
        self.assertEqual(result["status"], "slip")

    def test_a_different_figure_is_unmatched_not_called_a_discrepancy(self):
        # The nearest number in the excerpt is often another quantity altogether.
        check = numeric_check("募资500亿元", "募集资金295亿元")
        self.assertEqual(check["status"], "none")
        self.assertEqual(check["unmatched"], ["500亿元"])

    def test_pre_and_post_offering_share_capital_are_not_paired(self):
        # The I-02 claim itself: 发行前 6,019,279.75万股 has no counterpart in a
        # paragraph about the post-offering total, and 3,000万元 is not the par value.
        check = numeric_check("发行前总股本为6,019,279.75万股，股本总额不低于人民币3,000万元，每股面值为人民币1元",
                              "每股面值人民币1.00 元。发行后总股本不超过7,081,505.7468 万股")
        self.assertEqual(check["status"], "partial")
        self.assertEqual(check["unmatched"], ["6,019,279.75万股", "3,000万元"])

    def test_round_numbers_a_power_of_ten_apart_are_not_called_slips(self):
        # 50% and 5% are two figures far more often than one misplaced unit.
        self.assertEqual(numeric_check("占比50%", "占比5%")["status"], "none")
        self.assertEqual(numeric_check("募资300亿元", "募资30亿元")["status"], "none")

    def test_a_unit_slip_is_reported_as_such(self):
        self.assertEqual(numeric_check("总股本70.815亿股", "总股本7,081,505.7468万股")["status"], "slip")

    def test_a_figure_the_filing_never_states_is_unmatched_not_agreed(self):
        check = numeric_check("募资295亿元，发行10亿股", "募集资金295亿元")
        self.assertEqual(check["status"], "partial")
        self.assertEqual(check["unmatched"], ["10亿股"])

    def test_nothing_to_compare(self):
        self.assertEqual(numeric_check("公司技术领先", "募集资金295亿元")["status"], "none")


class AmountNormalizationTests(unittest.TestCase):
    def test_chinese_scales_convert_to_base_units(self):
        self.assertEqual(normalize_amount("295亿元")["value_normalized"], 295 * 10**8)
        self.assertEqual(normalize_amount("3019.09万元")["value_normalized"], 3019.09 * 10**4)

    def test_currency_is_detected_not_assumed_when_stated(self):
        usd = normalize_amount("20万美元")
        self.assertEqual((usd["value_normalized"], usd["currency"]), (200000.0, "USD"))
        self.assertFalse(usd["currency_assumed"])

    def test_yuan_is_not_mistaken_for_dollars(self):
        self.assertEqual(normalize_amount("100亿元")["currency"], "CNY")

    def test_english_scales_are_supported(self):
        self.assertEqual(normalize_amount("$1.2 billion")["value_normalized"], 1.2 * 10**9)

    def test_thousands_separators_are_handled(self):
        self.assertEqual(normalize_amount("1,062,225万股")["value_normalized"], 1062225 * 10**4)

    def test_missing_currency_is_flagged_as_assumed(self):
        self.assertTrue(normalize_amount("50亿")["currency_assumed"])

    def test_text_without_a_number_is_unresolvable(self):
        self.assertIsNone(normalize_amount("大量资金"))

    def test_a_ratio_is_never_read_as_money(self):
        # "约31%" must not become 31 元 just because it contains a number.
        for raw in ("约31%", "3个百分点", "百分之十"):
            self.assertIsNone(normalize_amount(raw), raw)

    def test_values_are_rounded_to_the_fen(self):
        self.assertEqual(normalize_amount("约313.9亿人民币")["value_normalized"], 31390000000.0)


class RatioNormalizationTests(unittest.TestCase):
    def test_percent_and_percentage_point_are_kept_apart(self):
        self.assertEqual(normalize_ratio("20.5%")["ratio_type"], "percent")
        self.assertEqual(normalize_ratio("3个百分点")["ratio_type"], "percentage_point")
        self.assertEqual(normalize_ratio("增加6.77个百分点")["value"], 6.77)

    def test_english_percentage_points_are_recognised(self):
        self.assertEqual(normalize_ratio("3 percentage points")["ratio_type"], "percentage_point")

    def test_negative_ratios_keep_their_sign(self):
        self.assertEqual(normalize_ratio("-4.5%")["value"], -4.5)

    def test_bare_number_without_a_ratio_marker_is_unresolvable(self):
        self.assertIsNone(normalize_ratio("12"))


class SubjectResolutionTests(unittest.TestCase):
    def test_aliases_and_canonical_names_link_to_the_company(self):
        for name in ("长鑫科技", "CXMT", "长鑫科技集团股份有限公司"):
            self.assertEqual(resolve_subject(name, ENTITIES), "cxmt", name)
        self.assertEqual(resolve_subject("药明康德", ENTITIES), "wuxi_apptec")

    def test_stock_code_links_to_the_company(self):
        self.assertEqual(resolve_subject("长鑫科技(688825)", ENTITIES), "cxmt")

    def test_excluded_names_are_not_merged_into_a_monitored_company(self):
        # 长江存储 and 药明生物 are separate issuers; merging them would corrupt
        # both companies' event ledgers.
        self.assertIsNone(resolve_subject("长江存储", ENTITIES))
        self.assertIsNone(resolve_subject("药明生物", ENTITIES))

    def test_a_traditional_mention_is_the_same_company_as_its_simplified_name(self):
        # WuXi AppTec files in Hong Kong in traditional characters; comparing
        # scripts literally left the case company itself unresolved 37 times.
        self.assertEqual(resolve_subject("藥明康德", ENTITIES), "wuxi_apptec")
        self.assertEqual(resolve_subject("無錫藥明康德新藥開發股份有限公司", ENTITIES),
                         "wuxi_apptec")

    def test_an_exclusion_holds_in_both_scripts(self):
        # Otherwise folding would open a hole the exclusion list was closing.
        self.assertIsNone(resolve_subject("長江存儲", ENTITIES))
        self.assertIsNone(resolve_subject("藥明生物", ENTITIES))

    def test_bare_ambiguous_short_alias_is_not_resolved(self):
        self.assertIsNone(resolve_subject("长鑫", ENTITIES))

    def test_unknown_subject_returns_none(self):
        self.assertIsNone(resolve_subject("某科技公司", ENTITIES))
        self.assertIsNone(resolve_subject("", ENTITIES))


ORGANIZATIONS = {
    "cicc": {"canonical_name": "中国国际金融股份有限公司",
             "aliases": ["中金公司", "中金", "CICC"]},
    "sse": {"canonical_name": "上海证券交易所",
            "aliases": ["上交所", "Shanghai Stock Exchange"]},
    "wuxi_biologics": {"canonical_name": "药明生物技术有限公司",
                       "aliases": ["药明生物", "WuXi Biologics"]},
}


class SubjectShapeTests(unittest.TestCase):
    """The two mention shapes no alias table could ever have matched."""

    def test_the_company_id_itself_resolves(self):
        # `wuxi_apptec` was the single most common unresolved subject in the
        # corpus, 72 occurrences of the case company not recognising itself:
        # the labelling pass backfills the id when the model returns no
        # subject, and an id is not one of the company's aliases.
        self.assertEqual(resolve_subject_with_basis("wuxi_apptec", ENTITIES),
                         ("wuxi_apptec", "company_id_literal"))

    def test_anaphora_resolves_to_the_document_own_company(self):
        for mention in ("the Company", "本公司", "发行人", "the Group"):
            self.assertEqual(resolve_subject_with_basis(mention, ENTITIES, "cxmt"),
                             ("cxmt", "document_company"), mention)

    def test_anaphora_without_a_document_company_stays_unresolved(self):
        # A pointer with nothing to point at is not a subject.
        self.assertEqual(resolve_subject_with_basis("the Company", ENTITIES), (None, None))

    def test_a_board_is_not_its_company(self):
        # An organ of a company is not the company.  Resolving it would be an
        # invented equivalence, so it stays unresolved on purpose.
        self.assertEqual(resolve_subject_with_basis("the Board", ENTITIES, "cxmt"), (None, None))
        self.assertEqual(resolve_subject_with_basis("董事会", ENTITIES, "cxmt"), (None, None))

    def test_an_ordinary_alias_still_reports_its_basis(self):
        self.assertEqual(resolve_subject_with_basis("药明康德", ENTITIES), ("wuxi_apptec", "alias"))

    def test_an_exclusion_survives_the_new_paths(self):
        # The whole point of the exclusion list is that 药明生物 is a separate
        # issuer.  Neither new shape may open a way around it.
        self.assertEqual(resolve_subject_with_basis("药明生物", ENTITIES, "wuxi_apptec"),
                         (None, None))


class OrganizationResolutionTests(unittest.TestCase):
    def test_canonical_names_and_aliases_both_link(self):
        for name in ("中国国际金融股份有限公司", "中金公司", "CICC", "中金"):
            self.assertEqual(resolve_organization(name, ORGANIZATIONS),
                             ("cicc", "organization_registry"), name)

    def test_a_branch_office_is_its_firm(self):
        # Four spellings of one firm sat in the corpus as four separate rows.
        # Enumerating branch addresses is a losing game; the prefix is not.
        org, basis = resolve_organization(
            "中国国际金融股份有限公司北京建国门外大街证券营业部", ORGANIZATIONS)
        self.assertEqual((org, basis), ("cicc", "organization_branch"))

    def test_a_short_alias_does_not_match_by_prefix(self):
        # 中金 is only four characters; a prefix rule that accepted it would
        # claim any name starting with those characters.
        self.assertEqual(resolve_organization("中金岭南有色金属股份有限公司", ORGANIZATIONS),
                         (None, None))

    def test_matching_folds_scripts_like_the_company_path_does(self):
        self.assertEqual(resolve_organization("上海證券交易所", ORGANIZATIONS)[0], "sse")

    def test_an_excluded_issuer_gets_a_name_without_getting_a_company(self):
        # 药明生物 must not become 药明康德 -- and must not stay nameless either.
        self.assertIsNone(resolve_subject("药明生物", ENTITIES))
        self.assertEqual(resolve_organization("药明生物", ORGANIZATIONS)[0], "wuxi_biologics")

    def test_an_unknown_organisation_returns_nothing(self):
        self.assertEqual(resolve_organization("某某咨询有限公司", ORGANIZATIONS), (None, None))
        self.assertEqual(resolve_organization("", ORGANIZATIONS), (None, None))


class OrganizationRegistryFileTests(unittest.TestCase):
    """The shipped file, not a fixture."""

    def setUp(self):
        self.registry = load_organizations()

    def test_the_registry_loads_and_is_not_empty(self):
        self.assertGreater(len(self.registry), 20)

    def test_every_entry_has_a_canonical_name_and_a_kind(self):
        for org_id, record in self.registry.items():
            self.assertTrue(record.get("canonical_name"), org_id)
            self.assertTrue(record.get("kind"), org_id)

    def test_no_name_form_is_claimed_by_two_organisations(self):
        # A duplicated alias makes resolution depend on dict order, which is
        # the kind of thing that changes silently and is never noticed.
        seen: dict[str, str] = {}
        for org_id, record in self.registry.items():
            forms = [record["canonical_name"], *(record.get("aliases") or [])]
            for form in forms:
                key = form.strip().lower()
                self.assertNotIn(key, seen, f"{form} claimed by {seen.get(key)} and {org_id}")
                seen[key] = org_id

    def test_no_monitored_company_is_also_registered_as_an_organisation(self):
        # The two namespaces exist to stay apart.  A company appearing in both
        # would resolve differently depending on which lookup ran first.
        from processor.normalize import load_entities
        for record in self.registry.values():
            for form in [record["canonical_name"], *(record.get("aliases") or [])]:
                self.assertIsNone(resolve_subject(form, load_entities()), form)


if __name__ == "__main__":
    unittest.main()
