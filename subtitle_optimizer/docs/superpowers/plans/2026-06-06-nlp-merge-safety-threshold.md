# NLP 合并安全检查阈值调整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 调整 `_nlp_merge_is_safe` 的短条目保护和标点检查阈值，减少对口语化字幕的过度拒绝

**Architecture:** 修改 `core/segmenter.py` 中 `_nlp_merge_is_safe` 函数的两条规则（R3 短条目保护、R4 逗号结尾检查），并清理 `core/workers.py` 中的 dead import

**Tech Stack:** Python, pytest, unittest

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `test_nlp_safety.py` | 创建 | `_nlp_merge_is_safe` 的单元测试 |
| `core/segmenter.py:294-309` | 修改 | R3 阈值 + R4 逗号检查 |
| `core/workers.py:15` | 修改 | 移除 dead import |

---

### Task 1: 为 `_nlp_merge_is_safe` 编写测试（TDD）

**Files:**
- Create: `test_nlp_safety.py`
- Read: `core/segmenter.py:261-311`

- [ ] **Step 1: 创建测试文件，编写 R3 阈值测试**

```python
import unittest
from core.models import SubtitleEntry
from core.segmenter import _nlp_merge_is_safe


class NlpMergeSafetyTest(unittest.TestCase):
    """测试 _nlp_merge_is_safe 的安全检查规则"""

    # --- R3: 短条目保护 ---

    def test_rejects_entry_with_3_words_or_fewer(self):
        """≤3词的条目应被拒绝合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,000",
                          ["你好", "Hi there."]),  # 2词
            SubtitleEntry(2, "00:00:02,000", "00:00:03,500",
                          ["这是测试", "This is a test."]),  # 5词
        ]
        self.assertFalse(_nlp_merge_is_safe(entries))

    def test_allows_entry_with_4_words(self):
        """4词的条目应被允许合并（如果其他条件满足）"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["很受欢迎", "and it's very popular."]),  # 4词
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["很多人用", "Many people use it."]),  # 4词
        ]
        # 应该通过（4词 > 3词阈值）
        self.assertTrue(_nlp_merge_is_safe(entries))

    def test_rejects_entry_with_duration_500ms_or_less(self):
        """时长≤500ms的条目应被拒绝合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:01,400",  # 400ms
                          ["测试文本", "Test text here."]),  # 3词
            SubtitleEntry(2, "00:00:01,400", "00:00:03,000",
                          ["更多文本", "More text here."]),  # 3词
        ]
        self.assertFalse(_nlp_merge_is_safe(entries))

    def test_allows_entry_with_duration_501ms(self):
        """时长501ms的条目应被允许合并（如果其他条件满足）"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:01,600",  # 600ms
                          ["这是测试", "This is a test."]),  # 5词
            SubtitleEntry(2, "00:00:01,600", "00:00:03,000",
                          ["更多内容", "More content here."]),  # 3词
        ]
        self.assertTrue(_nlp_merge_is_safe(entries))

    # --- R4: 逗号结尾检查 ---

    def test_allows_entry_ending_with_comma(self):
        """以逗号结尾的条目应被允许合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["周二", "Today is Tuesday,"]),  # 逗号结尾
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["十月三十一号", "October 31st."]),  # 句号结尾
        ]
        # 逗号结尾不再阻止合并
        self.assertTrue(_nlp_merge_is_safe(entries))

    def test_rejects_short_sentence_ending_with_period(self):
        """以句号结尾且≤15词的条目应被拒绝合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["市场结构复杂", "The market is complex."]),  # 4词, 句号
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["交易者需要理解", "Traders need to understand."]),  # 4词
        ]
        self.assertFalse(_nlp_merge_is_safe(entries))

    def test_allows_long_sentence_ending_with_period(self):
        """以句号结尾且>15词的条目应被允许合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:04,000",
                          ["长句", "This is a very long sentence that has "
                           "more than fifteen words and ends with a period."]),  # 19词
            SubtitleEntry(2, "00:00:04,000", "00:00:06,000",
                          ["下一句", "And here comes the next sentence."]),  # 6词
        ]
        # 19词 > 15词阈值，句号检查不阻止
        self.assertTrue(_nlp_merge_is_safe(entries))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试，确认 R3 测试失败（当前阈值 8词/1500ms 会通过 4词测试）**

Run: `python -m pytest test_nlp_safety.py -v`
Expected: `test_allows_entry_with_4_words` 和 `test_allows_entry_with_duration_501ms` FAIL（当前阈值过严）

- [ ] **Step 3: 确认 R4 测试失败（当前逗号检查会拒绝逗号结尾条目）**

Run: `python -m pytest test_nlp_safety.py::NlpMergeSafetyTest::test_allows_entry_ending_with_comma -v`
Expected: FAIL（当前逗号检查会返回 False）

- [ ] **Step 4: Commit 测试文件**

```bash
git add test_nlp_safety.py
git commit -m "test: add tests for _nlp_merge_is_safe threshold changes"
```

---

### Task 2: 修改 R3 短条目保护阈值

**Files:**
- Modify: `core/segmenter.py:294-301`

- [ ] **Step 1: 修改阈值**

在 `core/segmenter.py` 第 294-301 行，将：

```python
    # 保护短条目：任何条目 <=8 词或时长 <=1.5s，禁止合并
    for e in entries:
        source_text = _get_source_text(e, lang_code)
        word_count = _source_text_length(source_text, lang_code)
        duration_ms = _srt_time_to_ms(e.end_time) - _srt_time_to_ms(e.start_time)
        if word_count <= 8 or duration_ms <= 1500:
            logger.debug(f"[NLP] 短条目保护: [{e.index}] words={word_count} dur={duration_ms}ms -> 禁止合并")
            return False
