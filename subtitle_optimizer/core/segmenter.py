import re
import subprocess
import sys
from core.models import SubtitleEntry, _english_word_count, _srt_time_to_ms, _source_text_length

try:
    from ..utils import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# 语言代码 -> spaCy 模型名（使用连字符格式，与 pip 包名一致）
SPACY_MODELS = {
    "zh": "zh-core-web-sm",      # 中文
    "en": "en-core-web-sm",      # 英文
    "it": "it-core-news-sm",     # 意大利语
    "ja": "ja-core-news-sm",     # 日语
    "ko": "ko-core-news-sm",     # 韩语
    "fr": "fr-core-news-sm",     # 法语
    "de": "de-core-news-sm",     # 德语
    "es": "es-core-news-sm",     # 西班牙语
    "pt": "pt-core-news-sm",     # 葡萄牙语
    "ru": "ru-core-news-sm",     # 俄语
}

_nlp_cache = {}  # 语言代码 -> spaCy 模型


def _get_venv_python() -> str:
    """获取虚拟环境的 Python 路径，如果不存在则返回当前 Python"""
    import os
    # 检查是否在虚拟环境中
    if hasattr(sys, 'prefix') and hasattr(sys, 'base_prefix') and sys.prefix != sys.base_prefix:
        # 已经在虚拟环境中，直接使用当前 Python
        return sys.executable

    # 尝试查找虚拟环境
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    venv_python = os.path.join(project_dir, ".venv", "Scripts", "python.exe")

    if os.path.exists(venv_python):
        return venv_python

    # 回退到当前 Python
    return sys.executable


