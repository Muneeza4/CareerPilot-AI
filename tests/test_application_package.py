import pytest
from fastapi.testclient import TestClient

from app import server
from database.repository import Repository
from models.schemas import FactType, JobPosting, VerificationStatus
from services.application_package import check_package, draft_letter, save_package
from services.job_ingest import database_path, ingest
from services.tailoring import tailor_cv
from tests.conftest import single_column_pdf
from tests.test_tailoring import fact, job, profile


def failed(checks):
    return [check.detail for check in checks if not check.ok]


def test_letter_names_the_job_and_shows_used_and_listed_skills_apart():
    source = profile()
    letter = draft_letter(tailor_cv(job("Requirements\n• Python\n• SQL\n• Kubernetes"), source), source)
    assert "Dear Acme hiring team," in letter
    assert "Data Analyst role at Acme" in letter
    assert "I have used Python in my projects." in letter
    assert "My skills also include SQL." in letter
    assert letter.endswith("Sincerely,\nTest Person")


def test_letter_never_mentions_a_missing_skill():
    source = profile()
    assert "Kubernetes" not in draft_letter(tailor_cv(job(), source), source)


def test_letter_uses_the_most_relevant_project_text():
    source = profile()
    letter = draft_letter(tailor_cv(job(), source), source)
    assert "A relevant project is Revenue Model. Predicted revenue from a PostgreSQL database with Python." in letter


def test_letter_without_experience_does_not_claim_work():
    source = profile()
    assert "work and projects" not in draft_letter(tailor_cv(job(), source), source)
    with_job = profile(fact(FactType.EXPERIENCE, "Analyst, Acme 2023 - 2024"))
    assert "work and projects" in draft_letter(tailor_cv(job(), with_job), with_job)


def test_drafted_letter_has_no_blocking_problem():
    source = profile()
    cv = tailor_cv(job(), source)
    assert not any(check.blocks for check in check_package(cv, draft_letter(cv, source), source))


def test_edited_letter_claiming_an_unproven_skill_is_blocked():
    source = profile()
    cv = tailor_cv(job(), source)
    checks = check_package(cv, draft_letter(cv, source) + "\n\nI run Kubernetes daily.", source)
    assert [check.detail for check in checks if check.blocks] == [
        "The letter names skills your confirmed facts do not show: Kubernetes."
    ]


def test_company_and_title_that_are_also_skills_do_not_block_the_letter():
    source = profile()
    posting = job().model_copy(update={"company": "Databricks", "title": "Kubernetes Engineer"})
    cv = tailor_cv(posting, source)
    assert not any(check.blocks for check in check_package(cv, draft_letter(cv, source), source))


def test_checklist_warns_about_gaps_without_blocking():
    source = profile(fact(FactType.SKILL, "Docker", status=VerificationStatus.EXTRACTED))
    cv = tailor_cv(job(), source)
    problems = failed(check_package(cv, draft_letter(cv, source), source))
    assert "Confirm your email and phone so they appear on the CV." in problems
    assert "1 CV facts still need review, so they are left out." in problems
    assert "This job asks for skills you do not show: Kubernetes." in problems


def test_package_folder_is_safe_and_saving_again_replaces_files(tmp_path):
    source = profile()
    posting = job().model_copy(update={"company": "../Acme/Inc"})
    cv = tailor_cv(posting, source)

    folder, files = save_package(tmp_path, posting, cv, "First")
    assert folder.parent == tmp_path
    assert folder.name == "7-acme-inc-data-analyst"
    assert sorted(path.name for path in folder.iterdir()) == sorted(files)

    save_package(tmp_path, posting, cv, "Second")
    assert (folder / "cover-letter.txt").read_text(encoding="utf-8") == "Second"
    assert "Kubernetes" in (folder / "job.txt").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "load_config", lambda: {})
    return TestClient(server.app)


def test_package_api(client):
    with Repository(database_path()) as repo:
        posting = JobPosting(
            external_id="1",
            title="Python Developer",
            company="Acme",
            description="Requirements\n• Python",
            apply_url="https://example.com/1",
        )
        ingest(repo, repo.ensure_source("a"), [posting])
        job_id = repo.search_jobs(limit=1)[0].id

    assert client.get(f"/api/package/{job_id}").status_code == 409
    uploaded = client.post("/api/profile/cv", files={"file": ("cv.pdf", single_column_pdf())}).json()
    ids = [item["id"] for item in uploaded["facts"]]
    client.put("/api/profile/facts/status", json={"ids": ids, "status": "confirmed"})

    draft = client.get(f"/api/package/{job_id}").json()
    assert "Ayesha Khan" in draft["letter"]

    refused = client.post(f"/api/package/{job_id}", json={"letter": "I am a Kubernetes expert."})
    assert refused.status_code == 422
    assert "Kubernetes" in refused.json()["detail"]

    saved = client.post(f"/api/package/{job_id}", json={"letter": draft["letter"]})
    assert saved.status_code == 201
    assert "cv.pdf" in saved.json()["files"]
    assert client.get("/api/package/999999").status_code == 404
