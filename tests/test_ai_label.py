import sqlite3
import json
import json
import threading
import time
import unittest
from unittest import mock

import processor.ai_label as ai_label
from processor.ai_label import (call_model, candidate_rows, dispatch_windows, locate_claims,
                                locate_loosely, merge_labels, provider, refuses_strict_mode,
                                request_arguments, schema)


VALID_LABEL = {
    "eventhood": "NO_EVENT", "relevance_score": 0.5, "event_types": [], "issue_codes": [],
    "production_mode": "BRIEF", "affected_objects": [], "impact_direction": "neutral",
    "impact_magnitude": "minor", "article_sentiment": "neutral", "summary": "无事件",
    "claims": [], "extraction_confidence": 0.8, "event_time": "", "event_time_precision": "unknown",
    "event_subjects": [], "amounts": [], "ratios": [], "persons": [], "organizations": [],
    "products": [],
}


class FakeCompletions:
    """Return one queued outcome per call; exceptions are raised, dicts returned."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        message = mock.Mock()
        message.content = outcome
        choice = mock.Mock()
        choice.message = message
        response = mock.Mock()
        response.choices = [choice]
        return response


class FakeClient:
    def __init__(self, outcomes):
        self.chat = mock.Mock()
        self.chat.completions = FakeCompletions(outcomes)


class StructuredOutputTests(unittest.TestCase):
    def setUp(self):
        ai_label._STRICT_UNSUPPORTED.clear()
        self.addCleanup(ai_label._STRICT_UNSUPPORTED.clear)
        # Retry backoff is real time; the tests only care about the ladder order.
        sleep_patch = mock.patch("processor.ai_label.time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_json_schema_mode_is_preferred(self):
        client = FakeClient([json.dumps(VALID_LABEL)])
        payload, mode = call_model(client, "qwen-plus", "prompt")
        self.assertEqual(mode, "json_schema")
        self.assertEqual(payload["eventhood"], "NO_EVENT")
        self.assertEqual(client.chat.completions.calls[0]["response_format"]["type"], "json_schema")

    def test_falls_back_to_json_object_when_gateway_rejects_strict_schema(self):
        client = FakeClient([RuntimeError("response_format not supported")] * 3 + [json.dumps(VALID_LABEL)])
        payload, mode = call_model(client, "glm-4-flash", "prompt")
        self.assertEqual(mode, "json_object")
        self.assertEqual(payload["eventhood"], "NO_EVENT")
        # The schema travels in the system prompt when the gateway cannot enforce it.
        fallback_call = client.chat.completions.calls[-1]
        self.assertEqual(fallback_call["response_format"], {"type": "json_object"})
        self.assertIn("JSON Schema", fallback_call["messages"][0]["content"])

    def test_unsupported_strict_model_is_remembered(self):
        # Remembered only on the gateway naming the parameter it will not honour.
        refusal = RuntimeError("response_format json_schema is not supported")
        first = FakeClient([refusal] * 3 + [json.dumps(VALID_LABEL)])
        call_model(first, "glm-4-flash", "prompt")
        second = FakeClient([json.dumps(VALID_LABEL)])
        _, mode = call_model(second, "glm-4-flash", "prompt")
        self.assertEqual(mode, "json_object")
        self.assertEqual(len(second.chat.completions.calls), 1, "strict mode should not be retried")

    def test_a_transient_failure_does_not_cost_the_corpus_strict_mode(self):
        # One dropped connection used to demote every later document to
        # json_object, where the schema is advisory: a 958-document run then
        # returned out-of-range numbers and arrays, and 200 documents failed.
        flaky = FakeClient([RuntimeError("Connection reset by peer")] * 3
                           + [json.dumps(VALID_LABEL)])
        call_model(flaky, "deepseek-v3.2", "prompt")
        healthy = FakeClient([json.dumps(VALID_LABEL)])
        _, mode = call_model(healthy, "deepseek-v3.2", "prompt")
        self.assertEqual(mode, "json_schema")

    def test_schema_violation_triggers_one_guided_repair_retry(self):
        invalid = json.dumps({**VALID_LABEL, "eventhood": "MAYBE"})
        client = FakeClient([invalid, json.dumps(VALID_LABEL)])
        payload, _ = call_model(client, "qwen-plus", "prompt")
        self.assertEqual(payload["eventhood"], "NO_EVENT")
        repair_messages = client.chat.completions.calls[1]["messages"]
        self.assertIn("不符合 Schema", repair_messages[-1]["content"])

    def test_all_modes_failing_raises(self):
        client = FakeClient([RuntimeError("boom")] * 6)
        with self.assertRaises(RuntimeError):
            call_model(client, "qwen-plus", "prompt")

    def test_repair_message_is_absent_on_first_attempt(self):
        arguments = request_arguments("qwen-plus", "prompt", "json_schema")
        self.assertEqual(len(arguments["messages"]), 2)


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_a_single_window_at_offset_zero(self):
        self.assertEqual(ai_label.chunk_spans("短文", "independent_news"), [(0, "短文")])

    def test_long_pdf_splits_on_page_markers_and_offsets_are_absolute(self):
        pages = "".join(f"[PAGE {n}]\n" + "内容。" * 4000 for n in range(1, 6))
        spans = ai_label.chunk_spans(pages, "prospectus")
        self.assertGreater(len(spans), 1)
        for offset, window in spans:
            self.assertEqual(pages[offset:offset + len(window)], window)
            self.assertTrue(window.startswith("[PAGE "))

    def test_ordinary_documents_are_capped_but_disclosure_is_read_further(self):
        pages = "".join(f"[PAGE {n}]\n" + "内容。" * 4000 for n in range(1, 12))
        self.assertLessEqual(len(ai_label.chunk_spans(pages, "independent_news")),
                             ai_label.DEFAULT_CHUNK_LIMIT)
        self.assertGreater(len(ai_label.chunk_spans(pages, "prospectus")),
                           ai_label.DEFAULT_CHUNK_LIMIT)

    def test_text_without_page_markers_still_splits(self):
        body = "\n".join("这是一个段落。" * 20 for _ in range(400))
        spans = ai_label.chunk_spans(body, "announcements")
        self.assertGreater(len(spans), 1)
        self.assertEqual(spans[0][0], 0)

    def test_claim_offsets_from_a_later_window_index_the_full_document(self):
        text = "[PAGE 1]\n" + "甲" * 12000 + "\n[PAGE 2]\n公司完成注册。"
        window_offset = text.index("[PAGE 2]")
        window = text[window_offset:]
        label = {"eventhood": "EVENT", "extraction_confidence": 0.9,
                 "claims": [{"text": "公司完成注册", "issue_code": None, "claim_type": "fact"}]}
        parsed, _ = locate_claims(label, window, window_offset)
        claim = parsed["claims"][0]
        self.assertEqual(text[claim["source_start"]:claim["source_end"]], "公司完成注册")
        self.assertEqual(claim["cited_source"], "page:2")


class WindowDispatchTests(unittest.TestCase):
    """A document's windows are independent calls and are answered together."""

    def worker(self, record, gate=None):
        def label_window(numbered):
            index, (offset, window) = numbered
            with record["lock"]:
                record["in_flight"] += 1
                record["peak"] = max(record["peak"], record["in_flight"])
            time.sleep(0.02)
            with record["lock"]:
                record["in_flight"] -= 1
            return f"{index}:{window}"
        return label_window

    def state(self):
        return {"in_flight": 0, "peak": 0, "lock": threading.Lock()}

    def test_windows_come_back_in_the_order_they_were_cut(self):
        # Claim offsets are absolute positions; merging assumes window order.
        record = self.state()
        windows = [(0, "a"), (100, "b"), (200, "c")]
        self.assertEqual(dispatch_windows(windows, self.worker(record)),
                         ["1:a", "2:b", "3:c"])

    def test_several_windows_are_in_flight_at_once(self):
        record = self.state()
        dispatch_windows([(0, "a"), (100, "b"), (200, "c")], self.worker(record))
        self.assertGreater(record["peak"], 1)

    def test_a_single_window_needs_no_pool(self):
        record = self.state()
        self.assertEqual(dispatch_windows([(0, "only")], self.worker(record)), ["1:only"])
        self.assertEqual(record["peak"], 1)

    def test_parallelism_is_capped(self):
        # A long document must not turn into a burst the gateway rate-limits.
        record = self.state()
        dispatch_windows([(index * 100, str(index)) for index in range(12)],
                         self.worker(record), parallelism=3)
        self.assertLessEqual(record["peak"], 3)

    def test_no_windows_is_no_calls(self):
        record = self.state()
        self.assertEqual(dispatch_windows([], self.worker(record)), [])


