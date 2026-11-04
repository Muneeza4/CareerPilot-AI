from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import server
from database.repository import Repository
from models.schemas import (
    CandidateProfile,
    Fact,
    FactType,
    Job,
    JobPosting,
    Preferences,
    RemotePreference,
    VerificationStatus,
)
from services.job_ingest import database_path, ingest
from services.matching import analyze_job, build_candidate, rank_jobs, score_job, vocabulary
from tests.conftest import single_column_pdf

DESCRIPTION = """About us
We use Kubernetes across the company.

Requirements
• Strong Python and SQL
• 2+ years of experience

Nice to have
• Docker
• Experience with AWS"""


def fact(fact_type, value, status=VerificationStatus.CONFIRMED, key=None):
    return Fact(
        key=key or f"{fact_type.value}-{value}",
        type=fact_type,
        value=value,
        normalized_value=value.lower(),
        verification_status=status,
    )


def profile(*facts):
    return CandidateProfile(cv_sha256="x", raw_text="", facts=list(facts))


def job(title="Data Engineer", description=DESCRIPTION, **overrides):
    values = {
        "id": 1,
        "source": "test",
        "external_id": "1",
        "company": "Acme",
        "location": "Berlin",
        "description": description,
        "apply_url": "https://example.com/1",
        "first_seen_at": "2026-01-01",
        "is_active": True,
    }
    return Job(title=title, **{**values, **overrides})


def skills(analysis, importance):
    return {item.skill for item in analysis.requirements if item.importance == importance}


def test_vocabulary_avoids_false_skills():
    vocab = vocabulary()
    assert vocab.find("We react quickly and excel at rust removal") == set()
    assert vocab.find("Built in React and Excel") == {"React", "Excel"}
    assert vocab.find("JavaScript, not Java") == {"JavaScript", "Java"}
    assert vocab.find("C++ and C# with MySQL") == {"C++", "C#", "MySQL"}
    assert vocab.find("MySQL only") == {"MySQL"}


def test_required_and_preferred_are_separated():
    analysis = analyze_job("Data Engineer", DESCRIPTION)
    assert skills(analysis, "required") == {"Python", "SQL"}
    assert skills(analysis, "preferred") == {"Docker", "AWS", "Kubernetes"}
    assert analysis.min_years == 2


def test_inline_plus_is_preferred():
    analysis = analyze_job("Engineer", "Requirements\n• Python\n• Knowledge of Docker is a plus")
    assert skills(analysis, "preferred") == {"Docker"}


def test_title_skill_is_required():
    assert "Python" in skills(analyze_job("Python Developer", "We build things."), "required")


def test_job_without_headings_treats_named_skills_as_required():
    assert skills(analyze_job("Engineer", "You will write Python and SQL."), "required") == {"Python", "SQL"}


def test_senior_title_without_stated_years_assumes_experience():
    assert analyze_job("Senior Engineer", "Python").min_years == 5


def test_only_confirmed_facts_count():
    candidate = build_candidate(
        profile(
            fact(FactType.SKILL, "Python"),
            fact(FactType.SKILL, "SQL", VerificationStatus.EXTRACTED),
            fact(FactType.SKILL, "Docker", VerificationStatus.REJECTED),
        )
    )
    assert set(candidate.skills) == {"Python"}


def test_work_evidence_is_stronger_than_a_listed_skill():
    candidate = build_candidate(
        profile(fact(FactType.SKILL, "Python"), fact(FactType.SKILL, "SQL"), fact(FactType.ACHIEVEMENT, "Built reports in SQL"))
    )
    assert candidate.skills["Python"].strength == 0.6
    assert candidate.skills["SQL"].strength == 1.0
    assert candidate.skills["SQL"].evidence == "Built reports in SQL"


def test_project_skill_counts_without_being_in_the_skills_list():
    assert "PyTorch" in build_candidate(profile(fact(FactType.PROJECT, "Vision model in PyTorch"))).skills


@pytest.mark.parametrize(
    "value, years",
    [
        ("Intern, Acme Jun 2023 - Aug 2023", 0.25),
        ("Analyst, Beta 2023 - Present", 3.92),
        ("Engineer, Gamma Jan 2024 – Dec 2024", 1.0),
        ("Teaching Assistant", 0),
    ],
)
def test_experience_years(value, years):
    candidate = build_candidate(profile(fact(FactType.EXPERIENCE, value)), today=date(2026, 11, 1))
    assert candidate.years == pytest.approx(years, abs=0.01)


def test_score_is_calculated_and_explained():
    candidate = build_candidate(
        profile(
            fact(FactType.ACHIEVEMENT, "Used Python and SQL"),
            fact(FactType.EXPERIENCE, "Analyst 2022 - 2024"),
        )
    )
    result = score_job(job(), candidate, Preferences())
    # Two readable required skills cap the score at 70, even though both are held.
    assert result.score == 70
    assert result.components == {"required": 100, "preferred": 0, "experience": 100, "constraints": 70}
    assert {item.skill for item in result.matched} == {"Python", "SQL"}
    assert set(result.missing_preferred) == {"AWS", "Docker", "Kubernetes"}
    assert result.reasons[0] == "You have 2 of 2 required skills."


def test_thin_job_cannot_score_highly_and_says_why():
    candidate = build_candidate(profile(fact(FactType.ACHIEVEMENT, "Built a Python service")))
    result = score_job(job(title="Full Stack Engineer", description="We use Python."), candidate, Preferences())
    assert result.score == 60
    assert "Only 1 required skills could be read from this job, so this score is less certain." in result.reasons


