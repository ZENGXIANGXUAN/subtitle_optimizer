# Batch Glossary AI Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make batch glossary preprocessing automatic by default, add an AI self-review step, and turn the existing glossary checkbox into a manual-review opt-in.

**Architecture:** `BatchGlossaryWorker` remains responsible for per-file glossary extraction, global unification, and the new AI self-review. `BatchGlossarySummary` carries three outputs: the current-batch `session_glossary`, optional `fixed_glossary` text safe to save, and `candidates` only for manual review. `MainWindow` always starts glossary preprocessing for normal batch runs and only opens the confirmation table when `manual_glossary_review` is enabled.

**Tech Stack:** Python, PyQt6 worker threads, existing Mistral chat client, existing pure function tests run by direct Python invocation because bundled pytest is unavailable.

---

### Task 1: Extend Summary Data For Auto Fixed Terms

**Files:**
- Modify: `core/batch_glossary.py`
- Modify: `test_batch_glossary.py`

- [ ] **Step 1: Write the failing test**

Add this test to `test_batch_glossary.py`:

```python
def test_batch_glossary_summary_can_carry_auto_fixed_glossary():
    summary = BatchGlossarySummary(
        candidates=[],
        skipped_existing=[],
        failures=[],
        session_glossary="order flow->订单流",
        fixed_glossary="SPX->标普500指数",
    )

    assert summary.session_glossary == "order flow->订单流"
    assert summary.fixed_glossary == "SPX->标普500指数"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
rtk powershell -NoProfile -Command "`$py='C:\Users\xuan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; `$code=@'
import test_batch_glossary as t
t.test_batch_glossary_summary_can_carry_auto_fixed_glossary()
'@; & `$py -c `$code"
```

Expected: `TypeError: BatchGlossarySummary.__init__() got an unexpected keyword argument 'fixed_glossary'`.

- [ ] **Step 3: Implement the minimal data field**

Change `BatchGlossarySummary` in `core/batch_glossary.py` to:

```python
@dataclass(frozen=True)
class BatchGlossarySummary:
    candidates: list[GlossaryCandidate]
    skipped_existing: list[str]
    failures: list[GlossaryFileFailure]
    session_glossary: str = ""
    fixed_glossary: str = ""
```

- [ ] **Step 4: Run pure glossary tests**

Run the direct pure test runner:

```powershell
rtk powershell -NoProfile -Command "`$py='C:\Users\xuan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; `$code=@'
import test_batch_glossary as t
for name in sorted(n for n in dir(t) if n.startswith('test_')):
    getattr(t, name)()
print('pure glossary tests passed')
'@; & `$py -c `$code"
```

Expected: `pure glossary tests passed`.

### Task 2: Add AI Self-Review Parsing And Runtime Behavior

**Files:**
- Modify: `core/workers.py`
- Create: `test_batch_glossary_ai_review.py`

- [ ] **Step 1: Write failing parser tests**

Create `test_batch_glossary_ai_review.py`:

```python
from core.batch_glossary import BatchGlossarySummary, GlossaryCandidate
from core.workers import BatchGlossaryWorker


def make_worker():
    return BatchGlossaryWorker(
        client=None,
        files=[],
        concurrency=1,
        fixed_glossary="SPY->已有译法",
        input_lang="English",
        output_lang="中文",
    )


def test_ai_review_parser_preserves_session_and_fixed_glossary():
    worker = make_worker()
    original = BatchGlossarySummary(
        candidates=[GlossaryCandidate("SPX", "标普500指数", ("a.srt",), "译法冲突")],
        skipped_existing=[],
        failures=[],
        session_glossary="order flow->订单流",
    )
    raw = (
        '{"session_glossary":[{"source":"order flow","target":"订单流"}],'
        '"fixed_candidates":[{"source":"SPX","target":"标普500指数","reason":"stable cross-context term"}],'
        '"dropped":[{"source":"lot","reason":"common word"}]}'
    )

    reviewed = worker._parse_ai_review_result(raw, original)

    assert reviewed.session_glossary == "order flow->订单流"
    assert reviewed.fixed_glossary == "SPX->标普500指数"
    assert reviewed.candidates == []


