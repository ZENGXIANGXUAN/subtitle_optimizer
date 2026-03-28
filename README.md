# Subtitle Optimizer

一个强大的字幕优化工具，用于分析、翻译和优化字幕文件，提升字幕质量和可读性。

## 功能特性

- **智能翻译**：使用AI模型自动翻译和优化字幕
- **情景分析**：自动识别字幕内容的上下文情景，提高翻译准确性
- **术语表支持**：支持自定义术语表，确保专业词汇翻译一致性
- **批量处理**：支持批量优化多个字幕文件
- **实时预览**：提供字幕优化前后的实时对比预览
- **多格式支持**：支持常见字幕格式

## 项目结构

```
subtitle_optimizer/
├── core/              # 核心功能模块
│   ├── client.py      # API客户端
│   ├── models.py      # 数据模型
│   └── workers.py     # 工作器（翻译和优化逻辑）
├── ui/                # 用户界面
│   ├── main_window.py # 主窗口
│   ├── highlighter.py # 语法高亮
│   └── styles.py      # 样式定义
├── main.py            # 主入口
└── runTTS.bat         # 批处理文件
```

## 安装要求

- Python 3.10+
- PyQt5
- 相关依赖包

## 安装步骤

1. 克隆仓库：
   ```bash
   git clone https://github.com/ZENGXIANGXUAN/subtitle_optimizer.git
   cd subtitle_optimizer
   ```

2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 使用方法

1. 运行主程序：
   ```bash
   python main.py
   ```

2. 或者使用批处理文件：
   ```bash
   runTTS.bat
   ```

3. 在界面中：
   - 点击「打开」按钮选择字幕文件
   - 点击「分析字幕」自动识别翻译情景
   - 可选择添加术语表，确保专业词汇翻译一致性
   - 点击「开始优化」进行字幕翻译和优化
   - 查看优化结果并保存

## 配置说明

- API配置：在界面中输入API密钥和基础URL
- 模型选择：选择适合的AI模型
- 并发设置：根据系统性能调整并发数和批处理大小

## 注意事项

- 确保网络连接正常，因为需要调用AI API进行翻译
- 对于大型字幕文件，优化过程可能需要较长时间
- 建议先进行小批量测试，以调整最佳参数

## 贡献

欢迎提交Issue和Pull Request，共同改进这个项目。

## 许可证

MIT License