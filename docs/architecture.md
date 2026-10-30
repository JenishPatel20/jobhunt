# Architecture

Jobhunt is a local modular Python application. SQLite is the private system of record;
files hold candidate inputs, immutable generated document versions, and browser evidence.
The CLI orchestrates restartable stages. [Operations](operations.md) describes operator actions.

## Storage and identity

`JOBHUNT_HOME` selects the runtime home; otherwise the current directory is used. Shared
settings/rules are tracked; personal profiles/answers, `.env`, databases, browser sessions,
documents, and exports are ignored. Tests use temporary fictional homes.

Schema version 2 adds tables/columns to the original schema. Before migrating an existing
database, a `*.before-v2.db` backup is made. Stable job IDs and old attempts remain. The old
unique company/title/location key is retired in place; new jobs prefer source + board + posting
identity and otherwise canonical URL. Similar titles are hints requiring duplicate review,
not grounds for merging histories. Source URL aliases preserve discovery provenance.

- `observations`: every returned discovery record, including malformed/stale records.
- `snapshots`, `job_sources`, `source_runs`: content versions, source links, adapter health.
- `availability_checks`, `assessments`: timestamped employer checks and cached fit evidence.
- `artifacts`: document paths/hashes, source facts/plan, profile version, validation results.
- `applications`: separate attempt/submission history, actual entered values, artifact versions,
  form fingerprint, and employer confirmation evidence.
- `journal`: state changes, decisions, human actions, and recovery events.
- `human_requests`, `scoped_answers`, `application_approvals`, `workflow_approvals`: explicit
  pauses and scoped permissions. Legacy unscoped learned answers are retained but not reused.
- `outbox`, `telegram_updates`: delivery retries and replay protection.
- `events`, `email_reviews`: read-only email outcomes and ambiguous matches needing review.

SQLite transactions protect claims and human resolution. The outer advisory CLI lock prevents
concurrent pipeline mutations; Telegram/human commands use short database transactions so
waiting for the user does not hold a worker indefinitely.

## Discovery, availability, and eligibility

ATS adapters retain source IDs and available type/compensation/location metadata. Public-board
results are leads. A direct employer URL can resolve to a supported ATS; otherwise an operator
can link the supported posting using `job-link`. Stale/invalid observations are persisted before
exclusion. Adapter failures are recorded distinctly from successful empty results. Workday
currently uses bounded discovery and has no closure/application adapter.

Availability is `open`, `closed`, or `uncertain`. Supported employer endpoints are checked before
tailoring, before browser preparation, and immediately before submission. Errors, invalid JSON,
identity/title mismatches, or incomplete feeds remain uncertain. Changed descriptions invalidate
scores, documents, and approval and return to filtering. Unsupported endpoints need human help.

Rule restrictions include their source text and are interpreted in sentence context. A mere
mention or optional restriction is held for review. Model-only restriction flags are also held,
not treated as conclusive evidence. Sponsorship silence stays unknown; role wording, historical
employer evidence, and E-Verify are separate. Sponsorship ordering never raises technical fit.
Candidate authorization answers are explicitly provided, dated facts, not inferred eligibility.

Fit uses current profile/preferences, full supplied job text, and retained metadata. Quoted
model evidence must occur in the inputs. Assessments are cached by job/profile/rule/settings
content; changed inputs invalidate them. The minimum raw fit threshold controls shortlisting.
Daily scoring has a separate batch limit to leave model capacity for documents.
Internships and full-time roles share this queue; compensation retains its source units.

## Model access and documents

The provider interface exposes text/JSON generation and cooldown state. The enabled provider
uses Codex CLI with verified ChatGPT sign-in, isolated read-only runs, shell/web tools disabled,
API-key overrides stripped, local call limits, and durable cooldowns. It does not switch to an
API-billed route or purchase/top up credits. Provider access and installed CLI compatibility
remain account/environment-specific. The legacy Gemini client remains only for regression
coverage; CLI orchestration and the doctor no longer select it.

Tailoring selects/reorders/rewords facts with source ownership checks and per-source numeric/
technology checks. Malformed suggestions fall back to locked facts. These checks cannot prove
semantic truth. Source facts/plans and profile hashes are retained with document versions.

Chromium renders PDFs. DOM checks reject horizontal overflow, and PDF extraction checks reject
missing/empty text and excessive page count. Rendering failure blocks upload; there is no HTML
upload fallback. Cover letters are generated only when required, including requirements discovered
by form inspection. Required-letter failure blocks the application.

## Preparation, permission, and recovery

`greenhouse-classic-v1` recognizes a classic `application_form` with accessible labels. It fills
known fields deterministically; unfamiliar controls/layouts pause. No LLM has generic browser
click permission. Preparation blocks mutating HTTP requests. Final submission requires live
mode, the global dry-run gate, reviewed facts, intact PDFs, fresh availability, and either an
exact application approval or an explicitly promoted workflow with a previously validated form
shape. The outbound submission boundary also checks that live mode remains enabled.

Approval binds the job/description, profile, document bytes, form labels/types/options, visible terms/link targets, and entered
values. Changed forms or consent text require review. Workflow promotion requires reviewed,
confirmed live trials and a separate operator command. Confirmation is captured from a recognized
employer receipt element, including text, URL, and a hashed screenshot.

A claim and attempt are persisted before invoking the backend. Explicit pre-submission failures
open an issue request; crashes/lost responses at uncertain boundaries enter reconciliation. On
restart unresolved attempts are held rather than retried. `done`/`retry` require human receipt or
non-submission evidence. Recruitment status changes do not erase the submission ledger.

Human requests preserve job context, exact questions/choices, and a browser checkpoint. Persistent
per-job browser sessions support operator takeover. Resume reopens/refills the form and rechecks
availability/permissions. Answers are job-scoped by default; exact attestations may be explicitly
approved for the same employer/workflow. Profile edits invalidate prior scoped answers.

## Notifications, outcomes, and scheduling

Telegram long polling uses a private chat/user allowlist. The local outbox survives delivery failures;
resolved requests use transactional compare-and-set so duplicate updates cannot repeat an action.
After extended downtime, still-open requests are reissued because Telegram's pending updates have
a 24-hour retention limit. Telegram replies record decisions; the pipeline resumes on its next run.
Optional legacy ntfy/SMTP notifications remain available; Telegram has durable delivery semantics.

Gmail tracking is opt-in and read-only, using metadata/snippets. It matches role/requisition evidence,
holds ambiguous matches, normalizes event dates, and avoids regression from late events. CSV/JSON
exports include every saved job and private history. Submission totals use confirmed applications,
independently of a job's latest recruitment state. Daily caps use the machine's local calendar day.

Optional launchd agents run the daily pipeline and Telegram polling while the Mac is available.
Sleep/network loss pauses operation; persisted states and uncertain attempts support recovery.
Live external compatibility, notifications, OAuth, and launchd behavior require reviewed live checks;
offline and local-browser tests do not establish them.
