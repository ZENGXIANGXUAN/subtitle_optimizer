# 字幕处理流水线重构方案（统一单文件与批量模式）

## 背景

当前项目存在两套几乎重复的实现：

### 单文件模式

* AnalysisWorker
* SegmentWorker
* TranslateWorker
* OptimizeWorker

### 批量模式

* BatchWorker._analyze()
* BatchWorker._segment()
* BatchWorker._translate()
* BatchWorker._optimize()

两套代码维护成本高，后续修改 Prompt、重试逻辑、上下文策略、并发策略时容易出现：

* 单文件修复了 Bug
* 批量模式遗漏修复

或反之。

目前已经出现大量重复代码。

---

# 重构目标

统一业务逻辑。

保证：

* 单文件模式
* 批量模式

完全使用同一套处理实现。

未来任何修改只需改一个地方。

---

# 重要结论

本次重构不修改：

* Prompt
* API调用
* 翻译逻辑
* 优化逻辑
* 上下文策略
* 并发策略

仅修改调度层。

确保结果与当前单文件模式一致。

---

# 第一阶段：删除 BatchWorker 中重复实现

删除以下方法：

```python
BatchWorker._segment()

BatchWorker._translate()

BatchWorker._optimize()
```

保留：

```python
BatchWorker._analyze()
```

（分析阶段暂时可保留，后续再统一）

---

# 第二阶段：抽取公共 Pipeline

新增：

```python
class SubtitlePipeline:
```

职责：

```python
analyze()

segment()

translate()

optimize()
```

内部直接复用现有 Worker 的核心实现。

例如：

```python
class SubtitlePipeline:

    async def segment(...):
        worker = SegmentWorker(...)
        return await worker._run_internal()

    async def translate(...):
        worker = TranslateWorker(...)
        return await worker._run_internal()

    async def optimize(...):
        worker = OptimizeWorker(...)
        return await worker._run_internal()
```

注意：

不要调用 Qt Signal。

只保留纯业务逻辑。

---

# 第三阶段：Worker 变成 UI 外壳

保留：

```python
SegmentWorker
TranslateWorker
OptimizeWorker
```

但它们只负责：

* Qt Signal
* UI进度通知
* 调用 Pipeline

结构：

```python
SegmentWorker
      ↓
SubtitlePipeline.segment()

TranslateWorker
      ↓
SubtitlePipeline.translate()

OptimizeWorker
      ↓
SubtitlePipeline.optimize()
```

---

# 第四阶段：BatchWorker 改为调用 Pipeline

原来：

```python
entries = await self._segment(...)
entries = await self._translate(...)
entries = await self._optimize(...)
```

改成：

```python
pipeline = SubtitlePipeline(...)

entries = await pipeline.segment(...)
entries = await pipeline.translate(...)
entries = await pipeline.optimize(...)
```

这样：

单文件：

```text
UI
 ↓
Worker
 ↓
Pipeline
```

批量：

```text
BatchWorker
 ↓
Pipeline
```

业务逻辑完全统一。

---

# 并发问题说明（必须保留）

不要因为担心字幕错位而关闭翻译和优化阶段并发。

原因：

当前系统写回逻辑使用：

```python
entry.index
```

作为唯一键。

例如：

```python
result[item["id"]] = tgt
```

以及：

```python
result_map.get(entry.index)
```

写回。

因此不会发生：

```text
3写到4
4写到3
```

这种线程竞争问题。

---

# 真正风险

风险来自：

```text
上下文污染
```

而不是：

```text
线程竞争
```

例如：

```text
字幕3引用了字幕4内容
字幕4又引用了字幕3内容
```

这是 LLM 输出问题。

与并发无关。

即使顺序执行也会发生。

---

# Segment阶段特别说明

重组阶段存在跨批次问题：

例如：

批次A：

1
2
3

批次B：

4
5
6

真实语义：

3 + 4

应该合并。

但：

A看不到4

B看不到3

则无法合并。

此问题：

* 与并发无关
* 顺序执行同样存在

因此本次重构不要因为此问题关闭并发。

后续如果需要解决：

应增加：

```python
SEGMENT_CONTEXT_SIZE
```

而不是取消并发。

---

# 本次重构验收标准

## 功能一致

同一字幕文件：

单文件模式输出

与

批量模式输出

完全一致。

---

## 代码删除

删除 BatchWorker 中重复实现。

减少重复代码。

---

## 后续维护

未来修改：

* Prompt
* 重试策略
* 上下文策略
* 并发策略

只需修改一处。

单文件与批量自动同步。

---

# 暂不实施

本次不做：

## 文件级并发

暂时保持：

```python
for file in files:
```

顺序处理。

原因：

当前每个文件内部已经存在：

```python
Semaphore(concurrency)
```

高并发。

后续稳定后再考虑：

```python
file_concurrency = 2~3
```

实现文件级并发。
