# 翻译流程优化版说明

## 必须同时替换的文件

- `pipeline.py`
- `client.py`
- `workers.py`
- `models.py`
- `srt_lint.py`
- `quality.py`（新增）

建议直接用整个压缩包覆盖原项目的 `core` 目录，避免漏掉新增模块。

## 新流程

1. 情境分析
2. 首轮字幕重组
3. 原始批次边界专项复查
4. 初译（并发上下文只读取固定原文）
5. 漏译检查与并发修复
6. 独立忠实度审校
7. 自然度与字幕表达润色
8. 全文一致性候选提取
9. 统一术语/专名规则整合
10. 全文一致性执行审校
11. 确定性 SRT 质检与修复

## 关键变化

- 一个处理阶段共用一个 `aiohttp.ClientSession` 和连接池。
- Chat API 对 408/409/425/429/5xx、网络异常和超时执行指数退避重试。
- 初译阶段不再读取其他并发批次正在变化的译文上下文。
- 每个字幕分批边界都会额外复查，允许跨边界合并。
- 边界复查结果只有在原始来源 ID 完整且无重复时才会被接受。
- 漏译修复仍按单条精确上下文处理，但最多 4 条并发执行。
- 润色 JSON 解析失败不再静默返回初译并标记成功。
- 新增 `stage_status` 和 `quality_flags`，同时保留旧 `status` 字段兼容 UI。
- SRT 自动合并字幕时会保留质量标记。

## 兼容性

原有公开方法签名保持不变：

- `SubtitlePipeline.analyze()`
- `SubtitlePipeline.segment()`
- `SubtitlePipeline.translate()`
- `SubtitlePipeline.optimize()`
- `SubtitlePipeline.finalize()`

`SubtitleEntry` 只在字段末尾新增默认字段，原有位置参数调用不受影响。
