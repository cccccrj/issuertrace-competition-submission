import sqlite3
import unittest
from datetime import datetime, timezone

from collector.ijiwei_latest import (
    ensure_schema, entity_match, parse_detail_metadata, parse_latest, seed_due,
)


class IjiweiLatestTests(unittest.TestCase):
    def test_entity_matching_requires_strong_alias_or_memory_context(self):
        self.assertTrue(entity_match("长鑫科技IPO进展"))
        self.assertTrue(entity_match("CXMT expands DRAM output"))
        self.assertTrue(entity_match("长鑫LPDDR6芯片进入测试"))
        self.assertFalse(entity_match("长鑫今日发布消息"))
        self.assertFalse(entity_match("长江存储发布新品"))

    def test_parser_uses_only_same_host_numeric_news_cards(self):
        body = b'''<ul>
          <li class="card" data-href="/n/100"><p class="title">CXMT expands DRAM</p><p class="intro">Summary</p></li>
          <li class="card" data-href="/n/101"><p class="title">Other vendor</p></li>
          <li class="card" data-href="/n/102"><p class="title">\xe9\x95\xbf\xe9\x91\xabLPDDR6\xe8\x8a\xaf\xe7\x89\x87</p></li>
          <li class="card" data-href="https://outside.example/n/103"><p class="title">CXMT outside</p></li>
        </ul>'''
        visible, items = parse_latest(body)
        self.assertEqual(visible, 3)
        self.assertEqual([item.url for item in items], [
            "https://www.ijiwei.com/n/100",
            "https://www.ijiwei.com/n/102",
        ])

    def test_seed_refresh_is_daily(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        self.assertTrue(seed_due(db, now))
        db.execute("""UPDATE ijiwei_latest_state
            SET last_seed_fetched_at='2026-08-24T12:00:00+00:00'""")
        self.assertFalse(seed_due(db, now))
        self.assertTrue(seed_due(db, datetime(2026, 8, 25, 13, tzinfo=timezone.utc)))
        db.close()

    def test_detail_metadata_uses_embedded_publication_timestamp(self):
        body = '''<h1 class="media-title">长鑫 LPDDR6 进展</h1>
        <script>window.data={"published_time":"20260825073412"}</script>'''.encode()
        self.assertEqual(parse_detail_metadata(body),
                         ("长鑫 LPDDR6 进展", "2026-08-25"))


if __name__ == "__main__":
    unittest.main()
