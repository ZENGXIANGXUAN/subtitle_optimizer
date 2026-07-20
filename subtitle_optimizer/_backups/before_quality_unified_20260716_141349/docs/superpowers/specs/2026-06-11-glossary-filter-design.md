# 术语表按字幕内容过滤

## 问题

术语表（glossary）全量加载到每次 API 调用的 prompt 中。用户可能维护了 20+ 条术语，但当前字幕只用到其中 3-5 条。多余的术语：
- 浪费 token（每条 ~30-50 tokens）
- 分散模型注意力，增加遗漏风险

## 方案

在术语表传入 Worker 之前，用纯文本匹配过滤：只保留在字幕原文中实际出现的术语源词。

### 匹配规则

- **大小写不敏感**：术语 `API` 能匹配字幕中的 `api`、`Api`
- **子串匹配**：术语 `volume` 能匹配 `Volume Profiling` 中的 `Volume`
- **逐行匹配**：每条术语独立判断，不依赖其他术语

### 数据流

```
UI输入框 (raw text: "source -> target")
  ├─ 单文件: _after_analysis() → _get_glossary_prompt(entries=self.entries)
  │           内部: filter raw → parse → format
  └─ 批量:   _batch_start() 存 raw text → BatchWorker 每个文件加载后
             → filter_glossary_by_entries(raw, entries) → _format_glossary(filtered)
```

注意：`_get_glossary_prompt()` 返回的是格式化后的字符串（`"source" 必须译为 "target"`），
而过滤函数操作的是原始格式（`source -> target`）。因此 BatchWorker 需要存 raw text。

## 改动清单

### 1. 新增 `filter_glossary_by_entries()` — `core/workers.py`

纯函数，签名：
```python
def filter_glossary_by_entries(raw_glossary: str, entries: list[SubtitleEntry]) -> str
```

逻辑：
1. 拼接所有 `entry.lines` 为一个字符串，转小写
2. 逐行解析 `raw_glossary`（按 `->` 或 `→` 分割）
3. 检查源词（小写）是否出现在拼接文本中
4. 返回过滤后的原始格式文本（`source -> target`）

### 2. 拆分 `_get_glossary_prompt()` — `ui/main_window.py:1538`

将格式化逻辑提取为独立函数，供 BatchWorker 复用：

```python
def _format_glossary_prompt(raw: str) -> str:
    """将原始 glossary 文本格式化为 prompt 字符串"""
    # 现有 _get_glossary_prompt() 的解析+格式化逻辑移至此处

def _get_glossary_prompt(self, entries=None) -> str:
    raw = self.input_glossary.toPlainText().strip()
    if not raw:
        return ""
    if entries:
        raw = filter_glossary_by_entries(raw, entries)
    return _format_glossary_prompt(raw)
```

- `entries=None` 时行为不变（向后兼容）
- `entries` 非空时，先过滤再格式化

### 3. 修改 `_after_analysis()` — `ui/main_window.py:1180`

```python
# 改前
glossary_prompt = self._get_glossary_prompt()
# 改后
glossary_prompt = self._get_glossary_prompt(entries=self.entries)
```

### 4. 修改 `BatchWorker` — `core/workers.py`

**改动 a**: `__init__` 新增 `raw_glossary` 参数，存储原始文本：
```python
def __init__(self, ..., glossary_prompt: str = "", raw_glossary: str = "", ...):
    self.glossary_prompt = glossary_prompt  # 保留兼容
    self.raw_glossary = raw_glossary        # 新增：原始格式
```

**改动 b**: `run()` 中每个文件 entries 加载后，过滤并格式化：
```python
if self.raw_glossary:
    filtered_raw = filter_glossary_by_entries(self.raw_glossary, entries)
    glossary = _format_glossary_prompt(filtered_raw)  # 格式化
else:
    glossary = self.glossary_prompt  # 降级：用原有全量
```

### 5. 修改 `_batch_start()` — `ui/main_window.py:874`

传入 raw text：
```python
raw = self.input_glossary.toPlainText().strip()
self._batch_worker = BatchWorker(
    ...,
    glossary_prompt=self._get_glossary_prompt(),
    raw_glossary=raw,  # 新增
    ...
)
```

## 预期效果

| 场景 | 术语表条数 | 命中条数 | 节省 tokens |
|------|-----------|---------|------------|
| 金融字幕 + 20条术语 | 20 | 5 | ~450 |
| 技术字幕 + 10条术语 | 10 | 8 | ~60 |
| 无术语表 | 0 | 0 | 0 |

## 不改的部分

- `_translate_batch_impl` / `_optimize_batch_impl` 的 prompt 结构不变
- 术语表原始格式不变（`source -> target`）
- UI 输入框不变
- 过滤结果对用户透明（无 UI 提示）
