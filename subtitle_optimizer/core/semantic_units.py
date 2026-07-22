"""Semantic-unit metadata and monotonic subtitle reflow helpers.

Translation should operate on a complete semantic sentence, while display cues
must retain the source timeline. The helpers here keep those two concerns
separate and are deterministic so they can be covered by regression tests.
"""

from __future__ import annotations

import re
from typing import Iterable

from core.models import SourceCueSnapshot, SubtitleEntry
from core.segmenter import _get_source_text
from core.source_normalization import join_source_lines


def cue_snapshot(entry: SubtitleEntry, input_lang_code: str = "en") -> SourceCueSnapshot:
    return SourceCueSnapshot(
        index=entry.index,
        start_time=entry.start_time,
        end_time=entry.end_time,
        source_text=_get_source_text(entry, input_lang_code).strip(),
        original_lines=tuple(entry.original_lines or entry.lines),
        source_indexes=tuple(entry.source_indexes or [entry.index]),
    )


def ensure_source_cues(
    entries: Iterable[SubtitleEntry],
    input_lang_code: str = "en",
) -> None:
    """Attach immutable source snapshots before any semantic merge mutates cues."""
    for entry in entries:
        if not entry.source_cues:
            entry.source_cues = [cue_snapshot(entry, input_lang_code)]


def merge_source_cues(entries: Iterable[SubtitleEntry], input_lang_code: str = "en") -> list[SourceCueSnapshot]:
    merged: list[SourceCueSnapshot] = []
    seen: set[tuple[int, str, str, str]] = set()
    for entry in entries:
        values = entry.source_cues or [cue_snapshot(entry, input_lang_code)]
        for value in values:
            key = (value.index, value.start_time, value.end_time, value.source_text)
            if key not in seen:
                seen.add(key)
                merged.append(value)
    return merged


def source_cue_signature(cues: Iterable[SourceCueSnapshot]) -> tuple[tuple, ...]:
    return tuple(
        (
            cue.index,
            cue.start_time,
            cue.end_time,
            re.sub(r"\s+", " ", cue.source_text).strip(),
            tuple(cue.source_indexes),
        )
        for cue in cues
    )


def _effective_lines(entry: SubtitleEntry, use_optimized: bool) -> list[str]:
    values = entry.optimized_lines if use_optimized and entry.optimized_lines else entry.lines
    return [str(value).strip() for value in values if str(value).strip()]


def _copy_quality_metadata(source: SubtitleEntry, target: SubtitleEntry) -> None:
    target.status = source.status
    target.error_msg = source.error_msg
    target.stage_status = source.stage_status
    target.quality_flags = list(source.quality_flags)


def reflow_semantic_entries(
    entries: list[SubtitleEntry],
    *,
    input_lang_code: str = "en",
    output_lang_code: str = "zh",
    use_optimized: bool = True,
) -> list[SubtitleEntry]:
    """Place a complete semantic translation onto its immutable cue span.

    A translation can cover several source cues, but character length cannot
    establish which target characters belong to each cue.  Do not split the
    target without an explicit validated cue-to-target mapping: emitting the
    complete target over the combined span is safer than producing a name,
    number, or punctuation as a false translation.
    """
    _ = output_lang_code  # Retained for the stable public API and future mappings.
    result: list[SubtitleEntry] = []
    for entry in entries:
        lines = _effective_lines(entry, use_optimized)
        target_text = lines[0] if len(lines) >= 2 else ""
        cues = list(entry.source_cues) or [cue_snapshot(entry, input_lang_code)]

        # Do not invent a target for a failed translation. Preserve the source
        # and quality flag so the export gate can block it explicitly.
        if not target_text:
            clone = SubtitleEntry(
                entry.index,
                entry.start_time,
                entry.end_time,
                list(lines or entry.lines),
            )
            clone.original_lines = list(entry.original_lines or entry.lines)
            clone.source_indexes = list(entry.source_indexes or [entry.index])
            clone.source_cues = cues
            _copy_quality_metadata(entry, clone)
            result.append(clone)
            continue

        source_text = join_source_lines(
            [cue.source_text for cue in cues],
            input_lang_code,
        )
        source_ids: list[int] = []
        original_lines: list[str] = []
        for cue in cues:
            source_ids.extend(cue.source_indexes or (cue.index,))
            original_lines.extend(cue.original_lines or (cue.source_text,))
        flowed = SubtitleEntry(
            index=cues[0].index,
            start_time=cues[0].start_time,
            end_time=cues[-1].end_time,
            lines=[target_text, source_text] if source_text else [target_text],
        )
        flowed.optimized_lines = list(flowed.lines)
        flowed.original_lines = original_lines or [source_text]
        flowed.source_indexes = list(dict.fromkeys(source_ids))
        flowed.source_cues = cues
        _copy_quality_metadata(entry, flowed)
        result.append(flowed)

    return result


def source_integrity_errors(
    original_cues: Iterable[SourceCueSnapshot],
    entries: Iterable[SubtitleEntry],
    input_lang_code: str = "en",
) -> list[str]:
    """Return human-readable errors when reflow loses/reorders source cues."""
    expected = source_cue_signature(original_cues)
    actual_cues: list[SourceCueSnapshot] = []
    for entry in entries:
        actual_cues.extend(entry.source_cues or [cue_snapshot(entry, input_lang_code)])
    actual = source_cue_signature(actual_cues)
    if actual == expected:
        return []
    return [
        "语义翻译回流后的原始 cue 顺序或文本发生变化；已阻止将该结果视为高质量完成。"
    ]
