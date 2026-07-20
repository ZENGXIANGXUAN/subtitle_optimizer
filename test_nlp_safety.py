import unittest
from core.models import SubtitleEntry
from core.segmenter import _nlp_merge_is_safe


class NlpMergeSafetyTest(unittest.TestCase):
    """测试 _nlp_merge_is_safe 的安全检查规则"""

    # --- R3: 短条目保护 ---

    def test_rejects_entry_with_3_words_or_fewer(self):
        """<=3词的条目应被拒绝合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,000",
                          ["你好", "Hi there."]),  # 2词
            SubtitleEntry(2, "00:00:02,000", "00:00:03,500",
                          ["这是测试", "This is a test."]),  # 5词
        ]
        self.assertFalse(_nlp_merge_is_safe(entries))

    def test_allows_entry_with_4_words(self):
        """4词的条目应被允许合并（如果其他条件满足）"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["很受欢迎", "and it's very popular"]),  # 4词, 无句号
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["很多人用", "Many people use it"]),  # 4词, 无句号
        ]
        self.assertTrue(_nlp_merge_is_safe(entries))

    def test_rejects_entry_with_duration_500ms_or_less(self):
        """时长<=500ms的条目应被拒绝合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:01,400",  # 400ms
                          ["测试文本", "Test text here."]),  # 3词
            SubtitleEntry(2, "00:00:01,400", "00:00:03,000",
                          ["更多文本", "More text here."]),  # 3词
        ]
        self.assertFalse(_nlp_merge_is_safe(entries))

    def test_allows_entry_with_duration_501ms(self):
        """时长501ms的条目应被允许合并（如果其他条件满足）"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:01,600",  # 600ms
                          ["这是测试", "This is a test"]),  # 4词, 无句号
            SubtitleEntry(2, "00:00:01,600", "00:00:03,000",
                          ["更多内容", "More content here for testing"]),  # 5词, 无句号
        ]
        self.assertTrue(_nlp_merge_is_safe(entries))

    # --- R4: 逗号结尾检查 ---

    def test_allows_entry_ending_with_comma(self):
        """以逗号结尾的条目应被允许合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["周二", "Today is Tuesday morning,"]),  # 4词, 逗号结尾
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["十月三十一号", "October 31st is here"]),  # 4词, 无句号
        ]
        self.assertTrue(_nlp_merge_is_safe(entries))

    def test_rejects_short_sentence_ending_with_period(self):
        """以句号结尾且<=15词的条目应被拒绝合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["市场结构复杂", "The market is complex."]),  # 4词, 句号
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["交易者需要理解", "Traders need to understand."]),  # 4词
        ]
        self.assertFalse(_nlp_merge_is_safe(entries))

    def test_allows_long_sentence_ending_with_period(self):
        """以句号结尾且>15词的条目应被允许合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:04,000",
                          ["长句", "This is a very long sentence that has "
                           "more than fifteen words and ends with a period."]),  # 19词
            SubtitleEntry(2, "00:00:04,000", "00:00:06,000",
                          ["下一句", "And here comes the next sentence"]),  # 6词, 无句号
        ]
        self.assertTrue(_nlp_merge_is_safe(entries))


if __name__ == "__main__":
    unittest.main()
