# Consistency API size/debug trace

## Objective

Determine whether the failed full-document consistency guide was caused only by
the local proxy, by the request content/size, or by the official Mistral API.
The user supplied a temporary direct API credential; it was kept only in a
process environment variable and is deliberately not recorded here.

## Pipeline trace

`build_consistency_guide()` runs 24 candidate-extraction map calls for the
1,872-entry job, then sends all collected candidate terms in one reducer call.
The map calls are limited to four concurrent requests, while the reducer has no
input-size cap, candidate-count cap, or output-token cap. The job log shows the
guide-level retries fail at the final single-request stage after the map phase.

## Direct comparison (same model: `mistral-medium-3-5`)

| Payload | Size | Official Mistral | Local proxy |
| --- | ---: | --- | --- |
| Minimal JSON ping | tiny | HTTP 200, 3.50 s | HTTP 200, 0.92 s |
| Real candidate prompt, 10 cues | 1,176 bytes | HTTP 200, 3.95 s, 613 completion tokens | HTTP 200, 16.32 s, 614 completion tokens |
| Real candidate prompt, 40 cues | 4,373 bytes | HTTP 200, 13.70 s, 1,784 completion tokens | HTTP 200, 17.69 s, 1,306 completion tokens |
| Real candidate prompt, 80 cues | 10,852 bytes | one HTTP 200 response with empty content after 38.04 s; one upstream disconnect after about 70 s | timed out after 120.03 s |

The actual job has 24 map chunks. Their user JSON payloads range from 4,962 to
12,111 bytes (median 9,981 bytes). The complete subtitle data alone is 239,260
bytes across those chunks. If every cue became a candidate, the reducer input
would reach 324,611 bytes before prompts and JSON envelope; the current code
does not constrain how many terms or variants each map response can contribute.

## Root cause hypothesis confirmed by evidence

This is not a credentials/model-name problem: direct official model listing and
small real requests succeed. It is also not a local-proxy-only outage: the
official endpoint became unstable for the 80-cue real prompt. The design sends
large, unconstrained JSON-generation jobs (four concurrently) and then performs
an even larger unconstrained reduce request. That substantially raises the
probability of upstream disconnection, empty output, and proxy-budget timeout.
The proxy adds a stricter failure mode, but it is not the sole cause.

## Recommended repair direction

1. Use a byte/token budget for candidate map chunks (start at about 4 KB of
   user JSON, rather than fixed 80 cues), and lower candidate extraction
   concurrency to two for proxy-backed runs.
2. Enforce a small maximum number of candidates and variants per map response.
3. Deduplicate candidates locally, then reduce in bounded tiers or use a
   deterministic local merge for obvious duplicates; never send the whole set
   in one unconstrained reducer request.
4. Request JSON-object mode and bound output tokens for the two consistency
   calls; validate empty assistant content as a retryable failure.
5. Treat consistency auditing as non-blocking as it is now, but log a precise
   machine-readable failure reason and retry count.

## Status

Implemented and verified locally on 2026-07-22:

- `core/quality.py` now packs consistency map and reducer prompts by a 4,096
  byte request estimate, caps candidate/rule counts and output tokens, requests
  JSON-object mode, and replaces the one global reducer with local per-source
  evidence merging plus bounded reducer groups.
- `core/pipeline.py` preserves an in-memory retry state across the two
  guide-level attempts, so successful map and reducer groups are not replayed.
  Existing account concurrency remains unchanged.
- `tests/test_consistency_guide_budget.py` covers all 1,872 synthetic entries
  staying within budget and verifies a failed reducer retry reuses successful
  map output.
- Verification: `82 passed, 3 subtests passed`.

An official single-account smoke test using 40 real cues was started after the
implementation, but the desktop task was interrupted twice by user status
messages before the external request returned. This does not affect the local
test result; a later run can complete that optional live smoke check.
