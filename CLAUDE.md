# Accentic — Compliance Document Analysis Tool

## What we are building

Accentic is a pipeline that ingests regulatory PDF documents (starting with RBI KYC),
extracts sections with exact character offsets, lets an LLM identify obligations in each
section, and exposes the results through a web viewer where every claim is traceable back
to a precise location in the original document.

The core value proposition is **verifiable traceability**: every extracted obligation must
link to a character range in the source text that a human can cross-check against the
physical PDF.

## Non-negotiable rules

1. **Offsets are computed in Python, never by the model.** The LLM identifies clauses and
   obligations by content. Python code computes `char_start` and `char_end` by searching
   the joined full text. The model never outputs offsets.

2. **`raw_text` is produced by slicing `full_text`, never by concatenating fragments.**
   `raw_text = full_text[char_start:char_end]` — always this, never anything else. After
   every insert, assert that `full_text[char_start:char_end] == raw_text`. Raise
   immediately if the assertion fails.

3. **Do not modify `full_text` after it is built.** No stripping, normalising, or
   whitespace collapsing. All offsets are relative to the exact string produced by joining
   pages with `"\n\n"`.

4. **Failed sections go to `needs_review`, never dropped silently.** Any section that fails
   to parse or fails the offset assertion is inserted into `needs_review` with a reason.

5. **Split on clause numbering patterns, never on character count or token count.**

6. **All database IDs are UUIDs.** Use `gen_random_uuid()` in Postgres.

7. **No ORM.** Raw psycopg connections only.

8. **No build step for the frontend.** Plain HTML/CSS/JS, one file, no framework, no CDN.

## Architecture

```
data/regulations/       raw PDF inputs
extract/
  sectioner.py          PDF → regulation + reg_section rows
  verify_offsets.py     spot-check and audit offset correctness
  obligations.py        (Sprint 2) LLM obligation extraction per section
db/
  schema.sql            table definitions
  connection.py         psycopg connection helper
  migrate.py            idempotent schema apply
api/
  main.py               FastAPI: /api/regulations, /api/sections, /api/sections/{id}
web/
  index.html            single-file viewer, no build step
```

## Database tables (Sprint 1)

- `regulation` — one row per document; stores `full_text` of the entire document
- `reg_section` — one row per clause; all `char_start`/`char_end` are positions into
  `regulation.full_text`
- `needs_review` — rows that failed parsing or offset assertion

## Sprint 1 exit criteria

- [ ] Three tables in Postgres, migration re-runnable
- [ ] RBI KYC sections ingested with clause paths that look like real RBI numbering
- [ ] `python -m extract.verify_offsets 38` prints PASS and matches the physical PDF page
- [ ] `python -m extract.verify_offsets --audit` reports 100%
- [ ] The viewer runs at `localhost:8000` and clicking a clause shows text plus highlighted
      context

## Environment

- Postgres 16 via Docker on host port 5433, database `assent_compliance`, user `assent`
- Python dependencies: psycopg[binary], pdfplumber, python-dotenv, fastapi, uvicorn, anthropic
- `.env` file (not committed) holds `DATABASE_URL` and `ANTHROPIC_API_KEY`
