import pytest

from models.schemas import Evidence, Fact, FactType, SourceType
from services.resume_parser import (
    InvalidPdfError,
    ScannedPdfError,
    extract_document,
    find_gaps,
    parse_cv,
    render_evidence,
    verify_facts,
)
from tests.conftest import paragraph_projects_pdf


def values(profile, fact_type):
    return [f.value for f in profile.facts if f.type is fact_type]


def fact_named(profile, fact_type, text):
    return next(f for f in profile.facts if f.type is fact_type and text in f.value)


def test_single_column_extracts_every_section(single_column_cv):
    result = parse_cv(single_column_cv)
    profile = result.profile

    assert result.rejected == []
    assert profile.contact("name") == "Ayesha Khan"
    assert profile.contact("email") == "ayesha.khan@example.com"
    assert profile.contact("phone") == "+92 300 1234567"
    assert profile.contact("link") == "github.com/ayeshak"
    assert values(profile, FactType.SUMMARY) == ["Computer science graduate focused on applied machine learning."]
    assert values(profile, FactType.SKILL) == ["Python", "SQL", "C++", "Git", "Docker", "Machine Learning"]
    assert values(profile, FactType.CERTIFICATION) == ["AWS Cloud Practitioner"]
    assert values(profile, FactType.EDUCATION) == ["BS Computer Science, FAST NUCES, Karachi, 2020 - 2024"]
    assert values(profile, FactType.PROJECT) == ["Resume Screener"]


def test_skill_categories_are_kept_as_labels(single_column_cv):
    profile = parse_cv(single_column_cv).profile
    assert fact_named(profile, FactType.SKILL, "Python").label == "Languages"
    assert fact_named(profile, FactType.SKILL, "Docker").label == "Tools"


def test_right_aligned_date_stays_with_its_title(single_column_cv):
    profile = parse_cv(single_column_cv).profile
    assert "Data Science Intern, Acme Labs Jun 2023 - Aug 2023" in values(profile, FactType.EXPERIENCE)


def test_bullets_link_to_their_role_and_wrapped_lines_merge(single_column_cv):
    profile = parse_cv(single_column_cv).profile
    role = fact_named(profile, FactType.EXPERIENCE, "Acme Labs")
    assistant = fact_named(profile, FactType.EXPERIENCE, "Teaching Assistant")

    assert [f.value for f in profile.facts if f.parent_key == role.key] == [
        "Built a churn model in Python with 87% accuracy for 2,000 customers.",
        "Automated weekly reports using SQL and Airflow pipelines across three teams in the analytics group.",
    ]
    assert [f.value for f in profile.facts if f.parent_key == assistant.key] == [
        "Mentored 40 students in data structures."
    ]


def test_two_column_text_is_not_interleaved(two_column_cv):
    document = extract_document(two_column_cv)
    assert "Kubernetes\nCommunication" in document.raw_text
    assert "Deployed TensorFlow models serving 5 million requests a day.\n" in document.raw_text


def test_two_column_extraction(two_column_cv):
    profile = parse_cv(two_column_cv).profile

    assert profile.contact("name") == "Bilal Ahmed"
    assert profile.contact("email") == "bilal@example.org"
    assert values(profile, FactType.SKILL) == ["Python", "TensorFlow", "Kubernetes", "Communication"]
    assert values(profile, FactType.EXPERIENCE) == ["ML Engineer, Visionary AI, 2023 - Present"]
    assert values(profile, FactType.PROJECT) == ["Crop Disease Detector"]


def test_sidebar_two_page_extraction(sidebar_cv):
    profile = parse_cv(sidebar_cv).profile

    assert values(profile, FactType.SKILL) == ["JavaScript", "React", "PostgreSQL", "Figma", "CI/CD"]
    assert values(profile, FactType.CERTIFICATION) == ["Google UX Certificate"]
    role = fact_named(profile, FactType.EXPERIENCE, "Bright Apps")
    assert "Mar 2022 - Dec 2024" in role.value
    assert [f.value for f in profile.facts if f.parent_key == role.key] == [
        "Rebuilt the checkout in React for 40,000 monthly users.",
        "Set up CI/CD with PostgreSQL migrations.",
    ]
    assert fact_named(profile, FactType.PROJECT, "Campus Maps").evidence[0].page == 2