def _install_spacy_model(model_name: str) -> bool:
    """自动下载并安装 spaCy 模型"""
    try:
        python_exe = _get_venv_python()
        print(f"📦 正在下载 spaCy 模型 {model_name}... (使用 {python_exe})")
        subprocess.check_call(
            [python_exe, "-m", "spacy", "download", model_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"✅ spaCy 模型 {model_name} 安装成功")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ spaCy 模型 {model_name} 安装失败: {e}")
        return False
    except Exception as e:
        print(f"❌ spaCy 模型 {model_name} 安装异常: {e}")
        return False


def _get_nlp(lang_code: str = "en", auto_install: bool = True):
    """获取指定语言的 spaCy 模型，支持缓存和自动安装，失败则抛出异常"""
    if lang_code not in _nlp_cache:
        import spacy
        model_name = SPACY_MODELS.get(lang_code, "en-core-web-sm")
        # spaCy load() 需要下划线格式（en_core_web_sm），pip 包名使用连字符格式（en-core-web-sm）
        spacy_model_name = model_name.replace("-", "_")
        print(f"🔍 尝试加载 spaCy 模型: lang_code={lang_code}, model_name={model_name}, spacy_model_name={spacy_model_name}")

        try:
            _nlp_cache[lang_code] = spacy.load(spacy_model_name)
            print(f"✅ spaCy 模型 {spacy_model_name} 加载成功")
        except OSError as e:
            print(f"❌ spaCy 模型 {spacy_model_name} 加载失败: {e}")
            # 模型未安装，尝试自动安装
            if auto_install:
                print(f"⚠️ spaCy 模型 {model_name} 未安装，正在自动下载...")
                if _install_spacy_model(model_name):
                    try:
                        _nlp_cache[lang_code] = spacy.load(spacy_model_name)
                        return _nlp_cache[lang_code]
                    except OSError:
                        pass
            # 自动安装失败或不需要安装，抛出异常而不是回退
            raise RuntimeError(f"spaCy 模型 {model_name} 未安装，请先运行: python -m spacy download {model_name}")
    return _nlp_cache[lang_code]


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


def nlp_segment(batch: list[SubtitleEntry],
                log_fn=None,
                lang_code: str = "en") -> list[SubtitleEntry]:
    """使用 spaCy 对一批字幕做句子边界检测并合并碎片条目。

    返回合并后的条目列表，条目数可能少于输入。
    """
    if not batch:
        return []

    nlp = _get_nlp(lang_code)

    # 1. 拼接所有原文文本，记录每条条目在拼接文本中的起止位置
    positions: list[tuple[int, int]] = []
    parts: list[str] = []
    offset = 0
    for entry in batch:
        source_text = _get_source_text(entry, lang_code).strip()
        if not source_text:
            positions.append((offset, offset))
            parts.append("")
            continue
        parts.append(source_text)
        positions.append((offset, offset + len(source_text)))
        offset += len(source_text) + 1  # +1 for space separator

    joined = " ".join(p for p in parts if p)
    if not joined:
        if log_fn:
            empty_count = sum(1 for e in batch if not _get_source_text(e, lang_code).strip())
            if empty_count == len(batch):
                log_fn(f"  ⚠️ 所有条目的原文文本为空，请检查「原文行数」设置是否正确")
            else:
                log_fn(f"  ⚠️ 拼接后文本为空，但并非所有条目都为空 ({empty_count}/{len(batch)})")
        return []

    # 2. spaCy 句子边界检测
    doc = nlp(joined)
    sentences = list(doc.sents)

    # 3. 将每个 spaCy 句子映射到它覆盖的字幕条目
    sent_to_entries: list[list[int]] = [[] for _ in sentences]
    for entry_idx, (entry_start, entry_end) in enumerate(positions):
        entry = batch[entry_idx]
        if not _get_source_text(entry, lang_code).strip():
            continue
        for sent_idx, sent in enumerate(sentences):
            if entry_start < sent.end_char and entry_end > sent.start_char:
                sent_to_entries[sent_idx].append(entry_idx)

    # 4. 合并：每个句子生成一条合并后的字幕
    results: list[SubtitleEntry] = []
    emitted: set[int] = set()

    for sent_idx, covered_indices in enumerate(sent_to_entries):
        if not covered_indices:
            continue

        # 跳过已被上一句覆盖的条目（一个条目只属于一个句子）
        new_indices = [i for i in covered_indices if i not in emitted]
        if not new_indices:
            continue
        emitted.update(new_indices)

        entries_to_merge = [batch[i] for i in sorted(new_indices)]

        # 调试日志：记录合并候选
        if log_fn and len(entries_to_merge) > 1:
            ids = [e.index for e in entries_to_merge]
            texts = [_get_source_text(e, lang_code)[:30] for e in entries_to_merge]
            log_fn(f"  🔍 NLP合并候选: {ids} texts={texts}")

        # 安全约束检查
        if not _nlp_merge_is_safe(entries_to_merge, lang_code):
            if log_fn and len(entries_to_merge) > 1:
                log_fn(f"  🔒 NLP合并被安全检查拒绝: {[e.index for e in entries_to_merge]}")
            for e in entries_to_merge:
                clone = SubtitleEntry(e.index, e.start_time, e.end_time,
                                      list(e.lines))
                clone.original_lines = list(e.original_lines or e.lines)
                clone.source_indexes = list(e.source_indexes or [e.index])
                results.append(clone)
            continue

        if log_fn and len(entries_to_merge) > 1:
            log_fn(f"  ✅ NLP合并被安全检查允许: {[e.index for e in entries_to_merge]}")

        # 构建合并后的条目
        first_entry = entries_to_merge[0]
        last_entry = entries_to_merge[-1]

        merged_source = " ".join(_get_source_text(e, lang_code).strip()
                                 for e in entries_to_merge
                                 if _get_source_text(e, lang_code).strip())
        merged_zh = "".join(e.target.strip() for e in entries_to_merge
                           if e.target.strip())

        lines = []
        if lang_code == "zh":
            # 中文输入时，中文是原文
            if merged_source:
                lines.append(merged_source)
        else:
            # 非中文输入时，中文是译文
            if merged_zh:
                lines.append(merged_zh)
            if merged_source:
                lines.append(merged_source)

        if log_fn and len(entries_to_merge) > 1:
            ids = [e.index for e in entries_to_merge]
            log_fn(f"  🔗 NLP合并 [{ids[0]}] ← {ids}")

        merged = SubtitleEntry(
            index=first_entry.index,
            start_time=first_entry.start_time,
            end_time=last_entry.end_time,
            lines=lines,
        )
        orig = []
        for e in entries_to_merge:
            orig.extend(e.lines)
        merged.original_lines = orig if orig else list(lines)
        # 继承来源链路
        merged.source_indexes = []
        for e in entries_to_merge:
            merged.source_indexes.extend(e.source_indexes or [e.index])
        results.append(merged)

    results.sort(key=lambda e: _srt_time_to_ms(e.start_time))

    # 诊断日志：NLP 合并前后条目数变化
    # 用 source_indexes 做对比，避免子条目（大序号）导致的误报
    if log_fn and len(batch) != len(results):
        before_ids = {idx for e in batch for idx in (e.source_indexes or [e.index])}
        after_ids = {idx for e in results for idx in (e.source_indexes or [e.index])}
        lost_ids = sorted(before_ids - after_ids)
        if lost_ids:
            log_fn(f"  ⚠️ NLP合并丢失条目: {lost_ids}")

    return results


def _nlp_merge_is_safe(entries: list[SubtitleEntry], lang_code: str = "en") -> bool:
    """检查 NLP 合并候选是否安全。

    保护规则：
    1. gap 过大（>1.2s）说明是独立句子，不该合并
    2. 单条过长（>=35词）说明是完整长句，不该硬拼
    3. 极短碎片（<=3词）或极短时长（<=500ms）禁止被合并，防止丢失
    4. 完整短句（句号结尾且<=15词）禁止合并
    """
    if len(entries) <= 1:
        return True

    # 调试：记录被检查的条目
    ids = [e.index for e in entries]
    details = []
    for e in entries:
        src = _get_source_text(e, lang_code)
        wc = _source_text_length(src, lang_code)
        dur = _srt_time_to_ms(e.end_time) - _srt_time_to_ms(e.start_time)
        details.append(f"[{e.index}]w={wc}d={dur}ms")
    logger.info(f"[NLP_SAFE] checking {ids} details={details}")

    for left, right in zip(entries, entries[1:]):
        gap = _srt_time_to_ms(right.start_time) - _srt_time_to_ms(left.end_time)
        if gap > 1200:
            return False

    for e in entries:
        source_text = _get_source_text(e, lang_code)
        # 使用语言感知的长度检查（中文按字符数，英文按单词数）
        if _source_text_length(source_text, lang_code) >= 35:
            return False

    # 合并后总词数上限：超过 60 词则拒绝，防止多条短句合成超长块送给 LLM
    total_words = sum(
        _source_text_length(_get_source_text(e, lang_code), lang_code)
        for e in entries
    )
    if total_words > 60:
        return False

    # 保护极短碎片：任何条目 <=3 词或时长 <=500ms，禁止合并
    for e in entries:
        source_text = _get_source_text(e, lang_code)
        word_count = _source_text_length(source_text, lang_code)
        duration_ms = _srt_time_to_ms(e.end_time) - _srt_time_to_ms(e.start_time)
        if word_count <= 3 or duration_ms <= 500:
            logger.debug(f"[NLP] 短条目保护: [{e.index}] words={word_count} dur={duration_ms}ms -> 禁止合并")
            return False

    # 保护完整短句：以句号结尾且词数<=15，禁止合并
    for e in entries:
        source_text = _get_source_text(e, lang_code).strip()
        if source_text.endswith(".") and _source_text_length(source_text, lang_code) <= 15:
            return False

    return True


def get_model_install_command(lang_code: str) -> str:
    """获取 spaCy 模型安装命令"""
    model_name = SPACY_MODELS.get(lang_code, "en_core_web_sm")
    return f"python -m spacy download {model_name}"