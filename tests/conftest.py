"""Synthetic CVs in different layouts. Real CVs never go in the repository."""

import pymupdf
import pytest

WIDTH, HEIGHT = 595, 842


def _write(page, x, y, lines, size=10):
    for text in lines:
        if text:
            page.insert_text((x, y), text, fontsize=size)
        y += size + 4
    return y


def _finish(doc) -> bytes:
    data = doc.tobytes()
    doc.close()
    return data


def single_column_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    y = _write(page, 50, 50, ["Ayesha Khan"], size=18)
    y = _write(page, 50, y + 6, ["ayesha.khan@example.com | +92 300 1234567 | github.com/ayeshak"])
    lines = [
        "",
        "SUMMARY",
        "Computer science graduate focused on applied machine learning.",
        "",
        "EDUCATION",
        "BS Computer Science, FAST NUCES, Karachi, 2020 - 2024",
        "",
        "TECHNICAL SKILLS",
        "Languages: Python, SQL, C++",
        "Tools: Git, Docker, Machine Learning",
        "",
        "EXPERIENCE",
        "Data Science Intern, Acme Labs",
        "- Built a churn model in Python with 87% accuracy for 2,000 customers.",
        "- Automated weekly reports using SQL and Airflow pipelines across three",
        "teams in the analytics group.",
        "Teaching Assistant, FAST NUCES",
        "- Mentored 40 students in data structures.",
        "",
        "PROJECTS",
        "Resume Screener",
        "- Ranked resumes with machine learning and a Streamlit interface.",
        "",
        "CERTIFICATIONS",
        "AWS Cloud Practitioner",
    ]
    _write(page, 50, y, lines)
    # A right-aligned date on the same visual row as its job title.
    row_y = page.search_for("Data Science Intern")[0].y1 - 3
    page.insert_text((470, row_y), "Jun 2023 - Aug 2023", fontsize=9)
    return _finish(doc)


def two_column_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    _write(page, 40, 50, ["Bilal Ahmed"], size=18)
    _write(
        page,
        40,
        92,
        [
            "CONTACT",
            "bilal@example.org",
            "+1 415 555 0123",
            "",
            "SKILLS",
            "Python",
            "TensorFlow",
            "Kubernetes",
            "Communication",
            "",
            "EDUCATION",
            "MS Data Science",
            "NUST, 2021 - 2023",
        ],
    )
    _write(
        page,
        240,
        92,
        [
            "PROFESSIONAL SUMMARY",
            "Machine learning engineer shipping vision models.",
            "",
            "WORK EXPERIENCE",
            "ML Engineer, Visionary AI, 2023 - Present",
            "• Deployed TensorFlow models serving 5 million requests a day.",
            "• Reduced inference cost by 30% through quantization.",
            "",
            "PROJECTS",
            "Crop Disease Detector",
            "• Trained a Python image classifier on 12,000 labelled photos.",
        ],
    )
    return _finish(doc)


def sidebar_two_page_pdf() -> bytes:
    """Narrow left sidebar, glyph-only bullet lines, and a second page."""
    doc = pymupdf.open()
    first = doc.new_page(width=WIDTH, height=HEIGHT)
    _write(
        first,
        30,
        60,
        ["Technical Skills", "JavaScript | React", "PostgreSQL | Figma | CI/CD", "", "Courses", "Google UX Certificate"],
        size=8,
    )
    _write(
        first,
        210,
        60,
        [
            "Sara Malik",
            "sara@example.net",
            "+44 20 7946 0958",
            "",
            "Employment History",
            "Frontend Developer, Bright Apps",
            "Mar 2022 - Dec 2024",
            "•",
            "Rebuilt the checkout in React for 40,000 monthly users.",
            "•",
            "Set up CI/CD with PostgreSQL migrations.",
        ],
    )
    second = doc.new_page(width=WIDTH, height=HEIGHT)
    _write(
        second,
        50,
        60,
        [
            "Academic Projects",
            "Campus Maps",
            "• Designed a React navigation app for 3 campuses.",
            "",
            "Academic Background",
            "BSc Software Engineering, LUMS",
        ],
    )
    return _finish(doc)


def paragraph_projects_pdf() -> bytes:
    """Bold project titles above plain paragraphs, with a date on the same row as a heading."""
    doc = pymupdf.open()
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    y = _write(page, 50, 50, ["Hina Raza", "hina@example.com | +92 300 7654321", "", "EDUCATION"])
    page.insert_text((470, y - 14), "Sep 2021 - June2025", fontsize=10)
    y = _write(page, 50, y, ["BS Data Science", "Example University. CGPA: 3.9", "", "PROJECTS"])
    for title, body in (
        ("Sales Forecast", ["I forecast sales in Python using a long model that wraps onto a second line with a hyphen-", "ated word."]),
        ("Churn Dashboard", ["Tools Used: Power BI"]),
    ):
        page.insert_text((50, y), title, fontsize=10, fontname="hebo")
        y = _write(page, 50, y + 14, body)
    _write(page, 50, y, ["", "SKILLS", "Analysis: Python, Exploratory Data Analysis (EDA), Tensor flow"])
    return _finish(doc)


def scanned_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    page.draw_rect(pymupdf.Rect(50, 50, 500, 700), fill=(0.9, 0.9, 0.9))
    return _finish(doc)


@pytest.fixture
def single_column_cv():
    return single_column_pdf()


@pytest.fixture
def two_column_cv():
    return two_column_pdf()


@pytest.fixture
def sidebar_cv():
    return sidebar_two_page_pdf()


@pytest.fixture
def scanned_cv():
    return scanned_pdf()
