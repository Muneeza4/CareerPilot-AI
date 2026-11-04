"""Filter, deduplicate, and store postings from each source."""

from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import yaml
from rapidfuzz import fuzz

from database.repository import Repository
from models.schemas import JobPosting
from services.job_sources import JobSource, build_sources, clean_text, make_session

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"

TRACKING_PARAMS = {"ref", "source", "src", "gclid", "fbclid", "trk", "lever-source", "gh_src"}
COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|co|pvt|private)\b")
TITLE_THRESHOLD = 90
LOCATION_THRESHOLD = 85
DESCRIPTION_THRESHOLD = 85
DESCRIPTION_COMPARE_CHARS = 2000


@dataclass
class SourceResult:
    source: str
    status: str
    fetched: int = 0
    new_jobs: int = 0
    closed: int = 0
    error: str | None = None


def database_path() -> Path:
    data_dir = Path(os.environ.get("CAREERPILOT_DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "careerpilot.db"


def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def company_key(company: str) -> str:
    text = COMPANY_SUFFIXES.sub(" ", re.sub(r"[^a-z0-9 ]", " ", company.lower()))
    return " ".join(text.split())


def find_duplicate(repo: Repository, posting: JobPosting, url: str, key: str) -> int | None:
    """Match the same job across sources: by URL first, then by company, title, location, and description."""
    same_url = repo.find_by_canonical_url(url)
    if same_url:
        return same_url

    for row in repo.company_jobs(key):
        if fuzz.token_set_ratio(posting.title.lower(), row["title"].lower()) < TITLE_THRESHOLD:
            continue
        # The same role in two cities is two jobs.
        if fuzz.ratio((posting.location or "").lower(), (row["location"] or "").lower()) < LOCATION_THRESHOLD:
            continue
        ratio = fuzz.ratio(
            posting.description[:DESCRIPTION_COMPARE_CHARS].lower(),
            row["description"][:DESCRIPTION_COMPARE_CHARS].lower(),
        )
        if ratio >= DESCRIPTION_THRESHOLD:
            return row["id"]
    return None


def clean_posting(posting: JobPosting) -> JobPosting:
    def one_line(value: str) -> str:
        return " ".join(html.unescape(value).split())

    return posting.model_copy(
        update={
            "title": one_line(posting.title),
            "company": one_line(posting.company),
            "location": one_line(posting.location) or None if posting.location else None,
            "description": clean_text(posting.description),
        }
    )


def ingest(repo: Repository, source_id: int, postings: list[JobPosting]) -> int:
    """Save every posting. Keywords filter at search time, so changing them needs no new fetch."""
    new_jobs = 0
    for posting in map(clean_posting, postings):
        existing = repo.find_existing(source_id, posting.external_id)
        if existing:
            repo.update_job(existing, posting)
            continue
        url = canonical_url(posting.apply_url)
        key = company_key(posting.company)
        duplicate_of = find_duplicate(repo, posting, url, key)
        repo.insert_job(source_id, posting, url, key, duplicate_of)
        new_jobs += duplicate_of is None
    return new_jobs


def run_source(repo: Repository, source: JobSource, session: requests.Session) -> SourceResult:
    """Run one source. A failure is logged here so that it cannot stop the other sources."""
    source_id = repo.ensure_source(source.name)
    run_id = repo.start_run(source_id)
    try:
        postings = source.fetch(session)
        new_jobs = ingest(repo, source_id, postings)
        # An empty response is more likely a source problem than every job closing at once.
        closed = repo.close_missing(source_id, {p.external_id for p in postings}) if postings else 0
    except Exception as exc:
        logger.exception("Source %s failed", source.name)
        repo.finish_run(run_id, "failed", error=str(exc)[:500])
        return SourceResult(source.name, "failed", error=str(exc))

    repo.finish_run(run_id, "success", fetched=len(postings), new_jobs=new_jobs)
    return SourceResult(source.name, "success", len(postings), new_jobs, closed)


def run_all(
    repo: Repository,
    config: dict,
    sources: list[JobSource] | None = None,
    on_result: Callable[[SourceResult], None] | None = None,
) -> list[SourceResult]:
    session = make_session()
    results = []
    for source in sources or build_sources(config):
        result = run_source(repo, source, session)
        results.append(result)
        if on_result:
            on_result(result)
    return results
