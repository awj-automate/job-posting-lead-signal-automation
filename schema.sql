-- Schema for the job-posting-lead-signal pipeline.
-- Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS companies (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL,
    normalized_key         TEXT NOT NULL UNIQUE,
    domain                 TEXT,
    employee_count         INTEGER,
    industry               TEXT,
    passed_lookup          TEXT,
    rejection_reason       TEXT,
    contacts_discovered_at TIMESTAMPTZ,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_looked_up_at      TIMESTAMPTZ,
    CONSTRAINT companies_passed_lookup_chk
        CHECK (passed_lookup IN ('yes', 'no') OR passed_lookup IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_companies_passed ON companies (passed_lookup);
CREATE INDEX IF NOT EXISTS idx_companies_pending_contacts
    ON companies (passed_lookup)
    WHERE passed_lookup = 'yes' AND contacts_discovered_at IS NULL;

CREATE TABLE IF NOT EXISTS jobs (
    id             SERIAL PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title          TEXT,
    url            TEXT,
    location       TEXT,
    source         TEXT,
    search_role    TEXT,
    run_timestamp  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs (company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_run     ON jobs (run_timestamp);

CREATE TABLE IF NOT EXISTS contacts (
    id                     SERIAL PRIMARY KEY,
    company_id             INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    first_name             TEXT,
    last_name              TEXT,
    title                  TEXT,
    email                  TEXT,
    email_status           TEXT,
    email_source           TEXT,
    verified_at            TIMESTAMPTZ,
    synced_to_instantly_at TIMESTAMPTZ,
    instantly_lead_id      TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT contacts_company_email_uq UNIQUE (company_id, email)
);

CREATE INDEX IF NOT EXISTS idx_contacts_company  ON contacts (company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_unsynced ON contacts (synced_to_instantly_at)
    WHERE synced_to_instantly_at IS NULL;

CREATE TABLE IF NOT EXISTS runs (
    id                           SERIAL PRIMARY KEY,
    started_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at                  TIMESTAMPTZ,
    jobs_scraped                 INTEGER,
    companies_new                INTEGER,
    companies_cached_yes         INTEGER,
    companies_cached_no          INTEGER,
    contacts_found               INTEGER,
    contacts_verified_ok         INTEGER,
    contacts_synced_to_instantly INTEGER,
    error                        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);
