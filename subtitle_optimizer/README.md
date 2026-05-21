# Subtitle Optimizer · 字幕优化工具

基于 Mistral AI 的双语字幕智能优化工具，提供三阶段处理流水线：情景分析 → 智能重组 → 优化润色。

## 功能特性

- **三阶段处理流水线**
  - **① 分析语境** — AI 自动识别字幕内容领域、主题、语言风格，生成翻译风格指引
  - **② 智能重组** — 识别并合并不完整英文句子片段，丢弃空条目；原生英文字幕自动翻译
  - **③ 优化润色** — 基于语境和风格指引优化中文翻译，确保准确、自然、术语统一
- **术语表支持** — 自定义专业词汇翻译规则，AI 强制执行，确保全文术语一致
- **批量处理** — 多文件队列，每个文件独立走完整三阶段流水线
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
│   └── workers.py       # 工作器（分析/重组/优化/批量处理）
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
4. 点击「▶ 开始优化」，自动执行三阶段流水线
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
| 并发数 | 同时处理的批次数 (1–12) | 6 |
| 批大小 | 每批字幕条数 (1–50) | 5 |

配置自动保存至 `~/.subtitle_optimizer_config.json`。

## 日志

日志文件按日期写入 `logs/subtitle_optimizer_YYYYMMDD.log`，记录 API 请求详情、错误堆栈和处理阶段耗时。

## 许可证

MIT License
