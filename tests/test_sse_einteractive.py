import unittest

from collector.sse_einteractive import company_uid, normalize_cn_time, parse_feed


class SSEInteractiveTests(unittest.TestCase):
    def test_company_uid_comes_from_public_feed_url(self):
        body = b'<a href="ajax/userfeeds.do?type=11&amp;uid=121066&amp;page=2"></a>'
        self.assertEqual(company_uid(body), "121066")

    def test_parse_question_answer_and_times(self):
        body = '''
        <div class="m_feed_item"><input id="currentPage" value="1"></div>
        <div class="m_feed_item" id="item-1769302">
          <div class="m_feed_txt"><a>药明康德(603259)</a>：请问经营情况？</div>
          <div class="m_feed_from"><span>2026年07月14日 15:17</span></div>
          <div class="m_feed_txt" id="m_feed_txt-1769302">您好，<br>经营稳健。</div>
          <div class="m_feed_from"><span>2026年07月28日 18:18</span></div>
        </div>'''.encode()
        self.assertEqual(parse_feed(body), [{
            "item_id": "1769302",
            "question_text": "药明康德(603259) ：请问经营情况？",
            "question_at": "2026-07-14T15:17+08:00",
            "answer_text": "您好，\n经营稳健。",
            "answer_at": "2026-07-28T18:18+08:00",
        }])

    def test_normalize_cn_time_accepts_date_without_clock(self):
        self.assertEqual(normalize_cn_time("2024年1月2日"), "2024-01-02T00:00+08:00")


if __name__ == "__main__":
    unittest.main()
