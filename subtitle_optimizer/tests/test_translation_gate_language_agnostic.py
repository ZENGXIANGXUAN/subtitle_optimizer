"""Regression tests for the language-agnostic translation accept/reject gate.

``_translation_is_usable`` is the single gate for the translate coverage retry
and source fallback, optimize's apply/fail branches, and both the fidelity and
consistency review passes.  It used to wrap its whole body in
``if output_lang_code == "zh"`` and return True otherwise, so for the nine
non-Chinese output languages any non-empty string counted as a finished
translation -- including the model echoing the source back verbatim.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import target_echoes_source
from core.workers import LANGUAGE_MAP, _resolve_lang_code, _translation_is_usable


SOURCE = "The algorithm processes each frame independently."

# One real translation per configured output language.
REAL_TRANSLATIONS = {
    "中文": "该算法独立处理每一帧。",
    "English": "The algorithm handles every frame on its own.",
    "意大利语 (Italiano)": "L'algoritmo elabora ogni fotogramma in modo indipendente.",
    "日语 (日本語)": "アルゴリズムは各フレームを個別に処理します。",
    "韩语 (한국어)": "알고리즘은 각 프레임을 독립적으로 처리합니다.",
    "法语 (Français)": "L'algorithme traite chaque image indépendamment.",
    "德语 (Deutsch)": "Der Algorithmus verarbeitet jedes Bild unabhängig.",
    "西班牙语 (Español)": "El algoritmo procesa cada fotograma de forma independiente.",
    "葡萄牙语 (Português)": "O algoritmo processa cada quadro independentemente.",
    "俄语 (Русский)": "Алгоритм обрабатывает каждый кадр независимо.",
}


def test_every_configured_output_language_is_covered() -> None:
    """A new entry in LANGUAGE_MAP must come with gate coverage."""
    assert set(REAL_TRANSLATIONS) == set(LANGUAGE_MAP)


@pytest.mark.parametrize("output_lang", sorted(LANGUAGE_MAP))
def test_source_echo_is_rejected_for_every_target_language(output_lang: str) -> None:
    if output_lang == "English":
        pytest.skip("English -> English is a polish run, not a translation")
    assert not _translation_is_usable(SOURCE, SOURCE, output_lang, "English")


@pytest.mark.parametrize("output_lang", sorted(LANGUAGE_MAP))
def test_a_real_translation_is_accepted_for_every_target_language(
    output_lang: str,
) -> None:
    assert _translation_is_usable(
        REAL_TRANSLATIONS[output_lang], SOURCE, output_lang, "English"
    )


@pytest.mark.parametrize("output_lang", sorted(LANGUAGE_MAP))
def test_rewrapped_or_recased_echo_is_still_an_echo(output_lang: str) -> None:
    if output_lang == "English":
        pytest.skip("English -> English is a polish run, not a translation")
    noisy = "The  algorithm\nprocesses each frame INDEPENDENTLY."
    assert not _translation_is_usable(noisy, SOURCE, output_lang, "English")


@pytest.mark.parametrize("output_lang", sorted(LANGUAGE_MAP))
def test_nonverbal_and_numeric_cues_may_stay_identical(output_lang: str) -> None:
    """The escape hatch must survive in every language, not only Chinese."""
    assert _translation_is_usable("Hmm", "Hmm", output_lang, "English")
    assert _translation_is_usable("1, 2, 3.", "1, 2, 3.", output_lang, "English")


def test_same_language_polish_may_leave_a_cue_untouched() -> None:
    """translate() is skipped when input == output; optimize must not fail it."""
    assert _translation_is_usable(SOURCE, SOURCE, "English", "English")
    assert _translation_is_usable("你好世界", "你好世界", "中文", "中文")


def test_chinese_partial_echo_is_still_rejected() -> None:
    """The universal echo rule supplements, never replaces, the zh coverage rule."""
    assert not _translation_is_usable(
        "The market had a strong move down.",
        "And in the beginning of May, what you can see is that "
        "the market had a strong move down.",
        "中文",
        "English",
    )


def test_bare_language_codes_no_longer_resolve_to_chinese() -> None:
    """LANGUAGE_MAP.get(value, ("zh", value)) sent every unknown value to zh."""
    assert _resolve_lang_code("日语 (日本語)") == "ja"
    assert _resolve_lang_code("ja") == "ja"
    assert _resolve_lang_code("fr") == "fr"
    assert _resolve_lang_code("") == ""
    assert _resolve_lang_code(None) == ""
    # A bare code pair must be treated as a real translation direction.
    assert not _translation_is_usable(SOURCE, SOURCE, "ja", "en")
    assert _translation_is_usable(SOURCE, SOURCE, "en", "en")


def test_target_echoes_source_ignores_case_and_wrapping_only() -> None:
    assert target_echoes_source("Bonjour", "bonjour")
    assert target_echoes_source("a  b\nc", "a b c")
    assert not target_echoes_source("Bonjour le monde", "Hello world")
    assert not target_echoes_source("", "Hello world")
    assert not target_echoes_source("Hello world", "")
