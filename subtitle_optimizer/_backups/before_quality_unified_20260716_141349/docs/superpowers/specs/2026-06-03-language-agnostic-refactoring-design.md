# 语言无关重构设计文档

**日期**: 2026-06-03
**状态**: 已批准
**目标**: 把所有"语言硬编码"替换成"语言无关的角色概念"：**原文（source）** 和 **译文（target）**

---

## 1. 核心问题

当前代码中存在大量语言硬编码：
- `SubtitleEntry.chinese` / `SubtitleEntry.english` 字段名假设语言
- JSON 字段 `"zh"` / `"en"` 假设语言
- `chinese_first` 参数假设语言
- 多处 `if input_lang_code == "zh"` 分支处理语言特殊逻辑

这导致：
- 每次添加新语言都需要修改多处代码
- 容易出现语言相关的 bug（如中文输入时字段顺序错误）
- 代码可读性差，难以理解语言逻辑

---

## 2. 设计方案

### 2.1 数据模型改造

**文件**: `core/models.py`

| 旧字段 | 新字段 | 说明 |
|--------|--------|------|
| `chinese` | `target` | 翻译目标语言的文本 |
| `english` | `source` | 原始输入语言的文本 |
| `chinese_first` | `target_first` | 输出时译文是否在前 |

**属性改造**:
```python
@property
def source(self) -> str:
    """获取原文文本（根据输入语言）"""
    # 需要传入 lang_code，或从外部调用 _get_source_text()
    pass

@property
def target(self) -> str:
    """获取译文文本（根据输出语言）"""
    # 需要传入 lang_code，或从外部调用 _get_target_text()
    pass
```

**注意**: 由于 `source` 和 `target` 需要知道语言代码才能正确判断，建议：
- 保留 `chinese` 和 `english` 属性作为内部实现
- 添加 `source` 和 `target` 作为别名，调用 `_get_source_text()` 和 `_get_target_text()`
- 或者直接使用 `_get_source_text(entry, lang_code)` 函数

### 2.2 JSON 字段名改造

**文件**: `core/workers.py`

| 旧字段 | 新字段 | 说明 |
|--------|--------|------|
| `"zh"` | `"tgt"` | 译文字段 |
| `"en"` | `"src"` | 原文字段 |

**影响范围**:
- `_optimize_batch_impl()` - 发送和解析
- `_translate_batch_impl()` - 发送和解析
- `_repair_incomplete_segment_translations()` - 调用
- 所有 `prev_ctx` 构建逻辑
- 所有 retry 逻辑

### 2.3 分支逻辑消除

**当前问题**:
```python
if input_lang_code == "zh":
    items_json = json.dumps([
        {"id": e.index, "zh": e.english, "en": e.chinese}
        for e in batch
    ], ensure_ascii=False)
else:
    items_json = json.dumps([
        {"id": e.index, "zh": e.chinese, "en": e.english}
        for e in batch
    ], ensure_ascii=False)
```

**重构后**:
```python
items_json = json.dumps([
    {"id": e.index, "src": _get_source_text(e, input_lang_code), "tgt": _get_target_text(e, output_lang_code)}
    for e in batch
], ensure_ascii=False)
```

---

## 3. 实现步骤

### 第一步：改造 `SubtitleEntry` 数据模型

1. 在 `core/models.py` 中：
   - 保留 `chinese` 和 `english` 属性（向后兼容）
   - 添加 `source` 和 `target` 属性别名
   - 将 `chinese_first` 参数改名为 `target_first`
   - 更新 `to_srt_block()` 方法

2. 全项目搜索并替换：
   - `e.english` → `e.source` 或 `_get_source_text(e, lang_code)`
   - `e.chinese` → `e.target` 或 `_get_target_text(e, lang_code)`
   - `entry.english` → `entry.source`
   - `entry.chinese` → `entry.target`

### 第二步：修复 `_build_analysis_messages` 的样本取值

- 当前第 113 行固定从 `e.english` 取样本
- 改成调用 `_get_source_text(e, input_lang_code)`

### 第三步：修复 JSON 字段名

- `_optimize_batch_impl` 和 `_translate_batch_impl` 中：
  - 发出去的字段名改为 `"src"` 和 `"tgt"`
  - prompt 里的返回格式示例同步改
  - 解析响应时，从 `item.get("tgt")` 和 `item.get("src")`
  - 所有用到这两个字段的地方（包括 `prev_ctx`、retry 逻辑）都要同步改

### 第四步：重构字段顺序判断逻辑

- 消除所有 `if input_lang_code == "zh":` 分支
- 统一使用 `_get_source_text()` 和 `_get_target_text()` 获取文本
- 构建 JSON 时统一用 `{"id": ..., "src": source, "tgt": target}`

### 第五步：修复 `_repair_incomplete_segment_translations`

1. 不完整翻译检测：使用 `entry.target` 和 `_get_source_text(entry, input_lang_code)`
2. `retry_map` 调用时保持传入原始 label
3. 写回结果时使用 `source_text` 变量

### 第六步：修复 `_apply_llm_split` 的原文插入逻辑

- 每个分段都应该对应自己那段的原文文本
- `lines` 的构建应该是 `[seg_text]`（原文）
- 如果需要双语结构，应该在翻译阶段补充 `target`

### 第七步：修复 `LANGUAGE_MAP` 的 fallback

- 改成 `LANGUAGE_MAP.get(input_lang, ("en", input_lang))`
- 确保 fallback 结构和正常条目一致

### 第八步：全局回归验证

| 场景 | 验证重点 |
|------|----------|
| 英语 → 中文 | 确保没有退化 |
| 中文 → 英语 | source/target 对调是否正确 |
| 英语 → 日语 | zh/en 字段名消除是否彻底 |
| 日语 → 西班牙语 | 完全非中英场景，端到端跑通 |
| 输入=输出 | skip 逻辑是否正常触发 |

---

## 4. 改动影响范围

| 文件 | 改动类型 |
|------|----------|
| `core/models.py` | 字段改名、属性别名、参数改名 |
| `core/workers.py` | 字段访问、JSON key、prompt 模板、分支逻辑 |
| `core/segmenter.py` | 检查 `.english`/`.chinese` 引用 |
| `ui/main_window.py` | 跟随字段改名 |
| `test_*.py` | 更新测试用例 |

---

## 5. 风险与注意事项

1. **向后兼容**: 保留 `chinese` 和 `english` 属性作为别名，避免破坏现有代码
2. **渐进式重构**: 建议先改 `models.py`，让类型检查器报出所有引用点
3. **测试覆盖**: 每改一步都要运行测试，确保没有退化
4. **提交粒度**: 每个步骤单独提交，方便回滚

---

## 6. 验证标准

1. 所有语言对都能正确处理（英→中、中→英、英→日、日→西等）
2. 输入=输出时正确跳过翻译
3. JSON 字段名完全消除 zh/en
4. 没有 `if input_lang_code == "zh":` 分支
5. 所有测试通过
