"""Keeps the job database fresh, on a schedule or when the user asks."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import BaseModel

from database.repository import Repository
from services.job_ingest import SourceResult, database_path, load_config, run_all
from services.job_sources import build_sources

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 6


class RefreshStatus(BaseModel):
    running: bool = False
    total: int = 0
    done: int = 0
    current: str | None = None
    new_jobs: int = 0
    failed: list[str] = []
    error: str | None = None


class RefreshManager:
    """Runs one refresh at a time, whether the scheduler or the Fetch button started it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status = RefreshStatus()

    def run(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.status = RefreshStatus(running=True)
        self._execute()
        return True

    def start(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.status = RefreshStatus(running=True)
        threading.Thread(target=self._execute, daemon=True).start()
        return True

    def _record(self, result: SourceResult) -> None:
        self.status.done += 1
        self.status.new_jobs += result.new_jobs
        if result.status == "failed":
            self.status.failed.append(result.source)
        logger.info(
            "%s: %s, fetched %d, new %d, closed %d",
            result.source,
            result.status,
            result.fetched,
            result.new_jobs,
            result.closed,
        )

    def _execute(self) -> None:
        try:
            config = load_config()
            sources = build_sources(config)
            self.status.total = len(sources)
            with Repository(database_path()) as repo:
                run_all(repo, config, sources, self._record)
        except Exception as exc:
            logger.exception("Refresh failed")
            self.status.error = str(exc)[:500]
        finally:
            self.status.running = False
            self._lock.release()


def first_run_time(last_run: str | None, interval_hours: float) -> datetime:
    """Run now if the data is stale. Otherwise restarting the app would call every source again."""
    now = datetime.now(timezone.utc)
    if last_run is None:
        return now
    due = datetime.fromisoformat(last_run).replace(tzinfo=timezone.utc) + timedelta(hours=interval_hours)
    return max(now, due)


def build_scheduler(manager: RefreshManager, interval_hours: float, last_run: str | None) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=timezone.utc)
    scheduler.add_job(
        manager.run,
        "interval",
        hours=interval_hours,
        id="refresh_jobs",
        next_run_time=first_run_time(last_run, interval_hours),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler
