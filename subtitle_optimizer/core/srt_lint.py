"""Final SRT lint + conservative auto-fix helpers.

This module is intentionally rule-based. The LLM pipeline handles semantics;
this pass only fixes mechanical subtitle problems such as tiny durations,
small overlaps, adjacent duplicates, and numbering.

Important: this module is language-agnostic. It does not assume Chinese/English.
For bilingual subtitles it uses the pipeline's canonical line layout:
    line 1 = translated/target text
    line 2+ = original/source text
That means the same logic works for English->Chinese, English->Japanese,
Japanese->English, Spanish->French, etc.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from core.models import (
    SourceCueSnapshot,
    SubtitleEntry,
    _srt_time_to_ms,
    _ms_to_srt_time,
    source_can_remain_untranslated,
)

try:
    from ..utils import logger
except ImportError:  # pragma: no cover - depends on project layout
    import os
    import sys

    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    from utils import logger


MIN_DURATION_MS = 500
TARGET_EXTEND_MS = 600
SHORT_GAP_MS = 800
DUPLICATE_GAP_MS = 1200
SMALL_OVERLAP_MS = 300
READING_WARNING_CPS = 28.0
READING_BAD_CPS = 40.0
LONG_DURATION_MS = 8000
MAX_AUTO_MERGE_DURATION_MS = 10000

# A small multilingual filler list. This is only used as a bonus signal for
# safely merging tiny cues; bilingual completeness never depends on this list.
_FILLER_PATTERNS = {
    "ok",
    "okay",
    "right",
    "yeah",
    "yep",
    "yes",
    "no",
    "sure",
    "alright",
    "all right",
    "uh",
    "um",
    "uh huh",
    "mm",
    "hmm",
    "you know",
    "对吧",
    "是吧",
    "好的",
    "好",
    "嗯",
    "啊",
    "呃",
    "はい",
    "うん",
    "そう",
    "d'accord",
    "vale",
    "sí",
    "si",
}

_WORD_RE = re.compile(r"[\w]+(?:['’-][\w]+)?", re.UNICODE)
_ONLY_NUMBER_OR_SYMBOL_RE = re.compile(r"^[\d\W_]+$", re.UNICODE)


@dataclass
class SrtLintIssue:
    kind: str
    index: int | None = None
    message: str = ""


@dataclass
class SrtLintReport:
    before_count: int = 0
    after_count: int = 0
    renumbered: bool = False
    small_overlaps_fixed: int = 0
    large_overlaps: list[SrtLintIssue] = field(default_factory=list)
    short_entries_seen: int = 0
    short_entries_merged: int = 0
    short_entries_extended: int = 0
    short_entries_unfixed: list[SrtLintIssue] = field(default_factory=list)
    duplicates_merged: int = 0
    empty_entries_removed: int = 0
    incomplete_bilingual: list[SrtLintIssue] = field(default_factory=list)
    high_cps: list[SrtLintIssue] = field(default_factory=list)
    bad_cps: list[SrtLintIssue] = field(default_factory=list)
    long_entries: list[SrtLintIssue] = field(default_factory=list)

    # Backward-compatible aliases for callers that used the first version.
    # They intentionally stay empty because language-agnostic lint cannot know
    # whether a single remaining line is target-only or source-only.
    missing_target: list[SrtLintIssue] = field(default_factory=list)
    missing_source: list[SrtLintIssue] = field(default_factory=list)

    @property
    def total_auto_fixes(self) -> int:
        return (
            self.small_overlaps_fixed
            + self.short_entries_merged
            + self.short_entries_extended
            + self.duplicates_merged
            + self.empty_entries_removed
            + (1 if self.renumbered else 0)
        )

    @property
    def total_warnings(self) -> int:
        return (
            len(self.large_overlaps)
            + len(self.short_entries_unfixed)
            + len(self.incomplete_bilingual)
            + len(self.high_cps)
            + len(self.bad_cps)
            + len(self.long_entries)
        )

    def summary(self) -> str:
        return (
            f"字幕质检完成：{self.before_count} → {self.after_count} 条，"
            f"自动修复 {self.total_auto_fixes} 处，"
            f"仍需检查 {self.total_warnings} 处"
        )

    def to_text(self) -> str:
        lines = [
            "字幕质检报告",
            "",
            f"总条数：{self.before_count}",
            f"修复后条数：{self.after_count}",
            "",
            "自动修复：",
            f"- 重新编号：{'是' if self.renumbered else '否'}",
            f"- 时间轴小重叠：{self.small_overlaps_fixed} 处",
            f"- 极短字幕合并：{self.short_entries_merged} 条",
            f"- 极短字幕延长：{self.short_entries_extended} 条",
            f"- 相邻重复字幕合并：{self.duplicates_merged} 条",
            f"- 空字幕移除：{self.empty_entries_removed} 条",
            "",
            "仍需人工检查：",
            f"- 大重叠：{len(self.large_overlaps)} 条",
            f"- 极短字幕未修复：{len(self.short_entries_unfixed)} 条",
            f"- 双语结构不完整：{len(self.incomplete_bilingual)} 条",
            f"- 阅读压力偏高：{len(self.high_cps)} 条",
            f"- 阅读压力严重偏高：{len(self.bad_cps)} 条",
            f"- 超长字幕：{len(self.long_entries)} 条",
        ]
        for title, issues in [
            ("大重叠", self.large_overlaps),
            ("极短字幕未修复", self.short_entries_unfixed),
            ("双语结构不完整", self.incomplete_bilingual),
            ("阅读压力严重偏高", self.bad_cps),
            ("阅读压力偏高", self.high_cps),
            ("超长字幕", self.long_entries),
        ]:
            if issues:
                lines.append("")
                lines.append(f"{title}明细：")
                for issue in issues[:80]:
                    prefix = f"#{issue.index}" if issue.index is not None else "#?"
                    lines.append(f"- {prefix} {issue.message}".rstrip())
                if len(issues) > 80:
                    lines.append(f"- ... 还有 {len(issues) - 80} 条")
        return "\n".join(lines).strip() + "\n"


def _duration_ms(entry: SubtitleEntry) -> int:
    return max(0, _srt_time_to_ms(entry.end_time) - _srt_time_to_ms(entry.start_time))


def _gap_ms(left: SubtitleEntry, right: SubtitleEntry) -> int:
    return _srt_time_to_ms(right.start_time) - _srt_time_to_ms(left.end_time)


def _selected_lines(entry: SubtitleEntry, use_optimized: bool = True) -> list[str]:
    if use_optimized and entry.optimized_lines:
        return [str(line).strip() for line in entry.optimized_lines if str(line).strip()]
    return [str(line).strip() for line in entry.lines if str(line).strip()]


def _set_selected_lines(entry: SubtitleEntry, lines: Iterable[str], use_optimized: bool = True) -> None:
    cleaned = [str(line).strip() for line in lines if str(line).strip()]
    # Keep the finalized entry canonical regardless of whether the caller later
    # serializes with use_optimized=True or False. original_lines still preserves
    # pre-finalize text for comparison views.
    entry.lines = cleaned
    if use_optimized:
        entry.optimized_lines = cleaned


def _normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"[。！？!?,，.；;：:\"'“”‘’（）()\[\]{}<>]", "", text)
    return text


def _text_for_compare(entry: SubtitleEntry, use_optimized: bool = True) -> str:
    return _normalize_for_compare("\n".join(_selected_lines(entry, use_optimized)))


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _join_fragments(parts: list[str]) -> str:
    pieces = [p.strip() for p in parts if p and p.strip()]
    if not pieces:
        return ""
    out = pieces[0]
    for piece in pieces[1:]:
        if out and piece and _is_word_char(out[-1]) and _is_word_char(piece[0]):
            out += " " + piece
        else:
            out += piece
    return re.sub(r"\s+", " ", out).strip()


def _is_bilingual_layout(lines: list[str]) -> bool:
    # Pipeline canonical format after translation/optimization:
    # [target_text, source_text]. If there are more than two lines, the first is
    # still target and the remaining lines are treated as source/original block.
    return len([line for line in lines if line.strip()]) >= 2


def _split_by_layout(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    cleaned = [line.strip() for line in lines if line.strip()]
    if len(cleaned) >= 2:
        return [cleaned[0]], cleaned[1:], []
    return [], [], cleaned


def _merged_lines(left: SubtitleEntry, right: SubtitleEntry, use_optimized: bool = True) -> list[str]:
    left_lines = _selected_lines(left, use_optimized)
    right_lines = _selected_lines(right, use_optimized)

    # If both cues are bilingual, preserve the language-agnostic two-part layout.
    if _is_bilingual_layout(left_lines) and _is_bilingual_layout(right_lines):
        left_target, left_source, _ = _split_by_layout(left_lines)
        right_target, right_source, _ = _split_by_layout(right_lines)
        target = _join_fragments(left_target + right_target)
        source = _join_fragments(left_source + right_source)
        return [line for line in [target, source] if line]

    # If either side is not bilingual, do not guess its language. Keep line order.
    merged: list[str] = []
    for line in left_lines + right_lines:
        if line and (not merged or _normalize_for_compare(line) != _normalize_for_compare(merged[-1])):
            merged.append(line)
    return merged


def _merge_quality_metadata(left: SubtitleEntry, right: SubtitleEntry) -> None:
    """Preserve quality/audit metadata when deterministic lint merges cues."""
    left.quality_flags = list(dict.fromkeys(
        list(getattr(left, "quality_flags", []) or [])
        + list(getattr(right, "quality_flags", []) or [])
    ))
    if right.status in {"error", "needs_review"}:
        left.status = right.status
    stages = {
        str(getattr(left, "stage_status", "") or ""),
        str(getattr(right, "stage_status", "") or ""),
    }
    stages.discard("")
    if len(stages) > 1:
        if any(
            marker in stage
            for stage in stages
            for marker in ("failed", "fallback", "invalid", "timeout", "needs_review")
        ):
            left.stage_status = "lint_merged_with_quality_flags"
        else:
            left.stage_status = "lint_merged"
    errors = [
        message for message in (left.error_msg, right.error_msg)
        if str(message or "").strip()
    ]
    if errors:
        left.error_msg = " | ".join(dict.fromkeys(errors))


def _fallback_source_cue(
    entry: SubtitleEntry,
    use_optimized: bool = True,
) -> SourceCueSnapshot:
    """Create provenance for legacy callers that did not attach source_cues."""
    selected = _selected_lines(entry, use_optimized)
    source_lines = selected[1:] if len(selected) >= 2 else selected
    source_indexes = tuple(entry.source_indexes or [entry.index])
    original_lines = tuple(
        entry.original_lines if entry.original_lines is not None else entry.lines
    )
    return SourceCueSnapshot(
        index=entry.index,
        start_time=entry.start_time,
        end_time=entry.end_time,
        source_text=_join_fragments(source_lines),
        original_lines=original_lines,
        source_indexes=source_indexes,
    )


def _source_cue_identity(cue: SourceCueSnapshot) -> tuple[int, str, str, str]:
    """Return the same immutable cue identity used by semantic-unit merging."""
    return (cue.index, cue.start_time, cue.end_time, cue.source_text)


def _merge_source_metadata(
    left: SubtitleEntry,
    right: SubtitleEntry,
    use_optimized: bool = True,
) -> None:
    """Merge source provenance in cue order without losing or duplicating it.

    ``source_cues`` is the authoritative immutable record.  ``original_lines``
    is rebuilt from the de-duplicated snapshots so identical text from two
    distinct source cues is retained, while an overlapping snapshot is not
    appended twice.
    """
    left_cues = list(left.source_cues) or [_fallback_source_cue(left, use_optimized)]
    right_cues = list(right.source_cues) or [_fallback_source_cue(right, use_optimized)]

    merged_cues: list[SourceCueSnapshot] = []
    seen_cues: set[tuple[int, str, str, str]] = set()
    for cue in left_cues + right_cues:
        identity = _source_cue_identity(cue)
        if identity in seen_cues:
            continue
        seen_cues.add(identity)
        merged_cues.append(cue)

    source_indexes: list[int] = []
    seen_indexes: set[int] = set()

    def add_indexes(values: Iterable[int]) -> None:
        for value in values:
            if value in seen_indexes:
                continue
            seen_indexes.add(value)
            source_indexes.append(value)

    for cue in merged_cues:
        add_indexes(cue.source_indexes or (cue.index,))
    add_indexes(left.source_indexes or [left.index])
    add_indexes(right.source_indexes or [right.index])

    original_lines: list[str] = []
    for cue in merged_cues:
        cue_lines = list(cue.original_lines)
        if cue_lines:
            original_lines.extend(cue_lines)
        elif cue.source_text:
            original_lines.append(cue.source_text)

    left.source_cues = merged_cues
    left.source_indexes = source_indexes
    left.original_lines = original_lines


def _merge_into_left(left: SubtitleEntry, right: SubtitleEntry, use_optimized: bool = True) -> SubtitleEntry:
    _merge_source_metadata(left, right, use_optimized)
    left.end_time = right.end_time
    _set_selected_lines(left, _merged_lines(left, right, use_optimized), use_optimized)
    _merge_quality_metadata(left, right)
    return left


def _looks_like_filler(entry: SubtitleEntry, use_optimized: bool = True) -> bool:
    text = _text_for_compare(entry, use_optimized)
    if not text:
        return False
    if text in _FILLER_PATTERNS:
        return True
    for line in _selected_lines(entry, use_optimized):
        if _normalize_for_compare(line) in _FILLER_PATTERNS:
            return True
    return False


def _visible_units(text: str) -> int:
    """Language-agnostic reading unit estimate.

    For spaced languages, a word roughly costs more than a single character;
    for unspaced scripts, visible characters are a better proxy. This is only a
    warning heuristic, never an auto-fix decision.
    """
    text = unicodedata.normalize("NFKC", text or "")
    non_space = [ch for ch in text if not ch.isspace()]
    if not non_space:
        return 0
    words = _WORD_RE.findall(text)
    has_spaces = bool(re.search(r"\w\s+\w", text, flags=re.UNICODE))
    punctuation = len(re.findall(r"[0-9%$€£¥.,:;!?，。！？：；]", text))
    if has_spaces and len(words) >= 2:
        return int(round(len(words) * 1.8 + punctuation))
    return len(non_space)


def _can_merge_short(entry: SubtitleEntry, neighbor: SubtitleEntry, use_optimized: bool = True) -> bool:
    combined_start = min(_srt_time_to_ms(entry.start_time), _srt_time_to_ms(neighbor.start_time))
    combined_end = max(_srt_time_to_ms(entry.end_time), _srt_time_to_ms(neighbor.end_time))
    combined_duration = combined_end - combined_start
    if combined_duration > MAX_AUTO_MERGE_DURATION_MS:
        return False
    if _looks_like_filler(entry, use_optimized):
        return True

    lines = _selected_lines(entry, use_optimized)
    text = " ".join(lines).strip()
    if not text or _ONLY_NUMBER_OR_SYMBOL_RE.fullmatch(text):
        return False

    # Language-neutral tiny-fragment rule: short, non-numeric text fragments can
    # be merged; labels/numbers are protected above.
    if _visible_units(text) <= 8 and len(_WORD_RE.findall(text)) <= 4:
        return True
    return False


def _renumber(entries: list[SubtitleEntry]) -> bool:
    changed = False
    for idx, entry in enumerate(entries, start=1):
        if entry.index != idx:
            changed = True
        entry.index = idx
    return changed


def _remove_empty(entries: list[SubtitleEntry], report: SrtLintReport, use_optimized: bool = True) -> list[SubtitleEntry]:
    kept: list[SubtitleEntry] = []
    for entry in entries:
        if _selected_lines(entry, use_optimized):
            kept.append(entry)
        else:
            report.empty_entries_removed += 1
    return kept


def _fix_small_overlaps(entries: list[SubtitleEntry], report: SrtLintReport) -> None:
    for i in range(len(entries) - 1):
        cur = entries[i]
        nxt = entries[i + 1]
        overlap = _srt_time_to_ms(cur.end_time) - _srt_time_to_ms(nxt.start_time)
        if overlap <= 0:
            continue
        if overlap <= SMALL_OVERLAP_MS:
            new_end = max(_srt_time_to_ms(cur.start_time) + 1, _srt_time_to_ms(nxt.start_time) - 1)
            cur.end_time = _ms_to_srt_time(new_end)
            report.small_overlaps_fixed += 1
        else:
            report.large_overlaps.append(
                SrtLintIssue("large_overlap", cur.index, f"与下一条重叠 {overlap}ms，未自动硬修")
            )


def _merge_adjacent_duplicates(entries: list[SubtitleEntry], report: SrtLintReport, use_optimized: bool = True) -> list[SubtitleEntry]:
    if not entries:
        return []
    result: list[SubtitleEntry] = [entries[0]]
    for entry in entries[1:]:
        prev = result[-1]
        if (
            _text_for_compare(prev, use_optimized)
            and _text_for_compare(prev, use_optimized) == _text_for_compare(entry, use_optimized)
            and _gap_ms(prev, entry) <= DUPLICATE_GAP_MS
        ):
            _merge_source_metadata(prev, entry, use_optimized)
            prev.end_time = entry.end_time
            _merge_quality_metadata(prev, entry)
            report.duplicates_merged += 1
        else:
            result.append(entry)
    return result


def _fix_short_entries(entries: list[SubtitleEntry], report: SrtLintReport, use_optimized: bool = True) -> list[SubtitleEntry]:
    result = list(entries)
    i = 0
    while i < len(result):
        entry = result[i]
        duration = _duration_ms(entry)
        if duration >= MIN_DURATION_MS:
            i += 1
            continue

        report.short_entries_seen += 1
        prev_gap = _gap_ms(result[i - 1], entry) if i > 0 else 10**9
        next_gap = _gap_ms(entry, result[i + 1]) if i + 1 < len(result) else 10**9

        if i > 0 and prev_gap <= SHORT_GAP_MS and _can_merge_short(entry, result[i - 1], use_optimized):
            _merge_into_left(result[i - 1], entry, use_optimized)
            del result[i]
            report.short_entries_merged += 1
            continue

        if i + 1 < len(result) and next_gap <= SHORT_GAP_MS and _can_merge_short(entry, result[i + 1], use_optimized):
            nxt = result[i + 1]
            _merge_into_left(entry, nxt, use_optimized)
            del result[i + 1]
            report.short_entries_merged += 1
            i += 1
            continue

        start_ms = _srt_time_to_ms(entry.start_time)
        desired_end = start_ms + TARGET_EXTEND_MS
        max_end = _srt_time_to_ms(result[i + 1].start_time) - 1 if i + 1 < len(result) else desired_end
        if desired_end <= max_end:
            entry.end_time = _ms_to_srt_time(desired_end)
            report.short_entries_extended += 1
        else:
            report.short_entries_unfixed.append(
                SrtLintIssue("short_unfixed", entry.index, f"时长 {duration}ms，邻近字幕间隔不足，未自动修复")
            )
        i += 1
    return result


def _infer_bilingual_mode(
    entries: list[SubtitleEntry],
    use_optimized: bool,
    input_lang_code: str | None,
    output_lang_code: str | None,
    bilingual_mode: bool | None,
) -> bool:
    if bilingual_mode is not None:
        return bilingual_mode
    if input_lang_code and output_lang_code and str(input_lang_code).lower() != str(output_lang_code).lower():
        return True
    sample = entries[: min(50, len(entries))]
    if not sample:
        return False
    bilingual_like = sum(1 for entry in sample if len(_selected_lines(entry, use_optimized)) >= 2)
    return bilingual_like >= max(1, int(len(sample) * 0.6))


def _has_chinese(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text or "")


def _detect_remaining_issues(
    entries: list[SubtitleEntry],
    report: SrtLintReport,
    use_optimized: bool = True,
    expect_bilingual: bool = True,
    input_lang_code: str = "",
    output_lang_code: str = "",
) -> None:
    for entry in entries:
        lines = _selected_lines(entry, use_optimized)
        source_identity_allowed = len(lines) == 1 and (
            "source_fallback" in entry.quality_flags
            or (
                "source_identity_allowed" in entry.quality_flags
                and source_can_remain_untranslated(lines[0])
            )
        )
        if expect_bilingual and len(lines) < 2 and not source_identity_allowed:
            preview = " / ".join(lines)[:80] if lines else "空文本"
            report.incomplete_bilingual.append(
                SrtLintIssue(
                    "incomplete_bilingual",
                    entry.index,
                    f"当前只有 {len(lines)} 行，预期为译文+原文双语结构：{preview}",
                )
            )

        # Chinese-target hard check: in the canonical bilingual layout, line 1
        # must be the Chinese translation. This prevents English-only fallback
        # lines from being accepted just because there are two lines.
        if expect_bilingual and output_lang_code == "zh" and not source_identity_allowed:
            target_line = lines[0] if lines else ""
            if not _has_chinese(target_line):
                preview = target_line[:80] if target_line else "空译文"
                report.incomplete_bilingual.append(
                    SrtLintIssue(
                        "missing_chinese_target",
                        entry.index,
                        f"第一行缺少中文译文：{preview}",
                    )
                )

        duration_ms = _duration_ms(entry)
        if duration_ms > LONG_DURATION_MS:
            report.long_entries.append(SrtLintIssue("long_entry", entry.index, f"时长 {duration_ms / 1000:.2f}s"))
        if duration_ms <= 0:
            report.bad_cps.append(SrtLintIssue("bad_cps", entry.index, "时长为0，无法阅读"))
            continue
        cps = _visible_units("\n".join(lines)) / max(0.001, duration_ms / 1000.0)
        if cps >= READING_BAD_CPS:
            report.bad_cps.append(SrtLintIssue("bad_cps", entry.index, f"CPS={cps:.1f}"))
        elif cps >= READING_WARNING_CPS:
            report.high_cps.append(SrtLintIssue("high_cps", entry.index, f"CPS={cps:.1f}"))


def finalize_entries(
    entries: list[SubtitleEntry],
    use_optimized: bool = True,
    input_lang_code: str = "",
    output_lang_code: str = "",
    bilingual_mode: bool | None = None,
) -> tuple[list[SubtitleEntry], SrtLintReport]:
    """Run the final conservative SRT engineering pass.

    The pass is language-agnostic. When bilingual mode is enabled, completeness
    is checked by line layout, not by detecting Chinese/English characters.
    """
    report = SrtLintReport(before_count=len(entries))
    if not entries:
        report.after_count = 0
        return [], report

    expect_bilingual = _infer_bilingual_mode(
        entries,
        use_optimized=use_optimized,
        input_lang_code=input_lang_code,
        output_lang_code=output_lang_code,
        bilingual_mode=bilingual_mode,
    )

    fixed = sorted(list(entries), key=lambda e: (_srt_time_to_ms(e.start_time), _srt_time_to_ms(e.end_time)))
    fixed = _remove_empty(fixed, report, use_optimized=use_optimized)
    fixed = _merge_adjacent_duplicates(fixed, report, use_optimized=use_optimized)
    fixed = _fix_short_entries(fixed, report, use_optimized=use_optimized)
    _fix_small_overlaps(fixed, report)
    fixed = _merge_adjacent_duplicates(fixed, report, use_optimized=use_optimized)
    report.renumbered = _renumber(fixed)
    report.after_count = len(fixed)
    _detect_remaining_issues(
        fixed,
        report,
        use_optimized=use_optimized,
        expect_bilingual=expect_bilingual,
        input_lang_code=input_lang_code,
        output_lang_code=output_lang_code,
    )

    logger.info(report.summary())
    return fixed, report


def lint_entries(
    entries: list[SubtitleEntry],
    use_optimized: bool = True,
    input_lang_code: str = "",
    output_lang_code: str = "",
    bilingual_mode: bool | None = None,
) -> SrtLintReport:
    """Report-only convenience wrapper. Does not mutate caller-visible ordering."""
    _, report = finalize_entries(
        list(entries),
        use_optimized=use_optimized,
        input_lang_code=input_lang_code,
        output_lang_code=output_lang_code,
        bilingual_mode=bilingual_mode,
    )
    return report
