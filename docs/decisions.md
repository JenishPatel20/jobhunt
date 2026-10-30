# Project decisions

Keep lasting choices here; use Git for individual changes and the
[handoff](handoff.md) for current work. See [maintenance](development.md#agent-memory-workflow).
Existing code and architecture document observed behavior, not evidence of an
unrecorded historical rationale.

## Entry format

Use the next available sequential ID, `DEC-0002`, `DEC-0003`, and so on.
Each entry includes:

- **ID and title**
- **Date:** `YYYY-MM-DD`
- **Status:** proposed, accepted, rejected, or superseded (with replacement ID)
- **Context:** the problem and relevant constraints
- **Alternatives:** meaningful options considered
- **Decision:** the selected approach
- **Rationale:** why it was chosen
- **Consequences:** costs, benefits, and follow-up obligations
- **References:** related code, documentation, issues, or commits when available

Record significant architecture, behavior, configuration, dependency, and
development-practice choices. Label proposals as proposed; do not present them
as accepted. To replace a decision, add a new entry and mark the old one superseded
with a link to its replacement. Preserve the historical rationale.

## DEC-0001: Shared repository memory

- **Date:** 2026-10-30
- **Status:** accepted
- **Context:** New chats and different coding agents need project instructions,
  lasting reasoning, and enough current state to resume work without chat history.
- **Alternatives:** Independent Claude notes in every folder; tool-local automatic
  memory; a shared instruction hierarchy with separate decisions and handoffs.
- **Decision:** Use root and scoped `AGENTS.md` files as shared instructions,
  adjacent uppercase `CLAUDE.md` files importing `@AGENTS.md`, this decision log,
  and a dated task handoff. Add folder guidance at meaningful boundaries only.
- **Rationale:** One instruction source avoids duplicated rules. Separating lasting
  reasoning from current tasks keeps startup context small and useful across tools.
  The project owner explicitly approved this design in the implementation request.
- **Consequences:** Agents must read and maintain the shared files alongside changes.
  Git remains the detailed change history. Other tools use the README onboarding
  prompt if they do not discover instructions automatically. Automatic loading and
  updates are not guaranteed; no hooks or memory database are introduced.
- **References:** [Root instructions](../AGENTS.md), [handoff](handoff.md),
  [development workflow](development.md#agent-memory-workflow),
  [Codex instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
  [Claude shared instructions](https://code.claude.com/docs/en/memory#share-one-file-with-other-coding-tools).

## DEC-0002: Evidence-backed local workflow

- **Date:** 2026-10-30
- **Status:** accepted
- **Context:** Discovery age/URL checks, current-state rows, and backend exit codes
  did not establish current job availability or successful application submission.
- **Alternatives:** Extend the model-controlled browser prompt; replace the stack
  with hosted orchestration; retain the local application and add explicit evidence/gates.
- **Decision:** Keep Python/SQLite and add observation/snapshot history, authoritative
  availability checks, separate eligibility/fit, versioned artifacts, atomic attempts,
  exact approvals, and a narrow deterministic Greenhouse classic-form adapter.
- **Rationale:** The approved implementation plan prioritizes traceability and
  recoverability before autonomy. Local storage and small adapters fit the workload.
- **Consequences:** Legacy IDs/attempts are retained with a v2 migration backup.
  Similar requisitions require review instead of merging. Unknown availability,
  unrecognized forms, missing PDFs, and uncertain submissions pause. Broader ATS
  application coverage needs separately validated adapters and operator promotion.
- **References:** [Architecture](architecture.md), [operations](operations.md),
  [application orchestration](../jobhunt/apply/__init__.py),
  [Greenhouse public/submission API boundaries](https://docs.greenhouse.io/job-board.html).

## DEC-0003: Subscription-only model execution and explicit cost boundaries

- **Date:** 2026-10-30
- **Status:** accepted
- **Context:** Consumer subscriptions do not establish an unrestricted API allowance.
  The application cannot enforce that an API organization spends only included credits.
- **Alternatives:** API calls with a claimed free-model list; automatic provider
  fallback; supported subscription CLI access with limits and a hard pause.
- **Decision:** Enable only Codex CLI with verified ChatGPT authentication in the
  provider factory. Strip API-key overrides, disable shell/web tools, use temporary
  read-only runs, and preserve call caps/cooldowns. Disable legacy Gemini browser
  automation, unverified ApplyPilot submission, and configured fake submission.
- **Rationale:** The approved plan permits existing included access but no paid fallback.
  Rejecting unprovable API billing routes makes the implemented boundary explicit.
- **Consequences:** Codex CLI/login and actual subscription access must be checked
  locally. Limits stop model work. No credits are bought or topped up by the code.
  Operators must also disable account-level automatic credit purchasing; this app
  does not administer vendor billing settings. Included API-credit support remains
  disabled until a reliable credits-only boundary can be enforced.
- **References:** [Provider](../jobhunt/providers.py),
  [Codex authentication](https://learn.chatgpt.com/docs/auth),
  [noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode),
  [API-credit exhaustion behavior](https://developers.openai.com/api/docs/guides/chatgpt-api-credits),
  [Google consumer CLI deprecation](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals).

## DEC-0004: Durable private Telegram help and scoped permissions

- **Date:** 2026-10-30
- **Status:** accepted
- **Context:** Console fallbacks and global learned answers were insufficient for
  resumable human help, company-specific answers, and changed certifications.
- **Alternatives:** Local inbox only; public webhook hosting; private Telegram long polling.
- **Decision:** Use a SQLite outbox, user/chat allowlist, replay-safe request resolution,
  per-job browser sessions, and exact scoped answers. Keep CLI resolution available.
  Reissue unresolved requests after extended downtime; require explicit operator
  promotion after reviewed trials and hold previously unseen form shapes.
- **Rationale:** The approved plan selects Telegram replies and local execution,
  enabling help without public hosting or additional service purchases.
- **Consequences:** Polling/automation pauses while the Mac is unavailable. Telegram
  updates may expire after 24 hours. Answers resume preparation on the next run;
  they do not independently authorize submission. Personal facts stay in ignored
  runtime storage and must not be copied into shared project memory.
- **References:** [Operator guide](operations.md), [human resolution](../jobhunt/apply/hitl.py),
  [Telegram update retention and polling](https://core.telegram.org/bots/api#getting-updates).
