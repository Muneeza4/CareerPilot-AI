import time

import pytest
from fastapi.testclient import TestClient

from app import server
from database.repository import Repository
from models.schemas import JobPosting
from services import scheduler as scheduler_module
from services.job_ingest import database_path, ingest
from services.scheduler import RefreshManager, build_scheduler, first_run_time


def posting(external_id, title, **overrides):
    values = {
        "external_id": external_id,
        "title": title,
        "company": f"Company {external_id}",
        "location": "Lahore",
        "description": f"Description for {external_id}",
        "apply_url": f"https://example.com/{external_id}",
    }
    return JobPosting(**{**values, **overrides})


class FakeSource:
    name = "fake"

    def fetch(self, session):
        return [posting("fetched", "AI Engineer")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "load_config", lambda: {"keywords": ["default keyword"]})
    monkeypatch.setattr(scheduler_module, "load_config", lambda: {})
    monkeypatch.setattr(scheduler_module, "build_sources", lambda config: [FakeSource()])
    monkeypatch.setattr(server, "manager", RefreshManager())
    return TestClient(server.app)


def seed(*postings):
    with Repository(database_path()) as repo:
        ingest(repo, repo.ensure_source("greenhouse:acme"), list(postings))


def titles(client, **params):
    return [job["title"] for job in client.get("/api/jobs", params=params).json()["jobs"]]


def set_keywords(client, *keywords):
    return client.put("/api/keywords", json={"keywords": list(keywords)})


def test_page_and_assets_are_served(client):
    pages = {"/": "How it works", "/jobs": "Fetch new jobs", "/cv": "CV Studio", "/applications": "Applications"}
    for path, text in pages.items():
        assert text in client.get(path).text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_default_keywords_come_from_config_until_user_saves_their_own(client):
    assert client.get("/api/keywords").json() == {"keywords": ["default keyword"]}
    set_keywords(client, "python")
    assert client.get("/api/keywords").json() == {"keywords": ["python"]}


def test_user_can_remove_every_keyword_to_see_all_jobs(client):
    seed(posting("1", "Accountant"))
    set_keywords(client)
    assert client.get("/api/keywords").json() == {"keywords": []}
    assert titles(client) == ["Accountant"]


def test_keywords_are_cleaned_and_deduplicated(client):
    response = set_keywords(client, "  Data   Analyst ", "data analyst", "", "ML")
    assert response.json() == {"keywords": ["Data Analyst", "ML"]}


def test_changing_keywords_filters_saved_jobs_without_a_fetch(client):
    seed(posting("1", "Data Analyst"), posting("2", "Accountant"))
    set_keywords(client, "data analyst")
    assert titles(client) == ["Data Analyst"]
    set_keywords(client, "accountant")
    assert titles(client) == ["Accountant"]


def test_keywords_match_whole_words_only(client):
    seed(posting("1", "AI Engineer"), posting("2", "Retail Assistant"), posting("3", "C++ Developer"))
    set_keywords(client, "AI")
    assert titles(client) == ["AI Engineer"]
    set_keywords(client, "c++")
    assert titles(client) == ["C++ Developer"]


def test_description_matching_is_optional(client):
    seed(posting("1", "Engineer", description="Uses Kubernetes daily"))
    set_keywords(client, "kubernetes")
    assert titles(client) == []
    assert titles(client, search_description=True) == ["Engineer"]


def test_hide_senior_roles(client):
    seed(posting("1", "Senior Data Analyst"), posting("2", "Data Analyst"), posting("3", "Sr. Data Analyst"))
    set_keywords(client)
    assert titles(client, hide_senior=True) == ["Data Analyst"]


def test_source_filter(client):
    seed(posting("1", "Analyst"))
    set_keywords(client)
    assert titles(client, source="other") == []
    assert titles(client, source="greenhouse:acme") == ["Analyst"]


def test_pagination(client):
    seed(*[posting(str(i), f"Job {i}") for i in range(30)])
    set_keywords(client)
    first = client.get("/api/jobs").json()
    second = client.get("/api/jobs", params={"page": 2}).json()
    assert (first["total"], first["pages"], len(first["jobs"]), len(second["jobs"])) == (30, 2, 24, 6)
    assert not {j["id"] for j in first["jobs"]} & {j["id"] for j in second["jobs"]}


def test_job_detail_and_missing_job(client):
    seed(posting("1", "Analyst"))
    job_id = client.get("/api/jobs", params={}).json()
    set_keywords(client)
    job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
    assert client.get(f"/api/jobs/{job_id}").json()["description"] == "Description for 1"
    assert client.get("/api/jobs/999999").status_code == 404


def test_fetch_button_saves_new_jobs_that_then_appear(client):
    set_keywords(client, "AI Engineer")
    assert client.post("/api/refresh").status_code == 202
    deadline = time.time() + 5
    while client.get("/api/refresh").json()["running"] and time.time() < deadline:
        time.sleep(0.02)

    status = client.get("/api/refresh").json()
    assert (status["running"], status["done"], status["total"], status["new_jobs"]) == (False, 1, 1, 1)
    assert titles(client) == ["AI Engineer"]
    assert client.get("/api/filters").json()["last_refresh"]


def test_second_refresh_is_refused_while_one_runs(client):
    assert server.manager._lock.acquire(blocking=False)
    try:
        assert client.post("/api/refresh").status_code == 409
        assert server.manager.run() is False
    finally:
        server.manager._lock.release()


def test_failed_refresh_releases_the_lock(client, monkeypatch):
    def boom(config):
        raise RuntimeError("bad config")

    monkeypatch.setattr(scheduler_module, "build_sources", boom)
    assert server.manager.run() is True
    assert server.manager.status.error == "bad config"
    assert server.manager.status.running is False
    assert server.manager._lock.acquire(blocking=False)
    server.manager._lock.release()


def test_scheduler_settings():
    job = build_scheduler(RefreshManager(), 6, None).get_jobs()[0]
    assert job.trigger.interval.total_seconds() == 6 * 3600
    assert (job.max_instances, job.coalesce) == (1, True)


def test_restart_does_not_fetch_again_while_data_is_fresh():
    assert first_run_time("2999-01-01 00:00:00", 6).year == 2999
    assert first_run_time("2000-01-01 00:00:00", 6).year > 2000
