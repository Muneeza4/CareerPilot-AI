CREATE TABLE IF NOT EXISTS application (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES job(id),
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    apply_url TEXT NOT NULL,
    folder TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'approved', 'applied', 'interview', 'rejected', 'offer', 'withdrawn')),
    approved_at TEXT,
    applied_at TEXT,
    follow_up_on TEXT
);

CREATE TABLE IF NOT EXISTS application_event (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    happened_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_application ON application_event(application_id);