class MergeLabelTests(unittest.TestCase):
    def _part(self, **overrides):
        return {**VALID_LABEL, **overrides}

    def test_single_window_is_returned_unchanged(self):
        part = self._part()
        self.assertIs(ai_label.merge_labels([part], 8), part)

    def test_event_in_any_window_makes_the_document_an_event(self):
        merged = ai_label.merge_labels(
            [self._part(eventhood="NO_EVENT"),
             self._part(eventhood="EVENT", summary="完成注册", impact_direction="positive")], 8)
        self.assertEqual(merged["eventhood"], "EVENT")
        self.assertEqual(merged["summary"], "完成注册")
        self.assertEqual(merged["impact_direction"], "positive")

    def test_list_fields_accumulate_without_duplicates(self):
        merged = ai_label.merge_labels(
            [self._part(issue_codes=["FINANCIAL"], organizations=["上交所"]),
             self._part(issue_codes=["FINANCIAL", "IP_PATENT"], organizations=["上交所", "证监会"])], 8)
        self.assertEqual(merged["issue_codes"], ["FINANCIAL", "IP_PATENT"])
        self.assertEqual(merged["organizations"], ["上交所", "证监会"])

    def test_claims_are_capped_at_the_document_limit(self):
        parts = [self._part(claims=[{"text": f"主张{i}{j}"} for j in range(5)]) for i in range(4)]
        self.assertEqual(len(ai_label.merge_labels(parts, 8)["claims"]), 8)

    def test_union_reimposes_the_schemas_ceiling(self):
        # union de-duplicates but never re-applied maxItems, so windows each
        # answering within the limit could merge to a list over it.
        parts = [self._part(event_types=["FINANCIAL_AUDIT", "EQUITY_CONTROL"]),
                 self._part(event_types=["IPO_REFINANCING", "REGULATORY_COMPLIANCE"]),
                 self._part(event_types=["PRODUCT_TECH_IP"])]
        merged = ai_label.merge_labels(parts, 8)
        self.assertEqual(len(merged["event_types"]), 4)
        self.assertEqual(len(merged["event_types"]), len(set(merged["event_types"])))

    def test_first_stated_event_time_wins(self):
        merged = ai_label.merge_labels(
            [self._part(event_time=""), self._part(event_time="2026年5月27日", event_time_precision="day")], 8)
        self.assertEqual(merged["event_time"], "2026年5月27日")
        self.assertEqual(merged["event_time_precision"], "day")


