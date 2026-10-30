# Offline test guidance

Read the [root instructions](../AGENTS.md) and
[development guide](../docs/development.md) first.

- Run `python -m pytest -q` from the repository root in the development environment.
  CI uses Python 3.11 on Linux; browser and Gmail extras are not required.
- Use the `ctx` fixture in `conftest.py` or an equivalent temporary home. It copies
  shared settings/filters and fictional examples, then creates isolated storage.
  Do not load the developer's `.env`, personal profile, answers, or real database.
- The autouse fixture blocks unmocked Requests calls and removes Gemini/model
  environment overrides. Preserve this isolation; mock responses at the boundary.
- The Requests guard does not block every SDK, subprocess, browser, or SMTP path.
  Explicitly replace those boundaries with fakes when exercising them.
- Keep fixtures fictional and small. Optional dependency tests should use fakes
  or narrowly justified skips rather than requiring real accounts or credentials.
- Test meaningful behavior and regression cases: state transitions, identity,
  retries/cooldowns, factual fallbacks, application gates, and human resolution.
- For database changes, exercise existing data as well as a fresh schema. For
  application changes, assert relevant persisted outcomes and avoid live backends.

Record the exact commands/outcomes in the [handoff](../docs/handoff.md).
Passing offline tests does not verify ATS feeds, Gemini, browser submissions,
Gmail OAuth, notification delivery, or launchd scheduling.
