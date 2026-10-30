# Current project handoff

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
  Record actual loading results here. No runtime implementation task is currently active.
