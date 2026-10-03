-- SQLite schema (development fallback — mirrors db/schema.sql for Postgres)
-- Run via: python db/migrate.py

CREATE TABLE IF NOT EXISTS regulation (
    id              TEXT PRIMARY KEY,
    regulator       TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    title           TEXT NOT NULL,
    version         TEXT,
    effective_from  TEXT,
    source_url      TEXT,
    full_text       TEXT NOT NULL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reg_section (
    id              TEXT PRIMARY KEY,
    regulation_id   TEXT NOT NULL REFERENCES regulation(id) ON DELETE CASCADE,
    clause_path     TEXT NOT NULL,
    page_no         INTEGER NOT NULL,
    raw_text        TEXT NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reg_section_regulation_clause
    ON reg_section (regulation_id, clause_path);

CREATE INDEX IF NOT EXISTS idx_reg_section_char_start
    ON reg_section (char_start);

CREATE TABLE IF NOT EXISTS needs_review (
    id              TEXT PRIMARY KEY,
    stage           TEXT NOT NULL,
    payload         TEXT,
    failure_reason  TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
