# NLP 合并安全检查阈值调整

**日期:** 2026-06-06
**状态:** 已批准
**影响文件:** `core/segmenter.py`, `core/workers.py`

## 背景

阶段2（智能重组）存在大量合并被安全检查拒绝的情况。对 61 个被拒绝的合并候选进行分析后发现：

- 24 个案例由 `_merge_candidate_is_safe`（models.py）的 5 个条件解释
- 37 个案例无法由 models.py 解释，实际由 `_nlp_merge_is_safe`（segmenter.py）拒绝

**关键发现：** `_merge_candidate_is_safe` 在生产代码中从未被调用（仅在 workers.py 中导入但未使用），实际执行的安全检查是 `_nlp_merge_is_safe`。

## 问题分析

`_nlp_merge_is_safe` 的两条规则对口语化字幕过于严格：

### R3 短条目保护（≤8词 或 ≤1500ms）

SRT 数据中大量条目触发此规则：
- "Bryant here." — 2词, 570ms
- "2023." — 1词, 1035ms
- "and it's very popular." — 4词, 1380ms

这些条目是口语自然断句产生的碎片，恰恰是最需要合并的候选。

### R4 逗号结尾检查

SRT 中大量条目以逗号结尾：
- "Today is Tuesday, October 31st,"
- "...share a strategy I like to use whenever I don't have enough"
- "...two butterflies that"

逗号表示说话人在句中自然断句，以逗号结尾的条目是最需要合并的碎片。

## 设计

### 修改 1：短条目保护阈值

位置：`core/segmenter.py`，`_nlp_merge_is_safe` 函数

```python
# 修改前
if word_count <= 8 or duration_ms <= 1500:

# 修改后
if word_count <= 3 or duration_ms <= 500:
```

理由：仅保护极短碎片（≤3词或≤500ms），释放口语化断句产生的正常碎片。

### 修改 2：移除逗号结尾检查

位置：`core/segmenter.py`，`_nlp_merge_is_safe` 函数

```python
# 修改前
if source_text.endswith(","):
    return False
if source_text.endswith(".") and _source_text_length(source_text, lang_code) <= 15:
    return False

# 修改后（移除逗号检查，保留句号检查）
if source_text.endswith(".") and _source_text_length(source_text, lang_code) <= 15:
    return False
```

理由：逗号结尾是口语断句的正常标志，不应阻止合并。保留句号+≤15词检查，因为以句号结尾的短句通常是完整句子。

### 修改 3：清理 dead import

位置：`core/workers.py` 第 15 行

移除 `_merge_candidate_is_safe` 的导入（已确认未在生产代码中使用）。

### 不修改的部分

| 规则 | 当前值 | 保持理由 |
|------|--------|----------|
| R1 停顿 | > 1200ms | 合理的语音间隔边界 |
| R2 单条词数 | ≥ 35词 | 已是完整长句，不应硬拼 |
| 句号+≤15词 | 保留 | 完整短句不应被合并 |

## 预期影响

- R3 放宽：预计解放 15-25 个合并候选
- R4 放宽：预计解放 10-20 个合并候选
- 总计：37 个"无触发案例"中的大部分应被允许合并

## 验证方法

1. 使用原 SRT 文件（0DTE Twin Butterfly Strategy）重新运行阶段2
2. 对比修改前后的合并数量和拒绝数量
3. 检查日志中 `_nlp_merge_is_safe` 的拒绝案例是否显著减少
4. 验证合并后的字幕翻译质量无明显下降
