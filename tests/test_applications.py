from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import server
from database.repository import Repository
from models.schemas import Application, ApplicationStatus as Status
from models.schemas import JobPosting
from services.applications import attention_items
from services.job_ingest import database_path, ingest
from tests.conftest import single_column_pdf


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "load_config", lambda: {})
    return TestClient(server.app)


@pytest.fixture
def job_id(client):
    with Repository(database_path()) as repo:
        posting = JobPosting(
            external_id="1",
            title="Python Developer",
            company="Acme",
            description="Requirements\n• Python",
            apply_url="https://example.com/1",
        )
        ingest(repo, repo.ensure_source("a"), [posting])
        job = repo.search_jobs(limit=1)[0].id
    uploaded = client.post("/api/profile/cv", files={"file": ("cv.pdf", single_column_pdf())}).json()
    client.put("/api/profile/facts/status", json={"ids": [f["id"] for f in uploaded["facts"]], "status": "confirmed"})
    return job


def save(client, job, letter="Dear Acme, regards Ayesha Khan"):
    return client.post(f"/api/package/{job}", json={"letter": letter})


def only(client):
    return client.get("/api/applications").json()["applications"][0]


def status(client, application, value):
    return client.patch(f"/api/applications/{application['id']}", json={"status": value})


def test_saving_a_package_creates_one_application_for_review(client, job_id):
    assert client.get("/api/applications").json() == {"applications": [], "attention": []}
    save(client, job_id)
    save(client, job_id)
    data = client.get("/api/applications").json()
    assert len(data["applications"]) == 1
    assert data["applications"][0]["status"] == "ready"
    assert data["attention"][0]["title"] == "Review your package"


def test_cannot_be_marked_applied_without_approval(client, job_id):
    save(client, job_id)
    response = status(client, only(client), "applied")
    assert response.status_code == 409
    assert only(client)["status"] == "ready"


def test_full_journey_is_recorded(client, job_id):
    save(client, job_id)
    for value in ("approved", "applied", "interview", "offer"):
        assert status(client, only(client), value).status_code == 200
    application = only(client)
    assert application["status"] == "offer"
    assert application["applied_at"] and application["approved_at"]
    assert [event["status"] for event in application["history"]] == ["ready", "approved", "applied", "interview", "offer"]


def test_applying_sets_a_follow_up_a_week_later(client, job_id):
    save(client, job_id)
    status(client, only(client), "approved")
    status(client, only(client), "applied")
    application = only(client)
    assert (date.fromisoformat(application["follow_up_on"]) - date.today()).days == 7
    client.patch(f"/api/applications/{application['id']}", json={"follow_up_on": "2030-01-05"})
    assert only(client)["follow_up_on"] == "2030-01-05"


def test_finished_application_cannot_move(client, job_id):
    save(client, job_id)
    status(client, only(client), "withdrawn")
    assert status(client, only(client), "approved").status_code == 409


def test_saving_again_clears_approval(client, job_id):
    save(client, job_id)
    status(client, only(client), "approved")
    save(client, job_id, "Dear Acme, a new letter from Ayesha Khan")
    application = only(client)
    assert application["status"] == "ready"
    assert application["approved_at"] is None


def test_sent_package_cannot_be_replaced(client, job_id):
    save(client, job_id, "Original letter")
    status(client, only(client), "approved")
    status(client, only(client), "applied")
    assert save(client, job_id, "Changed letter").status_code == 409
    letter = client.get(f"/api/applications/{only(client)['id']}/files/cover-letter.txt")
    assert letter.text == "Original letter"


def test_only_package_files_can_be_opened(client, job_id):
    save(client, job_id)
    application = only(client)["id"]
    assert client.get(f"/api/applications/{application}/files/cv.pdf").content.startswith(b"%PDF")
    assert client.get(f"/api/applications/{application}/files/secret.txt").status_code == 404
    assert client.get(f"/api/applications/{application}/files/..%2F..%2Fcareerpilot.db").status_code == 404
    assert client.get("/api/applications/999/files/cv.pdf").status_code == 404


def test_unknown_application_returns_404(client):
    assert client.patch("/api/applications/999", json={"status": "approved"}).status_code == 404


def application(**overrides):
    values = {
        "id": 1,
        "job_id": 1,
        "title": "Analyst",
        "company": "Acme",
        "apply_url": "https://example.com",
        "folder": "x",
        "status": Status.APPLIED,
        "job_is_active": True,
    }
    return Application(**{**values, **overrides})


def test_follow_up_appears_only_once_due():
    today = date(2026, 11, 10)
    assert attention_items([application(follow_up_on="2026-11-11")], today) == []
    assert attention_items([application(follow_up_on="2026-11-10")], today)[0].title == "Follow up"
    assert attention_items([application(status=Status.REJECTED, follow_up_on="2026-11-01")], today) == []


def test_closed_job_warns_before_applying():
    titles = [item.title for item in attention_items([application(status=Status.READY, job_is_active=False)])]
    assert titles == ["Review your package", "This job may have closed"]
