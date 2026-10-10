import sqlite3
import unittest

from collector.local_environment_search import page_count, result_items, search_url, store_hint


class LocalEnvironmentSearchTests(unittest.TestCase):
    def test_exact_result_cards_include_date_and_type(self):
        body = '''
        <ul class="search-list search-gk">
          <li class="search-title"><a href="https://sthjt.ah.gov.cn/public/21691/123.html"
            title="关于长鑫存储技术有限公司危险废物跨省转移的许可意见">
            关于<span>长鑫存储技术有限公司</span>危险废物许可</a></li>
          <li><span class="date">发文日期：2026-03-24</span></li>
        </ul>
        <ul class="search-list search-gk"><li><a href="https://evil.example/a.html"
          title="长鑫存储技术有限公司">other host</a></li></ul>
        <script>pageCount: 2, currPage: 1</script>'''.encode()
        self.assertEqual(result_items(body, "长鑫存储技术有限公司"), [
            ("https://sthjt.ah.gov.cn/public/21691/123.html",
             "关于长鑫存储技术有限公司危险废物跨省转移的许可意见",
             "2026-03-24", "environmental_permission"),
        ])
        self.assertEqual(page_count(body), 2)

    def test_search_url_is_exact_and_paged(self):
        url = search_url("长鑫存储技术有限公司", 3)
        self.assertIn("fuzzySearch=false", url)
        self.assertIn("pageIndex=3", url)
        self.assertIn("pageSize=10", url)

    def test_subsidiary_term_is_preserved_as_matched_legal_entity(self):
        db = sqlite3.connect(":memory:")
        db.execute("""CREATE TABLE discovered_metadata(
            source_id TEXT,canonical_url TEXT,title TEXT,published_at TEXT,company_id TEXT,
            matched_legal_entity TEXT,document_type TEXT,original_timezone TEXT,
            review_status TEXT,UNIQUE(source_id,canonical_url))""")
        config = {"entities": {"cxmt": {"canonical_name": "长鑫科技集团股份有限公司"}}}
        store_hint(db, config, "cxmt", "https://sthjt.ah.gov.cn/public/1.html",
                   "许可意见", "2026-03-24", "environmental_permission",
                   "长鑫存储技术有限公司")
        self.assertEqual(db.execute("""SELECT company_id,matched_legal_entity,review_status
            FROM discovered_metadata""").fetchone(),
                         ("cxmt", "长鑫存储技术有限公司", "candidate_entity_match"))
        db.close()


if __name__ == "__main__":
    unittest.main()
