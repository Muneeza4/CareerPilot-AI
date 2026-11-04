import pytest

from database.repository import Repository
from models.schemas import JobPosting
from services.job_ingest import canonical_url, company_key, ingest, run_all

DESCRIPTION = "Build and ship machine learning systems for our customers. " * 20


def posting(external_id="1", **overrides):
    values = {
        "external_id": external_id,
        "title": "ML Engineer",
        "company": "Acme Inc",
        "location": "Lahore",
        "description": DESCRIPTION,
        "apply_url": f"https://acme.com/jobs/{external_id}",
    }
    return JobPosting(**{**values, **overrides})


class FakeSource:
    def __init__(self, name, postings=None, error=None):
        self.name = name
        self.postings = postings or []
        self.error = error

    def fetch(self, session):
        if self.error:
            raise self.error
        return self.postings


@pytest.fixture
def repo(tmp_path):
    with Repository(tmp_path / "test.db") as repository:
        yield repository


def test_saved_jobs_are_cleaned(repo):
    dirty = posting(title="  ML&nbsp;Engineer &amp; Analyst\n", company=" Acme ", location=" ", description="  a   b \n\n\n\n c ")
    ingest(repo, repo.ensure_source("a"), [dirty])
    job = repo.search_jobs()[0]
    assert (job.title, job.company, job.location, job.description) == ("ML Engineer & Analyst", "Acme", None, "a b\n\nc")


def test_canonical_url_removes_tracking_only():
    url = "https://ACME.com/jobs/1/?utm_source=x&gh_jid=55&ref=abc#apply"
    assert canonical_url(url) == "https://acme.com/jobs/1?gh_jid=55"


def test_company_key_ignores_legal_suffix():
    assert company_key("Acme, Inc.") == company_key("ACME") == "acme"


def test_new_job_is_saved_once_and_updated_on_next_run(repo):
    source_id = repo.ensure_source("a")
    assert ingest(repo, source_id, [posting()]) == 1
    assert ingest(repo, source_id, [posting(title="Senior ML Engineer")]) == 0
    jobs = repo.search_jobs()
    assert [job.title for job in jobs] == ["Senior ML Engineer"]


def test_same_url_from_another_source_is_a_duplicate(repo):
    ingest(repo, repo.ensure_source("a"), [posting("1")])
    new = ingest(
        repo,
        repo.ensure_source("b"),
        [posting("x", title="Other title", apply_url="https://acme.com/jobs/1?utm_medium=feed")],
    )
    assert new == 0
    assert len(repo.search_jobs()) == 1


def test_similar_job_from_another_source_is_a_duplicate(repo):
    ingest(repo, repo.ensure_source("a"), [posting("1")])
    other = posting("2", company="ACME", title="ML Engineer", apply_url="https://board.com/99")
    assert ingest(repo, repo.ensure_source("b"), [other]) == 0
    assert len(repo.search_jobs()) == 1


def test_same_role_in_another_city_is_not_a_duplicate(repo):
    source_id = repo.ensure_source("a")
    assert ingest(repo, source_id, [posting("1"), posting("2", location="Berlin")]) == 2


def test_same_title_with_different_description_is_not_a_duplicate(repo):
    source_id = repo.ensure_source("a")
    other = posting("2", description="Design payment fraud rules and investigate chargebacks. " * 20)
    assert ingest(repo, source_id, [posting("1"), other]) == 2


def test_one_failing_source_does_not_stop_the_others(repo):
    results = run_all(
        repo,
        {},
        [FakeSource("bad", error=RuntimeError("down")), FakeSource("good", [posting()])],
    )
    assert [(r.source, r.status) for r in results] == [("bad", "failed"), ("good", "success")]
    assert len(repo.search_jobs()) == 1
    runs = {run.source: run for run in repo.recent_runs()}
    assert runs["bad"].error == "down"
    assert runs["good"].new_jobs == 1


def test_job_removed_from_source_is_closed_and_can_reopen(repo):
    first, second = posting("1"), posting("2", location="Berlin")
    run_all(repo, {}, [FakeSource("a", [first, second])])

    result = run_all(repo, {}, [FakeSource("a", [first])])[0]
    assert result.closed == 1
    assert [job.external_id for job in repo.search_jobs()] == ["1"]
    assert len(repo.search_jobs(active_only=False)) == 2

    run_all(repo, {}, [FakeSource("a", [first, second])])
    assert len(repo.search_jobs()) == 2


def test_failed_or_empty_run_does_not_close_jobs(repo):
    run_all(repo, {}, [FakeSource("a", [posting()])])
    run_all(repo, {}, [FakeSource("a", error=RuntimeError("down"))])
    run_all(repo, {}, [FakeSource("a", [])])
    assert len(repo.search_jobs()) == 1


def test_search_filters(repo):
    source_id = repo.ensure_source("a")
    ingest(
        repo,
        source_id,
        [
            posting("1", title="C++ Engineer", description="Low latency systems", posted_at="2020-01-01"),
            posting("2", title="Data Analyst", company="Beta", location="Remote", is_remote=True,
                    description="SQL dashboards", posted_at="2099-01-01"),
        ],
    )
    assert [j.external_id for j in repo.search_jobs(query="dashboards")] == ["2"]
    assert [j.external_id for j in repo.search_jobs(query="C++")] == ["1"]
    assert [j.external_id for j in repo.search_jobs(company="beta")] == ["2"]
    assert [j.external_id for j in repo.search_jobs(location="lahore")] == ["1"]
    assert [j.external_id for j in repo.search_jobs(remote_only=True)] == ["2"]
    assert [j.external_id for j in repo.search_jobs(days=7)] == ["2"]
