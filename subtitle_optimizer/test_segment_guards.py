import unittest

from core.models import SubtitleEntry, _merge_candidate_is_safe, _translation_looks_incomplete


class SegmentGuardTest(unittest.TestCase):
    def test_detects_short_chinese_for_long_english_as_incomplete(self):
        self.assertTrue(
            _translation_looks_incomplete(
                "他们必须自己解套",
                "time is a key proponent in the auction process. If a market is unable to move prices "
                "in the direction it is going, and over time, eventually, those positions are going to "
                "fall underwater, and they are going to have to unwind themselves.",
            )
        )

    def test_rejects_merge_that_spans_too_much_time(self):
        batch = [
            SubtitleEntry(255, "00:13:19,800", "00:13:24,000", ["a"]),
            SubtitleEntry(256, "00:13:24,000", "00:13:28,000", ["b"]),
            SubtitleEntry(257, "00:13:28,000", "00:13:35,260", ["c"]),
        ]

        self.assertFalse(
            _merge_candidate_is_safe(
                batch,
                {
                    "id": 255,
                    "start": "00:13:19,800",
                    "end": "00:13:35,260",
                    "merged_from": [255, 256, 257],
                    "en": " ".join(["word"] * 20),
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
