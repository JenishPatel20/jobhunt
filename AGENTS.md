# Jobhunt agent instructions

## Project and sources

Jobhunt is a local Python CLI for discovering jobs, filtering matches, tailoring
documents, applying with human oversight, and tracking outcomes in SQLite.
Python 3.11 is the CI baseline; macOS/Linux are supported. Scheduling uses macOS launchd.

- [README](README.md): setup, commands, privacy, and current limitations.
- [Architecture](docs/architecture.md): data flow, storage, and external boundaries.
- [Development](docs/development.md): fixtures, validation, and memory maintenance.
- [Decisions](docs/decisions.md): accepted choices and their rationale.
- [Handoff](docs/handoff.md): current tasks, evidence, blockers, and next steps.

## Start each task

1. Read this file and the handoff. Consult decisions relevant to the task.
2. Inspect `git status --short` and relevant diffs; preserve unrelated user work.
3. Before editing, inspect every directory from the root to each target file for
   nested `AGENTS.md` files and read them in that order. Tools differ in automatic
   discovery; explicitly open applicable instructions even when starting at root.
4. Verify remembered claims against current code. Treat stale notes as context,
   correct them, and report consequential conflicts instead of guessing intent.
5. For substantive work, add or update its dated task section in the handoff.
   Preserve other tasks' progress and blockers.

Folder guidance exists for [core code](jobhunt/AGENTS.md),
[discovery](jobhunt/discover/AGENTS.md), [applications](jobhunt/apply/AGENTS.md),
[configuration](config/AGENTS.md), and [tests](tests/AGENTS.md).
Descendants without their own instructions inherit their ancestors' guidance.
Scoped guidance supplements project rules; it must not silently weaken them.

## Setup and validation

For a new development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

Use an existing environment when available. The offline suite needs no personal
config, API credentials, browser sessions, or optional browser/Gmail dependencies.
Use relevant tests for code changes and the offline suite before committing code.
For documentation-only work, verify paths, commands, imports, and consistency;
runtime tests are unnecessary unless executable behavior changed.
Report commands actually run, their outcomes, and anything left unverified.
Offline success does not establish live service compatibility.

## Preserve project boundaries

- Keep personal profiles, answers, company/query lists, credentials, databases,
  and generated artifacts local. Follow `.gitignore`; never force-add them.
- Use fictional examples for shared documentation and tests. Never put secrets,
  candidate facts, application payloads, or raw chat transcripts in memory files.
- Copy example configuration only for missing files in a new setup; never
  overwrite an existing user's configuration with examples.
- Preserve factual checks in tailoring and the application's dry-run/live gates,
  daily submission limits, human inbox, and interrupted-attempt reconciliation.
- Treat live submissions, notifications, account access, and scheduling as
  external actions requiring task authorization. Use mocks for development.
- Maintain read-only Gmail tracking and manual review of LinkedIn drafts.

## Finish or hand off

- Update the relevant documentation when behavior or development practices change.
- Record lasting choices in the decision log using its entry format. Keep
  individual code changes in Git rather than expanding instruction files.
- Update the task's handoff section at checkpoints and before ending a session:
  status, relevant files, validation evidence, blockers, and exact next step.
- Mark completed tasks and condense stale details after durable knowledge has
  moved into decisions or project documentation. Do not erase unrelated work.
- Keep instructions short and factual. `AGENTS.md` is the shared source;
  adjacent `CLAUDE.md` files contain only `@AGENTS.md` imports.
- Include memory updates with related changes when committing. Inspect the
  exact staged files and diff; report completion and outstanding validation.

These files guide cooperating agents; they do not enforce behavior or guarantee
automatic memory updates. Other tools should use the README onboarding prompt.