def test_ai_review_parser_allows_zero_fixed_candidates():
    worker = make_worker()
    original = BatchGlossarySummary(
        candidates=[GlossaryCandidate("order flow", "订单流", ("a.srt",), "译法冲突")],
        skipped_existing=[],
        failures=[],
        session_glossary="order flow->订单流",
    )
    raw = '{"session_glossary":[{"source":"order flow","target":"订单流"}],"fixed_candidates":[],"dropped":[]}'

    reviewed = worker._parse_ai_review_result(raw, original)

    assert reviewed.session_glossary == "order flow->订单流"
    assert reviewed.fixed_glossary == ""
    assert reviewed.candidates == []
```

- [ ] **Step 2: Run parser tests to verify failure**

Run:

```powershell
rtk powershell -NoProfile -Command "`$py='C:\Users\xuan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; `$code=@'
import test_batch_glossary_ai_review as t
t.test_ai_review_parser_preserves_session_and_fixed_glossary()
'@; & `$py -c `$code"
```

Expected: `AttributeError: 'BatchGlossaryWorker' object has no attribute '_parse_ai_review_result'`.

- [ ] **Step 3: Implement parser and message builder**

In `BatchGlossaryWorker`, add:

```python
    def _build_ai_review_messages(self, summary: BatchGlossarySummary) -> list[dict]:
        session_pairs = [
            {"source": source, "target": target}
            for source, target in parse_glossary_pairs(summary.session_glossary)
        ][:80]
        review_candidates = [
            {
                "source": candidate.source,
                "target": candidate.target,
                "files": list(candidate.files),
                "file_count": len(candidate.files),
                "status": candidate.status,
            }
            for candidate in summary.candidates[:20]
        ]
        system_prompt = (
            "你是批量术语自审器。你要在不打扰用户的前提下，审查批量术语统一结果。"
            "只保留本批次真正需要统一的术语；普通词、语境短语、已经正确且无争议的项不要进入固定词汇。"
            "必须严格返回 JSON，不要返回解释、Markdown 或代码块。格式："
            "{\"session_glossary\":[{\"source\":\"term\",\"target\":\"translation\"}],"
            "\"fixed_candidates\":[{\"source\":\"term\",\"target\":\"translation\",\"reason\":\"stable cross-context term\"}],"
            "\"dropped\":[{\"source\":\"term\",\"reason\":\"common word/context-only/already correct\"}]}"
        )
        user_prompt = (
            f"输入语言：{self.input_lang}\n"
            f"输出语言：{self.output_lang}\n"
            f"固定词汇表已有内容：\n{self.fixed_glossary}\n\n"
            "本次批量临时术语：\n"
            + json.dumps(session_pairs, ensure_ascii=False)
            + "\n\n需要自审的冲突/可疑项：\n"
            + json.dumps(review_candidates, ensure_ascii=False)
        )
        return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
```

Add `_parse_ai_review_result` using `parse_glossary_pairs`, `fixed_source_keys`, `is_low_value_glossary_source`, and `normalize_glossary_source_key`:

```python
    def _parse_ai_review_result(self, raw: str, original_summary: BatchGlossarySummary) -> BatchGlossarySummary:
        clean = re.sub(r'```(?:json)?|```', '', raw or '').strip()
        data = json.loads(clean)
        if not isinstance(data, dict):
            raise ValueError("AI 术语自审返回格式无效")

        session_items = data.get("session_glossary")
        fixed_items = data.get("fixed_candidates")
        if not isinstance(session_items, list):
            session_items = []
        if not isinstance(fixed_items, list):
            fixed_items = []

        session_lines = self._glossary_items_to_lines(session_items, limit=80)
        fixed_lines = self._glossary_items_to_lines(
            fixed_items,
            limit=20,
            existing_keys=fixed_source_keys(self.fixed_glossary),
        )

        return BatchGlossarySummary(
            candidates=[],
            skipped_existing=original_summary.skipped_existing,
            failures=original_summary.failures,
            session_glossary="\n".join(session_lines) if session_items else original_summary.session_glossary,
            fixed_glossary="\n".join(fixed_lines),
        )
