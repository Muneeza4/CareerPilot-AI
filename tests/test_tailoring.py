import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import server
from database.repository import Repository
from models.schemas import CandidateProfile, Fact, FactType, Job, JobPosting, VerificationStatus
from services.job_ingest import database_path, ingest
from services.tailoring import RELEVANT_GROUP, find_unsupported, render_pdf, tailor_cv
from tests.conftest import single_column_pdf


def fact(fact_type, value, key=None, parent=None, label=None, status=VerificationStatus.CONFIRMED):
    return Fact(
        key=key or f"{fact_type.value}-{value}",
        parent_key=parent,
        type=fact_type,
        label=label,
        value=value,
        normalized_value=value.lower(),
        verification_status=status,
    )


def profile(*extra):
    facts = [
        fact(FactType.CONTACT, "Test Person", label="name"),
        fact(FactType.CONTACT, "person@example.com", label="email"),
        fact(FactType.SUMMARY, "Data science graduate."),
        fact(FactType.SKILL, "Excel", label="Tools"),
        fact(FactType.SKILL, "Python", label="Languages"),
        fact(FactType.SKILL, "SQL", label="Languages"),
        fact(FactType.PROJECT, "Sales Dashboard", key="p1"),
        fact(FactType.ACHIEVEMENT, "Tools Used: Excel", parent="p1"),
        fact(FactType.PROJECT, "Revenue Model", key="p2"),
        fact(FactType.ACHIEVEMENT, "Predicted revenue from a PostgreSQL database with Python.", parent="p2"),
        fact(FactType.EDUCATION, "Sep 2021 - June2025 BS Data Science, Example University"),
        fact(FactType.CERTIFICATION, "Data Mining: Issuer: Example"),
        fact(FactType.CERTIFICATION, "Python: Issuer: Example"),
    ]
    return CandidateProfile(cv_sha256="x", raw_text="", facts=[*facts, *extra])


def job(description="Requirements\n• Python\n• PostgreSQL\n• Kubernetes"):
    return Job(
        id=7,
        source="test",
        external_id="7",
        title="Data Analyst",
        company="Acme",
        description=description,
        apply_url="https://example.com/7",
        first_seen_at="2026-01-01",
        is_active=True,
    )


def test_relevant_project_moves_first_and_says_why():
    cv = tailor_cv(job(), profile())
    assert [entry.title for entry in cv.projects] == ["Revenue Model", "Sales Dashboard"]
    assert cv.projects[0].shows == ["PostgreSQL", "Python"]
    assert "Moved 'Revenue Model' to the top of Projects because it shows PostgreSQL, Python." in cv.changes


def test_requested_skills_lead_and_all_others_stay():
    cv = tailor_cv(job(), profile())
    assert cv.skills[0].name == RELEVANT_GROUP
    assert cv.skills[0].skills[0] == "Python"
    all_skills = [skill for group in cv.skills for skill in group.skills]
    assert {"Excel", "SQL"} <= set(all_skills)


def test_skill_shown_in_confirmed_work_is_added_and_reported():
    cv = tailor_cv(job(), profile())
    assert "PostgreSQL" in cv.skills[0].skills
    assert "Added PostgreSQL to Skills, because your confirmed work shows them." in cv.changes


def test_missing_skill_is_reported_and_never_added():
    cv = tailor_cv(job(), profile())
    assert cv.missing_required == ["Kubernetes"]
    assert not any("Kubernetes" in skill for group in cv.skills for skill in group.skills)


@pytest.mark.parametrize("status", [VerificationStatus.EXTRACTED, VerificationStatus.REJECTED])
def test_unconfirmed_facts_never_appear(status):
    extra = fact(FactType.CERTIFICATION, "Unreviewed Kubernetes Course", status=status)
    assert "Unreviewed Kubernetes Course" not in tailor_cv(job(), profile(extra)).certifications


def test_every_line_comes_from_a_confirmed_fact():
    assert tailor_cv(job(), profile()).unsupported == []


def test_invented_text_is_caught():
    source = profile()
    cv = tailor_cv(job(), source)
    cv.projects[0].details.append("Led a team of 50 engineers.")
    cv.skills[0].skills.append("Kubernetes")
    assert find_unsupported(cv, source) == ["Kubernetes", "Led a team of 50 engineers."]


def test_dates_are_shown_apart_from_the_title():
    entry = tailor_cv(job(), profile()).education[0]
    assert entry.title == "BS Data Science, Example University"
    assert entry.date == "Sep 2021 - June2025"


def test_date_inside_a_title_is_not_reported_as_unsupported():
    extra = fact(FactType.EDUCATION, "Intermediate in Statistics 2019 - 2021 Example College. Grade: A+")
    cv = tailor_cv(job(), profile(extra))
    assert cv.education[1].title == "Intermediate in Statistics Example College. Grade: A+"
    assert cv.unsupported == []


def test_job_with_no_readable_skills_keeps_the_original_order():
    cv = tailor_cv(job("We build things."), profile())
    assert [entry.title for entry in cv.projects] == ["Sales Dashboard", "Revenue Model"]
    assert cv.changes == ["The order already suits this job, so nothing was moved."]


def test_pdf_contains_the_tailored_text():
    data = render_pdf(tailor_cv(job(), profile()))
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = doc[0].get_text()
    assert "Test Person" in text and "Revenue Model" in text


def test_long_cv_continues_on_a_new_page():
    extra = [fact(FactType.CERTIFICATION, f"Certificate number {index}") for index in range(90)]
    with pymupdf.open(stream=render_pdf(tailor_cv(job(), profile(*extra))), filetype="pdf") as doc:
        assert doc.page_count > 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "load_config", lambda: {})
    return TestClient(server.app)


def test_tailor_api(client):
    with Repository(database_path()) as repo:
        posting = JobPosting(
            external_id="1",
            title="Python Developer",
            company="Acme",
            description="Requirements\n• Python\n• Docker",
            apply_url="https://example.com/1",
        )
        ingest(repo, repo.ensure_source("a"), [posting])
        job_id = repo.search_jobs(limit=1)[0].id

    assert client.get(f"/api/tailor/{job_id}").status_code == 409
    uploaded = client.post("/api/profile/cv", files={"file": ("cv.pdf", single_column_pdf())}).json()
    ids = [item["id"] for item in uploaded["facts"]]
    client.put("/api/profile/facts/status", json={"ids": ids, "status": "confirmed"})

    assert client.get("/api/tailor/999999").status_code == 404
    data = client.get(f"/api/tailor/{job_id}").json()
    assert data["name"] == "Ayesha Khan"
    assert data["unsupported"] == []

    pdf = client.get(f"/api/tailor/{job_id}/cv.pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert client.get("/tailor").status_code == 200
