import re
import subprocess
import sys
from core.models import SubtitleEntry, _english_word_count, _srt_time_to_ms, _source_text_length

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


def _install_spacy_model(model_name: str) -> bool:
    """自动下载并安装 spaCy 模型"""
    try:
        print(f"📦 正在下载 spaCy 模型 {model_name}...")
        subprocess.check_call(
            [sys.executable, "-m", "spacy", "download", model_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"✅ spaCy 模型 {model_name} 安装成功")
        return True
    except subprocess.CalledProcessError:
        print(f"❌ spaCy 模型 {model_name} 安装失败")
        return False


def _get_nlp(lang_code: str = "en", auto_install: bool = True):
    """获取指定语言的 spaCy 模型，支持缓存、自动安装和回退"""
    if lang_code not in _nlp_cache:
        import spacy
        model_name = SPACY_MODELS.get(lang_code, "en-core-web-sm")
        # spaCy load() 需要下划线格式（en_core_web_sm），pip 包名使用连字符格式（en-core-web-sm）
        spacy_model_name = model_name.replace("-", "_")

        try:
            _nlp_cache[lang_code] = spacy.load(spacy_model_name)
        except OSError:
            # 模型未安装，尝试自动安装
            if auto_install and lang_code != "en":
                print(f"⚠️ spaCy 模型 {model_name} 未安装，正在自动下载...")
                if _install_spacy_model(model_name):
                    try:
                        _nlp_cache[lang_code] = spacy.load(spacy_model_name)
                        return _nlp_cache[lang_code]
                    except OSError:
                        pass

            # 自动安装失败或不需要安装，回退到英文模型
            if lang_code != "en":
                print(f"⚠️ 回退到英文模型")
            _nlp_cache[lang_code] = spacy.load("en_core_web_sm")
    return _nlp_cache[lang_code]


def _get_source_text(entry: SubtitleEntry, lang_code: str) -> str:
    """获取字幕条目的原文文本（根据输入语言）"""
    # 如果是中文输入，返回 chinese 属性
    if lang_code == "zh":
        return entry.chinese
    # 其他语言，返回 english 属性（非中文文本）
    return entry.english


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

        # 安全约束检查
        if not _nlp_merge_is_safe(entries_to_merge, lang_code):
            for e in entries_to_merge:
                clone = SubtitleEntry(e.index, e.start_time, e.end_time,
                                      list(e.lines))
                clone.original_lines = list(e.original_lines or e.lines)
                results.append(clone)
            continue

        # 构建合并后的条目
        first_entry = entries_to_merge[0]
        last_entry = entries_to_merge[-1]

        merged_source = " ".join(_get_source_text(e, lang_code).strip()
                                 for e in entries_to_merge
                                 if _get_source_text(e, lang_code).strip())
        merged_zh = "".join(e.chinese.strip() for e in entries_to_merge
                           if e.chinese.strip())

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
        results.append(merged)

    results.sort(key=lambda e: e.index)
    return results


def _nlp_merge_is_safe(entries: list[SubtitleEntry], lang_code: str = "en") -> bool:
    """检查 NLP 合并候选是否安全。

    仅检查语法层面：gap 过大说明是独立句子不该合并，
    单条过长说明是完整长句不该硬拼。长度约束（词数/时长）由 LLM 切分阶段处理。
    """
    if len(entries) <= 1:
        return True

    for left, right in zip(entries, entries[1:]):
        gap = _srt_time_to_ms(right.start_time) - _srt_time_to_ms(left.end_time)
        if gap > 1200:
            return False

    for e in entries:
        source_text = _get_source_text(e, lang_code)
        # 使用语言感知的长度检查（中文按字符数，英文按单词数）
        if _source_text_length(source_text, lang_code) >= 35:
            return False

    return True


def get_model_install_command(lang_code: str) -> str:
    """获取 spaCy 模型安装命令"""
    model_name = SPACY_MODELS.get(lang_code, "en_core_web_sm")
    return f"python -m spacy download {model_name}"
