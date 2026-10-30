# Configuration guidance

Read the [root instructions](../AGENTS.md) first. This guidance also covers `examples/`.

- `settings.yaml`, `titles.yaml`, and `dealbreakers.yaml` are shared tracked defaults.
  `examples/` contains fictional starting files for personal configuration.
- Personal `profile.yaml`, `answer_bank.yaml`, `companies.yaml`, `queries.yaml`,
  and `everify.txt` stay ignored. Preserve existing files; never bulk-copy examples
  over a configured project or force-add ignored files.
- Keep example keys/types consistent with `jobhunt/config.py`, profile consumers,
  and changed runtime behavior. Tests copy examples into temporary homes.
- Keep all candidate facts, companies, and answers in examples fictional; do not
  transfer a user's local facts into examples, instructions, or decision records.
- Keep credentials in local environment/OAuth files. Document variable names or
  placeholders, never actual tokens, account identifiers, or private topic URLs.
- Changes to submission gates, daily limits, model settings, or external endpoints
  affect operational behavior: document the choice and preserve explicit live gates.
- Model identifiers and quota assumptions require account-specific validation;
  do not describe defaults as currently available or free without verification.

For configuration changes, inspect consuming code, update examples where needed,
and run relevant [offline tests](../tests/AGENTS.md). See the
[development guide](../docs/development.md) for safe setup and maintenance.
