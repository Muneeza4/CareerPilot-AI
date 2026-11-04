"""Adapters that fetch job postings from public APIs and career pages."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models.schemas import JobPosting

logger = logging.getLogger(__name__)

USER_AGENT = "CareerPilot/0.1 (personal job search tool)"
TIMEOUT_SECONDS = 20
BLOCK_TAGS = ["p", "div", "section", "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]
JOB_LINK_HINTS = ("/job", "/jobs", "/career", "/careers", "/position", "/opening", "/vacanc")


class JobSource(Protocol):
    name: str

    def fetch(self, session: requests.Session) -> list[JobPosting]: ...


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    # Greenhouse escapes its HTML, so unescape before parsing.
    soup = BeautifulSoup(html.unescape(value), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all("li"):
        for paragraph in tag.find_all("p"):
            paragraph.unwrap()
        tag.insert_before("• ")
        tag.insert_after("\n")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n\n")
        tag.insert_after("\n\n")
    return clean_text(soup.get_text())


def clean_text(value: str) -> str:
    """Tidy spacing line by line and keep at most one blank line between paragraphs."""
    text = value.replace("​", "").replace("﻿", "")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def to_date(value) -> str | None:
    """Convert ISO strings, epoch seconds, or epoch milliseconds to YYYY-MM-DD."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def looks_remote(location: str | None) -> bool:
    return "remote" in (location or "").lower()


def _get_json(session: requests.Session, url: str):
    response = session.get(url, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _company_name(token: str) -> str:
    return token.replace("-", " ").title()


class GreenhouseSource:
    def __init__(self, token: str):
        self.token = token
        self.name = f"greenhouse:{token}"

    def fetch(self, session: requests.Session) -> list[JobPosting]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true"
        postings = []
        for item in _get_json(session, url)["jobs"]:
            location = (item.get("location") or {}).get("name")
            postings.append(
                JobPosting(
                    external_id=str(item["id"]),
                    title=item["title"],
                    company=item.get("company_name") or _company_name(self.token),
                    location=location,
                    description=html_to_text(item.get("content")),
                    posted_at=to_date(item.get("first_published") or item.get("updated_at")),
                    apply_url=item["absolute_url"],
                    is_remote=looks_remote(location),
                )
            )
        return postings


class LeverSource:
    def __init__(self, company: str):
        self.company = company
        self.name = f"lever:{company}"

    def fetch(self, session: requests.Session) -> list[JobPosting]:
        url = f"https://api.lever.co/v0/postings/{self.company}?mode=json"
        postings = []
        for item in _get_json(session, url):
            categories = item.get("categories") or {}
            location = categories.get("location")
            postings.append(
                JobPosting(
                    external_id=item["id"],
                    title=item["text"],
                    company=_company_name(self.company),
                    location=location,
                    description=item.get("descriptionPlain") or html_to_text(item.get("description")),
                    posted_at=to_date(item.get("createdAt")),
                    apply_url=item["hostedUrl"],
                    is_remote=item.get("workplaceType") == "remote" or looks_remote(location),
                )
            )
        return postings


class AshbySource:
    def __init__(self, board: str):
        self.board = board
        self.name = f"ashby:{board}"

    def fetch(self, session: requests.Session) -> list[JobPosting]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.board}"
        postings = []
        for item in _get_json(session, url)["jobs"]:
            if item.get("isListed") is False:
                continue
            location = item.get("location")
            postings.append(
                JobPosting(
                    external_id=item.get("id") or item["jobUrl"],
                    title=item["title"],
                    company=_company_name(self.board),
                    location=location,
                    description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")),
                    posted_at=to_date(item.get("publishedAt")),
                    apply_url=item["jobUrl"],
                    is_remote=bool(item.get("isRemote")) or looks_remote(location),
                )
            )
        return postings


class RemotiveSource:
    name = "remotive"

    def fetch(self, session: requests.Session) -> list[JobPosting]:
        return [
            JobPosting(
                external_id=str(item["id"]),
                title=item["title"],
                company=item["company_name"],
                location=item.get("candidate_required_location"),
                description=html_to_text(item.get("description")),
                posted_at=to_date(item.get("publication_date")),
                apply_url=item["url"],
                is_remote=True,
            )
            for item in _get_json(session, "https://remotive.com/api/remote-jobs")["jobs"]
        ]


class ArbeitnowSource:
    name = "arbeitnow"

    def fetch(self, session: requests.Session) -> list[JobPosting]:
        return [
            JobPosting(
                external_id=item["slug"],
                title=item["title"],
                company=item["company_name"],
                location=item.get("location"),
                description=html_to_text(item.get("description")),
                posted_at=to_date(item.get("created_at")),
                apply_url=item["url"],
                is_remote=bool(item.get("remote")),
            )
            for item in _get_json(session, "https://www.arbeitnow.com/api/job-board-api")["data"]
        ]


