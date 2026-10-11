# Frozen live-evaluation usage observer

`paired_usage_v1.py` is the byte-identical instrument used for the authorized
DeepSeek live pairs on 2026-10-11. SHA-256:
`6ca18bc9d6a13aba25a9fdf3e7d460265fa156e00eb737691e39f9404beff7dd`.

It wraps the existing paired harness without changing request parameters,
questions, reference results, verdicts or A/B order. It saves each execution
and numeric provider usage before proceeding. The usage hook does not collect
native provider reasoning content or credentials; checkpoints retain the normal
workflow trace. Its own hash is recorded separately
from the runtime source hash. This is a frozen CLI instrument, not an importable
library API.

Use all arguments supported by `scripts/run_paired_benchmark.py`. For example,
an offline four-execution check:

```bash
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/paired_usage_v1.py \
  --tier 1 --task anime_streaming_dev_1 --repeat 2 --report /tmp/observer.json
```

Tier 3 uses real paid APIs and requires the authorization specified in
AGENTS.md. Use a fresh output prefix; checkpoint/usage files are opened
exclusively. Raw reports remain regenerable, gitignored evidence; quoted
numbers and commands belong in tracked documentation.

`paired_resume_v1.py` preserves the completed prefix of an interrupted report
verbatim, including failures. It checks every manifest control, schedule and
execution fingerprint before calling the provider for unfinished cases. The
original report and usage stay intact; the combined report has a fresh prefix,
copied prior numeric usage, and observer digests linking both segments. It never
retries a completed timeout or chooses which completed results to keep.

```bash
# Same arguments as the original experiment; a fresh report path is required.
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/paired_resume_v1.py \
  /tmp/interrupted.json --tier 1 --task anime_streaming_dev_1 --repeat 2 \
  --report /tmp/resumed.json
```

The usage auditor reconciles mixed traces against reported model spans. A
timeout without provider usage stays unknown: its reported responses can be
priced, but total execution cost is null. The priced-response subtotal is not
a complete bill. Normal measured-token summaries retain the original accounting
and omit pairs whose full token totals are unknown.

`engine_replay_v1.py` is a supplementary offline check of originally passing
SQL through the sanctioned DatabaseTool. It verifies the original database and
SQL-policy hashes before replay and confirms that database bytes stay unchanged.
Its own digest is recorded in the audit. This replay checks stored/reference
rows, not independent policy or full semantic correctness, and calls no model.

```bash
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/engine_replay_v1.py \
  evaluation/reports/phase3_deepseek_flash_candidates_live_v1.json \
  --report /tmp/candidate_engine_replay.json
```
