-- runs.db app tables. runs.db can be wiped when this schema changes.

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    app_version    TEXT NOT NULL,
    status         TEXT NOT NULL,
    stop_reason    TEXT,
    router         TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    seeds_json     TEXT,
    city_override  TEXT,
    parsed_json    TEXT,
    plan_version   INTEGER
);

CREATE TABLE IF NOT EXISTS run_plans (
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    plan_version     INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('draft', 'frozen')),
    city             TEXT,
    target_qualified INTEGER NOT NULL,
    checklist_json   TEXT NOT NULL,
    checklist_key    TEXT,
    sender_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, plan_version)
);

CREATE TABLE IF NOT EXISTS run_overrides (
    run_id TEXT NOT NULL,
    key    TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (run_id, key, seq)
);

CREATE TABLE IF NOT EXISTS answers (
    pause_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('intake', 'supervisor', 'memory')),
    answered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_rounds (
    run_id        TEXT NOT NULL,
    round         INTEGER NOT NULL,
    plan_version  INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    closed_at     TEXT,
    new_companies INTEGER,
    stop_reason   TEXT,
    PRIMARY KEY (run_id, round)
);

CREATE TABLE IF NOT EXISTS search_calls (
    request_hash  TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    round         INTEGER NOT NULL,
    query         TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_candidates (
    run_id         TEXT NOT NULL,
    lead_id        TEXT NOT NULL,
    round          INTEGER NOT NULL,
    queue_pos      INTEGER NOT NULL,
    status         TEXT NOT NULL,
    counted        INTEGER NOT NULL DEFAULT 0,
    found_by_query TEXT,
    judge_json     TEXT,
    us_evidence    TEXT,
    drop_reason    TEXT,
    PRIMARY KEY (run_id, lead_id)
);

CREATE TABLE IF NOT EXISTS triage_labels (
    domain_key     TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    confidence     REAL,
    reason         TEXT,
    prompt_version TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    page_id        INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL,
    lead_id        TEXT NOT NULL,
    requested_url  TEXT NOT NULL,
    final_url      TEXT,
    http_status    INTEGER,
    fetch_status   TEXT NOT NULL,
    content_type   TEXT,
    raw_html_zlib  BLOB,
    clean_text     TEXT,
    sha256         TEXT,
    markers_json   TEXT,
    fetched_at     TEXT NOT NULL,
    UNIQUE (run_id, requested_url)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    lead_id     TEXT NOT NULL,
    page_id     INTEGER,
    kind        TEXT NOT NULL CHECK (kind IN ('name', 'person', 'location', 'verdict', 'own_site')),
    quote       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_observations (
    obs_id          TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    lead_id         TEXT NOT NULL,
    address_norm    TEXT NOT NULL,
    address_as_seen TEXT NOT NULL,
    page_id         INTEGER,
    source_url      TEXT NOT NULL,
    method          TEXT NOT NULL,
    domain_relation TEXT NOT NULL,
    owner_link      TEXT NOT NULL DEFAULT 'none' CHECK (owner_link IN ('none', 'name_match')),
    matched_form    TEXT,
    hunter_json     TEXT,
    UNIQUE (run_id, lead_id, address_norm, method)
);

CREATE TABLE IF NOT EXISTS phones_seen (
    run_id  TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    e164    TEXT NOT NULL,
    page_id INTEGER,
    PRIMARY KEY (run_id, lead_id, e164)
);

CREATE TABLE IF NOT EXISTS qualifications (
    run_id            TEXT NOT NULL,
    lead_id           TEXT NOT NULL,
    checklist_version INTEGER NOT NULL,
    points            INTEGER NOT NULL,
    verdicts_json     TEXT NOT NULL,
    qualified         INTEGER NOT NULL,
    email             TEXT,
    email_type        TEXT,
    email_source_kind TEXT CHECK (email_source_kind IN ('page', 'hunter')),
    email_source_url  TEXT,
    greeting_name     TEXT,
    drop_reason       TEXT,
    PRIMARY KEY (run_id, lead_id)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key           TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hunter_cache (
    domain_key    TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id         TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    lead_id          TEXT NOT NULL,
    subject          TEXT NOT NULL,
    body             TEXT NOT NULL,
    text_hash        TEXT NOT NULL,
    claim_flags_json TEXT,
    model            TEXT,
    prompt_version   TEXT,
    status           TEXT NOT NULL CHECK (status IN ('ok', 'draft_failed')),
    queue_pos        INTEGER NOT NULL,
    UNIQUE (run_id, lead_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    draft_id      TEXT NOT NULL,
    review_seq    INTEGER NOT NULL,
    action        TEXT NOT NULL,
    final_subject TEXT,
    final_body    TEXT,
    approved_hash TEXT,
    superseded    INTEGER NOT NULL DEFAULT 0,
    decided_at    TEXT NOT NULL,
    PRIMARY KEY (draft_id, review_seq)
);

CREATE TABLE IF NOT EXISTS send_log (
    draft_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    domain_key  TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    message_id  TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS supervisor_decisions (
    run_id      TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    status_json TEXT NOT NULL,
    legal_moves TEXT NOT NULL,
    router      TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    chosen_move TEXT NOT NULL,
    shadow_move TEXT,
    reason      TEXT,
    question_key TEXT,
    assumption  TEXT,
    PRIMARY KEY (run_id, turn)
);

CREATE TABLE IF NOT EXISTS api_calls (
    call_id       TEXT PRIMARY KEY,
    run_id        TEXT,
    vendor        TEXT NOT NULL,
    call_site     TEXT NOT NULL,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    units         REAL,
    stop_reason   TEXT,
    cost_estimate REAL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_candidates_status ON run_candidates (run_id, status, queue_pos);
CREATE INDEX IF NOT EXISTS ix_api_calls_run ON api_calls (run_id, call_site);
