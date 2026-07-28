"""Regression tests for the audio chunk -> transcribe -> merge chain.

These cover holes found in the 2026-07-26 audit:

* a middle chunk is cut once from the left and once from the right by two
  independent decisions, which can cross and drop the chunk entirely;
* a chunk that failed transcription was replaced by an empty transcript, so the
  caller received a "successful" SRT with a silent multi-minute hole;
* ``overlap >= chunk_duration`` made ``_compute_chunk_starts`` loop forever;
* ``CHUNK_MAX_FILE_MB`` was declared but never enforced, so wav chunks were
  27.5 MB against a stated 8 MB limit.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import transcription as T


def _relative(segments, chunk_start):
    """Convert absolute (start, end, text) triples into one chunk's response."""
    return {
        "segments": [
            {"start": s - chunk_start, "end": e - chunk_start, "text": t}
            for s, e, t in segments
        ]
    }


# --------------------------------------------------------------------------
# merge: never drop speech no other chunk transcribed
# --------------------------------------------------------------------------


def test_middle_chunk_spanning_both_overlaps_is_not_dropped() -> None:
    """One long ASR segment used to invert the chunk's keep-range to [0:0]."""
    chunk_starts = [0.0, 780.0, 1560.0]
    merged = T._merge_chunk_segments(
        [
            _relative([(100, 110, "c0-a"), (800, 860, "c0-tail")], 0.0),
            # A single segment covering almost the whole chunk: happens with
            # music, continuous speech, or a bad VAD.
            _relative([(790, 1700, "giant")], 780.0),
            _relative([(1600, 1610, "c2-a"), (1700, 1710, "c2-b")], 1560.0),
        ],
        chunk_starts,
        120.0,
    )
    texts = [segment["text"] for segment in merged]
    assert "giant" in texts, "11 minutes of speech existed in no other chunk"


def test_a_cue_another_chunk_already_covers_is_still_dropped() -> None:
    """Restoration is coverage-based, so real duplicates stay removed."""
    merged = T._merge_chunk_segments(
        [
            {"segments": [{"start": 3, "end": 4, "text": "older"}]},
            {"segments": [{"start": 1, "end": 2, "text": "newer"}]},
        ],
        chunk_starts=[0, 2],
        overlap=3,
    )
    assert [segment["text"] for segment in merged] == ["newer"]


def test_overlapping_near_duplicate_speech_keeps_the_complete_cue() -> None:
    """ASR can return a long cue plus a slightly shorter repeat at a handoff."""
    segments = [
        {
            "start": 3227.4,
            "end": 3236.3,
            "text": (
                "So, on its own, it doesn't mean something, but the fact "
                "that there's a strong relative change in volume, that is "
                "something we want to identify and pay attention to."
            ),
        },
        {
            "start": 3228.4,
            "end": 3236.2,
            "text": (
                "on its own it doesn't mean something but the fact is a "
                "strong relative change in volume that is something we want "
                "to identify and pay attention to"
            ),
        },
    ]

    deduped = T._dedupe_adjacent_segments(segments)

    assert len(deduped) == 1
    assert deduped[0]["start"] == pytest.approx(3227.4)
    assert deduped[0]["end"] == pytest.approx(3236.3)
    assert "there's a strong relative change" in deduped[0]["text"]


def test_overlapping_different_speech_is_not_collapsed() -> None:
    segments = [
        {"start": 0.0, "end": 4.0, "text": "The market has opened."},
        {"start": 3.0, "end": 7.0, "text": "We now wait for confirmation."},
    ]

    assert T._dedupe_adjacent_segments(segments) == segments


def test_merged_output_has_no_timeline_gap_across_three_chunks() -> None:
    chunk_starts = [0.0, 780.0, 1560.0]
    chunks = []
    for start, span in zip(chunk_starts, (900.0, 900.0, 900.0)):
        segments = [
            (start + t, start + t + 10, f"t{int(start + t):06d}")
            for t in range(0, int(span), 10)
        ]
        chunks.append(_relative(segments, start))

    merged = T._merge_chunk_segments(chunks, chunk_starts, 120.0)

    union = T._covered_intervals(merged)
    assert len(union) == 1, f"timeline is not contiguous: {union}"
    covered = union[0][1] - union[0][0]
    assert covered == pytest.approx(2460.0), covered


def test_uncovered_duration_accounts_for_partial_overlap() -> None:
    union = T._covered_intervals(
        [{"start": 0, "end": 10}, {"start": 20, "end": 30}]
    )
    assert T._uncovered_duration({"start": 0, "end": 10}, union) == 0
    assert T._uncovered_duration({"start": 5, "end": 25}, union) == pytest.approx(10)
    assert T._uncovered_duration({"start": 40, "end": 50}, union) == pytest.approx(10)


