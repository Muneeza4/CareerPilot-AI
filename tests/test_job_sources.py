import json

import pytest
import requests

from services.job_sources import (
    ArbeitnowSource,
    AshbySource,
    CareerPageSource,
    GreenhouseSource,
    LeverSource,
    RemotiveSource,
    build_sources,
    find_job_links,
    html_to_text,
    parse_job_postings,
    to_date,
)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            return FakeResponse(404)
        return page if isinstance(page, FakeResponse) else FakeResponse(text=page)


def json_session(url, payload):
    return FakeSession({url: json.dumps(payload)})


def test_greenhouse():
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    payload = {
        "jobs": [
            {
                "id": 7,
                "title": "ML Engineer",
                "company_name": "Acme",
                "location": {"name": "Remote - US"},
                "content": "&lt;p&gt;Build &lt;b&gt;models&lt;/b&gt;&lt;/p&gt;",
                "first_published": "2026-10-30T10:00:00-04:00",
                "absolute_url": "https://acme.com/jobs/7",
            }
        ]
    }
    job = GreenhouseSource("acme").fetch(json_session(url, payload))[0]
    assert (job.external_id, job.company, job.posted_at) == ("7", "Acme", "2026-10-30")
    assert job.is_remote
    assert "models" in job.description and "<" not in job.description


def test_lever():
    url = "https://api.lever.co/v0/postings/acme-labs?mode=json"
    payload = [
        {
            "id": "abc",
            "text": "Data Analyst",
            "categories": {"location": "Karachi"},
            "descriptionPlain": "Analyse data",
            "createdAt": 1793000000000,
            "hostedUrl": "https://jobs.lever.co/acme-labs/abc",
            "applyUrl": "https://jobs.lever.co/acme-labs/abc/apply",
        }
    ]
    job = LeverSource("acme-labs").fetch(json_session(url, payload))[0]
    assert job.company == "Acme Labs"
    assert job.apply_url == "https://jobs.lever.co/acme-labs/abc"
    assert not job.is_remote


def test_ashby_skips_unlisted_jobs():
    url = "https://api.ashbyhq.com/posting-api/job-board/acme"
    payload = {
        "jobs": [
            {"id": "1", "title": "AI Engineer", "location": "London", "isRemote": True,
             "jobUrl": "https://jobs.ashbyhq.com/acme/1", "publishedAt": "2026-10-01T00:00:00Z"},
            {"id": "2", "title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/2"},
        ]
    }
    jobs = AshbySource("acme").fetch(json_session(url, payload))
    assert [job.title for job in jobs] == ["AI Engineer"]
    assert jobs[0].is_remote


def test_remotive():
    payload = {
        "jobs": [
            {"id": 1, "title": "Python Dev", "company_name": "Acme", "url": "https://remotive.com/1",
             "candidate_required_location": "Worldwide", "description": "<p>Code</p>",
             "publication_date": "2026-10-20T01:00:00"}
        ]
    }
    job = RemotiveSource().fetch(json_session("https://remotive.com/api/remote-jobs", payload))[0]
    assert job.is_remote and job.description == "Code" and job.posted_at == "2026-10-20"


def test_arbeitnow():
    payload = {
        "data": [
            {"slug": "python-dev-1", "title": "Python Dev", "company_name": "Acme", "location": "Berlin",
             "remote": False, "description": "<p>Code</p>", "url": "https://arbeitnow.com/1",
             "created_at": 1793000000}
        ]
    }
    job = ArbeitnowSource().fetch(json_session("https://www.arbeitnow.com/api/job-board-api", payload))[0]
    assert job.external_id == "python-dev-1" and not job.is_remote


def test_html_to_text_keeps_sentences_whole_and_lists_readable():
    value = "<h2>About</h2><p>Build <b>great</b>&nbsp;models</p><ul><li>Python</li><li><p>SQL</p></li></ul><style>p{}</style>"
    assert html_to_text(value) == "About\n\nBuild great models\n\n• Python\n• SQL"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-01T00:00:00Z", "2026-10-01"),
        (1793000000, "2026-10-26"),
        (1793000000000, "2026-10-26"),
        ("not a date", None),
        (None, None),
    ],
)
def test_to_date(value, expected):
    assert to_date(value) == expected


