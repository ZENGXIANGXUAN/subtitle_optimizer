# 字幕处理流水线统一重构设计

## 目标

统一单文件模式与批量模式的业务逻辑，消除重复代码。未来修改只需改一处。

## 架构

```
单文件模式：
  UI → Worker (Qt Signal) → SubtitlePipeline → 业务逻辑

批量模式：
  BatchWorker (Qt Signal) → SubtitlePipeline → 业务逻辑
```

### 职责划分

| 层 | 职责 | 依赖 |
|---|---|---|
| `SubtitlePipeline` | 纯业务逻辑：API调用、重试、修复、并发控制 | aiohttp, MistralClient, models |
| `Worker` (单文件) | Qt Signal 发射、UI 进度通知、调用 Pipeline | Pipeline, PyQt6 |
| `BatchWorker` | 文件遍历、写文件、Qt Signal、调用 Pipeline | Pipeline, PyQt6 |

### 不变的部分

- Prompt 内容
- API 调用方式（MistralClient）
- 翻译/优化核心逻辑函数（`_translate_batch_impl`、`_optimize_batch_impl` 等）
- 并发策略（Semaphore + batch_size）

## SubtitlePipeline 类

新增文件：`subtitle_optimizer/core/pipeline.py`

### 构造函数

```python
class SubtitlePipeline:
    def __init__(
        self,
        client: MistralClient,
        input_lang: str = "English",
        output_lang: str = "中文",
        concurrency: int = 12,
        batch_size: int = 5,
    ):
```

### 4个阶段方法

```
Pipeline.analyze(entries) → context (str)
Pipeline.segment(entries, context, glossary) → entries
Pipeline.translate(entries, context, glossary) → entries
Pipeline.optimize(entries, context, glossary) → entries
```

| 阶段 | 方法 | 输入 | 输出 | 内部逻辑 |
|---|---|---|---|---|
| 1 | `analyze` | entries | context 字符串 | 构建分析 prompt → 调 API → 格式化结果 |
| 2 | `segment` | entries + context | 重组后的 entries | LLM合并不完整句 → 短句直通/长句LLM切分 → 去重排序 → 完整性校验 |
| 3 | `translate` | entries + context + glossary | 翻译后的 entries | 分批翻译（带上下文）→ 漏译检测 → 修复漏译（含 API 重试） |
| 4 | `optimize` | entries + context + glossary | 优化后的 entries | 分批优化（带上下文）→ 写入 optimized_lines |

阶段之间串行：前一阶段输出是后一阶段输入。

### 回调参数

每个方法接受可选回调：

```python
on_progress: Callable[[int, int], None] | None  # (current, total)
on_log: Callable[[str], None] | None
is_cancelled: Callable[[], bool] | None
```

`analyze` 的 `on_progress` 为 `Callable[[str], None]`（进度文字）。

取消检查：每个方法在批次开始前和 API 调用前检查 `is_cancelled()`，若返回 True 则 raise `CancelledError`。

## Worker 改造

### 单文件 Worker

SegmentWorker、TranslateWorker、OptimizeWorker 改为薄壳：

```python
class SegmentWorker(BaseWorker):
    progress = pyqtSignal(int, int)
    all_done = pyqtSignal(object)
    log = pyqtSignal(str)

    def __init__(self, client, entries, context, glossary,
                 concurrency=4, batch_size=12,
                 input_lang="English", output_lang="中文"):
        super().__init__()
        self.pipeline = SubtitlePipeline(client, input_lang, output_lang, concurrency, batch_size)
        self.entries = entries
        self.context = context
        self.glossary = glossary

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        try:
            result = await self.pipeline.segment(
                self.entries, self.context, self.glossary,
                on_progress=self.progress.emit,
                on_log=self.log.emit,
                is_cancelled=lambda: self._cancelled,
            )
            self.all_done.emit(result)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(str(e))
```

TranslateWorker、OptimizeWorker、AnalysisWorker 同理。

### BatchWorker

删除以下方法：
- `_analyze` → 改用 `pipeline.analyze()`
- `_segment` + `_segment_batch` + `_llm_merge` → 改用 `pipeline.segment()`
- `_translate` → 改用 `pipeline.translate()`
- `_optimize` + `_optimize_batch` → 改用 `pipeline.optimize()`

保留：
- 文件遍历、读写
- `_extract_original_line`（双语字幕提取）
- `_get_out_path`（输出路径计算）
- `write_phase_snapshot` 调用
- 所有 Qt Signal 发射

## 错误处理与取消

| 场景 | Pipeline 行为 | Worker/BatchWorker 行为 |
|---|---|---|
| 单批次 API 失败 | 该批次 fallback（保留原文），继续处理 | 无感知 |
| 全部批次失败 | 正常返回 entries | 调用方自行判断 |
| 取消 | raise CancelledError | Worker 静默退出 |
| 未知异常 | 向上抛出 | Worker 捕获，emit error signal |

翻译失败兜底：Pipeline.translate() 内部保留现有兜底逻辑——翻译失败的条目保留原文，标记 `error_msg="translation_fallback"`，不中断整体流程。

## 代码迁移来源

| Pipeline 方法 | 代码来源 |
|---|---|
| `analyze()` | `AnalysisWorker._run()` 核心逻辑 + `BatchWorker._analyze()` |
| `segment()` | `SegmentWorker._run()` 核心逻辑 + `BatchWorker._segment()` + `_segment_batch()` + `_llm_merge()` |
| `translate()` | `TranslateWorker._run()` 核心逻辑 + `BatchWorker._translate()` + `_repair_incomplete_segment_translations()` |
| `optimize()` | `OptimizeWorker._run()` 核心逻辑 + `BatchWorker._optimize()` + `_optimize_batch()` |

共用函数保留在 workers.py：`_translate_batch_impl`、`_optimize_batch_impl`、`_build_analysis_messages`、`_format_analysis_result`、`_apply_llm_merge`、`_apply_llm_split` 等。Pipeline import 它们。

## 并发说明

当前系统使用 `entry.index` 作为唯一键写回翻译/优化结果，不存在线程竞争问题。并发风险来自 LLM 上下文污染，与并发无关，顺序执行同样存在。本次重构不关闭并发。

## 暂不实施

- 文件级并发（当前保持 `for file in files` 顺序处理）
- analyze 阶段的深度重构（本次一并统一，但不改 prompt）

## 验收标准

1. 同一字幕文件，单文件模式输出与批量模式输出完全一致
2. BatchWorker 中删除重复实现，减少代码量
3. 未来修改 prompt、重试策略、上下文策略、并发策略只需修改 Pipeline 一处