class ProviderTests(unittest.TestCase):
    def test_gateway_key_takes_precedence_and_uses_configured_model(self):
        env = {"LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://example.invalid/v1",
               "LLM_MODEL": "qwen-plus", "OPENAI_API_KEY": "other"}
        with mock.patch.dict("os.environ", env, clear=True):
            _, model, name = provider()
        self.assertEqual((model, name), ("qwen-plus", "llm_gateway"))

    def test_missing_every_key_is_an_explicit_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                provider()


class AiLabelTests(unittest.TestCase):
    def test_schema_is_closed_and_has_controlled_eventhood(self):
        payload_schema = schema()
        self.assertFalse(payload_schema["additionalProperties"])
        self.assertEqual(payload_schema["properties"]["eventhood"]["enum"], ["EVENT", "NO_EVENT", "IRRELEVANT"])

    def test_unsupported_claim_is_dropped(self):
        label = {"eventhood": "EVENT", "extraction_confidence": 0.9,
                 "claims": [{"text": "原文不存在的说法", "issue_code": "FINANCIAL", "claim_type": "fact"}]}
        parsed, warnings = locate_claims(label, "长鑫科技披露了上市进展。")
        self.assertEqual(parsed["claims"], [])
        self.assertIn("unsupported_claim_dropped", warnings)
        self.assertLess(parsed["extraction_confidence"], 0.5)

    def test_exact_quote_gets_location(self):
        text = "[PAGE 2]\n公司于今日完成发行。"
        label = {"eventhood": "EVENT", "extraction_confidence": 0.9,
                 "claims": [{"text": "公司于今日完成发行", "issue_code": "IPO_REFINANCING", "claim_type": "fact"}]}
        parsed, _ = locate_claims(label, text)
        self.assertEqual(parsed["claims"][0]["cited_source"], "page:2")

    def test_pdf_line_break_is_mapped_to_exact_raw_span(self):
        text = "The security breach has been\ncontained and resolved."
        label = {"eventhood": "EVENT", "extraction_confidence": 0.9,
                 "claims": [{"text": "The security breach has been contained and resolved.",
                             "issue_code": "CYBERSECURITY", "claim_type": "fact"}]}
        parsed, warnings = locate_claims(label, text)
        self.assertEqual(warnings, [])
        claim = parsed["claims"][0]
        self.assertEqual(text[claim["source_start"]:claim["source_end"]], claim["text"])


if __name__ == "__main__":
    unittest.main()


class PdfWhitespaceClaimTests(unittest.TestCase):
    """PDF extraction scatters whitespace; a quote must survive either rendering."""

    TEXT = ("截至2024 年3 月31 日，公司累计通过集中竞价交易方式回购A 股股份6,519,226 股，"
            "占公司本公告日总股本的0.22%。")

    def locate(self, quote):
        label = {"eventhood": "EVENT", "extraction_confidence": 0.9,
                 "claims": [{"text": quote, "claim_type": "fact"}]}
        located, warnings = locate_claims(label, self.TEXT)
        return located["claims"], warnings

    def test_a_quote_that_kept_the_pdf_spacing_is_located(self):
        claims, _ = self.locate("公司累计通过集中竞价交易方式回购A 股股份6,519,226 股")
        self.assertEqual(len(claims), 1)

    def test_a_quote_that_dropped_the_pdf_spacing_is_located(self):
        # The failure that discarded 44 of 57 claims on a batch of PDF filings:
        # the model writes the sentence the way a reader would, without the
        # breaks PDF extraction inserted, and the claim was called unsupported.
        claims, warnings = self.locate("公司累计通过集中竞价交易方式回购A股股份6,519,226股")
        self.assertEqual(len(claims), 1)
        self.assertNotIn("unsupported_claim_dropped", warnings)

    def test_the_stored_span_is_the_raw_text_not_the_models_rendering(self):
        # A verdict cites the span, so it has to be what the document says.
        claims, _ = self.locate("截至2024年3月31日")
        self.assertEqual(claims[0]["text"], "截至2024 年3 月31 日")
        self.assertEqual(self.TEXT[claims[0]["source_start"]:claims[0]["source_end"]],
                         claims[0]["text"])

    def test_text_that_is_not_in_the_document_is_still_dropped(self):
        # Tolerating whitespace must not tolerate invention.
        claims, warnings = self.locate("公司回购了一千万股")
        self.assertEqual(claims, [])
        self.assertIn("unsupported_claim_dropped", warnings)

    def test_characters_out_of_order_are_not_matched(self):
        claims, _ = self.locate("6,519,226 股回购公司")
        self.assertEqual(claims, [])


