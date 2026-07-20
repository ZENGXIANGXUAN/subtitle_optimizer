import asyncio

import pytest

from core.batch_glossary import GlossaryFileFailure
from core.workers import BatchGlossaryWorker


class DummyPipeline:
    def __init__(self, glossary_by_first_line):
        self.glossary_by_first_line = glossary_by_first_line

    async def analyze_details(self, entries, is_cancelled=None):
        first_line = entries[0].lines[0]
        return type(
            "Analysis",
            (),
            {"auto_glossary": self.glossary_by_first_line[first_line]},
        )()


def make_srt(text):
    return f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n"


def test_prepare_file_entries_reads_srt_and_extracts_original_line(tmp_path):
    path = tmp_path / "sample.srt"
    path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n译文\nsource line\n",
        encoding="utf-8",
    )
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(path)],
        concurrency=3,
        fixed_glossary="",
        original_line=1,
        input_lang="English",
        output_lang="中文",
    )

    entries = worker._prepare_file_entries(str(path))

    assert [entry.lines for entry in entries] == [["source line"]]


def test_prepare_file_entries_rejects_empty_srt(tmp_path):
    path = tmp_path / "empty.srt"
    path.write_text("", encoding="utf-8")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(path)],
        concurrency=3,
        fixed_glossary="",
        original_line=0,
        input_lang="English",
        output_lang="中文",
    )

    with pytest.raises(ValueError, match="无法解析字幕文件"):
        worker._prepare_file_entries(str(path))


def test_analyze_files_merges_successes_and_failures(tmp_path):
    good = tmp_path / "good.srt"
    bad = tmp_path / "bad.srt"
    good.write_text(make_srt("good source"), encoding="utf-8")
    bad.write_text("", encoding="utf-8")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(good), str(bad)],
        concurrency=2,
        fixed_glossary="existing->已有",
        original_line=0,
        input_lang="English",
        output_lang="中文",
    )
    pipeline = DummyPipeline({"good source": "order flow->订单流\nexisting->已有"})

    summary = asyncio.run(worker._analyze_files(pipeline))

    assert [candidate.source for candidate in summary.candidates] == ["order flow"]
    assert summary.failures == [GlossaryFileFailure("bad.srt", "无法解析字幕文件")]
    assert summary.skipped_existing == ["existing"]


def test_analyze_files_uses_sidecar_srt_for_media_without_transcribing(tmp_path):
    media = tmp_path / "lesson.mp4"
    sidecar = tmp_path / "lesson.srt"
    media.write_bytes(b"not actually a video")
    sidecar.write_text(make_srt("media source"), encoding="utf-8")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(media)],
        concurrency=2,
        fixed_glossary="",
        original_line=0,
        input_lang="English",
        output_lang="中文",
    )
    pipeline = DummyPipeline({"media source": "order flow->订单流"})

    summary = asyncio.run(worker._analyze_files(pipeline))

    assert [candidate.source for candidate in summary.candidates] == ["order flow"]
    assert summary.failures == []


def test_analyze_files_skips_untranscribed_media(tmp_path):
    media = tmp_path / "lesson.mp4"
    media.write_bytes(b"not actually a video")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(media)],
        concurrency=2,
        fixed_glossary="",
        original_line=0,
        input_lang="English",
        output_lang="中文",
    )
    pipeline = DummyPipeline({})

    summary = asyncio.run(worker._analyze_files(pipeline))

    assert summary.candidates == []
    assert summary.failures == [
        GlossaryFileFailure("lesson.mp4", "未找到同名 SRT，已跳过词汇预分析")
    ]

