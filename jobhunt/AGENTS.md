# Core pipeline guidance

Read the [root instructions](../AGENTS.md) first. This file applies throughout
`jobhunt/`; also read discovery/application guidance when working there.
See [architecture](../docs/architecture.md) for the full data flow.

## Pipeline and storage

- Keep CLI orchestration consistent with discovery, filtering, tailoring,
  applications, tracking, and learning. Preserve job-state transitions across modules.
- `config.home()` selects `JOBHUNT_HOME` or the current directory. `Ctx` loads
  local configuration and creates database/output directories. Do not construct
  it against a user's home merely to inspect code; use a temporary test home.
- SQLite schema and helpers live in `db.py`. Preserve existing databases, stable
  job IDs, deduplication keys, and persisted states. Schema changes need an explicit
  compatibility approach and tests against an existing database, not just a new one.
- Keep SQL values parameterized. Preserve the CLI's advisory lock for mutating runs.

## Facts and external integrations

- Filtering applies hard rules before model scoring. Preserve provider
  call accounting, cooldown handling, and the subscription-only/no-paid-fallback
  boundary. The daily pipeline skips tailoring/applying when scoring is unavailable.
- Tailoring uses source-linked facts and numeric/technology checks. Preserve
  those checks and fallbacks; do not invent candidate claims or imply complete
  factual verification.
- Keep optional browser, scraper, and Gmail dependencies optional. Mock HTTP,
  model, browser, email, notification, and subprocess boundaries in tests.
- Gmail tracking reads metadata/snippets; learning writes local reports/drafts.
  Do not add Gmail sending or LinkedIn publishing as an incidental change.

Validate changed behavior with the [offline tests](../tests/AGENTS.md). Record
meaningful changes in [decisions](../docs/decisions.md) and unfinished work in
the [handoff](../docs/handoff.md).
