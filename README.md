# Jobhunt

[![Offline tests](https://github.com/JenishPatel20/jobhunt/actions/workflows/tests.yml/badge.svg)](https://github.com/JenishPatel20/jobhunt/actions/workflows/tests.yml)

A local Python pipeline for discovering jobs, ranking matches, tailoring application documents,
and tracking applications in SQLite. Built by **Jenish Patel**.

Job searching involves repeated work across job boards, resumes, forms, and email. Jobhunt
keeps that work in one queue, with explicit dry-run gates and a human inbox for questions
that need judgment.

```text
Profile → Discover → Filter → Tailor → Apply → Track → Learn
                      ↓                ↓       ↓
                 SQLite queue ← human inbox → digest
```

## What it does

- Reads public Greenhouse, Lever, Ashby, and Workday job feeds; optional JobSpy adapters broaden discovery.
- Filters by role, location, experience requirements, and sponsorship signals before Gemini fit scoring.
- Uses historical sponsor records from user-imported disclosure files as ranking signals.
- Selects and rewrites resume bullets using source IDs, numeric checks, and a known-technology vocabulary.
- Produces HTML resumes, optional PDFs, and checked cover letters.
- Routes application questions, attestations, and browser obstacles to a local human inbox.
- Classifies optional Gmail metadata and snippets into application events, and reports skill gaps.

**Stack:** Python 3.11+, SQLite, PyYAML, Requests, Gemini REST, optional Playwright, ntfy,
and read-only Gmail integration. See [architecture](docs/architecture.md) for data flow and boundaries.

## Setup

Use macOS or Linux; the CLI currently imports Unix `fcntl`. Scheduling commands use macOS launchd.

```bash
git clone https://github.com/JenishPatel20/jobhunt.git
cd jobhunt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
cp config/examples/* config/
jobhunt preflight
```

The examples describe a **fictional candidate**. Replace all candidate facts and application answers in
`config/profile.yaml` and `config/answer_bank.yaml` before applying. Review the role filters and add
reviewed ATS boards to `config/companies.yaml`; optional scraping queries start empty.

Set `GEMINI_API_KEY` in `.env` for scoring and tailoring. Review `llm.models` and quota settings in
`config/settings.yaml`: the snapshot's model names and free-tier assumptions have not been verified
against your account. `jobhunt llm-doctor` makes a small connectivity check without sending profile data.

Optional browser/PDF and scraper dependencies:

```bash
python -m pip install -e '.[pdf,scrape]'
playwright install chromium
npm install -g @google/gemini-cli
```

The browser application backend requires Gemini CLI, Node.js, and its Playwright MCP tooling.
Its complete live behavior remains unverified. Gmail additionally needs the `gmail` extra and your
local OAuth credentials. Missing notification settings fall back to console output.

## Example workflow

After configuring real facts, credentials, and job sources:

```bash
jobhunt discover
jobhunt jobs --limit 20
jobhunt filter --limit 10
jobhunt tailor --limit 5
jobhunt apply --dry-run --limit 1
jobhunt inbox
jobhunt status
```

To use sponsor signals, download disclosure data yourself, then run
`jobhunt sponsors-import path/to/disclosure.csv`. `jobhunt probe --top 200` guesses boards from
imported employers; review matches with `jobhunt companies` before relying on them.
Historical sponsorship is a ranking signal, not a guarantee about a current role.

Unknown questions and obstacles move jobs into `needs_human`; the rest of the queue can continue.
Use `jobhunt answer <id> '<answer>'`, `jobhunt done <id>`, or `jobhunt skip <id>` to resolve requests.
Interrupted application attempts also require human reconciliation before retrying.

Live submission requires the configured successful dry-run count (20 by default) and an explicit
`jobhunt go-live`. `jobhunt daily` runs the pipeline and can submit after live mode is enabled;
review browser behavior in dry runs before scheduling it. No schedule is installed during setup.

## Local data and credentials

Personal profiles, answer banks, company/query lists, `.env` files, OAuth files, SQLite databases,
resumes, reports, and generated drafts are ignored by Git. Tracked examples contain fictional data.
Tests use temporary copies of those examples, independent of your local configuration.

Candidate facts and job descriptions are sent to Gemini for scoring/tailoring. Phone, email, and profile
links are omitted from those API summaries, but application payloads are supplied to the Gemini CLI
browser backend. Gmail reads headers and snippets rather than full message bodies. Notification
payloads may contain application questions; choose a private topic or protected ntfy server.

## Tests and current limits

```bash
python -m pytest -q
```

CI runs the offline suite on Python 3.11. HTTP requests must be mocked in tests, and no personal
configuration, API keys, browser session, or external account is required.

The suite covers filtering, sponsor imports, tailoring checks, quota/fallback behavior, application gates,
human-inbox transitions, and email-event handling. Passing tests do **not** verify live ATS feeds,
Gemini availability, browser submissions, Gmail OAuth, ntfy delivery, or launchd scheduling.
Workday schemas and the optional ApplyPilot command template need installation-specific validation.
Resume checks detect selected classes of unsupported claims; they are not a complete factual verifier.
LinkedIn output is drafted locally for manual review and posting.

## Development

See [development notes](docs/development.md) for configuration, tests, and the snapshot workflow.

For coding agents, start with the shared [project instructions](AGENTS.md),
[current handoff](docs/handoff.md), and [decision log](docs/decisions.md).
Scoped instructions cover core code, discovery, applications, configuration, and tests;
Claude entry points import those same rules. Other agents can use this onboarding prompt:

> Read root AGENTS.md, docs/handoff.md, and relevant folder instructions. Consult docs/decisions.md for relevant decisions, then inspect the current code and Git status before making changes.

Maintain shared memory using the [development workflow](docs/development.md#agent-memory-workflow).
Agent tools differ in automatic loading; these files do not guarantee automatic updates.

This repository contains no license grant. An optional ApplyPilot integration must be evaluated
against the license of the specific implementation being used.
