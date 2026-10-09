-- PostgreSQL schema (production). Mirrors db/sqlite_schema.sql table for table.
-- Flags stay INTEGER (0/1) so the same queries run on both engines.

DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pgvector not available, skipping';
END $$;

CREATE TABLE IF NOT EXISTS regulation (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    regulator       TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    title           TEXT NOT NULL,
    version         TEXT,
    effective_from  DATE,
    source_url      TEXT,
    full_text       TEXT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    official_url    TEXT,
    retrieved_on    DATE
);
ALTER TABLE regulation ADD COLUMN IF NOT EXISTS official_url TEXT;
ALTER TABLE regulation ADD COLUMN IF NOT EXISTS retrieved_on DATE;

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
CREATE INDEX IF NOT EXISTS idx_reg_section_regulation_clause ON reg_section (regulation_id, clause_path);
CREATE INDEX IF NOT EXISTS idx_reg_section_char_start ON reg_section (char_start);

CREATE TABLE IF NOT EXISTS needs_review (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stage           TEXT NOT NULL,
    payload         TEXT,
    failure_reason  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS obligation (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id      UUID NOT NULL REFERENCES reg_section(id) ON DELETE CASCADE,
    regulation_id   UUID NOT NULL REFERENCES regulation(id) ON DELETE CASCADE,
    clause_path     TEXT NOT NULL,
    obligation_text TEXT NOT NULL,
    actor           TEXT,
    modality        TEXT,
    action          TEXT,
    condition       TEXT,
    deadline        TEXT,
    risk_tier       TEXT NOT NULL DEFAULT 'medium',
    category        TEXT,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,
    confidence      REAL NOT NULL DEFAULT 0.0,
    extracted_by    TEXT NOT NULL DEFAULT 'rule_engine',
    page_no         INTEGER,
    clause_ref      TEXT,
    applicability   TEXT NOT NULL DEFAULT 'applicable',
    applicability_reason TEXT,
    applies_to      TEXT,
    applicability_reviewed INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_obligation_regulation ON obligation (regulation_id);
CREATE INDEX IF NOT EXISTS idx_obligation_risk ON obligation (risk_tier);

CREATE TABLE IF NOT EXISTS control_doc (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    owner_org       TEXT,
    version         TEXT,
    full_text       TEXT NOT NULL,
    source_url      TEXT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    control_doc_id  UUID NOT NULL REFERENCES control_doc(id) ON DELETE CASCADE,
    control_ref     TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    owner           TEXT,
    frequency       TEXT,
    control_type    TEXT,
    automated       INTEGER NOT NULL DEFAULT 0,
    last_tested     TEXT,
    test_result     TEXT,
    effectiveness   REAL NOT NULL DEFAULT 0.0,
    reported_effectiveness REAL,
    independent_result TEXT,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_control_doc ON control (control_doc_id);

CREATE TABLE IF NOT EXISTS control_test (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    control_id      UUID NOT NULL REFERENCES control(id) ON DELETE CASCADE,
    test_type       TEXT NOT NULL,
    procedure       TEXT NOT NULL,
    result          TEXT NOT NULL,
    metric          TEXT,
    threshold       TEXT,
    conclusion      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_control_test_control ON control_test (control_id);

CREATE TABLE IF NOT EXISTS evidence (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    control_id      UUID NOT NULL REFERENCES control(id) ON DELETE CASCADE,
    evidence_type   TEXT NOT NULL,
    reference       TEXT NOT NULL,
    captured_on     TEXT,
    status          TEXT NOT NULL DEFAULT 'present',
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mapping (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_id   UUID NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    control_id      UUID REFERENCES control(id) ON DELETE CASCADE,
    coverage        TEXT NOT NULL,
    coverage_score  REAL NOT NULL DEFAULT 0.0,
    rationale       TEXT,
    method          TEXT NOT NULL DEFAULT 'lexical',
    confidence      REAL NOT NULL DEFAULT 0.0,
    scope_gap       TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mapping_obligation ON mapping (obligation_id);

CREATE TABLE IF NOT EXISTS gap (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_id   UUID NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    gap_type        TEXT NOT NULL,
    severity        TEXT NOT NULL,
    risk_score      REAL NOT NULL DEFAULT 0.0,
    likelihood      REAL NOT NULL DEFAULT 0.0,
    impact          REAL NOT NULL DEFAULT 0.0,
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gap_risk ON gap (risk_score);

CREATE TABLE IF NOT EXISTS remediation (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gap_id          UUID NOT NULL REFERENCES gap(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    owner_role      TEXT,
    effort          TEXT,
    priority        INTEGER NOT NULL DEFAULT 3,
    due_days        INTEGER,
    rationale       TEXT,
    generated_by    TEXT NOT NULL DEFAULT 'rule_engine',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contradiction (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_a    UUID NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    obligation_b    UUID NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    conflict_type   TEXT NOT NULL,
    explanation     TEXT,
    severity        TEXT NOT NULL DEFAULT 'medium',
    confidence      REAL NOT NULL DEFAULT 0.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reg_change (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    regulation_id   UUID NOT NULL REFERENCES regulation(id) ON DELETE CASCADE,
    prior_version   TEXT,
    change_type     TEXT NOT NULL,
    clause_path     TEXT,
    summary         TEXT NOT NULL,
    before_text     TEXT,
    after_text      TEXT,
    impact_level    TEXT NOT NULL DEFAULT 'medium',
    affected_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_run (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,
    input_summary   TEXT,
    output_summary  TEXT,
    reasoning       TEXT,
    tool_calls      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    recovered       INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_run_runid ON agent_run (run_id);

CREATE TABLE IF NOT EXISTS monitor_alert (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    title           TEXT NOT NULL,
    detail          TEXT,
    due_date        TEXT,
    ref_type        TEXT,
    ref_id          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_snapshot (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    content         TEXT,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_source_snapshot_path ON source_snapshot (path, captured_at);

CREATE TABLE IF NOT EXISTS review_decision (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_key  TEXT NOT NULL,
    target          TEXT NOT NULL,
    regulator       TEXT,
    clause_ref      TEXT,
    obligation_text TEXT NOT NULL,
    system_value    TEXT,
    decided_value   TEXT NOT NULL,
    note            TEXT,
    reviewer        TEXT NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_review_key ON review_decision (obligation_key, target);

CREATE TABLE IF NOT EXISTS remediation_task (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_key  TEXT NOT NULL UNIQUE,
    regulator       TEXT,
    clause_ref      TEXT,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    assignee        TEXT,
    first_detected  TEXT NOT NULL,
    last_detected   TEXT,
    closed_at       TEXT,
    resolution      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_comment (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_key  TEXT NOT NULL,
    author          TEXT NOT NULL,
    body            TEXT NOT NULL,
    system          INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_task_comment_key ON task_comment (obligation_key, created_at);

CREATE TABLE IF NOT EXISTS kri_snapshot (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          TEXT NOT NULL,
    obligations     INTEGER NOT NULL DEFAULT 0,
    applicable      INTEGER NOT NULL DEFAULT 0,
    coverage_pct    REAL NOT NULL DEFAULT 0.0,
    gaps            INTEGER NOT NULL DEFAULT 0,
    critical        INTEGER NOT NULL DEFAULT 0,
    high            INTEGER NOT NULL DEFAULT 0,
    verification_pct REAL NOT NULL DEFAULT 0.0,
    failing_controls INTEGER NOT NULL DEFAULT 0,
    alerts          INTEGER NOT NULL DEFAULT 0,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Official sanctions data. Owned by the sync, not the pipeline: never reset per run.
CREATE TABLE IF NOT EXISTS sanctions_snapshot (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_url      TEXT NOT NULL,
    date_generated  TEXT NOT NULL,
    retrieved_at    TIMESTAMPTZ NOT NULL,
    sha256          TEXT NOT NULL,
    individuals     INTEGER NOT NULL DEFAULT 0,
    entities        INTEGER NOT NULL DEFAULT 0,
    added           INTEGER NOT NULL DEFAULT 0,
    removed         INTEGER NOT NULL DEFAULT 0,
    changes         TEXT
);

CREATE TABLE IF NOT EXISTS sanctions_entry (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_number TEXT NOT NULL,
    kind             TEXT NOT NULL,
    name             TEXT NOT NULL,
    list_type        TEXT NOT NULL,
    listed_on        TEXT,
    aliases          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sanctions_entry_list ON sanctions_entry (list_type);