# --------------------------------------------------------------------------
# chunk planning
# --------------------------------------------------------------------------


def test_overlap_at_or_above_chunk_duration_no_longer_hangs() -> None:
    with pytest.raises(ValueError, match="must be smaller than"):
        T._compute_chunk_starts(3600.0, 900.0, 900.0)
    with pytest.raises(ValueError, match="must be smaller than"):
        T._compute_chunk_starts(3600.0, 900.0, 1000.0)


def test_clamp_chunk_plan_always_leaves_a_positive_stride() -> None:
    for requested_overlap in (0.0, 120.0, 900.0, 5000.0):
        duration, overlap = T._clamp_chunk_plan(900.0, requested_overlap, "mp3")
        assert overlap < duration
        # must not raise
        T._compute_chunk_starts(3600.0, duration, overlap)


@pytest.mark.parametrize("audio_format", ["mp3", "wav"])
def test_clamp_chunk_plan_enforces_the_declared_size_limit(audio_format: str) -> None:
    duration, _overlap = T._clamp_chunk_plan(
        T.CHUNK_DURATION, T.CHUNK_OVERLAP, audio_format
    )
    megabytes = duration * T._encoded_bytes_per_second(audio_format) / (1024 * 1024)
    assert megabytes <= T.CHUNK_MAX_FILE_MB + 1e-6, (
        f"{audio_format} chunk is {megabytes:.1f} MB"
    )


def test_wav_is_the_format_that_needed_clamping() -> None:
    """Documents why the guard exists: mp3 fit already, wav was 3x over."""
    unclamped_wav = T.CHUNK_DURATION * T._encoded_bytes_per_second("wav") / (1024 * 1024)
    unclamped_mp3 = T.CHUNK_DURATION * T._encoded_bytes_per_second("mp3") / (1024 * 1024)
    assert unclamped_mp3 < T.CHUNK_MAX_FILE_MB < unclamped_wav


# --------------------------------------------------------------------------
# failed chunks must not become a silent hole
# --------------------------------------------------------------------------


class _ChunkClient:
    """Fails the given chunk indexes for the first ``fail_times`` attempts."""

    def __init__(self, failing: set[int], fail_times: int = 10**9) -> None:
        self.failing = failing
        self.fail_times = fail_times
        self.attempts: dict[int, int] = {}

    async def transcribe_audio(self, audio_path, model=None, language=None, on_log=None):
        index = int(Path(audio_path).stem.split("_")[1])
        self.attempts[index] = self.attempts.get(index, 0) + 1
        if index in self.failing and self.attempts[index] <= self.fail_times:
            raise RuntimeError(f"simulated gateway failure for chunk {index}")
        return {
            "segments": [{"start": 0, "end": 10, "text": f"chunk-{index}"}],
            "text": f"chunk-{index}",
        }


@pytest.fixture()
def _stub_ffmpeg(monkeypatch, tmp_path):
    """Replace the ffmpeg/ffprobe calls; the chain's logic is what we test."""
    monkeypatch.setattr(T, "get_audio_duration", lambda path: 2400.0)
    monkeypatch.setattr(
        T, "prepare_transcription_audio", lambda path, **kw: (str(path), None)
    )

    def fake_extract(audio_path, start, duration, output_path, audio_format="mp3"):
        Path(output_path).write_bytes(b"\0" * 16)
        return output_path

    monkeypatch.setattr(T, "extract_audio_chunk", fake_extract)


def test_a_permanently_failing_chunk_refuses_instead_of_writing_a_hole(
    _stub_ffmpeg, tmp_path
) -> None:
    client = _ChunkClient(failing={1})
    source = tmp_path / "input.mp3"
    source.write_bytes(b"\0" * 16)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            T.transcribe_chunked_to_srt(
                client,
                str(source),
                output_path=str(tmp_path / "out.srt"),
                concurrency=2,
            )
        )

    message = str(excinfo.value)
    assert "missing" in message
    assert "min" in message, f"the failing span must be named: {message}"
    assert not (tmp_path / "out.srt").exists(), "no partial SRT may be written"


def test_a_transient_chunk_failure_is_retried_sequentially(
    _stub_ffmpeg, tmp_path
) -> None:
    client = _ChunkClient(failing={1}, fail_times=1)
    source = tmp_path / "input.mp3"
    source.write_bytes(b"\0" * 16)

    srt_path, content = asyncio.run(
        T.transcribe_chunked_to_srt(
            client,
            str(source),
            output_path=str(tmp_path / "out.srt"),
            concurrency=2,
        )
    )

    assert Path(srt_path).exists()
    assert "chunk-1" in content, "the recovered chunk must appear in the transcript"
    assert client.attempts[1] == 2, "chunk 1 should have been retried exactly once"
