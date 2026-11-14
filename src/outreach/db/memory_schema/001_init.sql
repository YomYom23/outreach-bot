-- memory.db v1. Kept across runs; later changes go in new numbered files.

CREATE TABLE leads (
    domain_key            TEXT PRIMARY KEY,
    research_status       TEXT NOT NULL,
    first_run_id          TEXT NOT NULL,
    last_run_id           TEXT NOT NULL,
    last_checklist_key    TEXT,
    normalization_version INTEGER NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE do_not_contact (
    domain_key TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    run_id     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE contacted (
    domain_key TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    draft_id   TEXT NOT NULL,
    sent_at    TEXT NOT NULL
);

CREATE TABLE sender_profiles (
    profile_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    business_name  TEXT NOT NULL,
    call_to_action TEXT NOT NULL,
    remembered     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE checklists (
    checklist_key TEXT PRIMARY KEY,
    items_json    TEXT NOT NULL,
    threshold     INTEGER,
    remembered    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    remembered INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
