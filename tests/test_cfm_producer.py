import sqlite3
import unittest
from datetime import datetime, timezone

from collector.cfm_producer import ensure_schema, parse_producer_items, seed_fetch_due


class CFMProducerTests(unittest.TestCase):
    def test_parses_only_dated_detail_and_newsflash_links(self):
        body = '''<div class="newsli clearfix"><h3><a href="/a/184063">长鑫科技发行价</a></h3>
        <span class="add-time gray">2026-07-15</span><p>摘要</p></div>
        <div class="flash-item clearfix"><span class="time">2026-08-24 16:02</span>
        <a href="/newsflash/38657"><p>苹果测试长鑫产品</p></a></div>
        <div class="newsli"><a href="https://outside.example/a/1">外站</a><span>2026-01-01</span></div>'''.encode()
        items = parse_producer_items(body, "https://www.chinaflashmarket.com/producer/cxmt")
        self.assertEqual([(item.url, item.title, item.published_at, item.document_type)
                          for item in items], [
            ("https://www.chinaflashmarket.com/a/184063", "长鑫科技发行价",
             "2026-07-15", "company_updates"),
            ("https://www.chinaflashmarket.com/newsflash/38657", "苹果测试长鑫产品",
             "2026-08-24", "market_newsflash"),
        ])

    def test_seed_fetch_cadence_is_independent(self):
        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        self.assertTrue(seed_fetch_due(db, now))
        db.execute("""INSERT INTO cfm_producer_state
            (source_id,last_seed_fetched_at) VALUES('chinaflashmarket','2026-08-24T12:00:00+00:00')""")
        self.assertFalse(seed_fetch_due(db, now))
        self.assertTrue(seed_fetch_due(db, datetime(2026, 8, 25, 13, tzinfo=timezone.utc)))
        db.close()


if __name__ == "__main__":
    unittest.main()
