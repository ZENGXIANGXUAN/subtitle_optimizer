"""Positional accessors for the pipeline's canonical bilingual line layout.

After translation, ``entry.lines`` is always ``[target, source]``. Script
detection cannot be used to tell the two apart: it breaks for Chinese -> English
and for any same-script pair such as English -> French. Both accessors below are
therefore purely positional.
"""

from core.models import SubtitleEntry


def _get_source_text(entry: SubtitleEntry, lang_code: str) -> str:
    """获取字幕条目的原文文本（根据输入语言）"""
    # 翻译后，entry.lines 的结构是 [译文, 原文]
    # 所以原文总是在第二个位置（索引 1）
    if len(entry.lines) >= 2:
        return entry.lines[1]
    # 如果只有一行，说明是单语字幕，直接返回
    if len(entry.lines) == 1:
        return entry.lines[0]
    return ""


def _get_target_text(entry: SubtitleEntry, lang_code: str) -> str:
    """获取字幕条目的译文文本（根据输出语言）"""
    # 翻译后，entry.lines 的结构是 [译文, 原文]
    # 所以译文总是在第一个位置（索引 0）
    if len(entry.lines) >= 2:
        return entry.lines[0]
    # 如果只有一行，说明是单语字幕，没有译文
    return ""
