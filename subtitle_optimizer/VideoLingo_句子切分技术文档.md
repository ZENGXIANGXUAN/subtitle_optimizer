# VideoLingo 句子切分技术文档

> Step 3 of 6 · v3.0.0 · [github.com/Huanshere/VideoLingo](https://github.com/Huanshere/VideoLingo)

---

## 3.1 第一阶段：spaCy NLP 粗切

**目标：** 将 ASR 输出的连续文字流，按语法结构做初步切分，生成词数不超过 `max_split_length`（默认 20）的候选字幕块。

### 工具与配置

| 项目 | 说明 |
|------|------|
| spaCy | 依存句法分析（Dependency Parsing）+ 词性标注，识别句子边界 |
| 语言模型映射 | `en_core_web_md` / `zh_core_web_md` / `ja_core_news_md` 等，按 `whisper.language` 自动加载 |
| `max_split_length` | 默认 20 词。< 18 切太细影响翻译；> 22 后续时间对齐困难 |

### 语言差异处理

| 分类 | 语言 | 分词策略 |
|------|------|----------|
| `language_split_with_space` | 英 / 西 / 法 / 德 / 意 / 俄 | 按空格分词，统计词数 |
| `language_split_without_space` | 中文 / 日文 | 按字符分词；中文额外使用 BELLE/large-v3 转录模型补充标点 |

### 切分逻辑

```
1. 加载语言对应的 spaCy 模型
2. 依存句法分析 → 获取句子边界列表（doc.sents）
3. 遍历候选句：
   if 词数 <= max_split_length:  → 直接保留
   else:  → 在依存关系最弱处强制切断（sub-split）
4. 输出：词数 ≤ max_split_length 的文本段列表，保留 word-level 时间戳
```

### 关键约束

- `max_split_length = 20`（推荐范围 18–22）：既保证翻译连贯，又让后续字幕对齐可控
- 保留 WhisperX 输出的词级时间戳，每个文本段均携带 start/end 时间信息
- 中文专用转录模型在 ASR 阶段预先增强标点，弥补无空格分词的劣势

---

## 3.2 第二阶段：LLM 语义细切

**目标：** 对第一阶段输出做语义级别的二次切分——处理 spaCy 无法识别的**口语化、无标点、长从句**等 ASR 场景，确保每块字幕在语义上也是完整单元。

### LLM 调用结构

```
System:  你是专业字幕切分助手，将文本按语义切分，
         每段 ≤ N 词，返回严格 JSON 数组。
User:    [第一阶段输出的文本块]

Response: { "segments": ["句子1", "句子2", ...] }
```

### 工程细节

| 机制 | 实现 |
|------|------|
| 多线程并发 | `max_workers`（默认 4）控制并发调用数；本地 LLM 建议设为 1 |
| JSON 修复 | 使用 `json_repair` 库自动修复非标准 JSON（缺引号、尾随逗号等） |
| 进度持久化 | 切分结果写入 `output/log/`，中断后可从断点续跑，无需重复调用 API |
| JSON 格式开关 | `llm_support_json: true` 可开启原生 JSON 模式，提升稳定性（视模型支持情况） |

### 支持的 LLM（推荐顺序）

| 模型 | 备注 |
|------|------|
| claude-3-5-sonnet / claude-opus-4-6 | JSON 遵从性强，首选 |
| gpt-4.1 | 稳定，推荐 |
| deepseek-v3 | 性价比高，推荐 |
| gemini-2.0-flash | 可用；2.5-flash 稳定性差，慎用 |
| Ollama 本地模型 | `max_workers: 1`；JSON 兼容性视模型而异 |

### 常见问题

- **LLM JSON 格式报错：** 删除 `output/` 目录，换用更强模型（如 claude-3-5-sonnet）重试
- **切割太碎：** 提高 `max_split_length`（如 22），或使用更强 LLM
- **本地运行：** api 指向 Ollama，`max_workers: 1`，TTS 使用 edge-tts

---

*VideoLingo v3.0.0 · Apache 2.0*
