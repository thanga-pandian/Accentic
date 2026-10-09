-- SQLite schema (development fallback — mirrors db/schema.sql for Postgres)
-- Run via: python -m db.migrate

CREATE TABLE IF NOT EXISTS regulation (
    id              TEXT PRIMARY KEY,
    regulator       TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    title           TEXT NOT NULL,
    version         TEXT,
    effective_from  TEXT,
    source_url      TEXT,
    full_text       TEXT NOT NULL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    official_url    TEXT,
    retrieved_on    TEXT
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

-- ===========================================================================
-- Feature 3: Regulatory obligation extraction
-- Every obligation carries offsets into regulation.full_text, verified in Python.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS obligation (
    id              TEXT PRIMARY KEY,
    section_id      TEXT NOT NULL REFERENCES reg_section(id) ON DELETE CASCADE,
    regulation_id   TEXT NOT NULL REFERENCES regulation(id) ON DELETE CASCADE,
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
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_obligation_regulation ON obligation (regulation_id);
CREATE INDEX IF NOT EXISTS idx_obligation_risk ON obligation (risk_tier);

-- ===========================================================================
-- Feature 4: Bank control-framework understanding
-- ===========================================================================
CREATE TABLE IF NOT EXISTS control_doc (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    owner_org       TEXT,
    version         TEXT,
    full_text       TEXT NOT NULL,
    source_url      TEXT,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS control (
    id              TEXT PRIMARY KEY,
    control_doc_id  TEXT NOT NULL REFERENCES control_doc(id) ON DELETE CASCADE,
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
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_control_doc ON control (control_doc_id);

-- ===========================================================================
-- Control testing: independent re-performance, not the control owner's say-so
-- ===========================================================================
CREATE TABLE IF NOT EXISTS control_test (
    id              TEXT PRIMARY KEY,
    control_id      TEXT NOT NULL REFERENCES control(id) ON DELETE CASCADE,
    test_type       TEXT NOT NULL,
    procedure       TEXT NOT NULL,
    result          TEXT NOT NULL,
    metric          TEXT,
    threshold       TEXT,
    conclusion      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_control_test_control ON control_test (control_id);

-- ===========================================================================
-- Feature 7: Evidence-based compliance assessment
-- ===========================================================================
CREATE TABLE IF NOT EXISTS evidence (
    id              TEXT PRIMARY KEY,
    control_id      TEXT NOT NULL REFERENCES control(id) ON DELETE CASCADE,
    evidence_type   TEXT NOT NULL,
    reference       TEXT NOT NULL,
    captured_on     TEXT,
    status          TEXT NOT NULL DEFAULT 'present',
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- Feature 5: Regulatory-to-control mapping
-- ===========================================================================
CREATE TABLE IF NOT EXISTS mapping (
    id              TEXT PRIMARY KEY,
    obligation_id   TEXT NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    control_id      TEXT REFERENCES control(id) ON DELETE CASCADE,
    coverage        TEXT NOT NULL,
    coverage_score  REAL NOT NULL DEFAULT 0.0,
    rationale       TEXT,
    method          TEXT NOT NULL DEFAULT 'lexical',
    confidence      REAL NOT NULL DEFAULT 0.0,
    scope_gap       TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mapping_obligation ON mapping (obligation_id);

-- ===========================================================================
-- Features 8 & 9: Gap identification + risk-based prioritization
-- ===========================================================================
CREATE TABLE IF NOT EXISTS gap (
    id              TEXT PRIMARY KEY,
    obligation_id   TEXT NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    gap_type        TEXT NOT NULL,
    severity        TEXT NOT NULL,
    risk_score      REAL NOT NULL DEFAULT 0.0,
    likelihood      REAL NOT NULL DEFAULT 0.0,
    impact          REAL NOT NULL DEFAULT 0.0,
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_gap_risk ON gap (risk_score);

-- ===========================================================================
-- Feature 10: Remediation recommendation engine
-- ===========================================================================
CREATE TABLE IF NOT EXISTS remediation (
    id              TEXT PRIMARY KEY,
    gap_id          TEXT NOT NULL REFERENCES gap(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    owner_role      TEXT,
    effort          TEXT,
    priority        INTEGER NOT NULL DEFAULT 3,
    due_days        INTEGER,
    rationale       TEXT,
    generated_by    TEXT NOT NULL DEFAULT 'rule_engine',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- Feature 14: Regulatory contradiction detection
-- ===========================================================================
CREATE TABLE IF NOT EXISTS contradiction (
    id              TEXT PRIMARY KEY,
    obligation_a    TEXT NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    obligation_b    TEXT NOT NULL REFERENCES obligation(id) ON DELETE CASCADE,
    conflict_type   TEXT NOT NULL,
    explanation     TEXT,
    severity        TEXT NOT NULL DEFAULT 'medium',
    confidence      REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- Feature 2 & 11: Regulatory change intelligence + impact analysis
-- ===========================================================================
CREATE TABLE IF NOT EXISTS reg_change (
    id              TEXT PRIMARY KEY,
    regulation_id   TEXT NOT NULL REFERENCES regulation(id) ON DELETE CASCADE,
    prior_version   TEXT,
    change_type     TEXT NOT NULL,
    clause_path     TEXT,
    summary         TEXT NOT NULL,
    before_text     TEXT,
    after_text      TEXT,
    impact_level    TEXT NOT NULL DEFAULT 'medium',
    affected_count  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===========================================================================
-- Observability: every agent action is logged (supporting-feature axis)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent_run (
    id              TEXT PRIMARY KEY,
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
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_run_runid ON agent_run (run_id);

-- ===========================================================================
-- Ongoing monitoring. source_snapshot and kri_snapshot persist across runs,
-- they are the memory of what the system saw last time.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS monitor_alert (
    id              TEXT PRIMARY KEY,
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    title           TEXT NOT NULL,
    detail          TEXT,
    due_date        TEXT,
    ref_type        TEXT,
    ref_id          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_snapshot (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    content         TEXT,
    captured_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_source_snapshot_path ON source_snapshot (path, captured_at);

-- Trainability: reviewer decisions persist across runs. They override the
-- agent on the obligation they name and teach the mapping model for others.
CREATE TABLE IF NOT EXISTS review_decision (
    id              TEXT PRIMARY KEY,
    obligation_key  TEXT NOT NULL,
    target          TEXT NOT NULL,
    regulator       TEXT,
    clause_ref      TEXT,
    obligation_text TEXT NOT NULL,
    system_value    TEXT,
    decided_value   TEXT NOT NULL,
    note            TEXT,
    reviewer        TEXT NOT NULL,
    decided_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_review_key ON review_decision (obligation_key, target);

-- Remediation workflow: tasks outlive runs, keyed on the obligation, so status,
-- owner and comments survive re-analysis. The SLA clock starts at first detection.
CREATE TABLE IF NOT EXISTS remediation_task (
    id              TEXT PRIMARY KEY,
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
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_comment (
    id              TEXT PRIMARY KEY,
    obligation_key  TEXT NOT NULL,
    author          TEXT NOT NULL,
    body            TEXT NOT NULL,
    system          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_task_comment_key ON task_comment (obligation_key, created_at);

CREATE TABLE IF NOT EXISTS kri_snapshot (
    id              TEXT PRIMARY KEY,
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
    captured_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Official sanctions data. Owned by the sync, not the pipeline: never reset per run.
CREATE TABLE IF NOT EXISTS sanctions_snapshot (
    id              TEXT PRIMARY KEY,
    source_url      TEXT NOT NULL,
    date_generated  TEXT NOT NULL,
    retrieved_at    TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    individuals     INTEGER NOT NULL DEFAULT 0,
    entities        INTEGER NOT NULL DEFAULT 0,
    added           INTEGER NOT NULL DEFAULT 0,
    removed         INTEGER NOT NULL DEFAULT 0,
    changes         TEXT
);

CREATE TABLE IF NOT EXISTS sanctions_entry (
    id               TEXT PRIMARY KEY,
    reference_number TEXT NOT NULL,
    kind             TEXT NOT NULL,
    name             TEXT NOT NULL,
    list_type        TEXT NOT NULL,
    listed_on        TEXT,
    aliases          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sanctions_entry_list ON sanctions_entry (list_type);
