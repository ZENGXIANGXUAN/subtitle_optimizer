import asyncio
import json
import sys
import types
import unittest

from core.models import SubtitleEntry


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
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

from core.workers import _translate_batch_with_repair


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, session):
        self.calls.append(messages)
        return self.responses.pop(0)


class TranslationRepairTest(unittest.TestCase):
    def test_retries_missing_translation_ids(self):
        entries = [
            SubtitleEntry(1, "00:00:00,000", "00:00:01,000", ["first source"]),
            SubtitleEntry(2, "00:00:01,000", "00:00:02,000", ["second source"]),
        ]
        client = FakeClient([
            json.dumps([{"id": 1, "tgt": "first translated"}]),
            json.dumps([{"id": 2, "tgt": "second translated"}]),
        ])

        result, unresolved = asyncio.run(
            _translate_batch_with_repair(
                entries, "", "", client, None, "English", "English"
            )
        )

        self.assertEqual(result[1], "first translated")
        self.assertEqual(result[2], "second translated")
        self.assertEqual(unresolved, [])
        self.assertEqual(len(client.calls), 2)

    def test_reports_unresolved_ids_after_retry(self):
        entries = [
            SubtitleEntry(1, "00:00:00,000", "00:00:01,000", ["first source"]),
            SubtitleEntry(2, "00:00:01,000", "00:00:02,000", ["second source"]),
        ]
        client = FakeClient([
            json.dumps([{"id": 1, "tgt": "first translated"}]),
            json.dumps([{"id": 2, "tgt": ""}]),
        ])

        result, unresolved = asyncio.run(
            _translate_batch_with_repair(
                entries, "", "", client, None, "English", "English"
            )
        )

        self.assertEqual(result, {1: "first translated"})
        self.assertEqual(unresolved, [2])
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
