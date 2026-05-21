import unittest
from core.models import SubtitleEntry
from core.segmenter import nlp_segment


class NlpSegmenterTest(unittest.TestCase):
    def test_complete_sentences_kept_separate(self):
        """完整句子保持独立，不合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["市场结构很复杂", "The market structure is complex."]),
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["交易者需要理解这一点", "Traders need to understand this."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].index, 1)
        self.assertEqual(result[1].index, 2)

    def test_fragments_merged_into_one(self):
        """英文半句碎片合并为一条"""
        entries = [
            SubtitleEntry(5, "00:00:10,000", "00:00:11,500",
                          ["市场结构", "The market structure is"]),
            SubtitleEntry(6, "00:00:11,500", "00:00:13,000",
                          ["由三部分组成", "composed of three key elements."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].index, 5)
        self.assertIn("The market structure is", result[0].english)
        self.assertIn("composed of three key elements", result[0].english)

    def test_empty_entries_dropped(self):
        """中英文为空的条目被丢弃"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,000",
                          ["正常字幕", "This is a normal subtitle."]),
            SubtitleEntry(2, "00:00:02,000", "00:00:03,000",
                          ["", ""]),
            SubtitleEntry(3, "00:00:03,000", "00:00:04,000",
                          ["另一条", "Another one here."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 2)
        ids = [e.index for e in result]
        self.assertIn(1, ids)
        self.assertIn(3, ids)
        self.assertNotIn(2, ids)

    def test_merged_timespan_matches_first_and_last(self):
        """合并后时间轴：start 取首条，end 取末条"""
        entries = [
            SubtitleEntry(1, "00:01:00,000", "00:01:01,500",
                          ["开头", "The beginning of"]),
            SubtitleEntry(2, "00:01:01,500", "00:01:03,000",
                          ["结尾", "the sentence."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].start_time, "00:01:00,000")
        self.assertEqual(result[0].end_time, "00:01:03,000")

    def test_long_complete_sentence_not_merged(self):
        """长完整句子即使语义相关也不合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:05,000",
                          ["长句A", "This is a very long complete sentence that "
                           "has more than twenty words and ends with proper "
                           "punctuation and is grammatically sound."]),
            SubtitleEntry(2, "00:00:05,000", "00:00:09,000",
                          ["长句B", "Another very long complete sentence that "
                           "also has more than twenty words and should stay "
                           "independent and separate from the previous one."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
