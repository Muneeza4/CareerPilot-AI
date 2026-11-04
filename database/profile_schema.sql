CREATE TABLE IF NOT EXISTS candidate (
    id INTEGER PRIMARY KEY,
    name TEXT,
    email TEXT,
    active_version_id INTEGER
);

CREATE TABLE IF NOT EXISTS preferences (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidate(id) ON DELETE CASCADE,
    target_roles TEXT NOT NULL DEFAULT '[]',
    locations TEXT NOT NULL DEFAULT '[]',
    remote_preference TEXT NOT NULL DEFAULT 'any',
    salary_min INTEGER,
    salary_currency TEXT
);

CREATE TABLE IF NOT EXISTS profile_version (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    cv_sha256 TEXT NOT NULL,
    cv_path TEXT,
    raw_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (candidate_id, cv_sha256)
);

CREATE TABLE IF NOT EXISTS fact (
    id INTEGER PRIMARY KEY,
    profile_version_id INTEGER NOT NULL REFERENCES profile_version(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    parent_key TEXT,
    type TEXT NOT NULL,
    label TEXT,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'extracted'
        CHECK (verification_status IN ('extracted', 'confirmed', 'rejected')),
    UNIQUE (profile_version_id, key)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 'cv' CHECK (source_type IN ('cv', 'user_stated')),
    section TEXT,
    page INTEGER,
    char_start INTEGER,
    char_end INTEGER,
    quote TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_version ON fact(profile_version_id);
CREATE INDEX IF NOT EXISTS idx_evidence_fact ON evidence(fact_id);
