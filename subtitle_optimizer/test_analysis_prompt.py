import unittest
import sys
import types

aiohttp_stub = types.ModuleType("aiohttp")
aiohttp_stub.ClientSession = object
sys.modules.setdefault("aiohttp", aiohttp_stub)

from core.models import SubtitleEntry
from core.workers import _build_analysis_messages


class AnalysisPromptTest(unittest.TestCase):
    def test_analysis_prompt_requests_structured_translation_brief(self):
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,000", ["欢迎回来", "Welcome back"]),
            SubtitleEntry(2, "00:00:02,000", "00:00:03,000", ["Delta 值很高", "The delta is high"]),
        ]

        messages = _build_analysis_messages(entries)
        system_prompt = messages[0]["content"]
        user_prompt = messages[1]["content"]

        self.assertIn('"domain"', system_prompt)
        self.assertIn('"translation_rules"', system_prompt)
        self.assertIn('"term_rules"', system_prompt)
        self.assertIn('"avoid"', system_prompt)
        self.assertIn("可执行", system_prompt)
        self.assertIn("[1] 欢迎回来 | Welcome back", user_prompt)
        self.assertIn("[2] Delta 值很高 | The delta is high", user_prompt)


if __name__ == "__main__":
    unittest.main()
