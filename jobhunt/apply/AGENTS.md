# Application guidance

Read the [root](../../AGENTS.md) and [core](../AGENTS.md) instructions first.

- `__init__.py` owns application orchestration and gates; `backends.py` adapts
  browser backends; `hitl.py` manages human requests and resolution.
- Live submission requires the configured successful dry-run count and explicit
  live enablement. Respect `max_applies_per_day`. Forced live requests must still
  pass the gates; keep dry runs distinct from actual submissions.
- Validate required profile facts before attempting applications. Unknown answers,
  attestations, and browser obstacles belong in the human inbox, not guessed facts.
- Preserve the application-attempt record and `applying` state before invoking
  the backend. An interrupted attempt may already have submitted externally:
  require human reconciliation before retrying it, avoiding duplicate requests.
- Keep backend result states consistent with the orchestrator and inbox resolution.
  Count a live submission only on the appropriate confirmed backend result.
- Preserve existing answers and human-request history. Test answer/done/skip/retry
  transitions against the request kinds they actually support.
- Treat browser payloads and backend results as potentially personal. Keep them
  out of shared memory and tracked fixtures; use fictional examples.
- Preserve optional backend/tooling behavior and actionable failures when tooling
  is unavailable. Do not claim browser automation is live-verified from mocked tests.

Use fake backends and notifiers in [offline tests](../../tests/AGENTS.md).
Cover gates, daily limits, dry/live outcomes, and interrupted-attempt handling when
changing them. Do not enable live mode or submit an application to validate code.
