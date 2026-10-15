from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.main_window import MainWindow


def test_batch_start_directly_starts_batch_worker_without_preanalysis() -> None:
    settings = {
        "files": ["one.srt", "two.srt", "three.srt"],
        "client": object(),
        "transcribe_only": False,
    }
    started: list[dict] = []
    logs: list[str] = []

    class FakeBar:
        def setMaximum(self, val): pass
        def setValue(self, val): pass
        def clear(self): pass

    class FakeList:
        def count(self): return 0

    class FakeButton:
        def setEnabled(self, val): pass

    class FakeWindow:
        _batch_files = settings["files"]
        btn_batch_start = FakeButton()
        btn_batch_stop = FakeButton()
        batch_overall_bar = FakeBar()
        batch_log = FakeBar()
        batch_list = FakeList()

        def _batch_settings(self):
            return settings

        def _cleanup_batch_pipeline(self):
            pass

        def _batch_log_msg(self, message: str) -> None:
            logs.append(message)

        def _batch_set_item_status(self, idx, status):
            pass

        def _start_batch_worker(self, batch_settings: dict) -> None:
            started.append(batch_settings)

    window = FakeWindow()
    MainWindow._batch_start(window)

    assert started == [settings]
    assert any("开始批量处理" in message for message in logs)
    assert not hasattr(MainWindow, "_start_batch_glossary_preanalysis")
