"""Storage for application tracking. Shares the Repository connection and schema."""

from __future__ import annotations

from models.schemas import Application, ApplicationEvent, ApplicationStatus, Job


class ApplicationStore:
    def __init__(self, conn):
        self.conn = conn

    def _load(self, where: str = "", params: tuple = ()) -> list[Application]:
        rows = self.conn.execute(
            f"""SELECT a.*, j.is_active AS job_is_active
                FROM application a JOIN job j ON j.id = a.job_id {where} ORDER BY a.id DESC""",
            params,
        ).fetchall()
        return [
            Application(
                **dict(row),
                history=[
                    ApplicationEvent(status=event["status"], happened_at=event["happened_at"])
                    for event in self.conn.execute(
                        "SELECT * FROM application_event WHERE application_id = ? ORDER BY id", (row["id"],)
                    )
                ],
            )
            for row in rows
        ]

    def list(self) -> list[Application]:
        return self._load()

    def get(self, application_id: int) -> Application | None:
        return next(iter(self._load("WHERE a.id = ?", (application_id,))), None)

    def for_job(self, job_id: int) -> Application | None:
        return next(iter(self._load("WHERE a.job_id = ?", (job_id,))), None)

    def _record(self, application_id: int, status: ApplicationStatus, now: str) -> None:
        self.conn.execute(
            "INSERT INTO application_event (application_id, status, happened_at) VALUES (?, ?, ?)",
            (application_id, status.value, now),
        )

    def save_package(self, job: Job, folder: str, now: str) -> int:
        """Record a saved package. New content always needs a fresh review, so any approval is cleared."""
        with self.conn:
            application_id = self.conn.execute(
                """INSERT INTO application (job_id, title, company, apply_url, folder) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (job_id) DO UPDATE SET
                       folder = excluded.folder, status = 'ready', approved_at = NULL, follow_up_on = NULL
                   RETURNING id""",
                (job.id, job.title, job.company, job.apply_url, folder),
            ).fetchone()["id"]
            self._record(application_id, ApplicationStatus.READY, now)
        return application_id

    def set_status(
        self,
        application_id: int,
        status: ApplicationStatus,
        now: str,
        *,
        approved_at: str | None = None,
        applied_at: str | None = None,
        follow_up_on: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE application SET status = ?, approved_at = ?, applied_at = COALESCE(?, applied_at),
                                          follow_up_on = ?
                   WHERE id = ?""",
                (status.value, approved_at, applied_at, follow_up_on, application_id),
            )
            self._record(application_id, status, now)

    def set_follow_up(self, application_id: int, follow_up_on: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE application SET follow_up_on = ? WHERE id = ?", (follow_up_on, application_id))