def test_detailed_job_with_full_coverage_scores_highly():
    stack = "Python, SQL, Docker, AWS, Kubernetes"
    candidate = build_candidate(
        profile(fact(FactType.ACHIEVEMENT, f"Built with {stack}"), fact(FactType.EXPERIENCE, "Engineer 2020 - 2024"))
    )
    description = f"Requirements\n{stack}\n3+ years"
    result = score_job(job(title="Engineer", description=description), candidate, Preferences(target_roles=["engineer"]))
    assert result.score == 100


def test_unknown_experience_and_role_do_not_earn_full_marks():
    candidate = build_candidate(profile(fact(FactType.ACHIEVEMENT, "Used Python and SQL, Docker, AWS, Kubernetes")))
    description = "Requirements\nPython, SQL, Docker, AWS, Kubernetes"
    assert score_job(job(title="Engineer", description=description), candidate, Preferences()).score < 100


def test_large_experience_gap_limits_the_score():
    stack = "Python, SQL, Docker, AWS, Kubernetes"
    candidate = build_candidate(profile(fact(FactType.ACHIEVEMENT, f"Built with {stack}")))
    description = f"Requirements\n{stack}\n8+ years"
    assert score_job(job(title="Engineer", description=description), candidate, Preferences()).score == 65


def test_company_naming_its_own_product_is_not_a_gap():
    candidate = build_candidate(profile(fact(FactType.ACHIEVEMENT, "Used Python")))
    posting = job(company="Databricks", title="Engineer", description="Requirements\nPython\nDatabricks")
    assert score_job(posting, candidate, Preferences()).missing_required == []


def test_same_input_gives_same_score():
    candidate = build_candidate(profile(fact(FactType.SKILL, "Python")))
    assert score_job(job(), candidate, Preferences()) == score_job(job(), candidate, Preferences())


def test_weak_required_coverage_is_capped():
    candidate = build_candidate(profile(fact(FactType.SKILL, "AWS"), fact(FactType.SKILL, "Docker")))
    description = "Requirements\n• Python\n• SQL\n• Java\nNice to have\n• AWS\n• Docker"
    assert score_job(job(description=description), candidate, Preferences()).score <= 59


def test_missing_required_skill_is_reported_not_hidden():
    candidate = build_candidate(profile(fact(FactType.ACHIEVEMENT, "Used Python")))
    assert score_job(job(), candidate, Preferences()).missing_required == ["SQL"]


def test_job_without_recognised_skills_is_not_scored():
    candidate = build_candidate(profile(fact(FactType.SKILL, "Python")))
    assert score_job(job(title="Sales Lead", description="Sell things."), candidate, Preferences()) is None


def test_remote_and_location_preferences_exclude_jobs():
    candidate = build_candidate(profile(fact(FactType.SKILL, "Python")))
    remote_only = Preferences(remote_preference=RemotePreference.REMOTE)
    assert score_job(job(), candidate, remote_only) is None
    assert score_job(job(is_remote=True), candidate, remote_only) is not None
    assert score_job(job(), candidate, Preferences(locations=["Karachi"])) is None
    assert score_job(job(), candidate, Preferences(locations=["berlin"])) is not None


def test_target_role_affects_fit():
    candidate = build_candidate(profile(fact(FactType.SKILL, "Python")))
    target = score_job(job(), candidate, Preferences(target_roles=["data engineer"]))
    other = score_job(job(), candidate, Preferences(target_roles=["designer"]))
    assert target.score > other.score
    assert "The title is not one of your target roles." in other.reasons


def test_job_outside_target_roles_cannot_rank_as_a_strong_match():
    stack = "Python, SQL, Docker, AWS, Kubernetes"
    candidate = build_candidate(
        profile(fact(FactType.ACHIEVEMENT, f"Built with {stack}"), fact(FactType.EXPERIENCE, "Engineer 2020 - 2024"))
    )
    posting = job(title="Product Manager", description=f"Requirements\n{stack}\n3+ years")
    assert score_job(posting, candidate, Preferences(target_roles=["data scientist"])).score == 59


def test_rank_orders_best_first():
    jobs = [job(id=1, title="Java Developer", description="Java"), job(id=2, title="Python Developer", description="Python")]
    ranked = rank_jobs(jobs, profile(fact(FactType.PROJECT, "Python app")), Preferences())
    assert [result.job_id for result in ranked] == [2, 1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "load_config", lambda: {})
    return TestClient(server.app)


def test_matches_api_needs_confirmed_facts_then_ranks(client):
    with Repository(database_path()) as repo:
        posting = JobPosting(
            external_id="1",
            title="Python Developer",
            company="Acme",
            description="Requirements\n• Python",
            apply_url="https://example.com/1",
        )
        ingest(repo, repo.ensure_source("a"), [posting])

    assert client.get("/api/matches").json()["ready"] is False
    profile_data = client.post("/api/profile/cv", files={"file": ("cv.pdf", single_column_pdf())}).json()
    assert client.get("/api/matches").json()["ready"] is False

    python = next(item for item in profile_data["facts"] if item["value"] == "Python")
    client.patch(f"/api/profile/facts/{python['id']}", json={"status": "confirmed"})

    data = client.get("/api/matches").json()
    assert data["ready"] is True and data["total"] == 1
    assert data["matches"][0]["matched"][0]["strength"] == "Listed in your CV only"
    assert client.get("/matches").status_code == 200
