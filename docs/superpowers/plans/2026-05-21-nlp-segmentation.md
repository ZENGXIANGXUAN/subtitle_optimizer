# NLP 断句预合并 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert spaCy-based NLP sentence boundary detection before LLM segment step to merge obvious grammar fragments, improving overall segmentation accuracy.

**Architecture:** New `core/segmenter.py` module with a single public function `nlp_segment(batch)` that uses spaCy to detect sentence boundaries and merge fragmented entries. Called in `SegmentWorker._segment_batch` and `BatchWorker._segment_batch` before the LLM call.

**Tech Stack:** spaCy 3.7+, en_core_web_sm, Python 3.10+, unittest

---

### Task 1: Install spaCy dependency

- [ ] **Step 1: Install spaCy**

```bash
pip install spacy
```

- [ ] **Step 2: Download en_core_web_sm model**

```bash
python -m spacy download en_core_web_sm
```

- [ ] **Step 3: Verify installation**

```bash
python -c "import spacy; nlp = spacy.load('en_core_web_sm'); print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Add to requirements.txt**

```
spacy>=3.7
```

Edit `subtitle_optimizer/requirements.txt` to append `spacy>=3.7`.

---

### Task 2: Create NLP segmenter module with tests

**Files:**
- Create: `subtitle_optimizer/core/segmenter.py`
- Create: `subtitle_optimizer/test_nlp_segmenter.py`

- [ ] **Step 1: Write the failing tests**

Create `subtitle_optimizer/test_nlp_segmenter.py`:

```python
import unittest
from core.models import SubtitleEntry
from core.segmenter import nlp_segment


class NlpSegmenterTest(unittest.TestCase):
    def test_complete_sentences_kept_separate(self):
        """完整句子保持独立，不合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,500",
                          ["市场结构很复杂", "The market structure is complex."]),
            SubtitleEntry(2, "00:00:02,500", "00:00:04,000",
                          ["交易者需要理解这一点", "Traders need to understand this."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].index, 1)
        self.assertEqual(result[1].index, 2)

    def test_fragments_merged_into_one(self):
        """英文半句碎片合并为一条"""
        entries = [
            SubtitleEntry(5, "00:00:10,000", "00:00:11,500",
                          ["市场结构", "The market structure is"]),
            SubtitleEntry(6, "00:00:11,500", "00:00:13,000",
                          ["由三部分组成", "composed of three key elements."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].index, 5)
        self.assertIn("The market structure is", result[0].english)
        self.assertIn("composed of three key elements", result[0].english)

    def test_empty_entries_dropped(self):
        """中英文为空的条目被丢弃"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:02,000",
                          ["正常字幕", "Normal subtitle"]),
            SubtitleEntry(2, "00:00:02,000", "00:00:03,000",
                          ["", ""]),
            SubtitleEntry(3, "00:00:03,000", "00:00:04,000",
                          ["另一条", "Another one."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 2)
        ids = [e.index for e in result]
        self.assertIn(1, ids)
        self.assertIn(3, ids)
        self.assertNotIn(2, ids)

    def test_merged_timespan_matches_first_and_last(self):
        """合并后时间轴：start 取首条，end 取末条"""
        entries = [
            SubtitleEntry(1, "00:01:00,000", "00:01:01,500",
                          ["开头", "The beginning of"]),
            SubtitleEntry(2, "00:01:01,500", "00:01:03,000",
                          ["结尾", "the sentence."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].start_time, "00:01:00,000")
        self.assertEqual(result[0].end_time, "00:01:03,000")

    def test_long_complete_sentence_not_merged(self):
        """长完整句子即使语义相关也不合并"""
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:05,000",
                          ["长句A", "This is a very long complete sentence that "
                           "has more than twenty words and ends with proper "
                           "punctuation and is grammatically sound."]),
            SubtitleEntry(2, "00:00:05,000", "00:00:09,000",
                          ["长句B", "Another very long complete sentence that "
                           "also has more than twenty words and should stay "
                           "independent and separate from the previous one."]),
        ]
        result = nlp_segment(entries)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd subtitle_optimizer && python -m pytest test_nlp_segmenter.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.segmenter'`

- [ ] **Step 3: Write the NLP segmenter module**

Create `subtitle_optimizer/core/segmenter.py`:

