"""Web server for CareerPilot. Run with: python -m app.server"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from database.repository import Repository
from models.schemas import Application, ApplicationStatus, FactType, Gap, Preferences, VerificationStatus
from services.application_package import MAX_LETTER_CHARS, PackageCheck, check_package, draft_letter, save_package
from services.applications import (
    PACKAGE_FILES,
    AttentionItem,
    StatusError,
    attention_items,
    can_replace_package,
    move,
    timestamp,
)
from services.job_ingest import database_path, load_config
from services.matching import rank_jobs
from services.resume_parser import CvParseError, find_gaps, parse_cv, render_evidence, sha256_bytes
from services.scheduler import DEFAULT_INTERVAL_HOURS, RefreshManager, RefreshStatus, build_scheduler
from services.tailoring import TailoredCv, render_pdf, tailor_cv

STATIC_DIR = Path(__file__).with_name("static")
PAGE_SIZE = 24
SNIPPET_CHARS = 180
MAX_KEYWORDS = 50
MAX_KEYWORD_CHARS = 60
MAX_CV_BYTES = 10 * 1024 * 1024
MAX_FACT_CHARS = 1000
MAX_MATCH_JOBS = 20000

manager = RefreshManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    interval = load_config().get("interval_hours", DEFAULT_INTERVAL_HOURS)
    with Repository(database_path()) as repo:
        last_run = repo.last_successful_run()
    scheduler = build_scheduler(manager, interval, last_run)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="CareerPilot", lifespan=lifespan)


def get_repo() -> Iterator[Repository]:
    with Repository(database_path()) as repo:
        yield repo


Repo = Annotated[Repository, Depends(get_repo)]


class JobSummary(BaseModel):
    id: int
    title: str
    company: str
    location: str | None
    is_remote: bool
    posted_at: str | None
    source: str
    apply_url: str
    snippet: str


class JobPage(BaseModel):
    jobs: list[JobSummary]
    total: int
    page: int
    pages: int


class KeywordList(BaseModel):
    keywords: list[str] = Field(max_length=MAX_KEYWORDS)


def saved_keywords(repo: Repository) -> list[str]:
    return repo.get_keywords(load_config().get("keywords") or [])


@app.get("/api/jobs")
def list_jobs(
    repo: Repo,
    query: str = "",
    company: str = "",
    location: str = "",
    source: str = "",
    remote_only: bool = False,
    hide_senior: bool = False,
    search_description: bool = False,
    days: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> JobPage:
    filters = {
        "query": query,
        "company": company,
        "location": location,
        "source": source,
        "remote_only": remote_only,
        "hide_senior": hide_senior,
        "search_description": search_description,
        "days": days,
        "keywords": saved_keywords(repo),
    }
    total = repo.count_jobs(**filters)
    jobs = repo.search_jobs(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **filters)
    return JobPage(
        jobs=[
            JobSummary(
                **job.model_dump(include=set(JobSummary.model_fields) - {"snippet"}),
                snippet=" ".join(job.description[:SNIPPET_CHARS].split()),
            )
            for job in jobs
        ],
        total=total,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, repo: Repo):
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/filters")
def filter_options(repo: Repo) -> dict:
    return {
        "companies": repo.companies(),
        "sources": repo.source_names(),
        "last_refresh": repo.last_successful_run(),
    }


@app.get("/api/keywords")
def get_keywords(repo: Repo) -> KeywordList:
    return KeywordList(keywords=saved_keywords(repo))


@app.put("/api/keywords")
def set_keywords(body: KeywordList, repo: Repo) -> KeywordList:
    cleaned: dict[str, str] = {}
    for keyword in body.keywords:
        text = " ".join(keyword.split())[:MAX_KEYWORD_CHARS]
        if text:
            cleaned.setdefault(text.lower(), text)
    repo.set_keywords(list(cleaned.values()))
    return KeywordList(keywords=list(cleaned.values()))


class FactView(BaseModel):
    id: int
    type: FactType
    label: str | None
    value: str
    status: VerificationStatus
    parent_key: str | None
    key: str
    source: str
    page: int | None


class ProfileView(BaseModel):
    facts: list[FactView]
    gaps: list[Gap]
    has_cv: bool


class FactUpdate(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=MAX_FACT_CHARS)
    status: VerificationStatus | None = None


class FactStatusUpdate(BaseModel):
    ids: list[int]
    status: VerificationStatus


class NewFact(BaseModel):
    type: FactType
    value: str = Field(min_length=1, max_length=MAX_FACT_CHARS)


def profile_view(repo: Repository) -> ProfileView:
    profile = repo.profiles.load_profile()
    if profile is None:
        return ProfileView(facts=[], gaps=[], has_cv=False)
    return ProfileView(
        facts=[
            FactView(
                id=fact.id,
                type=fact.type,
                label=fact.label,
                value=fact.value,
                status=fact.verification_status,
                parent_key=fact.parent_key,
                key=fact.key,
                source=fact.evidence[0].source_type.value if fact.evidence else "cv",
                page=fact.evidence[0].page if fact.evidence else None,
            )
            for fact in profile.facts
        ],
        gaps=find_gaps(profile),
        has_cv=True,
    )


@app.get("/api/profile")
def get_profile(repo: Repo) -> ProfileView:
    return profile_view(repo)


@app.post("/api/profile/cv")
def upload_cv(file: UploadFile, repo: Repo) -> ProfileView:
    data = file.file.read(MAX_CV_BYTES + 1)
    if len(data) > MAX_CV_BYTES:
        raise HTTPException(status_code=413, detail="Please upload a PDF smaller than 10 MB.")

    cv_dir = database_path().parent / "cvs"
    cv_path = cv_dir / f"{sha256_bytes(data)}.pdf"
    try:
        result = parse_cv(data, str(cv_path))
    except CvParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cv_dir.mkdir(exist_ok=True)
    cv_path.write_bytes(data)
    repo.profiles.save_profile(result.profile)
    return profile_view(repo)


@app.patch("/api/profile/facts/{fact_id}")
def update_fact(fact_id: int, body: FactUpdate, repo: Repo) -> ProfileView:
    if repo.profiles.get_fact(fact_id) is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    value = body.value.strip() if body.value else None
    repo.profiles.update_fact(fact_id, value=value, status=body.status)
    return profile_view(repo)


@app.put("/api/profile/facts/status")
def update_fact_statuses(body: FactStatusUpdate, repo: Repo) -> ProfileView:
    for fact_id in body.ids:
        repo.profiles.update_fact(fact_id, status=body.status)
    return profile_view(repo)


@app.post("/api/profile/facts", status_code=201)
def add_fact(body: NewFact, repo: Repo) -> ProfileView:
    profile = repo.profiles.load_profile()
    if profile is None:
        raise HTTPException(status_code=409, detail="Upload a CV first.")
    repo.profiles.add_user_fact(profile.version_id, body.type, body.value.strip())
    return profile_view(repo)


@app.get("/api/profile/facts/{fact_id}/source")
def fact_source(fact_id: int, repo: Repo) -> Response:
    fact = repo.profiles.get_fact(fact_id)
    profile = repo.profiles.load_profile()
    if fact is None or profile is None or not fact.evidence or fact.evidence[0].page is None:
        raise HTTPException(status_code=404, detail="No CV source for this fact")
    evidence = fact.evidence[0]
    png = render_evidence(profile.cv_path, evidence.page, evidence.quote)
    return Response(content=png, media_type="image/png")


@app.get("/api/preferences")
def get_preferences(repo: Repo) -> Preferences:
    return repo.profiles.load_preferences()


@app.put("/api/preferences")
def set_preferences(body: Preferences, repo: Repo) -> Preferences:
    repo.profiles.save_preferences(body)
    return body


@app.get("/api/matches")
def list_matches(repo: Repo, page: Annotated[int, Query(ge=1)] = 1) -> dict:
    profile = repo.profiles.load_profile()
    confirmed = [fact for fact in profile.facts if fact.verification_status is VerificationStatus.CONFIRMED] if profile else []
    if not confirmed:
        return {"ready": False, "matches": [], "total": 0, "page": 1, "pages": 1}

    jobs = repo.search_jobs(limit=MAX_MATCH_JOBS, keywords=saved_keywords(repo))
    ranked = rank_jobs(jobs, profile, repo.profiles.load_preferences())
    start = (page - 1) * PAGE_SIZE
    return {
        "ready": True,
        "matches": [match.model_dump() for match in ranked[start : start + PAGE_SIZE]],
        "total": len(ranked),
        "page": page,
        "pages": max(1, -(-len(ranked) // PAGE_SIZE)),
    }


def tailored_cv(job_id: int, repo: Repository) -> TailoredCv:
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    profile = repo.profiles.load_profile()
    if profile is None or not any(fact.verification_status is VerificationStatus.CONFIRMED for fact in profile.facts):
        raise HTTPException(status_code=409, detail="Confirm your CV facts first.")
    return tailor_cv(job, profile)


@app.get("/api/tailor/{job_id}")
def get_tailored_cv(job_id: int, repo: Repo) -> TailoredCv:
    return tailored_cv(job_id, repo)


@app.get("/api/tailor/{job_id}/cv.pdf")
def download_tailored_cv(job_id: int, repo: Repo) -> Response:
    cv = tailored_cv(job_id, repo)
    if cv.unsupported:
        raise HTTPException(status_code=422, detail="This CV has text that is not in your confirmed facts.")
    headers = {"Content-Disposition": f'attachment; filename="tailored-cv-{job_id}.pdf"'}
    return Response(content=render_pdf(cv), media_type="application/pdf", headers=headers)


class PackageDraft(BaseModel):
    letter: str
    checks: list[PackageCheck]


class PackageRequest(BaseModel):
    letter: str = Field(min_length=1, max_length=MAX_LETTER_CHARS)


class SavedPackage(BaseModel):
    application_id: int
    folder: str
    files: list[str]
    checks: list[PackageCheck]


@app.get("/api/package/{job_id}")
def get_package(job_id: int, repo: Repo) -> PackageDraft:
    cv = tailored_cv(job_id, repo)
    profile = repo.profiles.load_profile()
    letter = draft_letter(cv, profile)
    return PackageDraft(letter=letter, checks=check_package(cv, letter, profile))


@app.post("/api/package/{job_id}", status_code=201)
def create_package(job_id: int, body: PackageRequest, repo: Repo) -> SavedPackage:
    cv = tailored_cv(job_id, repo)
    if not can_replace_package(repo.applications.for_job(job_id)):
        raise HTTPException(status_code=409, detail="You already applied with this package, so it is kept as sent.")
    profile = repo.profiles.load_profile()
    checks = check_package(cv, body.letter, profile)
    blockers = [check.detail for check in checks if check.blocks]
    if blockers:
        raise HTTPException(status_code=422, detail=" ".join(blockers))
    root = database_path().parent / "applications"
    job = repo.get_job(job_id)
    folder, files = save_package(root, job, cv, body.letter)
    application_id = repo.applications.save_package(job, str(folder), timestamp())
    return SavedPackage(application_id=application_id, folder=str(folder), files=files, checks=checks)


class ApplicationBoard(BaseModel):
    applications: list[Application]
    attention: list[AttentionItem]


class ApplicationUpdate(BaseModel):
    status: ApplicationStatus | None = None
    follow_up_on: date | None = None


@app.get("/api/applications")
def list_applications(repo: Repo) -> ApplicationBoard:
    applications = repo.applications.list()
    return ApplicationBoard(applications=applications, attention=attention_items(applications))


@app.patch("/api/applications/{application_id}")
def update_application(application_id: int, body: ApplicationUpdate, repo: Repo) -> Application:
    application = repo.applications.get(application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if body.status is not None:
        try:
            move(repo.applications, application, body.status)
        except StatusError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    elif "follow_up_on" in body.model_fields_set:
        follow_up = body.follow_up_on.isoformat() if body.follow_up_on else None
        repo.applications.set_follow_up(application_id, follow_up)
    return repo.applications.get(application_id)


@app.get("/api/applications/{application_id}/files/{name}")
def application_file(application_id: int, name: str, repo: Repo) -> FileResponse:
    application = repo.applications.get(application_id)
    path = Path(application.folder) / name if application and name in PACKAGE_FILES else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@app.post("/api/refresh", status_code=202)
def start_refresh() -> RefreshStatus:
    if not manager.start():
        raise HTTPException(status_code=409, detail="A refresh is already running")
    return manager.status


@app.get("/api/refresh")
def refresh_status() -> RefreshStatus:
    return manager.status


def add_page(path: str, filename: str) -> None:
    app.add_api_route(path, lambda: FileResponse(STATIC_DIR / filename), include_in_schema=False)


add_page("/", "home.html")
add_page("/jobs", "jobs.html")
add_page("/matches", "matches.html")
add_page("/tailor", "tailor.html")
add_page("/cv", "cv.html")
add_page("/applications", "applications.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Only this computer can reach it, because the app has no login.
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
