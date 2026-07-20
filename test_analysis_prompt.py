import unittest
import sys
import types

aiohttp_stub = types.ModuleType("aiohttp")
aiohttp_stub.ClientSession = object
sys.modules.setdefault("aiohttp", aiohttp_stub)

pyqt6_stub = types.ModuleType("PyQt6")
qtcore_stub = types.ModuleType("PyQt6.QtCore")


class QObject:
    pass


class DummySignal:
    def connect(self, *args, **kwargs):
        pass

    def emit(self, *args, **kwargs):
        pass


def pyqtSignal(*args, **kwargs):
    return DummySignal()


def pyqtSlot(*args, **kwargs):
    def decorator(fn):
        return fn
    return decorator


qtcore_stub.QObject = QObject
qtcore_stub.pyqtSignal = pyqtSignal
qtcore_stub.pyqtSlot = pyqtSlot
pyqt6_stub.QtCore = qtcore_stub
sys.modules.setdefault("PyQt6", pyqt6_stub)
sys.modules.setdefault("PyQt6.QtCore", qtcore_stub)

from core.models import SubtitleEntry
from core.workers import _build_analysis_messages, _build_analysis_result, merge_glossaries


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
        self.assertIn("[1] Welcome back", user_prompt)
        self.assertIn("[2] The delta is high", user_prompt)

    def test_analysis_prompt_samples_across_full_file(self):
        entries = [
            SubtitleEntry(i, "00:00:01,000", "00:00:02,000", [f"generic intro {i}"])
            for i in range(1, 121)
        ]
        entries[90].lines = ["volume profile point of control"]

        messages = _build_analysis_messages(entries)
        user_prompt = messages[1]["content"]

        self.assertIn("volume profile point of control", user_prompt)

    def test_analysis_result_extracts_auto_glossary(self):
        raw = """
        {
          "domain": "期权交易课程",
          "audience": "有基础的交易者",
          "tone": "专业、直接",
          "context": "讲解 Gamma Exposure",
          "translation_rules": ["保留英文缩写"],
          "term_rules": [
            {"source": "Gamma Exposure", "target": "Gamma敞口", "note": "统一译法"},
            {"source": "0DTE", "target": "当日到期期权", "note": "保留缩写时可解释"}
          ],
          "avoid": ["术语漂移"],
          "style_guide": "简洁"
        }
        """

        result = _build_analysis_result(raw)

        self.assertIn("Gamma Exposure", result.context)
        self.assertIn("Gamma Exposure->Gamma敞口", result.auto_glossary)
        self.assertIn("0DTE->当日到期期权", result.auto_glossary)

    def test_analysis_result_falls_back_to_text_pairs(self):
        raw = """
        ```json
        {"term_rules":[
          {"source":"Volume Profile","target":"成交量分布"},
        ]}
        ```
        Extra: Point of Control -> 控制点
        """

        result = _build_analysis_result(raw)

        self.assertIn("Volume Profile->成交量分布", result.auto_glossary)
        self.assertIn("Point of Control->控制点", result.auto_glossary)

    def test_merge_glossaries_keeps_user_terms_first(self):
        merged = merge_glossaries(
            "Gamma Exposure->伽马暴露",
            "Gamma Exposure->Gamma敞口\n0DTE->当日到期期权",
        )

        self.assertIn("Gamma Exposure->伽马暴露", merged.raw)
        self.assertIn("0DTE->当日到期期权", merged.raw)
        self.assertEqual(merged.added, [("0DTE", "当日到期期权")])
        self.assertEqual(merged.conflicts, [("Gamma Exposure", "伽马暴露", "Gamma敞口")])


if __name__ == "__main__":
    unittest.main()