class StrictModeBlacklistTests(unittest.TestCase):
    """Only an explicit refusal demotes a model out of strict schema mode."""

    def test_a_transport_failure_does_not_demote_the_model(self):
        # One dropped connection used to blacklist json_schema for the whole
        # process.  The corpus then ran under json_object, where the contract is
        # advisory: a 958-document run came back with relevance_score -1 and
        # arrays where a number belongs, and 200 documents failed validation
        # after paying for three attempts each.
        for error in (Exception("Connection reset by peer"),
                      Exception("Request timed out"),
                      Exception("rate limit exceeded"),
                      TimeoutError()):
            self.assertFalse(refuses_strict_mode(error), error)

    def test_a_schema_violation_is_about_the_body_not_the_mode(self):
        self.assertFalse(refuses_strict_mode(json.JSONDecodeError("bad", "", 0)))
        self.assertFalse(refuses_strict_mode(Exception("-1 is less than the minimum of 0")))

    def test_the_gateway_naming_the_parameter_does_demote(self):
        for message in ("Error code: 400 - response_format json_schema is not supported for this model",
                        "unsupported parameter: response_format",
                        "unknown response_format type json_schema"):
            self.assertTrue(refuses_strict_mode(Exception(message)), message)

    def test_no_error_is_not_a_refusal(self):
        self.assertFalse(refuses_strict_mode(None))


