"""Tailor a CV to one job. Only confirmed facts are used, word for word, so nothing can be invented."""

from __future__ import annotations

import html
import io
import re

import pymupdf
from pydantic import BaseModel

from models.schemas import CandidateProfile, Fact, FactType, Job, VerificationStatus
from services.matching import build_candidate, job_requirements, vocabulary
from services.resume_parser import MONTH

REQUIRED_WEIGHT = 2
PREFERRED_WEIGHT = 1
RELEVANT_GROUP = "Most relevant to this job"
OTHER_GROUP = "Other skills"
PAGE_SIZE = "a4"
PAGE_MARGIN = 45
DATE_POINT = rf"(?:{MONTH}\s*)?(?:19|20)\d{{2}}"
DATE_SPAN_RE = re.compile(rf"\s*\b{DATE_POINT}(?:\s*[-–—]\s*(?:{DATE_POINT}|present|current))?\b", re.I)

PDF_CSS = """
* { font-family: sans-serif; font-size: 10pt; line-height: 1.3; }
h1 { font-size: 19pt; margin: 0; }
h2 { font-size: 11pt; margin: 11pt 0 3pt 0; border-bottom: 1px solid #555555; }
p { margin: 0 0 3pt 0; }
.contact { color: #444444; margin-bottom: 4pt; }
"""


class TailoredEntry(BaseModel):
    title: str
    date: str | None = None
    details: list[str]
    shows: list[str]


class SkillGroup(BaseModel):
    name: str
    skills: list[str]


class TailoredCv(BaseModel):
    job_id: int
    job_title: str
    company: str
    apply_url: str
    name: str | None
    contact: list[str]
    summary: str | None
    skills: list[SkillGroup]
    experience: list[TailoredEntry]
    projects: list[TailoredEntry]
    education: list[TailoredEntry]
    certifications: list[str]
    changes: list[str]
    requested_skills: list[str]
    missing_required: list[str]
    unsupported: list[str]


def _relevance(text: str, wanted: dict[str, int]) -> int:
    return sum(wanted.get(skill, 0) for skill in vocabulary().find(text))


def _ordered_by_relevance(items: list, key, wanted: dict[str, int]) -> list:
    # Sorting is stable, so equally relevant items keep the order they have in the CV.
    return sorted(items, key=lambda item: -_relevance(key(item), wanted))


def _split_date(text: str) -> tuple[str, str | None]:
    match = DATE_SPAN_RE.search(text)
    if not match:
        return text, None
    title = f"{text[: match.start()]} {text[match.end() :]}"
    return " ".join(title.split()), match.group().strip()


def _entries(titles: list[Fact], details: list[Fact], wanted: dict[str, int], reorder: bool) -> list[TailoredEntry]:
    entries = []
    for title in titles:
        children = [fact.value for fact in details if fact.parent_key == title.key]
        text, date = _split_date(title.value)
        ordered = _ordered_by_relevance(children, lambda value: value, wanted)
        shows = sorted(vocabulary().find(" ".join([title.value, *children])) & set(wanted))
        entries.append(TailoredEntry(title=text, date=date, details=ordered, shows=shows))
    return _ordered_by_relevance(entries, lambda entry: " ".join([entry.title, *entry.details]), wanted) if reorder else entries


def _skill_groups(facts: list[Fact], unlisted: list[str], wanted: dict[str, int]) -> list[SkillGroup]:
    relevant = _ordered_by_relevance(
        [fact.value for fact in facts if _relevance(fact.value, wanted)], lambda value: value, wanted
    )
    groups: dict[str, list[str]] = {}
    for fact in facts:
        if fact.value not in relevant:
            groups.setdefault(fact.label or OTHER_GROUP, []).append(fact.value)
    result = [SkillGroup(name=RELEVANT_GROUP, skills=relevant + unlisted)] if relevant or unlisted else []
    return result + [SkillGroup(name=name, skills=skills) for name, skills in groups.items()]


def _change_notes(
    original: list[str], tailored: list[TailoredEntry], unlisted: list[str], relevant_skills: int
) -> list[str]:
    notes = []
    if tailored and [entry.title for entry in tailored] != [_split_date(title)[0] for title in original]:
        top = tailored[0]
        notes.append(f"Moved '{top.title}' to the top of Projects because it shows {', '.join(top.shows)}.")
    if relevant_skills:
        notes.append(f"Moved {relevant_skills} skills this job asks for to the top of Skills.")
    if unlisted:
        notes.append(f"Added {', '.join(unlisted)} to Skills, because your confirmed work shows them.")
    return notes or ["The order already suits this job, so nothing was moved."]


