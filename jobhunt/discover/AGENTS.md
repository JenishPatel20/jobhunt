# Discovery guidance

Read the [root](../../AGENTS.md) and [core](../AGENTS.md) instructions first.

- `ats.py` normalizes public Greenhouse, Lever, Ashby, and Workday records;
  `scrapers.py` holds optional adapters; `sponsors.py` imports historical data.
  `__init__.py` orchestrates fetching and storage.
- Keep normalized records compatible with `DB.upsert_job`: source, company,
  title, location, URL/application URL, description, and posting date.
  Preserve application-URL fallback and normalization of missing scraper values.
- Deduplication and stable identity belong in the shared database helpers.
  Preserve enrichment of existing thin descriptions without resetting job status.
- Preserve bounded request timeouts and per-board/query failure isolation.
  Changes to pagination, dates, or vendor payloads need representative mocked cases.
- Treat guessed company boards as candidates for review. Do not turn a successful
  HTTP response into a claim of verified company ownership.
- Historical sponsorship and E-Verify data are signals; do not present them as
  guarantees that a current role sponsors a candidate.
- Import user-supplied sponsor files without committing source datasets. Use
  small fictional CSV/workbook fixtures for importer tests.
- Workday varies by tenant; offline fixtures cannot establish live compatibility.
  Preserve optional scraper dependencies and graceful handling when absent.

Mock feed requests, scraper clients, and sleeps as appropriate in
[offline tests](../../tests/AGENTS.md). Use temporary databases and configuration;
do not run live discovery or sponsor imports as a documentation check.
