"""Semantic-unit metadata and monotonic subtitle reflow helpers.

Translation should operate on a complete semantic sentence, while display cues
must retain the source timeline. The helpers here keep those two concerns
separate and are deterministic so they can be covered by regression tests.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Mapping

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


def has_validated_cue_target_mapping(
    entry: SubtitleEntry,
    cue_targets: Mapping[int, str] | None,
    *,
    target_is_usable: Callable[[str, str], bool] | None = None,
) -> bool:
    """Whether a model supplied an unambiguous target for every source cue.

    Character positions cannot prove semantic alignment.  The only safe way to
    return a translated semantic unit to its original timeline is an explicit
    one-to-one mapping from immutable source cue ids to usable target text.
    """
    cues = list(entry.source_cues)
    if len(cues) <= 1 or not cue_targets:
        return False

    cue_ids = [cue.index for cue in cues]
    if len(set(cue_ids)) != len(cue_ids):
        return False

    normalized: dict[int, str] = {}
    for raw_id, raw_target in cue_targets.items():
        try:
            cue_id = int(raw_id)
        except (TypeError, ValueError):
            return False
        if cue_id in normalized:
            return False
        normalized[cue_id] = str(raw_target or "").strip()

    if set(normalized) != set(cue_ids):
        return False

    for cue in cues:
        target = normalized[cue.index]
        if not target:
            return False
        if target_is_usable and not target_is_usable(target, cue.source_text):
            return False
    return True


def reflow_semantic_entries(
    entries: list[SubtitleEntry],
    *,
    input_lang_code: str = "en",
    output_lang_code: str = "zh",
    use_optimized: bool = True,
    cue_target_mappings: Mapping[int, Mapping[int, str]] | None = None,
    cue_target_mappings_verified: bool = False,
    target_is_usable: Callable[[str, str], bool] | None = None,
) -> list[SubtitleEntry]:
    """Place a complete semantic translation onto its immutable cue span.

    A translation can cover several source cues, but character length cannot
    establish which target characters belong to each cue. Do not split the
    target without a mapping that has passed an independent semantic check.
    An ID-complete model response proves only structure, not that each target
    belongs to the matching source cue. Emitting the complete target over the
    combined span is safer than placing fluent but unrelated text on a cue.
    """
    _ = output_lang_code  # Retained for the stable public API and future mappings.
    result: list[SubtitleEntry] = []
    for entry in entries:
        lines = _effective_lines(entry, use_optimized)
        target_text = lines[0] if len(lines) >= 2 else ""
        cues = list(entry.source_cues) or [cue_snapshot(entry, input_lang_code)]

        raw_mapped_targets = (cue_target_mappings or {}).get(entry.index) or {}
        mapped_targets: dict[int, str] = {}
        for raw_id, raw_target in raw_mapped_targets.items():
            try:
                mapped_targets[int(raw_id)] = str(raw_target or "").strip()
            except (TypeError, ValueError):
                mapped_targets = {}
                break
        if cue_target_mappings_verified and has_validated_cue_target_mapping(
            entry,
            mapped_targets,
            target_is_usable=target_is_usable,
        ):
            for cue in cues:
                target = str(mapped_targets[cue.index]).strip()
                flowed = SubtitleEntry(
                    index=cue.index,
                    start_time=cue.start_time,
                    end_time=cue.end_time,
                    lines=[target, cue.source_text] if cue.source_text else [target],
                )
                flowed.original_lines = list(cue.original_lines or (cue.source_text,))
                flowed.source_indexes = list(cue.source_indexes or (cue.index,))
                flowed.source_cues = [cue]
                _copy_quality_metadata(entry, flowed)
                if "semantic_cue_mapping_applied" not in flowed.quality_flags:
                    flowed.quality_flags.append("semantic_cue_mapping_applied")
                result.append(flowed)
            continue

        # During the post-translation mapping pass, an absent or invalid map is
        # deliberately a no-op.  The caller keeps the complete semantic target
        # and its combined span as the safe fallback; do not accidentally mark
        # it as an optimized reflow result before the quality stages run.
        if cue_target_mappings is not None:
            clone = SubtitleEntry(
                entry.index,
                entry.start_time,
                entry.end_time,
                list(entry.lines),
            )
            clone.optimized_lines = (
                list(entry.optimized_lines) if entry.optimized_lines else None
            )
            clone.original_lines = list(entry.original_lines or entry.lines)
            clone.source_indexes = list(entry.source_indexes or [entry.index])
            clone.source_cues = cues
            _copy_quality_metadata(entry, clone)
            result.append(clone)
            continue

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