@pytest.mark.parametrize("fixture", ["single_column_cv", "two_column_cv", "sidebar_cv"])
def test_every_quote_is_literally_in_the_cv(fixture, request):
    profile = parse_cv(request.getfixturevalue(fixture)).profile
    assert profile.facts
    for fact in profile.facts:
        assert fact.evidence
        for item in fact.evidence:
            assert profile.raw_text[item.char_start : item.char_end] == item.quote
            assert item.page >= 1


def test_invented_fact_is_rejected(single_column_cv):
    profile = parse_cv(single_column_cv).profile
    invented = Fact(
        key="skill-99",
        type=FactType.SKILL,
        value="Rust",
        normalized_value="rust",
        evidence=[Evidence(section="skills", page=1, char_start=0, char_end=4, quote="Rust")],
    )
    verified, rejected = verify_facts(profile.raw_text, [*profile.facts, invented])
    assert rejected == [invented]
    assert len(verified) == len(profile.facts)


def test_fact_without_evidence_is_rejected():
    fact = Fact(key="skill-1", type=FactType.SKILL, value="Go", normalized_value="go")
    assert verify_facts("anything", [fact]) == ([], [fact])


def test_only_invalid_evidence_is_dropped_from_a_fact():
    good = Evidence(char_start=0, char_end=2, quote="Go")
    bad = Evidence(char_start=0, char_end=2, quote="Rust")
    fact = Fact(key="skill-1", type=FactType.SKILL, value="Go", normalized_value="go", evidence=[good, bad])
    verified, rejected = verify_facts("Go lang", [fact])
    assert verified[0].evidence == [good]
    assert rejected == []


def test_user_stated_evidence_needs_no_cv_position():
    evidence = Evidence(source_type=SourceType.USER_STATED, quote="Available from March")
    fact = Fact(key="user-1", type=FactType.SUMMARY, value="x", normalized_value="x", evidence=[evidence])
    assert verify_facts("", [fact]) == ([fact], [])


def test_scanned_pdf_is_reported(scanned_cv):
    with pytest.raises(ScannedPdfError):
        parse_cv(scanned_cv)


def test_non_pdf_is_reported():
    with pytest.raises(InvalidPdfError):
        parse_cv(b"this is not a pdf")


def test_skill_used_in_work_gains_evidence_but_listed_only_is_flagged(single_column_cv):
    profile = parse_cv(single_column_cv).profile
    python = fact_named(profile, FactType.SKILL, "Python")
    assert {item.section for item in python.evidence} == {"skills", "experience"}

    flagged = {g.message.split("'")[1] for g in find_gaps(profile) if g.kind == "unsupported_skill"}
    assert flagged == {"C++", "Git", "Docker"}


def test_case_insensitive_multiword_skill_usage(single_column_cv):
    profile = parse_cv(single_column_cv).profile
    skill = fact_named(profile, FactType.SKILL, "Machine Learning")
    assert any(item.section == "projects" for item in skill.evidence)


def test_gaps_report_missing_dates(sidebar_cv, single_column_cv):
    assert any(g.kind == "missing_dates" and "LUMS" in g.message for g in find_gaps(parse_cv(sidebar_cv).profile))
    assert any(
        g.kind == "missing_dates" and "Teaching Assistant" in g.message
        for g in find_gaps(parse_cv(single_column_cv).profile)
    )


def test_render_evidence_returns_png(tmp_path, single_column_cv):
    path = tmp_path / "cv.pdf"
    path.write_bytes(single_column_cv)
    assert render_evidence(path, 1, "Ayesha Khan").startswith(b"\x89PNG")


def test_heading_sharing_a_row_with_a_date_still_starts_its_section():
    profile = parse_cv(paragraph_projects_pdf()).profile
    assert values(profile, FactType.SUMMARY) == []
    assert values(profile, FactType.EDUCATION) == ["Sep 2021 - June2025 BS Data Science Example University. CGPA: 3.9"]


def test_bold_titles_separate_projects_written_as_paragraphs():
    result = parse_cv(paragraph_projects_pdf())
    profile = result.profile
    assert result.rejected == []
    assert values(profile, FactType.PROJECT) == ["Sales Forecast", "Churn Dashboard"]

    forecast = fact_named(profile, FactType.PROJECT, "Sales Forecast")
    details = [f.value for f in profile.facts if f.parent_key == forecast.key]
    assert details == ["I forecast sales in Python using a long model that wraps onto a second line with a hyphen-ated word."]


def test_abbreviation_stays_with_its_skill():
    assert "Exploratory Data Analysis (EDA)" in values(parse_cv(paragraph_projects_pdf()).profile, FactType.SKILL)
