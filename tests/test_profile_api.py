import pytest
from fastapi.testclient import TestClient

from app import server
from database.repository import Repository
from models.schemas import VerificationStatus
from services.job_ingest import database_path
from tests.conftest import single_column_pdf, two_column_pdf


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERPILOT_DATA_DIR", str(tmp_path))
    return TestClient(server.app)


def upload(client, data=None):
    files = {"file": ("cv.pdf", data if data is not None else single_column_pdf(), "application/pdf")}
    return client.post("/api/profile/cv", files=files)


def fact(profile, value):
    return next(item for item in profile["facts"] if item["value"] == value)


def test_no_profile_before_upload(client):
    assert client.get("/api/profile").json() == {"facts": [], "gaps": [], "has_cv": False}


def test_upload_creates_a_profile_that_survives_a_new_connection(client):
    assert upload(client).status_code == 200
    profile = client.get("/api/profile").json()
    assert profile["has_cv"] is True
    assert fact(profile, "Python")["status"] == "extracted"
    assert any(gap["kind"] == "unsupported_skill" for gap in profile["gaps"])

    with Repository(database_path()) as repo:
        assert repo.profiles.load_profile().contact("name") == "Ayesha Khan"
    assert (database_path().parent / "cvs").exists()


def test_bad_files_get_a_clear_message(client, scanned_cv):
    response = upload(client, b"not a pdf")
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]
    assert "scanned" in upload(client, scanned_cv).json()["detail"]
    assert client.get("/api/profile").json()["has_cv"] is False


def test_oversized_file_is_refused(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_CV_BYTES", 10)
    assert upload(client).status_code == 413


def test_confirm_edit_and_reject(client):
    profile = upload(client).json()
    python, docker = fact(profile, "Python"), fact(profile, "Docker")

    client.patch(f"/api/profile/facts/{python['id']}", json={"status": "confirmed"})
    profile = client.patch(f"/api/profile/facts/{docker['id']}", json={"value": "Docker Compose"}).json()
    assert fact(profile, "Python")["status"] == "confirmed"
    assert fact(profile, "Docker Compose")["id"] == docker["id"]

    profile = client.patch(f"/api/profile/facts/{docker['id']}", json={"status": "rejected"}).json()
    assert not any("Docker Compose" in gap["message"] for gap in profile["gaps"])


def test_confirm_many_at_once(client):
    profile = upload(client).json()
    ids = [item["id"] for item in profile["facts"] if item["type"] == "skill"]
    profile = client.put("/api/profile/facts/status", json={"ids": ids, "status": "confirmed"}).json()
    assert {item["status"] for item in profile["facts"] if item["type"] == "skill"} == {"confirmed"}


def test_unknown_fact_returns_404(client):
    upload(client)
    assert client.patch("/api/profile/facts/999999", json={"status": "confirmed"}).status_code == 404


def test_user_stated_fact_is_confirmed_and_needs_no_cv_source(client):
    upload(client)
    profile = client.post("/api/profile/facts", json={"type": "skill", "value": "Go"}).json()
    added = fact(profile, "Go")
    assert (added["status"], added["source"], added["page"]) == ("confirmed", "user_stated", None)
    assert not any("'Go'" in gap["message"] for gap in profile["gaps"])
    assert client.get(f"/api/profile/facts/{added['id']}/source").status_code == 404


def test_adding_a_fact_needs_a_cv(client):
    assert client.post("/api/profile/facts", json={"type": "skill", "value": "Go"}).status_code == 409


def test_source_returns_highlighted_page(client):
    profile = upload(client).json()
    response = client.get(f"/api/profile/facts/{fact(profile, 'Python')['id']}/source")
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_reuploading_a_cv_keeps_edits_and_makes_it_active(client):
    cv = single_column_pdf()
    first = upload(client, cv).json()
    client.patch(f"/api/profile/facts/{fact(first, 'Python')['id']}", json={"status": "confirmed"})

    assert fact(upload(client, two_column_pdf()).json(), "TensorFlow")
    restored = upload(client, cv).json()
    assert fact(restored, "Python")["status"] == VerificationStatus.CONFIRMED.value


def test_preferences_round_trip(client):
    assert client.get("/api/preferences").json()["remote_preference"] == "any"
    body = {
        "target_roles": ["AI Engineer"],
        "locations": ["Karachi"],
        "remote_preference": "remote",
        "salary_min": 100000,
        "salary_currency": "PKR",
    }
    assert client.put("/api/preferences", json=body).status_code == 200
    assert client.get("/api/preferences").json() == body


def test_negative_salary_is_refused(client):
    assert client.put("/api/preferences", json={"salary_min": -1}).status_code == 422


def test_reupload_refreshes_facts_and_keeps_reviews(client, monkeypatch):
    data = single_column_pdf()
    profile = upload(client, data).json()
    python, docker = fact(profile, "Python"), fact(profile, "Docker")
    client.patch(f"/api/profile/facts/{python['id']}", json={"status": "confirmed"})
    client.patch(f"/api/profile/facts/{docker['id']}", json={"value": "Docker Compose", "status": "rejected"})
    client.post("/api/profile/facts", json={"type": "skill", "value": "Excel"})

    original_parse = server.parse_cv

    def parse_with_changes(data, path):
        result = original_parse(data, path)
        result.profile.facts = [item for item in result.profile.facts if item.value != "Git"]
        return result

    monkeypatch.setattr(server, "parse_cv", parse_with_changes)
    refreshed = upload(client, data).json()

    assert fact(refreshed, "Python")["status"] == "confirmed"
    assert fact(refreshed, "Docker Compose")["status"] == "rejected"
    assert fact(refreshed, "Excel")["source"] == "user_stated"
    assert not any(item["value"] == "Git" for item in refreshed["facts"])
