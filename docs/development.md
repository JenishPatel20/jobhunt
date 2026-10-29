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

## Initial snapshot

The initial publication was prepared in a separate copy while the original project remained active.
History starts with that snapshot. Later edits in the original folder are not included automatically.
After its current work reaches a checkpoint, connect it to the repository and reconcile newer edits
with the public examples, documentation, fixtures, and ignore rules before making the next commit.
Keep the existing personal config files local throughout that handoff.
