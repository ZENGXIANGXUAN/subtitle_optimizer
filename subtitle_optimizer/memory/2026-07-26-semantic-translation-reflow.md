# 2026-07-26 Semantic translation reflow

## Symptom

The translation merge gate rejected semantically valid adjacent source cues
solely because their combined display span exceeded eight seconds.  The
remaining fallback put a complete translation over one long combined cue,
because character-weight splitting had previously created name, number, and
punctuation-only target fragments.

## Root cause

`_llm_merge_group_is_safe()` mixed two independent responsibilities: semantic
translation context and display timing.  It imposed an 8000 ms span cap before
translation, even though the semantic group had no validated way to return a
translation to the original cue timeline.  The final reflow therefore had to
choose between rejecting useful semantic context and displaying a long atomic
translation.

## Fix

- Removed the active display-duration gate from translation-only merge safety;
  adjacency, silence-gap, and 60-word limits remain.
- Translation requests for merged semantic units now include immutable
  `source_cues` and require `cue_targets` keyed by original cue id, in addition
  to the complete translation used for safe fallback.
- The response parser validates id uniqueness and the pipeline accepts a cue
  mapping only when it covers every source cue exactly once and every target
  passes the normal translation usability gate.
- Validated mappings are reflowed back to the original timestamps immediately
  after translation, before fidelity, optimization, and consistency review.
- Missing or invalid mappings keep the full semantic translation over its
  combined span; no character-position splitting is reintroduced.

## Regression coverage

`tests/test_semantic_translation_mapping.py` covers an 18-second semantic
translation merge, explicit cue mapping and timeline restoration, malformed
mapping fallback, and the fidelity prompt's duration/budget coverage data.

## Verification

`python -m pytest -q` completed with `178 passed, 2 skipped, 3 subtests
passed`.  The only warnings are the pre-existing aiohttp Python 3.12.10
`enable_cleanup_closed` deprecation notices.

Status: DONE