class RobotsPolicy:
    """Caches robots.txt per host. A site whose robots.txt cannot be read is treated as disallowed."""

    def __init__(self, session: requests.Session):
        self.session = session
        self.parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self.parsers:
            self.parsers[host] = self._load(host)
        parser = self.parsers[host]
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def _load(self, host: str) -> RobotFileParser | None:
        try:
            response = self.session.get(f"{host}/robots.txt", timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            return None
        parser = RobotFileParser()
        if response.status_code == 404:
            parser.parse([])
        elif response.status_code == 200:
            parser.parse(response.text.splitlines())
        else:
            return None
        return parser


def _walk_json_ld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_json_ld(item)
    elif isinstance(node, dict):
        yield node
        yield from _walk_json_ld(node.get("@graph", []))


def _location_text(value) -> str | None:
    places = value if isinstance(value, list) else [value]
    names = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            parts = (address.get(key) for key in ("addressLocality", "addressRegion", "addressCountry"))
            text = ", ".join(part for part in parts if isinstance(part, str) and part)
            if text:
                names.append(text)
        elif isinstance(address, str):
            names.append(address)
    return "; ".join(names) or None


def parse_job_postings(page_html: str, page_url: str) -> list[JobPosting]:
    """Read schema.org JobPosting data, which sites publish for job search engines."""
    soup = BeautifulSoup(page_html, "html.parser")
    postings = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for node in _walk_json_ld(data):
            kind = node.get("@type")
            if "JobPosting" not in (kind if isinstance(kind, list) else [kind]) or not node.get("title"):
                continue
            organization = node.get("hiringOrganization")
            company = organization.get("name") if isinstance(organization, dict) else organization
            url = urljoin(page_url, node.get("url") or page_url)
            location = _location_text(node.get("jobLocation"))
            identifier = node.get("identifier")
            if isinstance(identifier, dict):
                identifier = identifier.get("value")
            postings.append(
                JobPosting(
                    external_id=str(identifier or url),
                    title=node["title"],
                    company=company or urlsplit(page_url).netloc,
                    location=location,
                    description=html_to_text(node.get("description")),
                    posted_at=to_date(node.get("datePosted")),
                    apply_url=url,
                    is_remote=node.get("jobLocationType") == "TELECOMMUTE" or looks_remote(location),
                )
            )
    return postings


def find_job_links(page_html: str, page_url: str) -> list[str]:
    host = urlsplit(page_url).netloc
    links: list[str] = []
    for anchor in BeautifulSoup(page_html, "html.parser").find_all("a", href=True):
        url = urljoin(page_url, anchor["href"]).split("#")[0]
        parts = urlsplit(url)
        if parts.netloc == host and url != page_url and url not in links:
            if any(hint in parts.path.lower() for hint in JOB_LINK_HINTS):
                links.append(url)
    return links


class CareerPageSource:
    """Scrape a company careers page, then its job links, staying on the site and within robots.txt."""

    def __init__(self, url: str, max_pages: int = 30, delay_seconds: float = 1.0):
        self.url = url
        self.name = f"careers:{urlsplit(url).netloc}"
        self.max_pages = max_pages
        self.delay_seconds = delay_seconds

    def fetch(self, session: requests.Session) -> list[JobPosting]:
        robots = RobotsPolicy(session)
        if not robots.allowed(self.url):
            raise PermissionError(f"robots.txt does not allow {self.url}")

        response = session.get(self.url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        found = {p.external_id: p for p in parse_job_postings(response.text, self.url)}

        for link in find_job_links(response.text, self.url)[: self.max_pages]:
            if not robots.allowed(link):
                continue
            time.sleep(self.delay_seconds)
            try:
                page = session.get(link, timeout=TIMEOUT_SECONDS)
                page.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("Skipping %s: %s", link, exc)
                continue
            for posting in parse_job_postings(page.text, link):
                found[posting.external_id] = posting
        return list(found.values())


def build_sources(config: dict) -> list[JobSource]:
    settings = config.get("sources") or {}
    sources: list[JobSource] = []
    sources += [GreenhouseSource(token) for token in settings.get("greenhouse") or []]
    sources += [LeverSource(company) for company in settings.get("lever") or []]
    sources += [AshbySource(board) for board in settings.get("ashby") or []]
    if settings.get("remotive"):
        sources.append(RemotiveSource())
    if settings.get("arbeitnow"):
        sources.append(ArbeitnowSource())
    sources += [CareerPageSource(url) for url in settings.get("career_pages") or []]
    return sources
