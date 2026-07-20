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


_SAFE_BOUNDARY_RE = re.compile(r"[，。！？；：、,.!?;:\s]")


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


def _visible_target_units(text: str, output_lang_code: str) -> int:
    value = (text or "").strip()
    if not value:
        return 0
    if (output_lang_code or "").lower() in {"zh", "ja", "ko"}:
        return len([char for char in value if not char.isspace()])
    words = re.findall(r"\w+(?:['’-]\w+)?", value, flags=re.UNICODE)
    return len(words) if words else len(value)


def _source_weight(text: str, input_lang_code: str) -> int:
    value = (text or "").strip()
    if not value:
        return 1
    if (input_lang_code or "").lower() in {"zh", "ja", "ko"}:
        return max(1, len([char for char in value if not char.isspace()]))
    return max(1, len(re.findall(r"\w+(?:['’-]\w+)?", value, flags=re.UNICODE)))


def _best_cut(text: str, expected: int, lower: int, upper: int) -> int:
    if lower >= upper:
        return lower
    candidates = [
        position
        for position in range(lower, upper + 1)
        if 0 < position < len(text) and _SAFE_BOUNDARY_RE.match(text[position - 1])
    ]
    if not candidates:
        return min(max(expected, lower), upper)
    return min(candidates, key=lambda position: (abs(position - expected), position))


def split_target_by_weights(text: str, weights: list[int]) -> list[str]:
    """Split target text monotonically while preserving all non-space content."""
    value = str(text or "").strip()
    if not weights:
        return []
    if len(weights) == 1:
        return [value]
    if not value:
        return [""] * len(weights)

    positive = [max(1, int(weight or 1)) for weight in weights]
    count = len(positive)
    length = len(value)
    total = sum(positive)
    cuts: list[int] = []
    consumed_weight = 0
    previous = 0
    for index, weight in enumerate(positive[:-1]):
        consumed_weight += weight
        expected = round(length * consumed_weight / total)
        remaining_parts = count - index - 1
        # When possible, leave at least one character for every remaining cue.
        lower = previous + (1 if length >= count else 0)
        upper = length - (remaining_parts if length >= count else 0)
        cut = _best_cut(value, expected, min(lower, length), max(min(upper, length), min(lower, length)))
        cuts.append(cut)
        previous = cut

    boundaries = [0] + cuts + [length]
    return [value[boundaries[i]:boundaries[i + 1]].strip() for i in range(count)]


def _group_cues(cues: list[SourceCueSnapshot], group_count: int, input_lang_code: str) -> list[list[SourceCueSnapshot]]:
    if group_count >= len(cues):
        return [[cue] for cue in cues]
    if group_count <= 1:
        return [list(cues)]

    weights = [_source_weight(cue.source_text, input_lang_code) for cue in cues]
    total = sum(weights)
    groups: list[list[SourceCueSnapshot]] = []
    current: list[SourceCueSnapshot] = []
    consumed = 0
    next_threshold_index = 1
    for position, (cue, weight) in enumerate(zip(cues, weights)):
        current.append(cue)
        consumed += weight
        remaining_cues = len(cues) - position - 1
        remaining_groups = group_count - len(groups) - 1
        threshold = total * next_threshold_index / group_count
        if (
            len(groups) < group_count - 1
            and consumed >= threshold
            and remaining_cues >= remaining_groups
        ):
            groups.append(current)
            current = []
            next_threshold_index += 1
    if current:
        groups.append(current)
    while len(groups) > group_count:
        groups[-2].extend(groups.pop())
    return groups


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
    """Flow sentence-level translations back onto immutable source cue timing."""
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

        target_units = _visible_target_units(target_text, output_lang_code)
        group_count = min(len(cues), max(1, target_units))
        cue_groups = _group_cues(cues, group_count, input_lang_code)
        group_weights = [
            sum(_source_weight(cue.source_text, input_lang_code) for cue in group)
            for group in cue_groups
        ]
        target_parts = split_target_by_weights(target_text, group_weights)

        for group, target_part in zip(cue_groups, target_parts):
            source_text = join_source_lines(
                [cue.source_text for cue in group],
                input_lang_code,
            )
            source_ids: list[int] = []
            original_lines: list[str] = []
            for cue in group:
                source_ids.extend(cue.source_indexes or (cue.index,))
                original_lines.extend(cue.original_lines or (cue.source_text,))
            flowed = SubtitleEntry(
                index=group[0].index,
                start_time=group[0].start_time,
                end_time=group[-1].end_time,
                lines=[target_part, source_text] if source_text else [target_part],
            )
            flowed.optimized_lines = list(flowed.lines)
            flowed.original_lines = original_lines or [source_text]
            flowed.source_indexes = list(dict.fromkeys(source_ids))
            flowed.source_cues = list(group)
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
