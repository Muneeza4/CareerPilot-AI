from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from database.application_repository import ApplicationStore
from database.profile_repository import ProfileStore
from models.schemas import FetchRun, Job, JobPosting

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
PROFILE_SCHEMA_PATH = Path(__file__).with_name("profile_schema.sql")
APPLICATION_SCHEMA_PATH = Path(__file__).with_name("application_schema.sql")
SENIOR_TITLES = ["senior", "sr", "staff", "lead", "principal", "head", "director", "manager", "vp"]


def _keyword_pattern(keywords: list[str]) -> str:
    """Whole-word match, so that the keyword "AI" does not match "Retail"."""
    options = "|".join(re.escape(keyword) for keyword in keywords)
    return rf"(?<![a-z0-9])(?:{options})(?![a-z0-9])"


@lru_cache(maxsize=32)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and _compiled(pattern).search(value) is not None


class Repository:
    def __init__(self, db_path: str | Path):
        # The scheduler and the web server use the database at the same time.
        self.conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("REGEXP", 2, _regexp, deterministic=True)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.executescript(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.executescript(APPLICATION_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.profiles = ProfileStore(self.conn)
        self.applications = ApplicationStore(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def ensure_source(self, name: str) -> int:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO job_source (name) VALUES (?)", (name,))
        return self.conn.execute("SELECT id FROM job_source WHERE name = ?", (name,)).fetchone()["id"]

    def start_run(self, source_id: int) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO fetch_run (source_id) VALUES (?)", (source_id,)).lastrowid

    def finish_run(
        self,
        run_id: int,
        status: str,
        fetched: int = 0,
        new_jobs: int = 0,
        error: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE fetch_run SET finished_at = datetime('now'), status = ?, fetched = ?,
                                        new_jobs = ?, error = ? WHERE id = ?""",
                (status, fetched, new_jobs, error, run_id),
            )

    def recent_runs(self, limit: int = 20) -> list[FetchRun]:
        rows = self.conn.execute(
            """SELECT s.name AS source, r.started_at, r.finished_at, r.status, r.fetched, r.new_jobs, r.error
               FROM fetch_run r JOIN job_source s ON s.id = r.source_id
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        )
        return [FetchRun(**dict(row)) for row in rows]

    def find_existing(self, source_id: int, external_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM job WHERE source_id = ? AND external_id = ?", (source_id, external_id)
        ).fetchone()
        return row["id"] if row else None

    def find_by_canonical_url(self, canonical_url: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM job WHERE canonical_url = ? AND duplicate_of IS NULL", (canonical_url,)
        ).fetchone()
        return row["id"] if row else None

    def company_jobs(self, company_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, location, description FROM job WHERE company_key = ? AND duplicate_of IS NULL",
            (company_key,),
        ).fetchall()

    def insert_job(
        self,
        source_id: int,
        posting: JobPosting,
        canonical_url: str,
        company_key: str,
        duplicate_of: int | None = None,
    ) -> int:
        with self.conn:
            return self.conn.execute(
                """INSERT INTO job (source_id, external_id, title, company, company_key, location, description,
                                    posted_at, apply_url, canonical_url, is_remote, duplicate_of)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id,
                    posting.external_id,
                    posting.title,
                    posting.company,
                    company_key,
                    posting.location,
                    posting.description,
                    posting.posted_at,
                    posting.apply_url,
                    canonical_url,
                    posting.is_remote,
                    duplicate_of,
                ),
            ).lastrowid

    def update_job(self, job_id: int, posting: JobPosting) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE job SET title = ?, location = ?, description = ?, posted_at = ?, apply_url = ?,
                                  is_remote = ?, is_active = 1, last_seen_at = datetime('now')
                   WHERE id = ?""",
                (
                    posting.title,
                    posting.location,
                    posting.description,
                    posting.posted_at,
                    posting.apply_url,
                    posting.is_remote,
                    job_id,
                ),
            )

    def close_missing(self, source_id: int, seen_external_ids: set[str]) -> int:
        """Mark active jobs that a source no longer lists as closed."""
        rows = self.conn.execute(
            "SELECT id, external_id FROM job WHERE source_id = ? AND is_active = 1", (source_id,)
        ).fetchall()
        missing = [(row["id"],) for row in rows if row["external_id"] not in seen_external_ids]
        with self.conn:
            self.conn.executemany("UPDATE job SET is_active = 0 WHERE id = ?", missing)
        return len(missing)

    def get_keywords(self, default: list[str]) -> list[str]:
        row = self.conn.execute("SELECT value FROM setting WHERE key = 'keywords'").fetchone()
        return json.loads(row["value"]) if row else default

    def set_keywords(self, keywords: list[str]) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO setting (key, value) VALUES ('keywords', ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
                (json.dumps(keywords),),
            )

    def last_successful_run(self) -> str | None:
        """When a source last finished successfully, in UTC."""
        return self.conn.execute(
            "SELECT MAX(finished_at) AS at FROM fetch_run WHERE status = 'success'"
        ).fetchone()["at"]

    def companies(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT company FROM job WHERE duplicate_of IS NULL AND is_active = 1 ORDER BY company"
        )
        return [row["company"] for row in rows]

    def source_names(self) -> list[str]:
        return [row["name"] for row in self.conn.execute("SELECT name FROM job_source ORDER BY name")]

    def get_job(self, job_id: int) -> Job | None:
        row = self.conn.execute(
            """SELECT j.*, s.name AS source FROM job j JOIN job_source s ON s.id = j.source_id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        return _to_job(row) if row else None

    def _job_filters(
        self,
        query: str = "",
        company: str = "",
        location: str = "",
        remote_only: bool = False,
        days: int | None = None,
        active_only: bool = True,
        keywords: list[str] | None = None,
        search_description: bool = False,
        hide_senior: bool = False,
        source: str = "",
    ) -> tuple[str, list]:
        where = ["j.duplicate_of IS NULL"]
        params: list = []
        if keywords:
            where.append(
                "(j.title REGEXP ? OR j.description REGEXP ?)" if search_description else "j.title REGEXP ?"
            )
            pattern = _keyword_pattern(keywords)
            params += [pattern, pattern] if search_description else [pattern]
        if hide_senior:
            where.append("NOT j.title REGEXP ?")
            params.append(_keyword_pattern(SENIOR_TITLES))
        if source:
            where.append("s.name = ?")
            params.append(source)
        if query.strip():
            # Quoting each word stops characters like "+" or ":" being read as FTS syntax.
            words = re.findall(r"\S+", query.replace('"', " "))
            where.append("j.id IN (SELECT rowid FROM job_fts WHERE job_fts MATCH ?)")
            params.append(" ".join(f'"{word}"' for word in words))
        if company.strip():
            where.append("j.company LIKE ?")
            params.append(f"%{company.strip()}%")
        if location.strip():
            where.append("j.location LIKE ?")
            params.append(f"%{location.strip()}%")
        if remote_only:
            where.append("j.is_remote = 1")
        if days is not None:
            where.append("COALESCE(j.posted_at, j.first_seen_at) >= date('now', ?)")
            params.append(f"-{days} days")
        if active_only:
            where.append("j.is_active = 1")

        return " AND ".join(where), params

    def search_jobs(self, limit: int = 5000, offset: int = 0, **filters) -> list[Job]:
        where, params = self._job_filters(**filters)
        rows = self.conn.execute(
            f"""SELECT j.*, s.name AS source FROM job j JOIN job_source s ON s.id = j.source_id
                WHERE {where}
                ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC, j.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return [_to_job(row) for row in rows]

    def count_jobs(self, **filters) -> int:
        where, params = self._job_filters(**filters)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM job j JOIN job_source s ON s.id = j.source_id WHERE {where}", params
        ).fetchone()[0]


def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        description=row["description"],
        posted_at=row["posted_at"],
        apply_url=row["apply_url"],
        is_remote=bool(row["is_remote"]),
        first_seen_at=row["first_seen_at"],
        is_active=bool(row["is_active"]),
    )
