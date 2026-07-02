# Batch Glossary AI Review Design

Date: 2026-07-02

## Goal

Batch processing should not turn glossary cleanup into manual labor. The batch glossary flow should automatically build a consistent glossary for the current batch, run an AI review after aggregation, and continue without a popup by default. Manual review should be an explicit opt-in switch for cases where the user wants to inspect or override the AI result.

## Product Decision

Rename the existing batch glossary switch to "手动审核术语".

This switch does not mean "whether to organize glossary". Batch glossary organization remains part of the batch preprocessing flow. The switch only controls whether the user sees a confirmation table.

- Switch off: run batch glossary extraction, global unification, and AI self-review automatically. Do not show the manual table.
- Switch on: run batch glossary extraction and global unification, then show the manual review table. Skip the AI self-review step because the user is doing the review.

The default should be off, because the product should optimize for automatic batch processing.

## Batch Flow

The batch workflow becomes:

1. Read the batch queue.
2. For each file with available subtitles, run stage 1 analysis to extract context and candidate glossary terms.
3. Merge candidates locally and remove known low-value/common entries.
4. Run the global glossary unifier to create:
   - `session_glossary`: terms used for this batch only.
   - `review_candidates`: terms that may need manual judgment.
   - `fixed_candidates`: terms safe enough to add to fixed settings, if any.
5. If "手动审核术语" is off, run AI self-review:
   - remove terms that are already correct and do not need attention;
   - resolve obvious conflicts;
   - keep only the final batch session glossary;
   - optionally keep very conservative fixed terms.
6. If "手动审核术语" is on, show the table and let the user delete or approve entries.
7. Start normal batch processing with fixed glossary plus the generated `session_glossary`.

## AI Self-Review

The AI self-review runs after the first global unifier, not per file.

It should receive:

- input/output language;
- existing fixed glossary keys;
- the proposed `session_glossary`;
- conflict/review candidates;
- source file counts and source filenames where available;
- short context summaries from stage 1 when available.

It should return strict JSON:

```json
{
  "session_glossary": [
    {"source": "term", "target": "translation"}
  ],
  "fixed_candidates": [
    {"source": "term", "target": "translation", "reason": "stable cross-context term"}
  ],
  "dropped": [
    {"source": "term", "reason": "common word/context-only/already correct"}
  ]
}
```

The self-review is allowed to return an empty `fixed_candidates` list. That should be normal, not an error.

## Stage 1 Cooperation

Each file still runs stage 1 during formal processing.

Stage 1 keeps doing context analysis because each file can have a different topic, speaker intent, tone, and local phrasing. However, its glossary role changes:

- The batch `session_glossary` has higher priority than per-file stage 1 glossary suggestions.
- Per-file stage 1 can add local hints for that file only.
- Per-file stage 1 must not override batch glossary terms.
- Per-file stage 1 must not write to fixed glossary settings.
- If a per-file term conflicts with batch glossary, the batch glossary wins.

In short: batch glossary sets the shared terminology; per-file stage 1 explains the local context.

## Fixed Glossary Writes

Automatic fixed glossary writes must be conservative.

Only AI self-review terms that are clearly stable across contexts should be saved automatically, such as product names, platform names, widely used abbreviations, indicators, or formal strategy names. Context-only terms, ordinary phrases, and correct-but-not-special translations should remain session-only.

If manual review is on, fixed glossary writes follow the user's approval in the table. Confirmed entries should be saved immediately as before.

## Failure Handling

If AI self-review fails or times out:

- keep the current `session_glossary`;
- do not show a popup unless "手动审核术语" is on;
- do not automatically add uncertain fixed terms;
- log the failure and continue batch processing.

If manual review is on and the user cancels the table, batch processing stops before formal processing, matching the existing manual-review behavior.

## UI Text

Batch page switch:

- Label: "手动审核术语"
- Tooltip or helper text: "关闭时由 AI 自动自审并继续；开启时弹出术语表供手动删除/确认。"

Logs:

- "开始批量术语统一..."
- "AI 正在自审批量术语..."
- "已生成本次批量临时术语 X 条"
- "AI 自审保留固定术语 X 条"
- "手动审核术语已开启，跳过 AI 自审"

## Test Plan

Pure tests:

- Switch off uses AI self-review and does not require popup candidates.
- Switch on skips AI self-review and returns manual review candidates.
- AI self-review can return zero fixed candidates while preserving session glossary.
- AI self-review failure preserves session glossary and does not create fixed candidates.
- Per-file stage 1 glossary suggestions cannot override batch session glossary.

Integration-level checks:

- Batch flow starts normally without a popup when manual review is off.
- Batch flow shows the confirmation table when manual review is on.
- Runtime glossary passed to `BatchWorker` includes fixed glossary plus `session_glossary`.
- Existing fixed glossary entries are not duplicated.

