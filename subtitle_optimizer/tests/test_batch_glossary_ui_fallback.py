from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.batch_glossary import BatchGlossarySummary, GlossaryCandidate
from ui.main_window import MainWindow


def test_auto_review_fallback_starts_formal_batch_without_promoting_candidates() -> None:
    settings = {
        "files": ["one.srt", "two.srt", "three.srt"],
        "manual_glossary_review": False,
    }
    started: list[dict] = []
    fixed_appends: list[str] = []
    logs: list[str] = []

    class FakeWindow:
        _batch_glossary_settings = settings

        def _batch_log_msg(self, message: str) -> None:
            logs.append(message)

        def _append_fixed_glossary_text(self, glossary: str) -> None:
            fixed_appends.append(glossary)

        def _start_batch_worker(self, batch_settings: dict) -> None:
            started.append(batch_settings)

        def _reset_batch_controls_after_preanalysis_stop(self) -> None:
            raise AssertionError("safe self-review fallback must not stop the batch")

    summary = BatchGlossarySummary(
        candidates=[
            GlossaryCandidate(
                source="gamma exposure",
                target="伽马敞口",
                files=("one.srt",),
                status="需确认",
            )
        ],
        skipped_existing=[],
        failures=[],
        session_glossary="gamma exposure->伽马敞口",
        fixed_glossary="",
    )

    MainWindow._after_batch_glossary_preanalysis(FakeWindow(), summary)

    assert started == [settings]
    assert settings["session_glossary"] == summary.session_glossary
    assert fixed_appends == []
    assert any("本次批量临时术语 1 条" in message for message in logs)
