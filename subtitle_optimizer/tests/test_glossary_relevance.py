"""Regression tests for which glossary terms reach the prompts.

Every term kept by ``filter_glossary_by_entries`` becomes a *mandatory* rule in
every downstream prompt ("必须严格统一翻译...绝不允许替换"), and the fidelity and
consistency passes are told the glossary outranks their own judgement. A wrong
entry is therefore not cosmetic -- it is a protected mistranslation.

Measured on a real run (NASA physics video + a futures-trading glossary),
the old bare-substring test kept 11 terms of which 8 had zero whole-word
occurrences, and one of those reached the output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.workers import (
    _term_rules_to_glossary_text,
    filter_glossary_by_entries,
    glossary_source_appears,
)


NASA_LINES = [
    "Hey there, welcome to NASA Launch Pad.",
    "Kind of a simple concept, right?",
    "there are a lot of different properties contained in there",
    "that also gets involved from a structure and load standpoint",
    "what scientists call inertia, or the resistance of an object to motion",
    "that center of gravity is what you would need to make it balanced",
    "putting ballast in, or some random heavy material added",
    "will experience a smaller acceleration",
    "She is the Mass Properties Working Group lead supporting the office",
    "Testing and retesting. Looking for the best answers.",
]


def _entries() -> list[SubtitleEntry]:
    return [
        SubtitleEntry(i, f"00:00:{i:02d},000", f"00:00:{i + 1:02d},000", [line])
        for i, line in enumerate(NASA_LINES, start=1)
    ]


# Source term -> the unrelated word it used to match inside.
SUBSTRING_TRAPS = {
    "he": "t-he / -he-y",
    "r": "matches any word containing r",
    "sim": "sim-ple",
    "support": "support-ing",
    "balance": "balance-d",
    "DOM": "ran-dom",
    "ratio": "accele-ratio-n",
    "retest": "retest-ing",
}


@pytest.mark.parametrize("term", sorted(SUBSTRING_TRAPS))
def test_substring_only_matches_are_rejected(term: str) -> None:
    haystack = " ".join(NASA_LINES).lower()
    assert not glossary_source_appears(term, haystack, "en"), (
        f"{term!r} only occurs inside {SUBSTRING_TRAPS[term]}"
    )


@pytest.mark.parametrize("term", ["lot", "structure", "resistance", "inertia"])
def test_genuine_whole_word_matches_are_kept(term: str) -> None:
    haystack = " ".join(NASA_LINES).lower()
    assert glossary_source_appears(term, haystack, "en")


def test_the_trading_glossary_no_longer_leaks_into_a_physics_video() -> None:
    trading = "\n".join([
        "HE->猪肉期货（HE）",
        "R->R（风险回报比）",
        "sim->模拟交易",
        "support->支撑位",
        "balance->平衡状态",
        "DOM->盘口",
        "ratio->比率",
        "retest->回测",          # this one reached the real output as 反复回测
        "resistance->阻力位",
        "structure->价格结构",
        "lot->手",
    ])
    kept = [
        line for line in
        filter_glossary_by_entries(trading, _entries(), "en").splitlines()
        if line.strip()
    ]
    sources = {line.split("->")[0] for line in kept}
    assert sources == {"lot", "structure", "resistance"}, sources
    # The entry that actually corrupted the output must be gone.
    assert "retest->回测" not in kept


def test_comment_lines_are_still_preserved() -> None:
    raw = "# 这是注释\ninertia->惯性\nretest->回测"
    kept = filter_glossary_by_entries(raw, _entries(), "en").splitlines()
    assert "# 这是注释" in kept
    assert "inertia->惯性" in kept
    assert "retest->回测" not in kept


def test_short_uppercase_abbreviations_need_their_own_casing() -> None:
    """`HE`/`R` must not be matched by lowercase prose."""
    haystack = "he said the rate was high".lower()
    assert not glossary_source_appears("HE", haystack, "en")
    assert not glossary_source_appears("R", haystack, "en")
    # ...but a genuine lowercase word of the same length still matches.
    assert glossary_source_appears("was", haystack, "en")


def test_cjk_sources_fall_back_to_substring_with_a_length_floor() -> None:
    """CJK has no word boundaries; a single character matches almost anything."""
    haystack = "这个算法独立处理每一帧图像"
    assert glossary_source_appears("算法", haystack, "zh")
    assert not glossary_source_appears("法", haystack, "zh")
    assert not glossary_source_appears("矩阵", haystack, "zh")


def test_identity_term_rules_are_dropped() -> None:
    """A source==target rule only pins a spelling, and the spelling may be an
    ASR error the review passes are then forbidden to correct."""
    rules = [
        {"source": "PataBort 1", "target": "PataBort 1"},      # mis-heard "Pad Abort 1"
        {"source": "CentaBort 1", "target": "CentaBort 1"},    # mis-heard "Ascent Abort 1"
        {"source": "Ascensivore 1", "target": "Ascensivore 1"},
        {"source": "inertia", "target": "惯性"},
    ]
    assert _term_rules_to_glossary_text(rules) == "inertia->惯性"


def test_identity_check_ignores_case_only_differences() -> None:
    rules = [{"source": "NASA", "target": "nasa"}, {"source": "ballast", "target": "压舱物"}]
    assert _term_rules_to_glossary_text(rules) == "ballast->压舱物"


def test_empty_inputs_are_passed_through_unchanged() -> None:
    assert filter_glossary_by_entries("", _entries()) == ""
    assert filter_glossary_by_entries("inertia->惯性", []) == "inertia->惯性"
