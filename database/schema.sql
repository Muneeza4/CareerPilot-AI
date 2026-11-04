CREATE TABLE IF NOT EXISTS job_source (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES job_source(id),
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    location TEXT,
    description TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    apply_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    is_remote INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    duplicate_of INTEGER REFERENCES job(id),
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_job_canonical_url ON job(canonical_url);
CREATE INDEX IF NOT EXISTS idx_job_company_key ON job(company_key);

CREATE VIRTUAL TABLE IF NOT EXISTS job_fts USING fts5(
    title, company, description, content='job', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS job_ai AFTER INSERT ON job BEGIN
    INSERT INTO job_fts (rowid, title, company, description)
    VALUES (new.id, new.title, new.company, new.description);
END;

CREATE TRIGGER IF NOT EXISTS job_ad AFTER DELETE ON job BEGIN
    INSERT INTO job_fts (job_fts, rowid, title, company, description)
    VALUES ('delete', old.id, old.title, old.company, old.description);
END;

CREATE TRIGGER IF NOT EXISTS job_au AFTER UPDATE OF title, company, description ON job BEGIN
    INSERT INTO job_fts (job_fts, rowid, title, company, description)
    VALUES ('delete', old.id, old.title, old.company, old.description);
    INSERT INTO job_fts (rowid, title, company, description)
    VALUES (new.id, new.title, new.company, new.description);
END;

CREATE TABLE IF NOT EXISTS fetch_run (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES job_source(id),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    fetched INTEGER NOT NULL DEFAULT 0,
    new_jobs INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
