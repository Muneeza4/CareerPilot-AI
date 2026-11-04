"""Draft a cover letter from confirmed facts, check it, and save one review-ready folder per job."""

from __future__ import annotations

import html
import re
from pathlib import Path

from pydantic import BaseModel

from models.schemas import CandidateProfile, FactType, Job, VerificationStatus
from services.matching import USED_IN_WORK, build_candidate, vocabulary
from services.tailoring import TailoredCv, html_to_pdf, render_pdf

MAX_LETTER_CHARS = 5000
LETTER_PROJECTS = 2
MAX_FOLDER_CHARS = 50


class PackageCheck(BaseModel):
    ok: bool
    blocks: bool = False
    detail: str


def _join(items: list[str]) -> str:
    return " and ".join(items) if len(items) < 3 else f"{', '.join(items[:-1])} and {items[-1]}"


def _project_lines(cv: TailoredCv) -> list[str]:
    relevant = [entry for entry in cv.projects if entry.shows] or cv.projects[:1]
    lines = []
    for index, entry in enumerate(relevant[:LETTER_PROJECTS]):
        lead = "Another relevant project is" if index else "A relevant project is"
        lines.append(" ".join([f"{lead} {entry.title}.", *entry.details]))
    return lines


def _skill_line(cv: TailoredCv, profile: CandidateProfile) -> str | None:
    candidate = build_candidate(profile)
    held = sorted({skill for entry in [*cv.projects, *cv.experience] for skill in entry.shows})
    held += [skill for skill in cv.requested_skills if skill in candidate.skills and skill not in held]
    used = [skill for skill in held if candidate.skills[skill].strength == USED_IN_WORK]
    listed = [skill for skill in held if skill not in used]
    has_experience = any(
        fact.type is FactType.EXPERIENCE and fact.verification_status is VerificationStatus.CONFIRMED
        for fact in profile.facts
    )
    parts = []
    if used:
        parts.append(f"I have used {_join(used)} in my {'work and projects' if has_experience else 'projects'}.")
    if listed:
        parts.append(f"My skills also include {_join(listed)}.")
    return " ".join(parts) or None


def draft_letter(cv: TailoredCv, profile: CandidateProfile) -> str:
    """Every claim comes from a confirmed fact, so the letter says nothing about missing skills."""
    opening = f"I am applying for the {cv.job_title} role at {cv.company}."
    if cv.education:
        degree = cv.education[0]
        date = f" ({degree.date})." if degree.date else ""
        opening += f" My education: {degree.title}{date}"
    paragraphs = [f"Dear {cv.company} hiring team,", opening, _skill_line(cv, profile), *_project_lines(cv)]
    paragraphs += ["My CV is attached. Thank you for reviewing my application.", f"Sincerely,\n{cv.name or ''}".strip()]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def check_package(cv: TailoredCv, letter: str, profile: CandidateProfile) -> list[PackageCheck]:
    """Blocking checks stop a false claim. The rest are warnings for the user to judge."""
    candidate = build_candidate(profile)
    # Naming the company or the job title, as "Databricks" does, claims no skill.
    claims = letter
    for name in (cv.company, cv.job_title):
        claims = re.sub(re.escape(name), " ", claims, flags=re.I)
    claimed = sorted(skill for skill in vocabulary().find(claims) if skill not in candidate.skills)
    unreviewed = sum(1 for fact in profile.facts if fact.verification_status is VerificationStatus.EXTRACTED)
    has_contact = bool(profile.contact("email") and profile.contact("phone"))
    has_work = bool(cv.projects or cv.experience)

    def check(ok: bool, good: str, bad: str, blocks: bool = False) -> PackageCheck:
        return PackageCheck(ok=ok, blocks=blocks and not ok, detail=good if ok else bad)

    return [
        check(not cv.unsupported, "Every CV line matches a confirmed fact.", "The CV has text that is not in your confirmed facts.", True),
        check(not claimed, "The letter names no skill outside your confirmed facts.", f"The letter names skills your confirmed facts do not show: {_join(claimed)}.", True),
        check(bool(letter.strip()) and cv.name is not None and cv.name.lower() in letter.lower(), "The letter is signed with your name.", "Your name is not in the letter."),
        check(cv.company.lower() in letter.lower(), "The letter names the company.", f"The letter does not name {cv.company}."),
        check(has_contact, "Your email and phone are on the CV.", "Confirm your email and phone so they appear on the CV."),
        check(has_work, "The CV shows projects or experience.", "No confirmed projects or experience are on this CV."),
        check(unreviewed == 0, "All CV facts are reviewed.", f"{unreviewed} CV facts still need review, so they are left out."),
        check(not cv.missing_required, "You show every required skill we could read.", f"This job asks for skills you do not show: {_join(cv.missing_required)}."),
    ]


def _letter_html(letter: str) -> str:
    paragraphs = [html.escape(part).replace("\n", "<br>") for part in letter.split("\n\n") if part.strip()]
    return "".join(f"<p>{part}</p><p>&nbsp;</p>" for part in paragraphs)


def _folder_name(job: Job) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job.company} {job.title}".lower()).strip("-")
    return f"{job.id}-{slug[:MAX_FOLDER_CHARS].rstrip('-')}"


def save_package(root: Path, job: Job, cv: TailoredCv, letter: str) -> tuple[Path, list[str]]:
    """Saving again replaces these files, so the folder always holds the latest reviewed version."""
    folder = root / _folder_name(job)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cv.pdf").write_bytes(render_pdf(cv))
    (folder / "cover-letter.pdf").write_bytes(html_to_pdf(_letter_html(letter)))
    (folder / "cover-letter.txt").write_text(letter, encoding="utf-8")
    # Postings get closed, so keep what was applied to.
    posting = f"{job.title}\n{job.company}\n{job.location or ''}\n{job.apply_url}\n\n{job.description}"
    (folder / "job.txt").write_text(posting, encoding="utf-8")
    return folder, ["cv.pdf", "cover-letter.pdf", "cover-letter.txt", "job.txt"]