def find_unsupported(cv: TailoredCv, profile: CandidateProfile) -> list[str]:
    """List any text that is not in a confirmed fact. Any future rewriting step must pass this too."""
    values = [fact.value for fact in _confirmed(profile)]
    # A date shown beside its title is the same fact, so compare against the text without it too.
    confirmed = [" ".join(text.lower().split()) for text in [*values, *(_split_date(value)[0] for value in values)]]
    candidate = build_candidate(profile)
    lines = [cv.name, cv.summary, *cv.contact, *cv.certifications]
    lines += [skill for group in cv.skills for skill in group.skills if skill not in candidate.skills]
    for entry in [*cv.experience, *cv.projects, *cv.education]:
        lines += [entry.title, entry.date, *entry.details]
    return [
        line
        for line in lines
        if line and not any(" ".join(line.lower().split()) in fact for fact in confirmed)
    ]


def _confirmed(profile: CandidateProfile) -> list[Fact]:
    return [fact for fact in profile.facts if fact.verification_status is VerificationStatus.CONFIRMED]


def tailor_cv(job: Job, profile: CandidateProfile) -> TailoredCv:
    facts = _confirmed(profile)
    requirements = job_requirements(job)
    wanted = {
        item.skill: REQUIRED_WEIGHT if item.importance == "required" else PREFERRED_WEIGHT for item in requirements
    }

    def of_type(*types: FactType) -> list[Fact]:
        return [fact for fact in facts if fact.type in types]

    contact = {fact.label: fact.value for fact in of_type(FactType.CONTACT)}
    skills = of_type(FactType.SKILL)
    candidate = build_candidate(profile)
    listed = vocabulary().find(" ".join(fact.value for fact in skills))
    unlisted = sorted(skill for skill in set(wanted) & set(candidate.skills) if skill not in listed)
    achievements = of_type(FactType.ACHIEVEMENT)
    project_titles = of_type(FactType.PROJECT)
    projects = _entries(project_titles, achievements, wanted, reorder=True)
    summary = next((fact.value for fact in of_type(FactType.SUMMARY)), None)
    groups = _skill_groups(skills, unlisted, wanted)

    cv = TailoredCv(
        job_id=job.id,
        job_title=job.title,
        company=job.company,
        apply_url=job.apply_url,
        name=contact.pop("name", None),
        contact=list(contact.values()),
        summary=summary,
        skills=groups,
        experience=_entries(of_type(FactType.EXPERIENCE), achievements, wanted, reorder=False),
        projects=projects,
        education=_entries(of_type(FactType.EDUCATION), achievements, wanted, reorder=False),
        certifications=_ordered_by_relevance(
            [fact.value for fact in of_type(FactType.CERTIFICATION)], lambda value: value, wanted
        ),
        changes=_change_notes(
            [fact.value for fact in project_titles],
            projects,
            unlisted,
            sum(1 for fact in skills if _relevance(fact.value, wanted)),
        ),
        requested_skills=[item.skill for item in requirements],
        missing_required=[
            item.skill for item in requirements if item.importance == "required" and item.skill not in candidate.skills
        ],
        unsupported=[],
    )
    return cv.model_copy(update={"unsupported": find_unsupported(cv, profile)})


def _entry_html(entries: list[TailoredEntry], heading: str) -> str:
    if not entries:
        return ""
    parts = [f"<h2>{heading}</h2>"]
    for entry in entries:
        date = f" ({html.escape(entry.date)})" if entry.date else ""
        parts.append(f"<p><b>{html.escape(entry.title)}</b>{date}</p>")
        parts += [f"<p>{html.escape(detail)}</p>" for detail in entry.details]
    return "".join(parts)


def _cv_html(cv: TailoredCv) -> str:
    parts = [f"<h1>{html.escape(cv.name or '')}</h1>"]
    parts.append(f'<p class="contact">{html.escape(" | ".join(cv.contact))}</p>')
    if cv.summary:
        parts.append(f"<h2>Summary</h2><p>{html.escape(cv.summary)}</p>")
    if cv.skills:
        parts.append("<h2>Skills</h2>")
        parts += [
            f"<p><b>{html.escape(group.name)}:</b> {html.escape(', '.join(group.skills))}</p>" for group in cv.skills
        ]
    parts.append(_entry_html(cv.experience, "Experience"))
    parts.append(_entry_html(cv.projects, "Projects"))
    parts.append(_entry_html(cv.education, "Education"))
    if cv.certifications:
        parts.append("<h2>Certifications</h2>")
        parts += [f"<p>{html.escape(item)}</p>" for item in cv.certifications]
    return "".join(parts)


def render_pdf(cv: TailoredCv) -> bytes:
    return html_to_pdf(_cv_html(cv))


def html_to_pdf(content: str) -> bytes:
    story = pymupdf.Story(html=content, user_css=PDF_CSS)
    page = pymupdf.paper_rect(PAGE_SIZE)
    area = page + (PAGE_MARGIN, PAGE_MARGIN, -PAGE_MARGIN, -PAGE_MARGIN)
    output = io.BytesIO()
    writer = pymupdf.DocumentWriter(output)
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(area)
        story.draw(device)
        writer.end_page()
    writer.close()
    return output.getvalue()
