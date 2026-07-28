# Batch glossary 504 investigation — 2026-07-27

- Symptom: batch glossary unification of 187 candidates sent five 40-item requests with `reasoning_effort=high` and concurrency two. Two requests reached the local proxy's hard 165-second budget and failed with `ProxyRequestBudgetExceeded` / HTTP 504.
- Evidence: the failed requests were both 40-item high-reasoning calls; subsequent 20-item split retries completed in 25–35 seconds. Historical local request dumps show 40-item bodies are only 5.8–6.3 KB, so this is not an HTTP payload-size limit. Hidden high-reasoning work and upstream disconnects are the cause; request cardinality amplifies the latency tail.
- Fix: use `reasoning_effort=none`, cap normal unifier requests at 20 candidates, serialize them, and apply a 160-second local deadline so a failed request splits to 10 candidates before the proxy's 165-second cutoff.
- Verification: `python -m pytest -q test_batch_glossary.py tests\\test_batch_glossary_review_fallback.py` (29 passed); related fallback/UI/worker tests (9 passed); full suite: 181 passed, 2 skipped.
- Concern: no live request was sent to the configured proxy during this investigation, to avoid using a user-provided API credential. The exact upstream error rate needs the next normal batch run to confirm.
