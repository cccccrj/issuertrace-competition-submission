import unittest
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import collector.collect as collect
from collector.collect import RuntimeSource, api_job_outcome, attachment_metadata_hints, backoff_seconds, canonicalize, challenge_marker, discovery_depth, enqueue_frontier, extract_metadata, frontier_priority, listing_page_dates, prune_archive_after_boundary, record_document, redirected_to_landing, relevant_links, reserve_host_slot, retry_after_seconds, robot_allowed, store_discovered_hint, url_in_scope


class CollectorTests(unittest.TestCase):
    def test_api_audit_outcome_distinguishes_incremental_jobs(self):
        self.assertEqual(api_job_outcome("cxmt:2024-01-01:2024-01-31:1", True), "api_backfill_success")
        self.assertEqual(api_job_outcome("incremental:cxmt:2026-08-25:1", True), "api_incremental_success")
        self.assertEqual(api_job_outcome("incremental:cxmt:2026-08-25:1", False), "api_incremental_error")

    def test_domain_rate_slots_are_shared_across_sources(self):
        db = sqlite3.connect(":memory:")
        first = reserve_host_slot(db, "example.com", "source_a", 10, lambda _: None, 0)
        second = reserve_host_slot(db, "example.com", "source_b", 10, lambda _: None, 0)
        self.assertEqual(first, 0)
        self.assertGreater(second, 9)
        self.assertEqual(db.execute("SELECT last_source_id FROM domain_rate_state WHERE host='example.com'").fetchone()[0],
                         "source_b")
        db.close()

    def test_connections_enable_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                collect, "STATE_DB", Path(temp_dir) / "collector.sqlite3"):
            db = collect.connect()
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            db.close()

    def test_attachment_hint_inherits_entity_from_search_result_parent(self):
        raw = {
            "source_id": "regulator", "base_url": "https://reg.example/",
            "company_scope": ["cxmt", "wuxi_apptec"],
            "document_types": ["record"], "collection_mode": "search",
        }
        source = RuntimeSource(raw, "DIRECT", {})
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                collect, "STATE_DB", Path(temp_dir) / "collector.sqlite3"):
            db = collect.connect()
            db.execute("""INSERT INTO discovered_metadata(
                source_id,canonical_url,company_id,matched_legal_entity,review_status)
                VALUES('regulator','https://reg.example/detail','cxmt','长鑫存储技术有限公司',
                       'candidate_entity_match')""")
            store_discovered_hint(
                db, source, "https://reg.example/file.pdf", "许可附件", "2026-08-25",
                "candidate_entity_match", parent_url="https://reg.example/detail")
            self.assertEqual(db.execute("""SELECT company_id,matched_legal_entity
                FROM discovered_metadata WHERE canonical_url='https://reg.example/file.pdf'""").fetchone(),
                ("cxmt", "长鑫存储技术有限公司"))
            db.close()

    def test_embedded_recaptcha_library_is_not_a_challenge_page(self):
        body = b'<html><title>News</title><script src="https://google.com/recaptcha/api.js"></script><main>Stories</main></html>'
        self.assertIsNone(challenge_marker(body))

    def test_login_modal_in_full_page_is_not_a_challenge_page(self):
        body = '<input placeholder="请输入验证码">'.encode() + b'x' * 100001
        self.assertIsNone(challenge_marker(body))

    def test_login_modal_placeholder_in_short_page_is_not_a_challenge_page(self):
        body = '<main>normal search results</main><input placeholder="请输入验证码">'.encode()
        self.assertIsNone(challenge_marker(body))

    def test_explicit_captcha_prompt_is_a_challenge_page(self):
        self.assertEqual(challenge_marker(b'<title>Captcha verification</title>Please complete the captcha'),
                         "complete the captcha")

    def test_fda_abuse_apology_is_a_challenge_page(self):
        self.assertEqual(
            challenge_marker(b'<script>location.href="/apology_objects/abuse-detection-apology.html"</script>'),
            "abuse-detection-apology")

    def test_robots_denial_has_explicit_reason(self):
        class Response:
            status_code = 200
            text = "User-agent: *\nDisallow: /private"

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        self.assertEqual(robot_allowed(Session(), "https://example.com/private/a", "collector", 5),
                         (False, "robots_disallowed"))

    def test_robots_wildcard_query_rule_is_enforced(self):
        body = "User-agent: *\nDisallow: *searchTerm=*\n"
        self.assertFalse(collect.robots_body_allows(
            body, "https://example.com/search?searchTerm=CXMT", "collector/1.0"))
        self.assertTrue(collect.robots_body_allows(
            body, "https://example.com/sitemap.xml", "collector/1.0"))

    def test_robots_allow_wins_equally_specific_rule_and_dollar_anchors(self):
        body = ("User-agent: *\nDisallow: /files/*\nAllow: /files/public*\n"
                "Disallow: /exact$\n")
        self.assertTrue(collect.robots_body_allows(
            body, "https://example.com/files/public-report", "collector/1.0"))
        self.assertFalse(collect.robots_body_allows(
            body, "https://example.com/exact", "collector/1.0"))
        self.assertTrue(collect.robots_body_allows(
            body, "https://example.com/exact/more", "collector/1.0"))

    def test_empty_disallow_ends_agent_group_without_merging_next_agents(self):
        body = ("User-agent: AmazonAdBot\nDisallow:\n"
                "User-agent: bytespider\nUser-agent: ExampleAI\nDisallow: /\n")
        self.assertTrue(collect.robots_body_allows(
            body, "https://example.com/article", "AmazonAdBot/1.0"))
        self.assertFalse(collect.robots_body_allows(
            body, "https://example.com/article", "ExampleAI/1.0"))

    def test_robots_cache_avoids_repeat_request_and_rechecks_path(self):
        class Response:
            status_code = 200
            text = "User-agent: *\nDisallow: /private"

        class Session:
            calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                return Response()

        db = sqlite3.connect(":memory:")
        session = Session()
        self.assertEqual(robot_allowed(session, "https://example.com/public", "collector", 5, db=db),
                         (True, "robots_checked"))
        self.assertEqual(robot_allowed(session, "https://example.com/private/a", "collector", 5, db=db),
                         (False, "robots_cached_disallowed"))
        self.assertEqual(session.calls, 1)
        db.close()

    def test_canonicalize_drops_tracking_and_fragment(self):
        self.assertEqual(canonicalize("HTTPS://Example.COM//news/a?utm_source=x&id=2#top"), "https://example.com/news/a?id=2")

    def test_deep_link_redirected_to_source_landing_is_not_content(self):
        base = "https://example.com/zh-hk/"
        self.assertTrue(redirected_to_landing(
            "https://example.com/zh-hk/investors/reports", "https://example.com/zh-hk/", base))
        self.assertFalse(redirected_to_landing(base, base, base))
        self.assertFalse(redirected_to_landing(
            "https://example.com/zh-hk/old", "https://example.com/zh-hk/new", base))

    def test_retry_after_floor(self):
        self.assertEqual(retry_after_seconds("120"), 120)
        self.assertEqual(retry_after_seconds(None), 3600)

    def test_backoff_is_bounded(self):
        self.assertEqual([backoff_seconds("server", i) for i in range(1, 6)], [30, 120, 600, 600, 600])

    def test_declared_pagination_is_discovered(self):
        body = b'<html><title>x</title><div class="paging" data-total="4" data-path="/news-$.html"></div></html>'
        _, links = relevant_links("https://example.com/news.html", body)
        self.assertIn("https://example.com/news-4.html", links)

    def test_svg_title_is_not_used_as_document_title(self):
        body = b'<html><head><title>Search Results</title></head><body><svg><title>Lock</title></svg></body></html>'
        title, _ = relevant_links("https://example.com/search", body)
        self.assertEqual(title, "Search Results")

    def test_search_discovery_is_restricted_to_declared_result_paths(self):
        source = RuntimeSource({
            "source_id": "committee", "base_url": "https://committee.example/?s=WuXi",
            "discovery_path_prefixes": ["/release/", "/wp-content/uploads/"],
            "discover_search_results": True,
            "search_result_path_prefixes": ["/release/"],
            "search_result_query_parameter": "highlight",
            "detail_discovery_attachments_only": True,
        }, "DIRECT", {})
        body = b'''<a href="/release/wuxi-report/?highlight=WuXi">Press release</a>
        <a href="/press/archive/">Press archive</a>
        <a href="/wp-content/uploads/wuxi-letter.pdf">Letter</a>'''
        _, links = relevant_links(source.raw["base_url"], body, source)
        self.assertEqual(links, ["https://committee.example/release/wuxi-report/?highlight=WuXi"])
        _, detail_links = relevant_links(
            "https://committee.example/release/wuxi-report/?highlight=WuXi", body, source)
        self.assertEqual(detail_links, [
            "https://committee.example/wp-content/uploads/wuxi-letter.pdf",
        ])

    def test_traditional_chinese_privacy_attachment_is_excluded(self):
        source = RuntimeSource({
            "source_id": "zh_hk", "base_url": "https://example.com/zh-hk/",
            "allowed_hosts": ["example.com", "static.example.com"],
            "path_allow_prefixes": ["/zh-hk/", "/upload/"],
        }, "DIRECT", {})
        body = b'<a href="https://static.example.com/upload/privacy.pdf">\xe9\x9a\xb1\xe7\xa7\x81\xe8\x81\xb2\xe6\x98\x8e</a>'
        _, links = relevant_links("https://example.com/zh-hk/", body, source)
        self.assertNotIn("https://static.example.com/upload/privacy.pdf", links)

    def test_article_dateline_beats_migration_timestamp(self):
        source = RuntimeSource({
            "source_id": "wuxi_en", "base_url": "https://example.com/", "company_scope": ["wuxi_apptec"],
            "_matched_legal_entity": "Example Co", "languages": ["en"], "document_types": ["company_news"],
        }, "PROXY", {})
        body = b'''<html lang="zh-CN"><meta name="description" content="(SHANGHAI, April 27, 2026) - results">
        <script>{"datePublished":"2026-08-07T09:37:06Z","dateModified":"2026-08-24T19:15:50Z"}</script></html>'''
        metadata = extract_metadata(source, "https://example.com/news/wuxi-news/article", body, "title")
        self.assertEqual(metadata["published_at"], "2026-04-27")
        self.assertEqual(metadata["page_updated_at"], "2026-08-24T19:15:50Z")

    def test_cxmt_two_segment_detail_page_uses_labeled_publish_date(self):
        source = RuntimeSource({
            "source_id": "cxmt_cn", "base_url": "https://www.cxmt.com/", "company_scope": ["cxmt"],
            "_matched_legal_entity": "长鑫科技集团股份有限公司", "languages": ["zh-CN"],
            "document_types": ["company_news"], "first_party_entity": True,
        }, "DIRECT", {})
        body = '<html><p class="title-day">发布时间：2025-10-28</p></html>'.encode()
        detail = extract_metadata(source, "https://www.cxmt.com/news/info_94.html", body, "title")
        archive = extract_metadata(source, "https://www.cxmt.com/news-12.html", body, "archive")
        self.assertEqual(detail["published_at"], "2025-10-28")
        self.assertEqual(detail["published_basis"], "labeled_page_field")
        self.assertEqual(detail["review_suggestion"], "auto_matched_first_party")
        self.assertIsNone(archive["published_at"])
        self.assertIsNone(archive["review_suggestion"])

    def test_explicit_official_attachment_filename_date(self):
        source = RuntimeSource({
            "source_id": "wuxi_zh_hk", "base_url": "https://www.example.com/zh-hk/",
            "company_scope": ["wuxi_apptec"], "_matched_legal_entity": "WuXi AppTec Co., Ltd.",
            "languages": ["zh-HK"], "document_types": ["investor_information"],
            "collection_mode": "crawl", "first_party_entity": True,
            "attachment_filename_date_pattern": r"(?:603259|02359)_(?P<date>[0-9]{8})_",
        }, "DIRECT", {})
        metadata = extract_metadata(source,
            "https://static.example/upload/603259_20260113_MWJW_deadbeef.pdf", b"", "公告")
        self.assertEqual(metadata["published_at"], "2026-01-13")
        self.assertEqual(metadata["published_basis"], "filename_date")
        self.assertEqual(metadata["review_suggestion"], "auto_matched_first_party")

    def test_first_party_flag_does_not_promote_archive_or_search_pages(self):
        source = RuntimeSource({
            "source_id": "official", "base_url": "https://example.com/news", "company_scope": ["cxmt"],
            "_matched_legal_entity": "长鑫科技集团股份有限公司", "languages": ["zh-CN"],
            "document_types": ["company_news"], "collection_mode": "crawl",
            "first_party_entity": True,
        }, "DIRECT", {})
        archive = extract_metadata(source, "https://example.com/news?page=2", b"<html>news</html>", "News")
        self.assertIsNone(archive["review_suggestion"])
        self.assertEqual(archive["document_type"], "company_news")

    def test_government_release_uses_structured_publication_date(self):
        source = RuntimeSource({
            "source_id": "committee", "base_url": "https://committee.example/?s=WuXi",
            "company_scope": ["wuxi_apptec"], "collection_mode": "search",
            "_canonical_names": {"wuxi_apptec": "WuXi AppTec Co., Ltd."},
            "_entity_alias_map": {"wuxi_apptec": ["WuXi AppTec"]},
            "languages": ["en"], "document_types": ["press_releases"],
        }, "DIRECT", {})
        body = b'<script>{"datePublished":"2024-05-15T10:00:00-04:00"}</script>WuXi AppTec'
        metadata = extract_metadata(
            source, "https://committee.example/release/wuxi-report/", body, "WuXi report")
        self.assertEqual(metadata["published_at"], "2024-05-15T10:00:00-04:00")
        self.assertEqual(metadata["published_basis"], "structured_data")
        self.assertEqual(metadata["company_id"], "wuxi_apptec")
        self.assertEqual(metadata["document_type"], "press_releases")

    def test_csrc_content_page_uses_chinese_labeled_date(self):
        source = RuntimeSource({
            "source_id": "csrc", "base_url": "https://www.csrc.gov.cn/",
            "company_scope": ["cxmt"], "collection_mode": "search",
            "_canonical_names": {"cxmt": "长鑫科技集团股份有限公司"},
            "_entity_alias_map": {"cxmt": ["长鑫科技集团股份有限公司", "688825"]},
            "languages": ["zh-CN"], "document_types": ["administrative_permission"],
        }, "DIRECT", {})
        body = '''<html><p>发文日期：</p><h1>关于同意长鑫科技集团股份有限公司首次公开发行股票注册的批复</h1>
        <p align="right"><span>202</span><span>6</span><span>年</span><span>6</span><span>月5</span><span>日</span></p></html>'''.encode()
        metadata = extract_metadata(
            source, "https://www.csrc.gov.cn/csrc/c105906/c7638905/content.shtml", body, "批复")
        self.assertEqual(metadata["published_at"], "2026-06-05")
        self.assertEqual(metadata["published_basis"], "document_signature")
        self.assertEqual(metadata["company_id"], "cxmt")
        self.assertEqual(metadata["document_type"], "administrative_permission")

    def test_generic_here_pdf_link_uses_filename_title(self):
        source = RuntimeSource({
            "source_id": "committee", "base_url": "https://committee.example/",
        }, "DIRECT", {})
        body = b'<a href="/uploads/WuXi-AppTec-Letter-FINAL.pdf">HERE</a>'
        self.assertEqual(
            collect.link_title_hints("https://committee.example/release/item/", body, source)[
                "https://committee.example/uploads/WuXi-AppTec-Letter-FINAL.pdf"],
            "WuXi AppTec Letter FINAL")

    def test_attachment_card_metadata(self):
        source = RuntimeSource({
            "source_id": "wuxi_announcements", "base_url": "https://www.example.com/announcements",
            "allowed_hosts": ["www.example.com", "static.example.com"], "path_allow_prefixes": ["/announcements", "/upload/"],
            "company_scope": ["wuxi_apptec"], "document_types": ["announcements"],
        }, "PROXY", {})
        body = b'''<div class="list-item"><p class="date">2026/08/24</p><h1 class="title">VOLUNTARY ANNOUNCEMENT</h1>
        <a href="https://static.example.com/upload/a.pdf">Read more</a></div>'''
        hints = attachment_metadata_hints("https://www.example.com/announcements", body, source)
        self.assertEqual(hints["https://static.example.com/upload/a.pdf"], ("VOLUNTARY ANNOUNCEMENT", "2026-08-24"))

    def test_attachment_hint_populates_version_and_document_titles(self):
        source = RuntimeSource({
            "source_id": "wuxi_announcements", "source_tier": "S1",
            "base_url": "https://www.example.com/announcements", "company_scope": ["wuxi_apptec"],
            "_matched_legal_entity": "WuXi AppTec Co., Ltd.", "languages": ["en"],
            "document_types": ["announcements"], "collection_mode": "crawl",
            "backfill_start": "2024-01-01",
        }, "PROXY", {})
        url = "https://static.example.com/upload/a.pdf"
        response = SimpleNamespace(
            headers={"Content-Type": "application/pdf"}, url=url, status_code=200)
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                collect, "STATE_DB", Path(temp_dir) / "collector.sqlite3"):
            db = collect.connect()
            store_discovered_hint(db, source, url, "VOLUNTARY ANNOUNCEMENT", "2026-08-24")
            metadata = record_document(db, source, url, response, "abc", "unused.pdf", "",
                                       "pending_entity_review")
            self.assertEqual(metadata["title"], "VOLUNTARY ANNOUNCEMENT")
            self.assertEqual(db.execute(
                "SELECT title FROM document_versions WHERE source_id=?", (source.source_id,)).fetchone()[0],
                "VOLUNTARY ANNOUNCEMENT")
            self.assertEqual(db.execute(
                "SELECT title FROM documents WHERE source_id=?", (source.source_id,)).fetchone()[0],
                "VOLUNTARY ANNOUNCEMENT")
            db.close()

    def test_document_signature_date_beats_listing_hint(self):
        source = RuntimeSource({
            "source_id": "csrc", "source_tier": "S0",
            "base_url": "https://www.csrc.gov.cn/guestweb4/s", "company_scope": ["cxmt"],
            "_matched_legal_entity": "长鑫科技集团股份有限公司",
            "_canonical_names": {"cxmt": "长鑫科技集团股份有限公司"},
            "_entity_alias_map": {"cxmt": ["长鑫科技集团股份有限公司"]},
            "languages": ["zh-CN"], "document_types": ["administrative_permission"],
            "collection_mode": "search", "backfill_start": "2024-01-01",
        }, "DIRECT", {})
        url = "https://www.csrc.gov.cn/csrc/example/content.shtml"
        body = '<html><head><title>长鑫批复</title></head><body><p align="right"><span>2026</span><span>年6月5日</span></p>长鑫科技集团股份有限公司</body></html>'.encode()
        response = SimpleNamespace(headers={"Content-Type": "text/html"}, url=url, status_code=200)
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                collect, "STATE_DB", Path(temp_dir) / "collector.sqlite3"), patch.object(
                collect, "ROOT", Path(temp_dir)):
            raw = Path(temp_dir) / "page.html"
            raw.write_bytes(body)
            db = collect.connect()
            store_discovered_hint(db, source, url, "长鑫批复", "2026-06-12", "candidate_entity_match")
            metadata = record_document(db, source, url, response, "abc", "page.html", "长鑫批复",
                                       "pending_entity_review")
            self.assertEqual(metadata["published_at"], "2026-06-05")
            self.assertEqual(metadata["published_basis"], "document_signature")
            db.close()

    def test_archive_pages_have_natural_priority(self):
        self.assertEqual(frontier_priority("https://example.com/news-14.html"), 14)
        self.assertEqual(frontier_priority("https://example.com/news?page=138"), 138)
        self.assertEqual(frontier_priority("https://example.com/news/item"), 0)

    def test_pagination_does_not_consume_content_depth(self):
        self.assertEqual(discovery_depth(3, "https://example.com/news?page=7"), 3)
        self.assertEqual(discovery_depth(3, "https://example.com/news-7.html"), 3)
        self.assertEqual(discovery_depth(3, "https://example.com/news/article"), 4)
        self.assertEqual(discovery_depth(
            3, "https://static.example.com/upload/a.pdf", "https://example.com/news?page=7"), 3)

    def test_archive_visible_dates_ignore_unlabeled_build_dates(self):
        body = b'<meta content="2026-08-24"><p class="date">2023/12/01</p><p class="publish-date">2023-11-30</p>'
        self.assertEqual(listing_page_dates("https://example.com/news?page=32", body), ["2023-12-01", "2023-11-30"])

    def test_boundary_prunes_only_older_pages_from_same_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                collect, "STATE_DB", Path(temp_dir) / "collector.sqlite3"):
            db = collect.connect()
            for url in (
                "https://example.com/announcements?page=56",
                "https://example.com/announcements?page=57",
                "https://example.com/announcements?page=197",
                "https://example.com/news?page=200",
            ):
                enqueue_frontier(db, "source", url, None, 0)
            self.assertEqual(prune_archive_after_boundary(
                db, "source", "https://example.com/announcements?page=57"), 1)
            states = dict(db.execute("SELECT url,status FROM crawl_frontier"))
            self.assertEqual(states["https://example.com/announcements?page=197"], "skipped_scope")
            self.assertEqual(states["https://example.com/announcements?page=56"], "pending")
            self.assertEqual(states["https://example.com/news?page=200"], "pending")
            db.close()

    def test_search_source_requires_entity_hit(self):
        raw = {
            "source_id": "regulator", "base_url": "https://reg.example/", "collection_mode": "search",
            "company_scope": ["cxmt"], "_matched_legal_entity": "长鑫科技集团股份有限公司",
            "_canonical_names": {"cxmt": "长鑫科技集团股份有限公司"},
            "_entity_alias_map": {"cxmt": ["长鑫科技", "688825"]}, "languages": ["zh-CN"],
            "document_types": ["policy"],
        }
        source = RuntimeSource(raw, "DIRECT", {})
        missed = extract_metadata(source, "https://reg.example/home", b'<html><title>Policy</title></html>', "Policy")
        matched = extract_metadata(source, "https://reg.example/search?q=688825", b'<html></html>', "Result")
        self.assertEqual(missed["review_suggestion"], "no_entity_match")
        self.assertEqual(matched["company_id"], "cxmt")
        self.assertEqual(matched["review_suggestion"], "candidate_entity_match")

    def test_search_source_landing_page_is_not_an_event_even_with_entity_hit(self):
        source = RuntimeSource({
            "source_id": "media", "base_url": "https://media.example/",
            "collection_mode": "search", "company_scope": ["cxmt"],
            "_canonical_names": {"cxmt": "长鑫科技集团股份有限公司"},
            "_entity_alias_map": {"cxmt": ["CXMT"]}, "languages": ["en"],
            "document_types": ["news"],
        }, "PROXY", {})
        metadata = extract_metadata(
            source, "https://media.example/", b'<html><title>CXMT latest</title></html>', "")
        self.assertEqual(metadata["company_id"], "cxmt")
        self.assertEqual(metadata["document_type"], "landing_page")
        self.assertEqual(metadata["review_suggestion"], "source_landing")

    def test_miit_query_response_is_not_counted_as_entity_document(self):
        source = RuntimeSource({
            "source_id": "miit", "base_url": "https://www.miit.gov.cn/",
            "collection_mode": "search", "company_scope": ["cxmt"],
            "_canonical_names": {"cxmt": "长鑫科技集团股份有限公司"},
            "_entity_alias_map": {"cxmt": ["CXMT", "长鑫存储"]},
            "languages": ["zh-CN"], "document_types": ["industry_policy"],
        }, "DIRECT", {})
        metadata = extract_metadata(
            source,
            "https://www.miit.gov.cn/search-front-server/api/search/info?q=CXMT",
            b'{"success":true,"data":{"searchResult":{"total":0}}}', "")
        self.assertEqual(metadata["review_suggestion"], "search_response")
        self.assertEqual(metadata["document_type"], "search_response")

    def test_fda_search_page_is_not_counted_as_entity_document(self):
        source = RuntimeSource({
            "source_id": "us_fda", "base_url": "https://www.fda.gov/",
            "collection_mode": "search", "company_scope": ["wuxi_apptec"],
            "_canonical_names": {"wuxi_apptec": "无锡药明康德新药开发股份有限公司"},
            "_entity_alias_map": {"wuxi_apptec": ["WuXi AppTec"]},
            "languages": ["en"], "document_types": ["inspection", "recalls"],
        }, "PROXY", {})
        metadata = extract_metadata(
            source, "https://www.fda.gov/search?s=WuXi%20AppTec",
            b'<input name="s" value="WuXi AppTec"><div>WuXi AppTec result</div>',
            "Search | FDA")
        self.assertEqual(metadata["review_suggestion"], "search_response")
        self.assertEqual(metadata["document_type"], "search_response")

    def test_configured_query_landing_is_a_search_response(self):
        source = RuntimeSource({
            "source_id": "committee",
            "base_url": "https://committee.example/?s=WuXi",
            "collection_mode": "search", "company_scope": ["wuxi_apptec"],
            "_canonical_names": {"wuxi_apptec": "无锡药明康德新药开发股份有限公司"},
            "_entity_alias_map": {"wuxi_apptec": ["WuXi AppTec", "WuXi"]},
            "languages": ["en"], "document_types": ["press_releases"],
        }, "PROXY", {})
        metadata = extract_metadata(
            source, "https://committee.example/?s=WuXi",
            b'<h1>Search results for WuXi</h1>', "You searched for WuXi")
        self.assertEqual(metadata["review_suggestion"], "search_response")
        self.assertEqual(metadata["document_type"], "search_response")

    def test_search_path_with_query_is_a_search_response(self):
        source = RuntimeSource({
            "source_id": "news", "base_url": "https://news.example/",
            "collection_mode": "search", "company_scope": ["cxmt"],
            "_canonical_names": {"cxmt": "长鑫科技集团股份有限公司"},
            "_entity_alias_map": {"cxmt": ["长鑫科技"]},
            "languages": ["zh-CN"], "document_types": ["news"],
        }, "DIRECT", {})
        metadata = extract_metadata(
            source, "https://news.example/channel/search/?k=长鑫科技",
            b"<h1>search</h1>", "文章搜索")
        self.assertEqual(metadata["review_suggestion"], "search_response")
        self.assertEqual(metadata["document_type"], "search_response")

    def test_cnipa_dynamic_query_shell_is_not_counted_as_entity_document(self):
        source = RuntimeSource({
            "source_id": "cnipa", "base_url": "https://www.cnipa.gov.cn/",
            "collection_mode": "manual", "company_scope": ["cxmt", "wuxi_apptec"],
            "languages": ["zh-CN"], "document_types": ["patents", "policy"],
        }, "DIRECT", {})
        metadata = extract_metadata(
            source,
            "https://www.cnipa.gov.cn/jsearchfront/search.do?q=长鑫科技集团股份有限公司",
            b"<html><title>search</title></html>", "搜索结果")
        self.assertIsNone(metadata["company_id"])
        self.assertEqual(metadata["review_suggestion"], "search_response")
        self.assertEqual(metadata["document_type"], "search_response")



if __name__ == "__main__":
    unittest.main()
