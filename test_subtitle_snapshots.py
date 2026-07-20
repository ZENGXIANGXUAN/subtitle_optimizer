import unittest

from core.models import SubtitleEntry
from core.models import _format_subtitle_snapshot


class SubtitleSnapshotTest(unittest.TestCase):
    def test_formats_all_subtitle_lines_for_debugging(self):
        entries = [
            SubtitleEntry(
                259,
                "00:13:33,700",
                "00:13:35,260",
                ["他们将不得不解套自己", "and they're going to have to unwind themselves."],
            ),
            SubtitleEntry(
                260,
                "00:13:35,260",
                "00:13:39,420",
                ["所以，当市场处于多头且你感受到这种主动性时"],
                optimized_lines=["优化后的中文", "source English"],
            ),
        ]

        snapshot = _format_subtitle_snapshot("AFTER_OPTIMIZE", entries, use_optimized=True)

        self.assertIn("AFTER_OPTIMIZE", snapshot)
        self.assertIn("259", snapshot)
        self.assertIn("00:13:33,700 --> 00:13:35,260", snapshot)
        self.assertIn("他们将不得不解套自己", snapshot)
        self.assertIn("and they're going to have to unwind themselves.", snapshot)
        self.assertIn("优化后的中文", snapshot)
        self.assertIn("source English", snapshot)


if __name__ == "__main__":
    unittest.main()