JOB_PAGE = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "JobPosting", "title": "ML Engineer",
 "identifier": {"value": "42"}, "hiringOrganization": {"name": "Acme"},
 "description": "<p>Train models</p>", "datePosted": "2026-10-15",
 "jobLocation": {"address": {"addressLocality": "Lahore", "addressCountry": "PK"}}}
</script><script type="application/ld+json">{broken</script></head></html>"""

LISTING = '<a href="/careers/ml-engineer">ML</a> <a href="/about">About</a> <a href="https://other.com/jobs/1">X</a>'


def test_parse_job_postings_reads_json_ld_and_ignores_broken_blocks():
    jobs = parse_job_postings(JOB_PAGE, "https://acme.com/careers/ml-engineer")
    assert len(jobs) == 1
    assert jobs[0].external_id == "42"
    assert jobs[0].location == "Lahore, PK"
    assert jobs[0].description == "Train models"


def test_parse_job_postings_reads_graph_blocks():
    page = """<script type="application/ld+json">
    {"@graph": [{"@type": "WebSite"}, {"@type": "JobPosting", "title": "Analyst", "url": "/jobs/9"}]}
    </script>"""
    jobs = parse_job_postings(page, "https://acme.com/careers")
    assert jobs[0].apply_url == "https://acme.com/jobs/9"


def test_find_job_links_stays_on_site():
    assert find_job_links(LISTING, "https://acme.com/careers") == ["https://acme.com/careers/ml-engineer"]


def test_career_page_follows_job_links():
    session = FakeSession(
        {
            "https://acme.com/robots.txt": "User-agent: *\nAllow: /",
            "https://acme.com/careers": LISTING,
            "https://acme.com/careers/ml-engineer": JOB_PAGE,
        }
    )
    jobs = CareerPageSource("https://acme.com/careers", delay_seconds=0).fetch(session)
    assert [job.title for job in jobs] == ["ML Engineer"]


def test_career_page_treats_missing_robots_as_allowed():
    session = FakeSession({"https://acme.com/careers": JOB_PAGE})
    assert len(CareerPageSource("https://acme.com/careers", delay_seconds=0).fetch(session)) == 1


def test_career_page_blocked_by_robots_is_never_requested():
    session = FakeSession(
        {"https://acme.com/robots.txt": "User-agent: *\nDisallow: /careers", "https://acme.com/careers": JOB_PAGE}
    )
    with pytest.raises(PermissionError):
        CareerPageSource("https://acme.com/careers", delay_seconds=0).fetch(session)
    assert session.requested == ["https://acme.com/robots.txt"]


def test_disallowed_job_link_is_skipped():
    session = FakeSession(
        {
            "https://acme.com/robots.txt": "User-agent: *\nDisallow: /careers/",
            "https://acme.com/careers": LISTING,
            "https://acme.com/careers/ml-engineer": JOB_PAGE,
        }
    )
    assert CareerPageSource("https://acme.com/careers", delay_seconds=0).fetch(session) == []
    assert "https://acme.com/careers/ml-engineer" not in session.requested


def test_robots_error_blocks_scraping():
    session = FakeSession(
        {"https://acme.com/robots.txt": FakeResponse(503), "https://acme.com/careers": JOB_PAGE}
    )
    with pytest.raises(PermissionError):
        CareerPageSource("https://acme.com/careers", delay_seconds=0).fetch(session)


def test_build_sources():
    config = {
        "sources": {
            "greenhouse": ["a"],
            "lever": ["b"],
            "ashby": ["c"],
            "remotive": True,
            "arbeitnow": False,
            "career_pages": ["https://acme.com/careers"],
        }
    }
    assert [s.name for s in build_sources(config)] == [
        "greenhouse:a",
        "lever:b",
        "ashby:c",
        "remotive",
        "careers:acme.com",
    ]
