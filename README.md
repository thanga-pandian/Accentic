# Accentic — Compliance Document Analysis

Ingests RBI regulatory PDFs, extracts sections with exact character offsets,
and provides a browser viewer where every clause is traceable to a precise
location in the original document.

## Quick start

### 1. Database

```bash
# With Docker (preferred)
docker compose up -d
docker compose ps   # wait for "healthy"

# Or use a local Postgres installation
# Create database: assent_compliance, user: assent, password: assent, port: 5433
```

### 2. Environment

```bash
cp .env.example .env
# Edit .env and fill in ANTHROPIC_API_KEY
```

### 3. Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the migration

```bash
python db/migrate.py
```

### 5. Ingest a regulation PDF

```bash
# Dry run first — check clause paths and page numbers before writing to DB
python -m extract.sectioner data/regulations/rbi-kyc.pdf --dry-run

# Full ingest
python -m extract.sectioner data/regulations/rbi-kyc.pdf
```

### 6. Verify offsets

```bash
# Spot-check a specific clause
python -m extract.verify_offsets 38

# Audit all sections (should report 100%)
python -m extract.verify_offsets --audit
```

### 7. Start the viewer

```bash
uvicorn api.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000).

## Project layout

```
data/regulations/       raw PDF inputs (not committed)
extract/
  sectioner.py          PDF/TXT → regulation + reg_section rows
  verify_offsets.py     offset spot-check and audit
db/
  schema.sql            table definitions (regulation, reg_section, needs_review)
  connection.py         psycopg connection helper
  migrate.py            idempotent schema apply
api/
  main.py               FastAPI: /api/regulations, /api/sections, /api/sections/{id}
web/
  index.html            single-file viewer (no build step)
prompts/
  obligation_extraction.md   Sprint 2 LLM prompt
```

## PDF fallback

If pdfplumber produces bad text for a PDF, create a plain-text fixture instead:

```bash
# data/regulations/rbi-kyc-extract.txt — paste real clauses by hand
python -m extract.sectioner data/regulations/rbi-kyc-extract.txt --dry-run
```

The sectioner accepts `.pdf` or `.txt` — everything downstream is identical.
