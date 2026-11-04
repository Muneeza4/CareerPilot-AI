from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class JobPosting(BaseModel):
    external_id: str
    title: str
    company: str
    location: str | None = None
    description: str = ""
    posted_at: str | None = None
    apply_url: str
    is_remote: bool = False


class Job(JobPosting):
    id: int
    source: str
    first_seen_at: str
    is_active: bool


class FetchRun(BaseModel):
    source: str
    started_at: str
    finished_at: str | None
    status: str
    fetched: int
    new_jobs: int
    error: str | None


class FactType(StrEnum):
    CONTACT = "contact"
    SUMMARY = "summary"
    EDUCATION = "education"
    SKILL = "skill"
    EXPERIENCE = "experience"
    PROJECT = "project"
    ACHIEVEMENT = "achievement"
    CERTIFICATION = "certification"


class VerificationStatus(StrEnum):
    EXTRACTED = "extracted"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SourceType(StrEnum):
    CV = "cv"
    USER_STATED = "user_stated"


class RemotePreference(StrEnum):
    ANY = "any"
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class Evidence(BaseModel):
    """Where a fact came from. For CV evidence, char offsets index into the profile's raw_text."""

    source_type: SourceType = SourceType.CV
    section: str | None = None
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote: str


class Fact(BaseModel):
    id: int | None = None
    key: str
    parent_key: str | None = None
    type: FactType
    label: str | None = None
    value: str
    normalized_value: str
    verification_status: VerificationStatus = VerificationStatus.EXTRACTED
    evidence: list[Evidence] = Field(default_factory=list)


class Gap(BaseModel):
    kind: str
    message: str
    fact_key: str | None = None


class Preferences(BaseModel):
    target_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_preference: RemotePreference = RemotePreference.ANY
    salary_min: int | None = Field(default=None, ge=0)
    salary_currency: str | None = None


class CandidateProfile(BaseModel):
    version_id: int | None = None
    cv_sha256: str
    cv_path: str | None = None
    raw_text: str
    facts: list[Fact] = Field(default_factory=list)

    def contact(self, label: str) -> str | None:
        for fact in self.facts:
            if fact.type is FactType.CONTACT and fact.label == label:
                return fact.value
        return None


class ApplicationStatus(StrEnum):
    READY = "ready"
    APPROVED = "approved"
    APPLIED = "applied"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    WITHDRAWN = "withdrawn"


class ApplicationEvent(BaseModel):
    status: ApplicationStatus
    happened_at: str


class Application(BaseModel):
    id: int
    job_id: int
    title: str
    company: str
    apply_url: str
    folder: str
    status: ApplicationStatus
    approved_at: str | None = None
    applied_at: str | None = None
    follow_up_on: str | None = None
    job_is_active: bool
    history: list[ApplicationEvent] = Field(default_factory=list)
