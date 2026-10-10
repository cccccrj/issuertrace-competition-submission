import unittest

from collector.csrc_search import result_links


class CsrcSearchTests(unittest.TestCase):
    def test_only_result_title_anchors_are_extracted(self):
        body = '''<a href="//www.csrc.gov.cn/csrc/c105906/c7638905/content.shtml\n"
          class="fl titleFont permitT" title="长鑫 IPO 批复">长鑫<span>科技</span></a>
        <span class="sourceDateFont">2026-06-12</span>
        <a href="/csrc/c100025/c1003151/content.shtml">联系我们</a>'''.encode()
        self.assertEqual(result_links(body), [
            ("https://www.csrc.gov.cn/csrc/c105906/c7638905/content.shtml", "长鑫 IPO 批复", "2026-06-12"),
        ])


if __name__ == "__main__":
    unittest.main()
