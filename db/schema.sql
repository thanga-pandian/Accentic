-- Enable pgvector if available (used in later sprints; safe to skip if not installed)
DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pgvector not available, skipping (not needed for Sprint 1)';
END $$;

-- Regulations: one row per ingested document
CREATE TABLE IF NOT EXISTS regulation (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    regulator       TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    title           TEXT NOT NULL,
    version         TEXT,
    effective_from  DATE,
    source_url      TEXT,
    full_text       TEXT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sections: one row per clause; offsets into regulation.full_text
CREATE TABLE IF NOT EXISTS reg_section (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    regulation_id   UUID NOT NULL REFERENCES regulation(id) ON DELETE CASCADE,
    clause_path     TEXT NOT NULL,
    page_no         INTEGER NOT NULL,
    raw_text        TEXT NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reg_section_regulation_clause
    ON reg_section (regulation_id, clause_path);

CREATE INDEX IF NOT EXISTS idx_reg_section_char_start
    ON reg_section (char_start);

-- Failed parses: never silently drop a section
CREATE TABLE IF NOT EXISTS needs_review (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stage           TEXT NOT NULL,
    payload         JSONB,
    failure_reason  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
