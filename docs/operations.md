# Operating the local workflow

Start with the [README setup](../README.md#setup) and use fictional profiles only for tests.
No schedule, live submission, workflow promotion, or external message is enabled by installation.

## Review the profile

Complete local facts and answers, then run `jobhunt profile-confirm`. This records the current
profile/answer-bank hash. Editing either invalidates the review and prior answer/document approval.
The fictional template must have been replaced and `example_profile` set to `false`.

Authorization and start-date answers have an explicit review window in the local profile:
`answers_confirmed_on` and `answers_valid_until`, both ISO dates. They are your confirmed
facts, not dates inferred by the application. Pending/expired values cause required questions
to pause. You can approve an exact question for a single job through the inbox; time-sensitive
question answers expire at the end of that local day.

## Telegram

Create a bot using Telegram's BotFather and put its token in local `.env`. Set:

```text
TELEGRAM_BOT_TOKEN=<your bot token>
TELEGRAM_CHAT_ID=<your private chat numeric ID>
TELEGRAM_USER_ID=<your Telegram numeric user ID>
```

Start a private conversation with the bot. Both numeric IDs are required; commands from
other users/chats are ignored. Do not publish these settings or tokens. To obtain the IDs,
inspect your bot's `getUpdates` response locally after sending `/start`; avoid sharing the
response or putting a token-containing URL into a public log.

```bash
jobhunt telegram-poll --once
jobhunt telegram-poll
```

The poller may run alongside `daily`; human commands use transactional updates rather than
holding the long pipeline lock. Delivery failures remain in SQLite and retry with backoff.
Replies are processed while the Mac/poller is running. Telegram retains undelivered updates
for at most 24 hours: after extended downtime, the bot reissues still-open requests. Resend a
reply if necessary. Replayed commands cannot resolve the same request twice.

Supported commands:

```text
/status
/answer REQUEST_ID exact answer
/approve REQUEST_ID
/resume REQUEST_ID
/skip REQUEST_ID
/done REQUEST_ID employer receipt evidence
/retry REQUEST_ID evidence that submission did not occur
```

A direct reply to a bot request message is also treated as an answer when that request supports
answers. Acknowledgement means the command was recorded; application work continues on the
next `apply`/`daily` run. It does not imply the application was submitted.

## Human review and takeover

`jobhunt inbox` lists pending requests; `jobhunt job JOB_ID` includes details and evidence.
For a prepared application, inspect `data/browser/JOB_ID/prepared-*.png` and its versioned PDFs
under `out/JOB_ID/`. The screenshots show the prepared fields; the PDFs are the actual uploads.
`jobhunt review-application JOB_ID` creates an approval request from a successful dry run.

The local equivalents of Telegram commands are `jobhunt answer`, `approve`, `resume`, `skip`,
`done`, and `retry`. `done` and `retry` require their evidence text as an argument.

For login/CAPTCHA/account steps, use `jobhunt takeover REQUEST_ID`. It opens a visible browser
with that job's persisted session. Finish the account/challenge step, then press Enter in the
terminal to save/close. Resolve the request with `jobhunt resume REQUEST_ID`; a fresh preparation
rechecks availability, fields, and approvals. If you submitted manually, use `done` with receipt
evidence instead. Unsupported form layouts can be completed manually or skipped.

Unknown answers default to the current job only. For a recurring attestation, you can explicitly
approve its exact wording for the same workflow/employer:

```bash
jobhunt answer REQUEST_ID Yes --scope workflow
```

Changed wording, a different employer, or an edited profile requires new approval. Demographic,
sensitive, and consent questions are not inferred from other candidate data.

`duplicate` requests show another possibly matching job. Inspect both requisitions; `resume`
explicitly keeps the new one separate, while `skip` excludes it. No histories are silently merged.

## Availability and documents

Unsupported or blocked availability checks remain uncertain. Resume retries the authoritative
check; it does not declare a job open. If a board lead has a supported employer posting, link it:

```bash
jobhunt job-link JOB_ID https://job-boards.greenhouse.io/BOARD/jobs/POSTING_ID --evidence 'Employer career page linked to this requisition'
```

Existing submission attempts must be reconciled before changing their employer link. If the URL
already belongs to another job, review the duplicate rather than overwriting its identity.

Closed postings stop. Changed descriptions invalidate scores/documents/approvals and return to
filtering. Unknown browser layouts require manual help. Workday and general third-party links
do not currently have an authoritative availability adapter or automatic application adapter.

A failed PDF/required letter enters `document_blocked`. Fix the rendering dependency or source
issue, then run `jobhunt documents-retry JOB_ID` followed by `jobhunt tailor`. The workflow
never uploads an HTML fallback as a resume. Numeric/technology/source checks are incomplete
semantic safeguards; review the actual generated content before trusting a workflow.

## Live promotion and stopping

Run dry trials first. After the configured dry-run gate, `jobhunt go-live` permits individually
approved submissions. It does not promote any ATS workflow. Review requests bind the current
profile, document bytes, form shape/options/visible terms/link targets, and entered answers; changes invalidate approval.

`jobhunt workflow-enable greenhouse-classic-v1 --evidence '...'` requires three distinct,
individually approved, confirmed live applications by default. Only previously validated form
shapes then qualify for automatic submission. This adapter covers classic `application_form`
DOM layouts; modern/custom forms still pause. Use `workflow-disable greenhouse-classic-v1`
to revoke promotion, or `pause-live` to stop live submission globally.

Ambiguous outcomes stay held. `retry` explicitly records your reconciliation that no submission
occurred; `done` records confirmed manual/submitted evidence. Neither command guesses from a
subprocess exit status. Required documents and availability are checked again before resuming.

## Scheduling, exports, and optional email

```bash
jobhunt schedule-install           # daily at 9 a.m. local time
jobhunt schedule-telegram-install  # Telegram polling while the Mac is running
jobhunt schedule-status
jobhunt schedule-telegram-status
jobhunt schedule-remove
jobhunt schedule-telegram-remove
jobhunt export
```

These commands install/remove local launchd agents only when you invoke them. Sleep/network
loss pauses work. SQLite retains completed stages, pending requests, and uncertain attempts;
restart reconciles claimed attempts. Logs/exports live under ignored `reports/`. Keep backups
of `data/` and `out/`, including browser sessions and document versions.

Gmail is opt-in (`tracking.gmail_enabled: true`) and uses read-only OAuth. Manual `jobhunt track`
also explicitly runs tracking. Review ambiguous company/role matches with `jobhunt email-reviews`,
then `jobhunt email-link EVENT_ID JOB_ID`. Old email events cannot overwrite a later event's
recruitment state. Submission totals are derived independently from the application ledger.
