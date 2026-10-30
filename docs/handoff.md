# Current project handoff

## Reliable workflow implementation — 2026-10-30

- **Objective:** Implement the approved ledger, availability/eligibility checks,
  grounded documents, resumable Telegram help, and guarded ATS submission workflow.
- **Status:** Local implementation and fixture validation complete; production activation
  and reviewed live trials require operator setup.
- **Progress:** Added schema-v2 backup/migration, observation/source/snapshot history,
  evidence-backed availability and fit, scoped dated answers, versioned validated PDFs,
  durable Telegram delivery/replies, human takeover, atomic attempts/reconciliation,
  explicit workflow promotion, complete exports, and an opt-in email-review flow.
  The deterministic application adapter supports Greenhouse classic forms; other layouts
  pause. Legacy Gemini/ApplyPilot automation and API-billing routes are disabled.
- **Relevant files:** Runtime changes in `jobhunt/`, fictional examples/settings in
  `config/`, regression/loopback tests in `tests/`, and [operations](operations.md).
  DEC-0002 through DEC-0004 record the lasting choices.
- **Validation performed:** Installed local test/PDF/browser dependencies. The original
  50-test suite passed after environment setup; final combined validation passed
  **104 tests** using `JOBHUNT_BROWSER_TESTS=1 PLAYWRIGHT_BROWSERS_PATH=/tmp/jobhunt-playwright
  .venv/bin/python -m pytest -q --tb=short` (91 offline tests, 13 loopback browser/PDF
  tests). Python source parses with 3.11 syntax; tests executed in the local Python 3.13
  environment. `git diff --check` passed. Checked 59 local Markdown links/anchors and
  six Claude imports. CLI init/status/export passed in a fictional `/tmp` home with
  zero model calls. Rendered/extracted and visually inspected a one-page fictional
  PDF: complete text, no visible clipping. No real notifications/submissions were sent.
- **Remaining limitations:** Codex CLI/sign-in and actual subscription limits are
  unverified here; Telegram credentials/delivery, real ATS compatibility, Gmail OAuth,
  launchd behavior, real candidate facts, and reviewed live trials remain unverified.
  API-credit routes are deliberately unavailable because credits-only billing cannot
  be enforced. Workday remains bounded discovery without authoritative availability
  or an automatic application adapter. Factual/layout checks are incomplete semantic
  and visual safeguards; operator document review remains necessary.
- **Next step:** Follow [setup](../README.md#setup) and [operations](operations.md): fill
  the ignored real profile/answer bank, verify subscription-only access and account
  purchase settings, configure the private Telegram bot, run dry trials, then explicitly
  enable reviewed live submissions. Promote a workflow only after reviewing confirmed
  trials. No schedule or live mode was enabled during implementation.

Update the relevant dated task section at checkpoints and before ending a session.
Preserve other tasks. Keep objective, status, progress, files, validation, blockers,
and exact next step; condense completed tasks after durable knowledge moves into
the [decisions](decisions.md) or project docs. See the
[maintenance workflow](development.md#agent-memory-workflow).

## Baseline observed — 2026-10-30

- The starting checkout was clean at `7aecca6` (initial snapshot). It contains a
  Python CLI, tracked fictional examples, SQLite helpers, and an offline test suite.
- [Architecture](architecture.md) describes observed pipeline/storage behavior.
  [Development](development.md#initial-snapshot) documents an original active copy
  that may contain newer edits; whether it still needs reconciliation is unknown.
  Do not assume that reconciliation has happened or make it an active task without scope.
- Inspected application code retains dry-run/live gates, daily caps, a human inbox,
  and interrupted-attempt reconciliation. Tests construct temporary homes and block
  unmocked Requests calls.
- Documented limitations, not live checks performed here: Gemini model/account
  availability, ATS feeds (especially Workday), browser backend and ApplyPilot
  compatibility, Gmail OAuth, notifications, and launchd remain unverified.
- Tailoring checks catch selected unsupported claims, not every factual error;
  LinkedIn content remains a local draft for manual review.

## Shared repository memory — 2026-10-30

- **Objective:** Make project rules, decisions, and current work portable across agents.
- **Status:** Completed documentation; fresh-session loading verification outstanding.
- **Progress:** Added shared root/scoped instructions and thin Claude imports;
  recorded DEC-0001; added onboarding and memory maintenance documentation.
- **Relevant files:** Root and scoped `AGENTS.md`/`CLAUDE.md` pairs;
  [decisions](decisions.md), this handoff, [development](development.md),
  and [README](../README.md).
- **Validation performed:** Inspected tracked project documentation, configuration
  consumers, discovery/database code, application gates, fixture setup, and CI.
  Confirmed Claude Code CLI version 2.1.179 is installed; Codex CLI is unavailable.
  Checked all 48 local Markdown links/anchors and all six matching Claude imports;
  no missing targets, recursive imports, or whitespace errors were found. Root
  instructions are 81 lines; scoped files are 23–34 lines. Reviewed guidance against
  code and existing docs, including configuration privacy and application gates.
  A fresh tool-disabled Claude session exited with `Not logged in`; it did not
  verify instruction loading. Runtime tests were not run: only Markdown changed.
- **Blockers:** Claude authentication is unavailable and the Codex CLI is absent.
  Neither prevents use of the shared documents; both prevent fresh-session verification here.
- **Next step:** After Claude login or when Codex CLI is available, start fresh
  sessions at repository root and in `jobhunt/apply/`. Ask each to list loaded
  repository instruction files and summarize startup/completion rules; in the
  scoped session also check application gates and interrupted-attempt handling.
  Record actual loading results here.