```

Add helper:

```python
    def _glossary_items_to_lines(
        self,
        items: list,
        *,
        limit: int,
        existing_keys: set[str] | None = None,
    ) -> list[str]:
        existing_keys = existing_keys or set()
        lines: list[str] = []
        seen_sources: set[str] = set()
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            source_key = normalize_glossary_source_key(source)
            if (
                not source
                or not target
                or not source_key
                or source_key in seen_sources
                or source_key in existing_keys
                or is_low_value_glossary_source(source)
            ):
                continue
            seen_sources.add(source_key)
            lines.append(f"{source}->{target}")
        return lines
```

- [ ] **Step 4: Implement async AI self-review**

Add `_review_candidates_with_ai`:

```python
    async def _review_candidates_with_ai(self, summary: BatchGlossarySummary) -> BatchGlossarySummary:
        if not summary.session_glossary and not summary.candidates:
            return summary
        timeout_seconds = batch_glossary_unifier_timeout_seconds()
        self.log.emit("  AI 正在自审批量术语...")
        try:
            async with aiohttp.ClientSession() as session:
                raw = await asyncio.wait_for(
                    self.client.chat(
                        self._build_ai_review_messages(summary),
                        session,
                        extra_payload=batch_glossary_reasoning_options(),
                    ),
                    timeout=timeout_seconds,
                )
            reviewed = self._parse_ai_review_result(raw, summary)
            fixed_count = len([line for line in reviewed.fixed_glossary.splitlines() if line.strip()])
            self.log.emit(f"  AI 自审完成，保留固定术语 {fixed_count} 条")
            return reviewed
        except TypeError:
            return summary
        except Exception as exc:
            self.log.emit(f"  AI 术语自审失败，保留本次临时术语继续处理：{exc}")
            return BatchGlossarySummary(
                candidates=[],
                skipped_existing=summary.skipped_existing,
                failures=summary.failures,
                session_glossary=summary.session_glossary,
                fixed_glossary="",
            )
```

- [ ] **Step 5: Wire manual review flag into worker**

Change `BatchGlossaryWorker.__init__` to accept `manual_review: bool = False` and set `self.manual_review`.

Change the end of `_analyze_files`:

```python
        unified = await self._unify_candidates_globally(summary)
        if self.manual_review:
            self.log.emit("  手动审核术语已开启，跳过 AI 自审")
            return unified
        return await self._review_candidates_with_ai(unified)
```

- [ ] **Step 6: Run AI review tests**

Run:

```powershell
rtk powershell -NoProfile -Command "`$py='C:\Users\xuan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; `$code=@'
import test_batch_glossary_ai_review as t
for name in sorted(n for n in dir(t) if n.startswith('test_')):
    getattr(t, name)()
print('ai review tests passed')
'@; & `$py -c `$code"
```

Expected: `ai review tests passed`.

### Task 3: Flip UI Checkbox Semantics

**Files:**
- Modify: `ui/main_window.py`

- [ ] **Step 1: Change checkbox label and default**

In `_build_batch_page`, change:

```python
self.chk_batch_glossary_review = QCheckBox("批量词汇整理与确认")
self.chk_batch_glossary_review.setChecked(True)
self.chk_batch_glossary_review.setToolTip("批量开始前提取新词，确认后加入固定词汇表再处理整批文件")
```

to:

```python
self.chk_batch_glossary_review = QCheckBox("手动审核术语")
self.chk_batch_glossary_review.setChecked(False)
self.chk_batch_glossary_review.setToolTip("关闭时由 AI 自动自审并继续；开启时弹出术语表供手动删除/确认。")
```

- [ ] **Step 2: Always run glossary preprocessing for normal batch**

Change `_batch_start`:

```python
if self.chk_batch_glossary_review.isChecked() and not settings["transcribe_only"]:
    self._start_batch_glossary_preanalysis(settings)
    return
```

to:

```python
if not settings["transcribe_only"]:
    self._start_batch_glossary_preanalysis(settings)
    return
