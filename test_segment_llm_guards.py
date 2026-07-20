import unittest
import asyncio
import sys
import types


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    aiohttp_stub.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp_stub

if "PyQt6.QtCore" not in sys.modules:
    pyqt_stub = types.ModuleType("PyQt6")
    qtcore_stub = types.ModuleType("PyQt6.QtCore")

    class QObject:
        pass

    class _Signal:
        def emit(self, *args, **kwargs):
            pass

    def pyqtSignal(*args, **kwargs):
        return _Signal()

    def pyqtSlot(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    qtcore_stub.QObject = QObject
    qtcore_stub.pyqtSignal = pyqtSignal
    qtcore_stub.pyqtSlot = pyqtSlot
    sys.modules["PyQt6"] = pyqt_stub
    sys.modules["PyQt6.QtCore"] = qtcore_stub

from core.models import SubtitleEntry, _source_text_length
from core.pipeline import SubtitlePipeline
from core.segmenter import _get_source_text
from core.workers import (
    MAX_SEGMENT_SOURCE_LENGTH,
    _apply_llm_merge,
    _apply_llm_split,
)


def _words(count: int) -> str:
    return " ".join(f"word{i}" for i in range(count))


class FakeSplitClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, session):
        self.calls += 1
        return self.responses.pop(0)


class HangingClient:
    async def chat(self, messages, session):
        await asyncio.sleep(3600)


class SegmentLlmGuardTest(unittest.TestCase):
    def test_split_guard_breaks_overlong_tail_from_llm_response(self):
        text = _words(45)
        entry = SubtitleEntry(1, "00:00:00,000", "00:00:09,000", [text])

        result = _apply_llm_split(
            [entry],
            [{"id": 1, "break_after_word": [20]}],
            lang_code="en",
        )

        self.assertEqual(len(result), 3)
        self.assertTrue(
            all(
                _source_text_length(_get_source_text(e, "en"), "en")
                <= MAX_SEGMENT_SOURCE_LENGTH
                for e in result
            )
        )
        self.assertEqual(
            " ".join(_get_source_text(e, "en") for e in result),
            text,
        )

    def test_split_guard_handles_empty_llm_breaks(self):
        text = _words(41)
        entry = SubtitleEntry(7, "00:00:00,000", "00:00:08,000", [text])

        result = _apply_llm_split(
            [entry],
            [{"id": 7, "break_after_word": []}],
            lang_code="en",
        )

        self.assertEqual(len(result), 3)
        self.assertTrue(
            all(
                _source_text_length(_get_source_text(e, "en"), "en")
                <= MAX_SEGMENT_SOURCE_LENGTH
                for e in result
            )
        )

    def test_pipeline_recursively_asks_llm_for_overlong_tail(self):
        text = _words(45)
        entry = SubtitleEntry(1, "00:00:00,000", "00:00:09,000", [text])
        client = FakeSplitClient([
            '[{"id": 1, "break_after_word": [20]}]',
            '[{"id": -10002, "break_after_word": [13]}]',
        ])
        pipeline = SubtitlePipeline(client)

        async def run():
            return await pipeline._split_long_entries_with_llm(
                [entry],
                "",
                None,
            )

        result = asyncio.run(run())

        self.assertEqual(client.calls, 2)
        self.assertEqual(len(result), 3)
        self.assertTrue(
            all(
                _source_text_length(_get_source_text(e, "en"), "en")
                <= MAX_SEGMENT_SOURCE_LENGTH
                for e in result
            )
        )
        self.assertEqual(
            " ".join(_get_source_text(e, "en") for e in result),
            text,
        )

    def test_optimize_batch_timeout_does_not_block_stage(self):
        entries = [
            SubtitleEntry(1, "00:00:00,000", "00:00:01,000", ["译文", "source one"]),
            SubtitleEntry(2, "00:00:01,000", "00:00:02,000", ["译文", "source two"]),
        ]
        pipeline = SubtitlePipeline(HangingClient(), batch_size=2, concurrency=1)
        pipeline.optimize_batch_timeout_seconds = 0.01

        result = asyncio.run(pipeline.optimize(entries, "", ""))

        self.assertIs(result, entries)
        self.assertEqual([e.status for e in entries], ["error", "error"])
        self.assertEqual(
            [e.error_msg for e in entries],
            ["optimize batch timed out", "optimize batch timed out"],
        )

    def test_merge_guard_rejects_overlong_llm_group(self):
        batch = [
            SubtitleEntry(1, "00:00:00,000", "00:00:02,000", [_words(25)]),
            SubtitleEntry(2, "00:00:02,000", "00:00:04,000", [_words(25)]),
            SubtitleEntry(3, "00:00:04,000", "00:00:06,000", [_words(25)]),
        ]

        result = _apply_llm_merge(batch, [{"ids": [1, 2, 3]}], lang_code="en")

        self.assertEqual([e.index for e in result], [1, 2, 3])

    def test_rejected_merge_group_is_not_logged_as_missing(self):
        batch = [
            SubtitleEntry(1, "00:00:00,000", "00:00:02,000", [_words(25)]),
            SubtitleEntry(2, "00:00:02,000", "00:00:04,000", [_words(25)]),
            SubtitleEntry(3, "00:00:04,000", "00:00:06,000", [_words(25)]),
        ]
        logs = []

        _apply_llm_merge(
            batch,
            [{"ids": [1, 2, 3]}],
            log_fn=logs.append,
            lang_code="en",
        )

        joined = "\n".join(logs)
        self.assertIn("拒绝不安全合并组", joined)
        self.assertIn("来自被拒绝的合并组", joined)
        self.assertNotIn("LLM未返回条目", joined)

    def test_merge_guard_rejects_non_contiguous_llm_group(self):
        batch = [
            SubtitleEntry(1, "00:00:00,000", "00:00:02,000", ["first part"]),
            SubtitleEntry(2, "00:00:02,000", "00:00:04,000", ["middle part"]),
            SubtitleEntry(3, "00:00:04,000", "00:00:06,000", ["last part"]),
        ]

        result = _apply_llm_merge(batch, [{"ids": [1, 3]}], lang_code="en")

        self.assertEqual([e.index for e in result], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