```

修改为：

```python
    # 保护极短碎片：任何条目 <=3 词或时长 <=500ms，禁止合并
    for e in entries:
        source_text = _get_source_text(e, lang_code)
        word_count = _source_text_length(source_text, lang_code)
        duration_ms = _srt_time_to_ms(e.end_time) - _srt_time_to_ms(e.start_time)
        if word_count <= 3 or duration_ms <= 500:
            logger.debug(f"[NLP] 短条目保护: [{e.index}] words={word_count} dur={duration_ms}ms -> 禁止合并")
            return False
```

- [ ] **Step 2: 运行 R3 相关测试**

Run: `python -m pytest test_nlp_safety.py -v -k "word or duration"`
Expected: 全部 PASS

- [ ] **Step 3: Commit**

```bash
git add core/segmenter.py
git commit -m "fix: loosen short entry protection from <=8 words/1500ms to <=3 words/500ms"
```

---

### Task 3: 移除 R4 逗号结尾检查

**Files:**
- Modify: `core/segmenter.py:303-309`

- [ ] **Step 1: 移除逗号检查**

在 `core/segmenter.py` 第 303-309 行，将：

```python
    # 保护残句/完整短句：以逗号结尾或以句号结尾且词数<=15，禁止合并
    for e in entries:
        source_text = _get_source_text(e, lang_code).strip()
        if source_text.endswith(","):
            return False
        if source_text.endswith(".") and _source_text_length(source_text, lang_code) <= 15:
            return False
```

修改为：

```python
    # 保护完整短句：以句号结尾且词数<=15，禁止合并
    for e in entries:
        source_text = _get_source_text(e, lang_code).strip()
        if source_text.endswith(".") and _source_text_length(source_text, lang_code) <= 15:
            return False
```

- [ ] **Step 2: 运行 R4 相关测试**

Run: `python -m pytest test_nlp_safety.py -v -k "comma or period"`
Expected: 全部 PASS

- [ ] **Step 3: Commit**

```bash
git add core/segmenter.py
git commit -m "fix: remove comma-ending check from NLP merge safety"
```

---

### Task 4: 清理 dead import

**Files:**
- Modify: `core/workers.py:15`

- [ ] **Step 1: 移除未使用的导入**

在 `core/workers.py` 第 10-22 行，将：

```python
from core.models import (
    SubtitleEntry,
    parse_srt,
    strip_annotation,
    _format_subtitle_snapshot,
    _merge_candidate_is_safe,
    _translation_looks_incomplete,
    _english_word_count,
    _srt_time_to_ms,
    _ms_to_srt_time,
    _source_text_length,
    verify_source_integrity,
)
```

修改为：

```python
from core.models import (
    SubtitleEntry,
    parse_srt,
    strip_annotation,
    _format_subtitle_snapshot,
    _translation_looks_incomplete,
    _english_word_count,
    _srt_time_to_ms,
    _ms_to_srt_time,
    _source_text_length,
    verify_source_integrity,
)
```

- [ ] **Step 2: 运行现有测试确认无回归**

Run: `python -m pytest test_segment_guards.py test_nlp_segmenter.py test_nlp_safety.py -v`
Expected: 全部 PASS

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "chore: remove unused _merge_candidate_is_safe import from workers.py"
```

---

### Task 5: 全量测试验证

**Files:**
- Test: `test_segment_guards.py`
- Test: `test_nlp_segmenter.py`
- Test: `test_nlp_safety.py`

- [ ] **Step 1: 运行所有相关测试**

Run: `python -m pytest test_segment_guards.py test_nlp_segmenter.py test_nlp_safety.py -v`
Expected: 全部 PASS，无回归

- [ ] **Step 2: 更新 docstring**

在 `core/segmenter.py` 第 262-268 行，将：

```python
    """检查 NLP 合并候选是否安全。

    保护规则：
    1. gap 过大（>1.2s）说明是独立句子，不该合并
    2. 单条过长（>=35词）说明是完整长句，不该硬拼
    3. 短条目（<=8词）或短时长（<=1.5s）禁止被合并，防止丢失
    4. 残句（逗号结尾）或完整短句（句号结尾且<=15词）禁止合并
    """
```

修改为：

```python
    """检查 NLP 合并候选是否安全。

    保护规则：
    1. gap 过大（>1.2s）说明是独立句子，不该合并
    2. 单条过长（>=35词）说明是完整长句，不该硬拼
    3. 极短碎片（<=3词）或极短时长（<=500ms）禁止被合并，防止丢失
    4. 完整短句（句号结尾且<=15词）禁止合并
    """
```

- [ ] **Step 3: Commit**

```bash
git add core/segmenter.py
git commit -m "docs: update _nlp_merge_is_safe docstring to reflect new thresholds"
```