```

Add this to `_batch_settings`:

```python
"manual_glossary_review": self.chk_batch_glossary_review.isChecked(),
```

- [ ] **Step 3: Pass manual review to worker and update logs**

In `_start_batch_glossary_preanalysis`, change the first log to:

```python
self._batch_log_msg("📖 开始批量术语统一...")
```

Pass:

```python
manual_review=settings.get("manual_glossary_review", False),
```

to `BatchGlossaryWorker`.

- [ ] **Step 4: Change preanalysis completion branch**

At the start of `_after_batch_glossary_preanalysis`, set:

```python
manual_review = bool(settings.get("manual_glossary_review"))
```

After session count logging, append automatic fixed glossary when manual review is off:

```python
if not manual_review and summary.fixed_glossary:
    self._append_fixed_glossary_text(summary.fixed_glossary)
    fixed_count = len([line for line in summary.fixed_glossary.splitlines() if line.strip()])
    self._batch_log_msg(f"✅ AI 自审保留固定术语 {fixed_count} 条，已加入固定词汇表")
```

Then if manual review is off, skip the popup:

```python
if not manual_review:
    if not session_count and len(summary.failures) >= len(settings["files"]):
        QMessageBox.warning(self, "批量术语统一失败", "所有文件都无法完成词汇预分析，已停止批量处理。")
        self._reset_batch_controls_after_preanalysis_stop()
        return
    self._start_batch_worker(settings)
    return
```

Keep the existing confirmation branch only for manual review mode.

### Task 4: Ensure Stage 1 Uses Batch Glossary As Authority

**Files:**
- Modify: `core/workers.py`

- [ ] **Step 1: Tighten the merge key normalization**

In `merge_glossaries`, replace `source.casefold()` keys with `normalize_glossary_source_key(source)` for both user and auto terms. This makes stage 1 additions respect batch terms despite case/punctuation differences.

Use:

```python
user_by_key = {normalize_glossary_source_key(source): target for source, target in user_pairs}
```

and:

```python
key = normalize_glossary_source_key(source)
```

- [ ] **Step 2: Add a pure test**

Add to `test_batch_glossary_ai_review.py`:

```python
from core.workers import merge_glossaries


def test_stage_one_auto_terms_do_not_override_batch_glossary_case_variants():
    merged = merge_glossaries(
        "OptionStrat->期权策略工具",
        "option strat->期权策略\nGamma->伽马",
    )

    assert merged.raw.splitlines() == [
        "OptionStrat->期权策略工具",
        "Gamma->伽马",
    ]
    assert merged.conflicts == [("option strat", "期权策略工具", "期权策略")]
```

- [ ] **Step 3: Run the new test**

Run the direct Python test runner for `test_batch_glossary_ai_review.py`.

Expected: `ai review tests passed`.

### Task 5: Final Verification

**Files:**
- Verify: `core/batch_glossary.py`
- Verify: `core/workers.py`
- Verify: `ui/main_window.py`
- Verify: `test_batch_glossary.py`
- Verify: `test_batch_glossary_ai_review.py`

- [ ] **Step 1: Run pure glossary tests**

Run the direct `test_batch_glossary.py` runner and expect `pure glossary tests passed`.

- [ ] **Step 2: Run AI review tests**

Run the direct `test_batch_glossary_ai_review.py` runner and expect `ai review tests passed`.

- [ ] **Step 3: Compile changed Python files without writing pyc**

Run:

```powershell
rtk powershell -NoProfile -Command "`$py='C:\Users\xuan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; `$code=@'
from pathlib import Path
files = [
    'core/batch_glossary.py',
    'core/workers.py',
    'ui/main_window.py',
    'test_batch_glossary.py',
    'test_batch_glossary_ai_review.py',
]
for file in files:
    source = Path(file).read_text(encoding='utf-8')
    compile(source, file, 'exec')
print('syntax compile passed')
'@; & `$py -c `$code"
```

Expected: `syntax compile passed`.

- [ ] **Step 4: Check whitespace**

Run:

```powershell
rtk git diff --check -- core/batch_glossary.py core/workers.py ui/main_window.py test_batch_glossary.py test_batch_glossary_ai_review.py
```

Expected: exit code 0 and no output.

