# Batch Glossary Review And Confirmation Design

## Summary

Add a default-on batch glossary review step before formal batch processing. The step extracts candidate glossary entries from every queued subtitle file, lets the user review/edit/delete candidates, immediately appends approved entries to the fixed settings glossary, saves the config, and then starts the normal batch pipeline with the updated fixed glossary.

This solves inconsistent terminology across files in the same batch. The fixed glossary remains the single source of truth: once the user confirms candidates, they become fixed glossary entries and are used by the current batch and future runs.

## Goals

- Keep terminology consistent across all files in a batch.
- Avoid sending all subtitles in one oversized request.
- Reuse the configured concurrency for fast pre-analysis.
- Only show candidates that are not already present in the fixed glossary.
- Let users resolve translation conflicts before entries are saved.
- Persist approved entries immediately, even if later batch processing fails or is canceled.
- Keep source/target language labels language-agnostic.

## Non-Goals

- Do not replace the existing fixed glossary table format.
- Do not rollback approved glossary entries after later batch failures.
- Do not group the review UI by file.
- Do not let per-file stage-one auto glossary override fixed glossary entries.

## User Flow

1. The batch page includes a default-on option named `批量词汇整理与确认`.
2. The user clicks batch start.
3. The app validates the batch queue and output settings as it does today.
4. If `批量词汇整理与确认` is enabled, the app runs glossary pre-analysis before formal batch translation.
5. Each queued subtitle file is parsed and sampled independently.
6. Each file sends its own glossary extraction request. Requests run concurrently using the configured concurrency value.
7. The app merges all returned candidates into one global candidate pool.
8. The app filters out candidates whose source already exists in the fixed glossary.
9. The app opens `批量词汇整理与确认`.
10. The user edits or deletes candidates and resolves conflicts.
11. On `确认并加入固定词汇表`, the app validates that no source has multiple remaining targets.
12. The app appends remaining candidates to the fixed glossary table and saves config immediately.
13. The normal batch pipeline starts and uses the updated fixed glossary.

If the user cancels the review dialog, no candidates are saved and formal batch processing does not start.

## File Handling

The pre-analysis phase must process files through the same batch queue semantics as the formal batch pipeline.

- Use the queued file paths, not arbitrary directory scanning.
- Parse each subtitle file independently with the existing SRT parser/model path.
- Skip or report files that cannot be read or parsed.
- Treat empty subtitle files or files with no usable subtitle entries as pre-analysis failures for that file.
- Continue pre-analysis for other files when one file fails, then show a summary before the review dialog.
- If every file fails pre-analysis, stop and return to the batch page.
- If some files fail but at least one candidate is extracted, allow review to continue and show which files were skipped.
- Do not start formal batch processing until the glossary review step has either completed successfully or has been canceled by the user.
- Formal batch processing still handles each file with the existing batch worker behavior after glossary confirmation.

## Request Strategy

Pre-analysis must not send all subtitles in one request.

For each file:

- Select representative subtitle samples from the beginning, middle, and end.
- Include enough surrounding subtitle text for domain context.
- Use the existing stage-one analysis/glossary extraction capability where practical.
- Return candidate entries in source-to-target form.

Concurrency:

- Run per-file extraction requests concurrently.
- Use the configured concurrency setting as the maximum number of simultaneous pre-analysis requests.
- Keep each request bounded to one file's sampled context.

## Candidate Merge Rules

All file-level candidates are merged into one global pool.

- If `source + target` are identical across files, merge into one row and aggregate sources.
- If the same `source` appears with multiple `target` values, keep all variants and mark them as a conflict set.
- Do not display candidates grouped by file, because file grouping hides cross-file conflicts.
- Display conflict variants next to each other so the user can delete or edit incorrect translations.
- Filter out any source already present in the fixed glossary before opening the dialog.

The fixed glossary wins over every automatic candidate. Existing fixed glossary entries are never overwritten by pre-analysis.

## Dialog

Title: `批量词汇整理与确认`

Table columns:

- `原文（<输入语言>）` if input language is known, otherwise `原文`
- `译文（<输出语言>）` if output language is known, otherwise `译文`
- `来源`
- `状态`
- `操作`

The source and target labels must be language-agnostic. They must not be hard-coded as English/Chinese. The same naming should be used in the fixed glossary settings table where possible.

Rows:

- Source and target cells are editable.
- `来源` shows the contributing files or a count such as `3 个文件`.
- `状态` is `新词` or `译法冲突`.
- `操作` provides a per-row `删除` button.

Footer actions:

- `确认并加入固定词汇表`
- `取消`
- `全部删除`

## Confirmation Rules

When the user clicks `确认并加入固定词汇表`:

- Ignore blank rows.
- Reject rows with a blank source or blank target.
- Validate that no source has more than one remaining target.
- If conflicts remain, keep the dialog open and show a clear message asking the user to delete or edit conflicting rows.
- If validation passes, append the remaining rows to the fixed glossary table.
- Save config immediately.
- Start formal batch processing with the updated fixed glossary.

Approved entries are not rolled back if later batch processing fails or is canceled.

## Batch Pipeline Interaction

After glossary confirmation, formal batch processing should behave like today's batch pipeline, with one important difference: the fixed glossary now includes the approved batch candidates.

Per-file stage-one analysis may still extract automatic glossary entries, but merge priority remains:

1. Fixed glossary entries.
2. Per-file automatic glossary entries.

Per-file automatic entries must not overwrite fixed glossary translations.

## Error Handling

- Pre-analysis API failure for one file: record the file failure and continue with other files.
- Pre-analysis API failure for all files: show an error summary and do not start formal batch processing.
- No new candidates after filtering fixed glossary entries: tell the user no new glossary entries were found, then start formal batch processing.
- User cancels review: save nothing and do not start formal batch processing.
- Config save failure after confirmation: show an error and do not start formal batch processing, because the approved glossary would not be reliably available.

## Testing

Core tests:

- Merge identical `source + target` candidates from multiple files.
- Preserve conflicting target variants for the same source.
- Filter candidates whose source already exists in the fixed glossary.
- Reject confirmation when one source still has multiple targets.
- Accept confirmation when conflicts are resolved.
- Preserve fixed glossary priority over automatic per-file glossary entries.

UI tests or manual checks:

- Dialog labels use current input/output language names, not fixed English/Chinese labels.
- Per-row delete removes candidates and updates conflict status.
- `全部删除` clears candidates.
- Cancel does not update fixed glossary and does not start formal batch.
- Confirm appends to fixed glossary, saves config, and starts formal batch.

Batch/file handling checks:

- One invalid file and one valid file: valid candidates still appear; invalid file is reported.
- All invalid files: no dialog opens and formal batch does not start.
- Empty subtitle file is reported.
- Pre-analysis respects configured concurrency.
