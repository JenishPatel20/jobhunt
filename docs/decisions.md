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