class TargetedRepairTests(unittest.TestCase):
    """Re-sending a failed run must not mean re-sending everything around it."""

    def _db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY, text_path TEXT,
                eventhood TEXT, relevance_score REAL, rule_eventhood TEXT,
                rule_relevance_score REAL, inclusion_status TEXT,
                extraction_status TEXT, text_chars INTEGER, page_count INTEGER, run_id TEXT);
            CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT,
                source_tier TEXT, company_id TEXT, title TEXT, published_at TEXT,
                document_type TEXT, content_hash TEXT, canonical_url TEXT, raw_path TEXT,
                review_status TEXT);
            CREATE TABLE event_records(event_id TEXT, version_id TEXT, label_source TEXT);
        """)
        for index in range(4):
            vid = f"{index}{'a' * 63}"
            db.execute("""INSERT INTO processed_documents VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (vid, f"t{index}.txt", "EVENT", 0.9, "EVENT", 0.9,
                        "included", "ok", 500, 3, "ai-old"))
            db.execute("""INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (vid, "sse", "S0", "cxmt", f"doc{index}", "2026-01-01", "announcements",
                        "h", "u", "r", "ok"))
        db.commit()
        return db

    def test_only_the_named_documents_are_selected(self):
        db = self._db()
        rows = candidate_rows(db, None, force=True, version_prefixes=["1aaaaaaa", "3aaaaaaa"])
        self.assertEqual(sorted(row["version_id"][:8] for row in rows), ["1aaaaaaa", "3aaaaaaa"])
        db.close()

    def test_naming_nothing_leaves_the_selection_alone(self):
        db = self._db()
        self.assertEqual(len(candidate_rows(db, None, force=True)), 4)
        self.assertEqual(len(candidate_rows(db, None, force=True, version_prefixes=None)), 4)
        db.close()

    def test_a_named_document_still_has_to_be_eligible(self):
        # The filter narrows the queue; it does not smuggle in a document the
        # pipeline excluded.
        db = self._db()
        db.execute("UPDATE processed_documents SET inclusion_status='excluded_no_body' WHERE version_id LIKE '1%'")
        db.commit()
        rows = candidate_rows(db, None, force=True, version_prefixes=["1aaaaaaa"])
        self.assertEqual(rows, [])
        db.close()


class RuleFloorTests(unittest.TestCase):
    """A model's verdict must not decide whether the model may be asked again."""

    def _db(self, eventhood, relevance, rule_eventhood, rule_relevance, run_id="pipeline-1"):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE processed_documents(version_id TEXT PRIMARY KEY, text_path TEXT,
                eventhood TEXT, relevance_score REAL, rule_eventhood TEXT,
                rule_relevance_score REAL, inclusion_status TEXT, extraction_status TEXT,
                text_chars INTEGER, page_count INTEGER, run_id TEXT);
            CREATE TABLE document_versions(version_id TEXT PRIMARY KEY, source_id TEXT,
                source_tier TEXT, company_id TEXT, title TEXT, published_at TEXT,
                document_type TEXT, content_hash TEXT, canonical_url TEXT, raw_path TEXT,
                review_status TEXT);
            CREATE TABLE event_records(event_id TEXT, version_id TEXT, label_source TEXT);
        """)
        db.execute("INSERT INTO processed_documents VALUES('v1','t.txt',?,?,?,?,'included','ok',900,3,?)",
                   (eventhood, relevance, rule_eventhood, rule_relevance, run_id))
        db.execute("""INSERT INTO document_versions VALUES('v1','us_govinfo','S0','wuxi_apptec',
            'H.R. 8333 — BIOSECURE Act','2024-05-01','bill','h','u','r','ok')""")
        db.commit()
        return db

    def test_a_document_the_model_called_no_event_can_still_be_re_sent(self):
        # Two BIOSECURE Act filings naming the company were labelled NO_EVENT,
        # which overwrote the rule layer's EVENT/0.98 with NO_EVENT/0.5 and put
        # them under the eligibility floor -- unreachable, and only a database
        # snapshot brought them back.
        db = self._db("NO_EVENT", 0.5, "EVENT", 0.98, run_id="ai-123")
        rows = candidate_rows(db, None, force=True)
        self.assertEqual([row["version_id"] for row in rows], ["v1"])
        db.close()

    def test_the_rule_reading_is_what_the_row_reports(self):
        db = self._db("NO_EVENT", 0.5, "EVENT", 0.98, run_id="ai-123")
        row = candidate_rows(db, None, force=True)[0]
        self.assertEqual(row["rule_eventhood"], "EVENT")
        self.assertEqual(row["rule_relevance"], 0.98)
        db.close()

    def test_a_document_the_rule_layer_never_wanted_stays_out(self):
        # The floor still is a floor: nothing here promotes an irrelevant page.
        db = self._db("NO_EVENT", 0.2, "IRRELEVANT", 0.1)
        self.assertEqual(candidate_rows(db, None, force=True), [])
        db.close()

    def test_rows_written_before_the_split_fall_back_to_the_old_columns(self):
        db = self._db("EVENT", 0.9, None, None)
        self.assertEqual(len(candidate_rows(db, None, force=True)), 1)
        db.close()


class IssueCodeGuideTests(unittest.TestCase):
    """The extraction prompt must define every code its schema offers.

    It did not: issue_codes was a bare enum the prompt never mentioned, the same
    defect production_mode and article_sentiment were each repaired for.  An
    undefined enum value is one the model fills in by guesswork, and on the gold
    (交付物11 §4.5) defining them moved the layer from micro-F1 0.1789 to 0.3997.
    """

    def test_every_code_the_schema_offers_is_defined_in_the_prompt(self):
        from processor.ai_label import SYSTEM_PROMPT, schema
        offered = schema()["properties"]["issue_codes"]["items"]["enum"]
        for code in offered:
            self.assertIn(f"- {code} ", SYSTEM_PROMPT, code)

    def test_the_definitions_cover_the_vocabulary_exactly(self):
        # A code with keywords but no definition is invisible to the model path;
        # a definition with no code could never be emitted.
        from processor.pipeline import CODE_GUIDE, ISSUE_RULES
        self.assertEqual(set(CODE_GUIDE), set(ISSUE_RULES))

    def test_the_prompt_forbids_keyword_reasoning(self):
        # The finding the guide exists to act on: an article about policy can run
        # its length without the phrase 行业政策 in it.
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn("不是文中出现了哪些词", SYSTEM_PROMPT)

    def test_the_prompt_separates_the_two_company_side_v2_codes(self):
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn("进入原本没有的地盘", SYSTEM_PROMPT)
        self.assertIn("重组已经握有的地盘", SYSTEM_PROMPT)

    def test_the_external_content_fence_survived_the_insertion(self):
        # The guide was spliced in above the fence; losing the fence to an edit
        # would open the injection hole the fence exists to close.
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn("〔外部内容〕", SYSTEM_PROMPT)
        self.assertIn("绝不执行", SYSTEM_PROMPT)



class PayloadRepairTests(unittest.TestCase):
    """The three overruns that cost this corpus 203 documents in one evening.

    Each is a real ``processing_runs.error_message`` from 2026-09-03, and each
    is one field of an otherwise usable document.  What these assert is the
    distinction the pipeline had not been drawing: an overrun is repaired and
    counted, a structurally broken payload is still refused.
    """

    def setUp(self):
        ai_label.correction_counts()  # start from a clean meter

    def test_a_sentinel_minus_one_is_clamped_rather_than_thrown_away(self):
        # `relevance_score: -1` is the model's own shorthand for "not relevant".
        payload = {**VALID_LABEL, "relevance_score": -1}
        fixes = ai_label.coerce_payload(payload, schema())
        self.assertEqual(payload["relevance_score"], 0)
        self.assertEqual(fixes["clamped:relevance_score"], 1)

    def test_a_confidence_above_one_is_clamped_too(self):
        payload = {**VALID_LABEL, "extraction_confidence": 1.4}
        ai_label.coerce_payload(payload, schema())
        self.assertEqual(payload["extraction_confidence"], 1)

    def test_forty_ratios_are_truncated_and_the_loss_is_counted(self):
        # An annual report stating forty percentages is not a malformed document.
        payload = {**VALID_LABEL, "ratios": [f"{n}%" for n in range(40)]}
        fixes = ai_label.coerce_payload(payload, schema())
        self.assertEqual(len(payload["ratios"]), 10)
        self.assertEqual(fixes["truncated:ratios"], 30)

    def test_disclosure_keeps_more_of_them_because_its_contract_says_so(self):
        payload = {**VALID_LABEL, "ratios": [f"{n}%" for n in range(40)]}
        ai_label.coerce_payload(payload, schema(ai_label.FULL_READ_LIST_CAP))
        self.assertEqual(len(payload["ratios"]), 24)

    def test_a_repeated_code_is_removed_and_the_loss_is_counted(self):
        # 396 stored rows carry a value twice, because the schema never said
        # uniqueItems and a single-window document returns its window directly
        # rather than through merge_labels.union, which is where de-duplication
        # used to happen.
        payload = {**VALID_LABEL, "event_types": ["FINANCIAL_AUDIT", "FINANCIAL_AUDIT"]}
        fixes = ai_label.coerce_payload(payload, schema())
        self.assertEqual(payload["event_types"], ["FINANCIAL_AUDIT"])
        self.assertEqual(fixes["deduped:event_types"], 1)

    def test_deduplication_happens_before_truncation_so_no_real_value_is_lost(self):
        # Truncating first would keep the repeat and drop the last real type:
        # the row would lose a value it reported and keep a phantom one.  Three
        # stored rows hold five event types under a maxItems of four this way.
        payload = {**VALID_LABEL, "event_types": ["FINANCIAL_AUDIT", "FINANCIAL_AUDIT",
                                                  "EQUITY_CONTROL", "IPO_REFINANCING",
                                                  "REGULATORY_COMPLIANCE"]}
        ai_label.coerce_payload(payload, schema())
        self.assertEqual(payload["event_types"], ["FINANCIAL_AUDIT", "EQUITY_CONTROL",
                                                  "IPO_REFINANCING", "REGULATORY_COMPLIANCE"])

    def test_an_over_long_reason_is_shortened_rather_than_refused(self):
        # 2026-09-06: five documents were lost because the model argued its
        # event-type judgement over several paragraphs against a maxLength of 300.
        # The whole point of this function is that an overrun is one field's
        # problem, not the document's.
        contract_schema = {"type": "object", "properties": {
            "reason": {"type": "string", "maxLength": 10}}}
        payload = {"reason": "一二三四五六七八九十十一十二"}
        fixes = ai_label.coerce_payload(payload, contract_schema)
        self.assertEqual(payload["reason"], "一二三四五六七八九十")
        self.assertEqual(fixes["shortened:reason"], 1)

    def test_a_verbatim_quote_inside_an_item_is_never_silently_shortened(self):
        # A claim's text is located character-by-character in the source text
        # afterwards; a quietly trimmed quote would either fail to locate or, worse,
        # locate a span that is not what the model actually cited.  Item-level
        # strings keep the drop-one-item behaviour instead.
        long_text = "公司" * 300
        payload = {**VALID_LABEL, "claims": [{"text": long_text, "issue_code": None,
                                              "claim_type": "fact"}]}
        ai_label.coerce_payload(payload, schema())
        self.assertEqual(payload["claims"], [])

    def test_every_controlled_array_declares_uniqueitems(self):
        properties = schema()["properties"]
        for field in ("event_types", "issue_codes", "affected_objects", "event_subjects"):
            with self.subTest(field=field):
                self.assertTrue(properties[field].get("uniqueItems"), f"{field} 允许数组内重复")

    def test_one_short_claim_is_dropped_and_the_rest_survive(self):
        # '1.7 %' is five characters against a minLength of six.  Losing the
        # document over it lost the other three claims as well.
        good = {"text": "公司募集资金总额为 295 亿元", "issue_code": None, "claim_type": "fact"}
        payload = {**VALID_LABEL, "claims": [good, {"text": "1.7 %", "issue_code": None,
                                                    "claim_type": "fact"}, good]}
        fixes = ai_label.coerce_payload(payload, schema())
        self.assertEqual(len(payload["claims"]), 2)
        self.assertEqual(fixes["dropped:claims"], 1)

    def test_a_repaired_payload_then_passes_validation(self):
        # The point of the repair is that parse_response stops raising.
        body = json.dumps({**VALID_LABEL, "relevance_score": -1,
                           "ratios": [f"{n}%" for n in range(40)]})
        payload = ai_label.parse_response(body)
        self.assertEqual(payload["relevance_score"], 0)
        self.assertEqual(len(payload["ratios"]), 10)

    def test_the_repairs_reach_the_run_meter(self):
        # Truncating is acceptable; truncating where nobody can count it is not.
        ai_label.parse_response(json.dumps({**VALID_LABEL, "relevance_score": -1}))
        self.assertEqual(ai_label.correction_counts()["clamped:relevance_score"], 1)
        self.assertEqual(ai_label.correction_counts(), {})  # reading clears it

    def test_a_clean_payload_records_nothing(self):
        ai_label.parse_response(json.dumps(VALID_LABEL))
        self.assertEqual(ai_label.correction_counts(), {})

    def test_a_missing_required_field_is_still_a_failure(self):
        # Repair covers overruns, not absences: this must keep its guided retry.
        broken = {key: value for key, value in VALID_LABEL.items() if key != "summary"}
        with self.assertRaises(Exception):
            ai_label.parse_response(json.dumps(broken))

    def test_a_bad_enum_is_still_a_failure(self):
        with self.assertRaises(Exception):
            ai_label.parse_response(json.dumps({**VALID_LABEL, "eventhood": "MAYBE"}))


class DroppedClaimEvidenceTests(unittest.TestCase):
    """A discarded claim has to leave the words it was discarded for.

    675 claims have been rejected across this corpus and not one left a trace.
    The counter says a drop happened; it cannot say whether the model was
    paraphrasing — which the matcher is right to reject — or whether the
    matcher failed on text it should have found. Telling those apart meant
    paying to run the model again.
    """

    def test_the_quote_is_kept_when_it_cannot_be_located(self):
        label = {**VALID_LABEL, "claims": [
            {"text": "这句话原文里没有", "issue_code": None, "claim_type": "fact"}]}
        located, warnings = locate_claims(label, "原文完全是别的内容。")
        self.assertEqual(warnings, ["unsupported_claim_dropped"])
        self.assertEqual(located["dropped_claims"], ["这句话原文里没有"])

    def test_a_located_claim_leaves_nothing_behind(self):
        label = {**VALID_LABEL, "claims": [
            {"text": "募资295亿元", "issue_code": None, "claim_type": "fact"}]}
        located, warnings = locate_claims(label, "公司公告称募资295亿元，用于扩产。")
        self.assertEqual(warnings, [])
        self.assertEqual(located.get("dropped_claims", []), [])

    def test_a_very_long_quote_is_truncated_rather_than_stored_whole(self):
        # This is diagnostic evidence, not a second copy of the document.
        long_quote = "甲" * 500
        label = {**VALID_LABEL, "claims": [
            {"text": long_quote, "issue_code": None, "claim_type": "fact"}]}
        located, _ = locate_claims(label, "原文与之无关。")
        self.assertEqual(len(located["dropped_claims"][0]), 200)

    def test_drops_from_every_window_survive_the_merge(self):
        # A long document is labelled window by window; keeping only the
        # leading window's drops would hide most of them on exactly the
        # documents where claims are hardest to locate.
        first = {**VALID_LABEL, "dropped_claims": ["第一段丢的"]}
        second = {**VALID_LABEL, "dropped_claims": ["第二段丢的"]}
        merged = merge_labels([first, second], 8)
        self.assertEqual(merged["dropped_claims"], ["第一段丢的", "第二段丢的"])

    def test_the_same_quote_dropped_twice_is_recorded_once(self):
        repeated = {**VALID_LABEL, "dropped_claims": ["同一句"]}
        merged = merge_labels([repeated, {**VALID_LABEL, "dropped_claims": ["同一句"]}], 8)
        self.assertEqual(merged["dropped_claims"], ["同一句"])


class LooseQuoteMatchingTests(unittest.TestCase):
    """Every case here is a real claim this matcher rejected.

    They were unreadable until the discarded text started being recorded; the
    counter said 675 drops and nothing said what they were. Read back, three
    causes turned out to be the model being *more* careful than the matcher
    allowed for, and two turned out to be genuine paraphrase.

    The stored span is always the source's own wording, never the model's
    rendering of it — the span is what a verdict cites.
    """

    def located(self, quote, text):
        found = locate_loosely(quote, text)
        return text[found.start():found.end()] if found else None

    # —— 该命中的三种 ——

    def test_whitespace_scattered_by_pdf_extraction(self):
        # 「截至2024 年3 月31 日」 in the source, written normally by the model.
        self.assertEqual(
            self.located("截至2024年3月31日", "公司称，截至2024 年3 月31 日，产能爬坡完成。"),
            "截至2024 年3 月31 日")

    def test_a_full_stop_the_model_supplied_itself(self):
        # The commonest of the three: the model closes a fragment the source
        # continues past. Verbatim but for one character it added.
        self.assertEqual(
            self.located("同比增长约32.6%。", "扣非归母净利润同比增长约32.6%，与经调整口径一致"),
            "同比增长约32.6%")

    def test_a_footnote_marker_the_model_left_out(self):
        self.assertEqual(
            self.located("淨利潤94.50億元；稀釋每股收益", "業績：淨利潤94.50億元[1]；稀釋每股收益3.26元"),
            "淨利潤94.50億元[1]；稀釋每股收益")

    def test_a_quotation_mark_retyped_in_another_family(self):
        # 「(the "2025F")」 rendered as 「(the '2025F')」 is a re-typed mark, not
        # a changed quotation.
        self.assertIsNotNone(self.located("the '2025F' plan", 'the "2025F" plan was'))

    # —— 该拒绝的三种 ——

    def test_an_ellipsis_joining_two_distant_sentences_is_a_summary(self):
        # 「2026年1~3月……公司一季度营收同比增长719.13%」 — the model summarising
        # across a gap. A summary is not a quotation, and this is the matcher
        # doing its job.
        self.assertIsNone(
            self.located("2026年1~3月……一季度营收同比增长719.13%",
                         "一季度营收同比增长719.13%，净利润330亿元"))

    def test_a_table_row_reassembled_from_separated_cells_is_refused(self):
        # 「Number of shares repurchased: 2,728,500」 where the extracted text
        # has another cell between the label and the figure. That is the table
        # flattening problem, and widening this matcher to paper over it would
        # accept genuine reorderings too.
        self.assertIsNone(
            self.located("Number of shares repurchased: 2,728,500",
                         "Number of shares repurchased Method 2,728,500"))

    def test_text_that_is_simply_not_there_is_refused(self):
        self.assertIsNone(self.located("这句话原文里没有", "原文完全是别的内容。"))

    def test_the_loosening_still_needs_the_whole_string(self):
        # One character genuinely different, and it is not a match — the three
        # allowances are narrow, not a similarity threshold.
        self.assertIsNone(self.located("募资295亿元", "公司募资296亿元，用于扩产。"))


class ClaimGuideTests(unittest.TestCase):
    """The prompt's claim instruction, which for two versions said only what
    *not* to do.

    v2 carried one sentence: 「claims.text 必须逐字复制原文…；无法原文定位则不要
    输出 claim」. It described the shape of a claim and the case for withholding
    one, and never said to produce any, how many, or what makes a sentence worth
    quoting. 442 events of 1,910 came back with none.

    The precedent is in this file already: ``issue_codes`` was a bare enum the
    prompt never explained, and supplying definitions took its micro-F1 from
    0.1789 to 0.3997.

    Measured on 30 documents that produced no claims under v2, same model,
    temperature 0: located claims 33 -> 68, rejected proposals 2 -> 16. The
    model proposes far more and a larger share of it fails to locate — which is
    acceptable only because the locator is the gate, not the prompt, and every
    rejection now keeps its text.
    """

    def test_the_prompt_says_what_a_claim_is_not_only_what_it_is_not(self):
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn("可以拿去和权威文件核对的断言", SYSTEM_PROMPT)
        self.assertIn("通常 1—5 条", SYSTEM_PROMPT)

    def test_it_names_the_four_shapes_that_get_rejected(self):
        # Each of these was read off a real rejected quote. Telling the model
        # about them is cheaper than discarding what it writes because of them.
        from processor.ai_label import SYSTEM_PROMPT
        for shape in ("省略号", "本公司", "译成英文", "表格"):
            self.assertIn(shape, SYSTEM_PROMPT, shape)

    def test_an_empty_list_is_still_allowed_for_a_document_with_no_assertion(self):
        # The guide must not push the model into inventing claims for a pure
        # index broadcast. Pressure to always produce something is how a
        # grounded extractor starts hallucinating.
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn("才给空列表", SYSTEM_PROMPT)

    def test_the_version_moved_with_the_prompt(self):
        # prompt_version is stored on every label (M0-5). A corpus holding both
        # can only say which prompt produced which conclusion if this changes.
        # v3 added the claim guide; v4 shared the issue-code rules.
        from processor.ai_label import PROMPT_VERSION
        self.assertTrue(PROMPT_VERSION.startswith("event-label-v"))
        self.assertGreaterEqual(int(PROMPT_VERSION.rsplit("v", 1)[1]), 3)

    def test_the_verbatim_requirement_survived_the_rewrite(self):
        # The guide is longer and more encouraging; the one rule that keeps a
        # claim checkable must not have been softened along the way.
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn("必须逐字复制原文", SYSTEM_PROMPT)


class SharedIssueRuleTests(unittest.TestCase):
    """One copy of the issue-code judging rules, read by both prompts.

    They used to be written twice — once here for extraction, once in
    ``processor.issue_codes`` for the benchmark sidecar — and they drifted. The
    four-way precedence among BUSINESS_EXPANSION / YIELD_CAPACITY / TECH_LEVEL /
    STRATEGY_TRANSFORM was measured on held-out gold through the sidecar (test
    micro-F1 0.3997 -> 0.4211) and for a while existed only there: the path the
    platform actually labels with never got the improvement that had been paid
    for and proven.
    """

    def test_both_prompts_carry_the_measured_precedence(self):
        from processor.ai_label import SYSTEM_PROMPT as extraction
        from processor.issue_codes import SYSTEM_PROMPT as sidecar
        for name, prompt in (("ai_label", extraction), ("issue_codes", sidecar)):
            self.assertIn("按下列顺序取第一个命中的", prompt, name)
            self.assertIn("YIELD_CAPACITY", prompt, name)

    def test_all_four_competing_codes_keep_their_place_in_the_order(self):
        # The shared-source test above catches divergence but not decay: both
        # prompts read one string, so gutting that string keeps them equal.
        # This pins the ordering that was actually measured — the four codes in
        # the precedence block, in the order the benchmark validated — without
        # pinning the prose around them.
        from processor.pipeline import issue_code_rules
        rules = issue_code_rules(3)
        # From the numbered list onward. BUSINESS_EXPANSION is also named in the
        # sentence introducing the list («不要因为「也算拓展」就退回…»), and
        # searching from before it finds that one instead of its own rank.
        block = rules[rules.index("按下列顺序取第一个命中的"):]
        block = block[block.index("1."):]
        positions = [block.index(code) for code in
                     ("YIELD_CAPACITY", "TECH_LEVEL", "STRATEGY_TRANSFORM", "BUSINESS_EXPANSION")]
        self.assertEqual(positions, sorted(positions))

    def test_the_rules_come_from_one_function_not_two_copies(self):
        # The drift guard: both must be built from pipeline.issue_code_rules,
        # so an improvement to one is an improvement to both.
        from processor.pipeline import issue_code_rules
        shared = issue_code_rules(3)
        from processor.ai_label import SYSTEM_PROMPT
        self.assertIn(shared, SYSTEM_PROMPT)
        from processor.issue_codes import SYSTEM_PROMPT as sidecar
        self.assertIn(issue_code_rules(2), sidecar)

    def test_the_one_legitimate_difference_is_the_code_ceiling(self):
        # Extraction's schema allows three codes; the benchmark task never has
        # more than two. That is the only thing the two copies may differ on.
        from processor.pipeline import issue_code_rules
        three, two = issue_code_rules(3), issue_code_rules(2)
        self.assertNotEqual(three, two)
        self.assertIn("极少超过 3 个", three)
        self.assertIn("不存在 3 个的情况", two)
        # Everything after the first bullet is identical.  Split on the bullet
        # marker rather than on a line count: the two-code ceiling wraps onto a
        # second line -- the wrapping every published E1 number was measured on
        # -- and a line-offset comparison broke the moment it was restored.
        self.assertEqual(three.split("\n- ", 1)[1], two.split("\n- ", 1)[1])

    def test_the_extraction_prompt_version_moved_with_the_rules(self):
        # prompt_version is stored on every label; a corpus holding both must be
        # able to say which prompt produced which conclusion.  v5 (2026-09-06) is
        # the event-type and impact guides; the 1,907 stored events were labelled
        # by v2 and their prompt_version still says so.
        from processor.ai_label import PROMPT_VERSION
        self.assertEqual(PROMPT_VERSION, "event-label-v8")
