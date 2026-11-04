"""Application tracking. CareerPilot never submits anything: the user applies on the official page."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from database.application_repository import ApplicationStore
from models.schemas import Application, ApplicationStatus as Status

FOLLOW_UP_DAYS = 7
PACKAGE_FILES = {"cv.pdf", "cover-letter.pdf", "cover-letter.txt", "job.txt"}

# "Applied" can only be reached through "approved", so nothing is marked sent without a review.
TRANSITIONS = {
    Status.READY: {Status.APPROVED, Status.WITHDRAWN},
    Status.APPROVED: {Status.READY, Status.APPLIED, Status.WITHDRAWN},
    Status.APPLIED: {Status.INTERVIEW, Status.REJECTED, Status.WITHDRAWN},
    Status.INTERVIEW: {Status.OFFER, Status.REJECTED, Status.WITHDRAWN},
    Status.REJECTED: set(),
    Status.OFFER: set(),
    Status.WITHDRAWN: set(),
}
# These packages record what was really sent, so they can no longer be replaced.
SENT = {Status.APPLIED, Status.INTERVIEW, Status.REJECTED, Status.OFFER}


class AttentionItem(BaseModel):
    application_id: int
    title: str
    detail: str


class StatusError(ValueError):
    """The move is not allowed. The message is safe to show the user."""


def timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def can_replace_package(application: Application | None) -> bool:
    return application is None or application.status not in SENT


def move(store: ApplicationStore, application: Application, status: Status, today: date | None = None) -> None:
    if status not in TRANSITIONS[application.status]:
        raise StatusError(f"An application that is {application.status.value} cannot become {status.value}.")
    today = today or date.today()
    now = timestamp()
    if status is Status.APPROVED and not Path(application.folder).is_dir():
        raise StatusError("The package folder is missing. Save the package again before approving it.")

    approved_at = application.approved_at
    if status is Status.APPROVED:
        approved_at = now
    elif status is Status.READY:
        approved_at = None

    follow_up = None
    if status is Status.APPLIED:
        follow_up = (today + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    elif status is Status.INTERVIEW:
        follow_up = application.follow_up_on
    store.set_status(
        application.id,
        status,
        now,
        approved_at=approved_at,
        applied_at=now if status is Status.APPLIED else None,
        follow_up_on=follow_up,
    )


def attention_items(applications: list[Application], today: date | None = None) -> list[AttentionItem]:
    today = today or date.today()
    items = []
    for application in applications:
        role = f"{application.title} at {application.company}"

        def add(title: str, detail: str) -> None:
            items.append(AttentionItem(application_id=application.id, title=title, detail=detail))

        if application.status is Status.READY:
            add("Review your package", f"{role} is waiting for your approval.")
        elif application.status is Status.APPROVED:
            add("Submit your application", f"You approved {role}. Apply on the official page, then mark it applied.")
        if application.status in (Status.READY, Status.APPROVED) and not application.job_is_active:
            add("This job may have closed", f"{role} is no longer listed at its source.")
        due = application.follow_up_on
        if application.status in (Status.APPLIED, Status.INTERVIEW) and due and due <= today.isoformat():
            add("Follow up", f"A follow-up for {role} was due on {due}.")
    return items
