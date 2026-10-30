# Development

Create a virtual environment and install `python -m pip install -e '.[dev]'`, then run
`python -m pytest -q`. The supported CI baseline is Python 3.11 on Linux.

Tests build temporary homes from tracked filter/settings files and fictional files under
`config/examples/`. They must not read the developer's profile or `.env`. The shared fixture blocks
unmocked Requests calls, so integration tests need explicit mocks. Optional browser and Gmail
packages are not needed for the offline suite.

Keep schemas consistent between the runtime files and examples. Local filenames remain unchanged;
new users run `cp config/examples/* config/` once before editing their personal values. Do not run
that command over an existing configured project: it would replace local facts.

Before committing, inspect `git status --short`, `git diff --cached`, and the exact staged file list.
Never force-add ignored profile, answer-bank, credential, database, or generated-document files.

## Agent memory workflow

Shared [root instructions](../AGENTS.md) define how to work. Folder-level `AGENTS.md`
files add local rules; adjacent `CLAUDE.md` files contain only `@AGENTS.md` imports.
Edit the shared instructions rather than maintaining separate copies for each tool.
Folders without local guidance inherit their ancestors. Add new scoped guidance
only when that area has distinct rules, keeping root guidance around 120 lines
and scoped guidance around 60 lines or less.

At the start of a task, read the [handoff](handoff.md), inspect Git status/diffs,
consult relevant [decisions](decisions.md), and explicitly read instructions along
the path to each target file. Do this even when a tool loads some files automatically.
Verify notes against current code; fix stale claims and surface consequential conflicts.

For substantive work, maintain a dated task section in the handoff with objective,
status, progress, relevant files, validation performed, blockers, and exact next step.
Update it at meaningful checkpoints and before ending a session so an interrupted
task can resume. Preserve unrelated tasks; do not claim work or tests were completed
without evidence. For a read-only task with no project state to preserve, no entry
is necessary.

Record lasting architecture, behavior, configuration, dependency, or workflow choices
in the decision log using its entry format. Explain alternatives and rationale;
label proposals and supersede obsolete decisions explicitly. Do not invent reasons
for existing code or log every edit. Git holds the detailed change history.

Before finishing, update affected documentation and task validation, mark completed
work, and condense obsolete handoff details once durable knowledge is recorded.
Include shared memory updates in the same commit as related changes. Keep secrets,
candidate facts, application payloads, and raw chat transcripts out of these files.

For documentation-only changes, check relative links and anchors, matching Claude
imports, command accuracy, and agreement with code; runtime tests are unnecessary
unless executable behavior changes. Where available, use a fresh session at root
and in a scoped folder to confirm instruction loading. Report unavailable checks
as outstanding. Files are guidance rather than enforcement; other agents should
use the README onboarding prompt. No automatic hooks or memory database are required.

## Initial snapshot

The initial publication was prepared in a separate copy while the original project remained active.
History starts with that snapshot. Later edits in the original folder are not included automatically.
After its current work reaches a checkpoint, connect it to the repository and reconcile newer edits
with the public examples, documentation, fixtures, and ignore rules before making the next commit.
Keep the existing personal config files local throughout that handoff.
