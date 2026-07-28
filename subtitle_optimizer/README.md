# Subtitle Optimizer · 字幕优化工具

基于 Mistral AI 的双语字幕智能优化工具，默认以翻译质量和信息完整性优先：情景分析 → 语义整句重组 → 翻译 → 忠实度/自然度/全文一致性审校 → SRT 质量门禁。

## 功能特性

- **质量优先处理流水线**
  - **① 分析语境** — AI 自动识别字幕内容领域、主题、语言风格，生成翻译风格指引
  - **② 语义重组** — 先合并跨时间轴的残句，按完整语义单元翻译，避免半句独立翻译
  - **③ 翻译** — 使用前后文和强制术语表生成完整初译；缺失条目自动逐条修复
  - **④ 三轮审校** — 独立忠实度核对、自然度润色、全文术语/专名一致性审计
  - **⑤ 确定性门禁** — 将译文回流到原时间轴，校验原文 cue exact-once，并执行 SRT lint；关键问题会阻止导出
- **无损输入布局** — 自动/单语/双语三种布局；多行单语字幕会合并保留，不再因误判双语而丢行
- **术语表支持** — 自定义专业词汇翻译规则，AI 强制执行，确保全文术语一致
- **术语自审安全降级** — 可选 AI 自审遇到 502、超时或连接失败时保留临时术语与待确认项，继续正式批量翻译；只有审核成功的候选才会写入固定词汇表
- **单/批统一接口** — 单文件和批量任务共用 `SubtitlePipeline.execute()`，阶段顺序、重试、lint 与质量门禁完全一致
- **批量处理** — 多文件队列，每个文件独立走完整质量流水线
- **实时对比预览** — 原始字幕 vs 优化后字幕逐条并排对比，变更高亮着色
- **SRT 语法高亮** — 序号、时间轴、中文、英文分色显示
- **配置持久化** — API Key、模型、术语表等自动保存至本地，下次启动恢复
- **详细日志** — 按日期滚动日志文件 + 界面实时日志面板
- **Animal Island 主题** — 温暖大地色系 UI，圆润立体风格

## 项目结构

```
subtitle_optimizer/
├── core/                # 核心业务模块
│   ├── client.py        # Mistral API 客户端（兼容 OpenAI 格式）
│   ├── models.py        # 数据模型（SubtitleEntry、SRT 解析器）
│   ├── pipeline.py      # 单任务/批量任务共用的业务执行入口
│   ├── semantic_units.py # 语义单元回流与来源完整性校验
│   ├── quality_gate.py  # 确定性质量门禁
│   └── workers.py       # Qt/批量任务适配器
├── ui/                  # 用户界面
│   ├── main_window.py   # 主窗口（单文件/批量/API 设置三页签）
│   ├── highlighter.py   # SRT 语法高亮
│   └── styles.py        # Animal Island 全局样式表
├── utils.py             # 日志系统工具
├── main.py              # 应用入口
├── run.bat              # Windows 启动脚本
└── requirements.txt     # 项目依赖
```

## 环境要求

- Python 3.10+
- PyQt6
- aiohttp

## 安装

```bash
git clone https://github.com/ZENGXIANGXUAN/subtitle_optimizer.git
cd subtitle_optimizer
pip install -r requirements.txt
```

## 使用方法

### 启动应用

```bash
python main.py
```

Windows 下也可双击 `run.bat` 启动。

### 单文件处理

1. 在「⚙ API 设置」页签配置 API Key、模型、并发数等参数
2. 点击「打开 SRT 文件」加载字幕
3. 在「术语表」区域填写专业词汇翻译规则（格式：`English->中文`，每行一条）
4. 点击「▶ 开始优化」，自动执行完整质量流水线
5. 在「对比查看」页签逐条审查优化结果
6. 点击「导出优化字幕」保存

### 批量处理

1. 切换到「批量处理」页签
2. 添加多个 SRT 文件到队列
3. 设置输出目录、覆盖选项
4. 点击「▶ 开始批量处理」

### 术语表格式

```
Algorithm->算法
Pipeline->流水线
Deployment->部署
```

翻译时 AI 将强制使用上述术语替换。

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| API Key | Mistral API 密钥 | — |
| Base URL | API 端点地址 | `https://api.mistral.ai/v1` |
| 模型 | 使用的 AI 模型 | `mistral-large-latest` |
| 并发数 | 同时处理的批次数 (1–100) | 6 |
| 批大小 | 每批字幕条数 (1–50) | 5 |
| 输入字幕布局 | 自动识别、强制单语或强制双语 | 自动识别 |

质量优先模式会把实际翻译/审校批大小和并发分别限制在 8 以内，使用最多 12 条前后文；批次失败后还会对失败条目逐条重试两轮。分段阶段保留独立批大小，不影响翻译与审校。

当 API 地址是 `localhost`、`127.0.0.0/8` 或 `::1` 时，程序按本地代理模式运行：单次 `chat` 最多发送一次代理请求，请求预算上限为 175 秒，避免与代理自身的上游重试叠加。远端 API 仍保留客户端重试策略。

## 统一执行接口

完成情景分析及术语确认后，单文件 UI 和批量任务都调用同一个入口：

```python
result = await pipeline.execute(
    entries,
    context,
    glossary,
    segment_batch_size=15,
    on_stage=on_stage,
    on_progress=on_progress,
    is_cancelled=is_cancelled,
)

if not result.quality_report.passed:
    # 不导出；先处理 result.quality_report.critical_issues
    ...
```

`PipelineRunResult.entries` 是经过语义回流、最终 lint、排序和重编号后的权威结果；调用方不得再按旧列表索引回填。

配置自动保存至 `~/.subtitle_optimizer_config.json`。

## 日志

日志写在**源码目录之外**：`~/.subtitle_optimizer/logs/YYYY-MM-DD/`，启动时会在日志里打印实际路径。
每天一个目录，包含 `subtitle_optimizer.log`（API 请求详情、错误堆栈、阶段耗时）和各阶段 SRT 快照。

- 单个日志文件超过 10 MB 自动轮转，最多保留 3 份备份
- 超过 30 天的日期目录在启动时自动清理（只删 `YYYY-MM-DD` 格式的目录，其他文件不动）

| 环境变量 | 作用 | 默认 |
|----------|------|------|
| `SUBTITLE_OPTIMIZER_LOG_DIR` | 自定义日志根目录 | `~/.subtitle_optimizer/logs` |
| `SUBTITLE_OPTIMIZER_PHASE_SNAPSHOTS` | 设为 `0` 关闭各阶段 SRT 快照 | 开启 |
| `SUBTITLE_OPTIMIZER_DEBUG_REQUESTS` | 设为 `1` 转储术语统一器请求体 | 关闭 |

## 许可证

MIT License