```python
import re
from core.models import SubtitleEntry, _english_word_count


_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def _srt_time_to_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", str(value).strip())
    if not match:
        return 0
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def nlp_segment(batch: list[SubtitleEntry]) -> list[SubtitleEntry]:
    """使用 spaCy 对一批字幕做句子边界检测并合并碎片条目。

    返回合并后的条目列表，条目数可能少于输入。
    """
    if not batch:
        return []

    nlp = _get_nlp()

    # 1. 拼接所有英文文本，记录每条条目在拼接文本中的起止位置
    positions: list[tuple[int, int]] = []  # (start, end) char offsets
    parts: list[str] = []
    offset = 0
    for entry in batch:
        en = entry.english.strip()
        if not en:
            positions.append((offset, offset))
            parts.append("")
            continue
        parts.append(en)
        positions.append((offset, offset + len(en)))
        offset += len(en) + 1  # +1 for space separator

    joined = " ".join(p for p in parts if p)
    if not joined:
        return []

    # 2. spaCy 句子边界检测
    doc = nlp(joined)
    sentences = list(doc.sents)

    # 3. 将每个 spaCy 句子映射到它覆盖的字幕条目
    sent_to_entries: list[list[int]] = [[] for _ in sentences]
    for entry_idx, (entry_start, entry_end) in enumerate(positions):
        entry = batch[entry_idx]
        if not entry.english.strip():
            continue  # 空英文丢弃
        for sent_idx, sent in enumerate(sentences):
            # 判断该条目文本是否与句子有重叠
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
        if not _nlp_merge_is_safe(entries_to_merge):
            # 不安全则保持原样
            for e in entries_to_merge:
                clone = SubtitleEntry(e.index, e.start_time, e.end_time,
                                      list(e.lines))
                clone.original_lines = list(e.original_lines or e.lines)
                results.append(clone)
            continue

        # 构建合并后的条目
        first_entry = entries_to_merge[0]
        last_entry = entries_to_merge[-1]

        merged_en = " ".join(e.english.strip() for e in entries_to_merge
                             if e.english.strip())
        merged_zh = "".join(e.chinese.strip() for e in entries_to_merge
                            if e.chinese.strip())

        lines = []
        if merged_zh:
            lines.append(merged_zh)
        if merged_en:
            lines.append(merged_en)

        merged = SubtitleEntry(
            index=first_entry.index,
            start_time=first_entry.start_time,
            end_time=last_entry.end_time,
            lines=lines,
        )
        # 收集原始行
        orig = []
        for e in entries_to_merge:
            orig.extend(e.lines)
        merged.original_lines = orig if orig else list(lines)
        results.append(merged)

    results.sort(key=lambda e: e.index)
    return results


def _nlp_merge_is_safe(entries: list[SubtitleEntry]) -> bool:
    """检查 NLP 合并候选是否安全，与 models._merge_candidate_is_safe 一致。"""
    if len(entries) <= 1:
        return True

    # 时间跨度不超过 8000ms
    start_ms = _srt_time_to_ms(entries[0].start_time)
    end_ms = _srt_time_to_ms(entries[-1].end_time)
    if end_ms - start_ms > 8000:
        return False

    # 相邻条目间隔不超过 1200ms
    for left, right in zip(entries, entries[1:]):
        gap = _srt_time_to_ms(right.start_time) - _srt_time_to_ms(left.end_time)
        if gap > 1200:
            return False

    # 合并后英文词数不超过 42
    merged_en = " ".join(e.english for e in entries if e.english)
    if _english_word_count(merged_en) > 42:
        return False

    # 任一来源条目本身已是长句（≥20词）则不参与合并
    for e in entries:
        if _english_word_count(e.english) >= 20:
            return False

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd subtitle_optimizer && python -m pytest test_nlp_segmenter.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add subtitle_optimizer/core/segmenter.py subtitle_optimizer/test_nlp_segmenter.py subtitle_optimizer/requirements.txt
git commit -m "feat: add spaCy NLP sentence boundary detection for subtitle pre-segmentation"
```

---

### Task 3: Integrate NLP segmenter into workers

**Files:**
- Modify: `subtitle_optimizer/core/workers.py`

- [ ] **Step 1: Insert nlp_segment call in SegmentWorker._segment_batch**

In `subtitle_optimizer/core/workers.py`, add import at top (after existing imports):

```python
from core.segmenter import nlp_segment
```

In `SegmentWorker._segment_batch` method (line 575), before `items_json = json.dumps(...)`, insert:

```python
        # NLP 预断句合并
        batch = nlp_segment(batch)
        if not batch:
            return []
```

The method currently starts at line 575 with:
```python
    async def _segment_batch(self, batch: list[SubtitleEntry],
                              session: aiohttp.ClientSession) -> list[SubtitleEntry]:
        items_json = json.dumps([
```

After modification:
```python
    async def _segment_batch(self, batch: list[SubtitleEntry],
                              session: aiohttp.ClientSession) -> list[SubtitleEntry]:
        # NLP 预断句合并
        batch = nlp_segment(batch)
        if not batch:
            return []

        items_json = json.dumps([
```

- [ ] **Step 2: Insert nlp_segment call in BatchWorker._segment_batch**

In `BatchWorker._segment_batch` method (line 808), before `items_json = json.dumps(...)`, insert the same block:

```python
    async def _segment_batch(self, batch: list[SubtitleEntry], context: str, glossary: str,
                               session: aiohttp.ClientSession,
                               is_native: bool = False) -> list[SubtitleEntry]:
        # NLP 预断句合并
        batch = nlp_segment(batch)
        if not batch:
            return []

        items_json = json.dumps([
```

- [ ] **Step 3: Run existing tests to verify no regressions**

```bash
cd subtitle_optimizer && python -m pytest test_segment_guards.py test_nlp_segmenter.py test_analysis_prompt.py -v
```

- [ ] **Step 4: Commit**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "feat: integrate NLP pre-segmentation into SegmentWorker and BatchWorker"
```

---

### Task 4: Verification

- [ ] **Step 1: Run all tests**

```bash
cd subtitle_optimizer && python -m pytest -v
```

- [ ] **Step 2: Verify spaCy model loaded correctly**

```bash
python -c "from core.segmenter import nlp_segment; from core.models import SubtitleEntry; e = [SubtitleEntry(1, '00:00:01,000', '00:00:02,000', ['测试', 'This is a test.'])]; r = nlp_segment(e); print(f'{len(r)} entries: {r[0].english}')"
```

Expected: `1 entries: This is a test.`
