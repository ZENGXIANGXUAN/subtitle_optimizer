"""Source-layout detection and lossless source normalization.

SRT line wrapping is a presentation detail, not proof that a cue is bilingual.
This module centralizes the decision for both single-file and batch tasks so a
two-line monolingual cue can never silently lose one of its lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from core.models import SourceCueSnapshot, SubtitleEntry


LAYOUT_AUTO = "auto"
LAYOUT_BILINGUAL = "bilingual"
LAYOUT_MONOLINGUAL = "monolingual"

_LATIN_LANGUAGE_CODES = {"en", "it", "fr", "de", "es", "pt"}
_CJK_LANGUAGE_CODES = {"zh", "ja", "ko"}


@dataclass(frozen=True)
class SourceNormalizationResult:
    entries: list[SubtitleEntry]
    detected_layout: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


def _script_counts(text: str) -> dict[str, int]:
    counts = {
        "latin": 0,
        "cjk": 0,
        "hiragana": 0,
        "katakana": 0,
        "hangul": 0,
        "cyrillic": 0,
    }
    for char in text or "":
        codepoint = ord(char)
        if ("A" <= char <= "Z") or ("a" <= char <= "z"):
            counts["latin"] += 1
        elif 0x4E00 <= codepoint <= 0x9FFF:
            counts["cjk"] += 1
        elif 0x3040 <= codepoint <= 0x309F:
            counts["hiragana"] += 1
        elif 0x30A0 <= codepoint <= 0x30FF:
            counts["katakana"] += 1
        elif 0xAC00 <= codepoint <= 0xD7AF:
            counts["hangul"] += 1
        elif 0x0400 <= codepoint <= 0x04FF:
            counts["cyrillic"] += 1
    return counts


def _language_script_score(text: str, lang_code: str) -> int:
    counts = _script_counts(text)
    code = (lang_code or "").lower()
    if code == "zh":
        return counts["cjk"]
    if code == "ja":
        return counts["hiragana"] + counts["katakana"] + counts["cjk"]
    if code == "ko":
        return counts["hangul"]
    if code == "ru":
        return counts["cyrillic"]
    if code in _LATIN_LANGUAGE_CODES:
        return counts["latin"]
    return sum(counts.values())


def _scripts_are_distinguishable(input_lang_code: str, output_lang_code: str) -> bool:
    left = (input_lang_code or "").lower()
    right = (output_lang_code or "").lower()
    if left == right:
        return False
    if left in _LATIN_LANGUAGE_CODES and right in _LATIN_LANGUAGE_CODES:
        return False
    if left in _CJK_LANGUAGE_CODES and right in _CJK_LANGUAGE_CODES:
        # Chinese/Japanese share Han characters. Japanese kana and Korean Hangul
        # can still provide evidence, but not enough to delete a line silently.
        return False
    return True


def detect_source_layout(
    entries: list[SubtitleEntry],
    *,
    source_line_index: int,
    input_lang_code: str,
    output_lang_code: str,
    requested_layout: str = LAYOUT_AUTO,
    sample_size: int = 100,
) -> tuple[str, float, list[str]]:
    """Return ``(layout, confidence, warnings)`` for a parsed SRT.

    Auto mode only selects bilingual layout when the source/target scripts are
    distinguishable and the selected source line consistently matches the input
    language. Ambiguous cases remain monolingual so all wrapped source lines are
    preserved. Callers can explicitly request bilingual layout when both
    languages use the same script.
    """
    requested = (requested_layout or LAYOUT_AUTO).lower()
    if requested in {LAYOUT_BILINGUAL, LAYOUT_MONOLINGUAL}:
        return requested, 1.0, []

    multiline = [entry for entry in entries if len([line for line in entry.lines if line.strip()]) >= 2]
    if not multiline:
        return LAYOUT_MONOLINGUAL, 1.0, []

    if not _scripts_are_distinguishable(input_lang_code, output_lang_code):
        return (
            LAYOUT_MONOLINGUAL,
            0.5,
            [
                "多行字幕的输入/输出语言文字系统相同，自动模式按单语换行保留全部文本；"
                "如文件确为双语字幕，请显式选择双语布局。"
            ],
        )

    strong = 0
    considered = 0
    for entry in multiline[: max(1, sample_size)]:
        lines = [line.strip() for line in entry.lines if line.strip()]
        if source_line_index >= len(lines):
            continue
        source_line = lines[source_line_index]
        other_lines = [line for idx, line in enumerate(lines) if idx != source_line_index]
        if not other_lines:
            continue
        considered += 1
        source_score = _language_script_score(source_line, input_lang_code)
        source_as_target = _language_script_score(source_line, output_lang_code)
        has_target_evidence = any(
            _language_script_score(line, output_lang_code) > 0
            and _language_script_score(line, output_lang_code)
            > _language_script_score(line, input_lang_code)
            for line in other_lines
        )
        if (
            source_score > 0
            and source_score >= source_as_target
            and has_target_evidence
        ):
            strong += 1

    confidence = strong / max(1, considered)
    # Require consistent file-level evidence. A one-cue file can still be
    # confidently classified when the scripts are unambiguous.
    minimum_hits = 1 if considered <= 2 else 2
    if strong >= minimum_hits and confidence >= 0.6:
        return LAYOUT_BILINGUAL, confidence, []
    return (
        LAYOUT_MONOLINGUAL,
        max(0.5, 1.0 - confidence),
        ["未检测到稳定的双语行结构，已按单语字幕合并并保留所有换行文本。"],
    )


def join_source_lines(lines: list[str], lang_code: str) -> str:
    cleaned = [re.sub(r"\s+", " ", str(line or "")).strip() for line in lines]
    cleaned = [line for line in cleaned if line]
    if not cleaned:
        return ""
    if (lang_code or "").lower() in _CJK_LANGUAGE_CODES:
        return "".join(cleaned)
    return " ".join(cleaned)


def _clone_with_source(entry: SubtitleEntry, source_text: str) -> SubtitleEntry:
    clone = SubtitleEntry(
        entry.index,
        entry.start_time,
        entry.end_time,
        [source_text] if source_text else [],
    )
    clone.original_lines = list(entry.original_lines or entry.lines)
    clone.source_indexes = list(entry.source_indexes or [entry.index])
    clone.status = entry.status
    clone.error_msg = entry.error_msg
    clone.stage_status = entry.stage_status
    clone.quality_flags = list(entry.quality_flags)
    clone.source_cues = list(entry.source_cues) or [
        SourceCueSnapshot(
            index=entry.index,
            start_time=entry.start_time,
            end_time=entry.end_time,
            source_text=source_text,
            original_lines=tuple(entry.original_lines or entry.lines),
            source_indexes=tuple(entry.source_indexes or [entry.index]),
        )
    ]
    return clone


def normalize_source_entries(
    entries: list[SubtitleEntry],
    *,
    source_line_index: int = 1,
    input_lang_code: str = "en",
    output_lang_code: str = "zh",
    requested_layout: str = LAYOUT_AUTO,
) -> SourceNormalizationResult:
    """Normalize parsed entries into one lossless source line per cue."""
    layout, confidence, warnings = detect_source_layout(
        entries,
        source_line_index=max(0, int(source_line_index)),
        input_lang_code=input_lang_code,
        output_lang_code=output_lang_code,
        requested_layout=requested_layout,
    )

    normalized: list[SubtitleEntry] = []
    requested = (requested_layout or LAYOUT_AUTO).lower()
    for entry in entries:
        lines = [str(line) for line in entry.lines if str(line).strip()]
        if not lines:
            normalized.append(_clone_with_source(entry, ""))
            continue
        if layout == LAYOUT_BILINGUAL and len(lines) > 1:
            index = max(0, int(source_line_index))
            if index >= len(lines):
                warnings.append(
                    f"字幕 #{entry.index} 不存在第 {index + 1} 行，已保留全部文本以防丢失。"
                )
                source_text = join_source_lines(lines, input_lang_code)
            else:
                source_like = [
                    line
                    for line in lines
                    if _language_script_score(line, input_lang_code) > 0
                    and _language_script_score(line, input_lang_code)
                    > _language_script_score(line, output_lang_code)
                ]
                target_like = [
                    line
                    for line in lines
                    if _language_script_score(line, output_lang_code) > 0
                    and _language_script_score(line, output_lang_code)
                    > _language_script_score(line, input_lang_code)
                ]
                cue_has_bilingual_evidence = bool(source_like and target_like)
                if requested == LAYOUT_AUTO and not cue_has_bilingual_evidence:
                    warnings.append(
                        f"字幕 #{entry.index} 与文件的双语布局不一致，已保留并合并该条全部文本。"
                    )
                    source_text = join_source_lines(lines, input_lang_code)
                elif source_like and _scripts_are_distinguishable(
                    input_lang_code, output_lang_code
                ):
                    # Preserve source-language continuation lines in 3/4-line
                    # bilingual layouts instead of selecting only one line.
                    source_text = join_source_lines(source_like, input_lang_code)
                else:
                    source_text = lines[index].strip()
        else:
            source_text = join_source_lines(lines, input_lang_code)
        normalized.append(_clone_with_source(entry, source_text))

    return SourceNormalizationResult(
        entries=normalized,
        detected_layout=layout,
        confidence=confidence,
        warnings=list(dict.fromkeys(warnings)),
    )
