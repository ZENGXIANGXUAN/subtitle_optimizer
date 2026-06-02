# 智能重组优化 — 设计文档

## 概述

将原来单一的「LLM 找碎片→合并」阶段拆分为两层：
1. **NLP 预断句**（spaCy 本地）：语法层面的碎片合并
2. **LLM 语义细切**：语义层面的长句切割

管道变化：
```
分析情景 → [NLP 预断句] → [LLM 语义细切] → 翻译 → 优化润色
              spaCy 合碎片      LLM 切长句
```

## 第一阶段：NLP 预断句（`core/segmenter.py` 新增）

使用 spaCy 依存句法分析做句子边界检测：

1. 拼接批次内所有条目的英文，记录每条在文本中的字符位置
2. spaCy 检测句子边界（`doc.sents`）
3. 一个句子覆盖 N 条条目 → 合并为 1 条
4. 一个句子覆盖 1 条条目 → 保持独立

安全约束（与 `_merge_candidate_is_safe` 一致）：
- 合并后词数 ≤ 42
- 合并后时长 ≤ 8 秒
- 相邻条目间隔 ≤ 1.2 秒
- 任一来源条目本身 ≥ 20 词则不参与合并

依赖：spaCy + en_core_web_sm（约 13MB），本地处理 ~10-20ms/批。

## 第二阶段：LLM 语义细切（workers.py 修改）

NLP 合并后，条目分为两类：
- **短条目**（≤20 词）：直通，跳过 LLM
- **长条目**（>20 词）：送 LLM 在语义最弱处切分为 2-3 段

LLM Prompt 核心：
- 在语义最自然的断点切分（从句结束、意群转折、自然停顿）
- 每段 ≤20 词，必须是相对完整的语义单元
- 不修改原文，只确定断点位置

时间戳分配：按切分段词数比例分配原条目的时间跨度。

## 变更文件

| 文件 | 操作 |
|------|------|
| `core/models.py` | 新增 `_ms_to_srt_time` 辅助函数 |
| `core/segmenter.py` | **新建** - NLP 预断句模块 |
| `test_nlp_segmenter.py` | **新建** - NLP 断句测试 |
| `core/workers.py` | NLP 集成 + LLM 阶段重写为切分模式 |
| `requirements.txt` | 添加 `spacy>=3.7` |
| `pyproject.toml` | 添加 `spacy>=3.7` |

## 参考

- [VideoLingo 句子切分技术文档](../subtitle_optimizer/VideoLingo_句子切分技术文档.md)
