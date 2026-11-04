"""Explainable job matching. The score is calculated by code from confirmed CV facts, never by an LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from models.schemas import CandidateProfile, FactType, Job, Preferences, RemotePreference, VerificationStatus

SKILLS_PATH = Path(__file__).resolve().parent.parent / "config" / "skills.yaml"

WEIGHTS = {"required": 0.60, "preferred": 0.15, "experience": 0.15, "constraints": 0.10}
USED_IN_WORK = 1.0
LISTED_ONLY = 0.6
WEAK_MATCH_CAP = 59
EXPERIENCE_GAP_CAP = 65
ROLE_MISMATCH_CAP = 59
UNKNOWN_FIT = 0.7
THIN_JOB_SKILLS = 3
THIN_JOB_BASE = 50
THIN_JOB_STEP = 10
ASSUMED_SENIOR_YEARS = 5

PREFERRED_RE = re.compile(r"nice to have|nice-to-have|preferred|bonus|desirable|good to have|is a plus|a plus\b", re.I)
REQUIRED_HEADING_RE = re.compile(
    r"requirements|qualifications|what you.ll need|what we.re looking for|must have|who you are|what you bring"
    r"|you have|you should have|skills",
    re.I,
)
OTHER_HEADING_RE = re.compile(
    r"^(about|benefits|perks|what we offer|why join|our (mission|values|team)|compensation|the role|the team)", re.I
)
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*\+?\s*years?", re.I)
SENIOR_RE = re.compile(r"\b(senior|sr|staff|lead|principal|head|director)\b", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DATE_PART = r"(?:([a-z]{3})[a-z]*\.?\s*)?((?:19|20)\d{2})"
DATE_RANGE_RE = re.compile(rf"{DATE_PART}\s*(?:-|–|—|to)\s*(?:{DATE_PART}|(present|current))", re.I)


class SkillMatch(BaseModel):
    skill: str
    importance: str
    strength: str
    evidence: str


class MatchResult(BaseModel):
    job_id: int
    title: str
    company: str
    location: str | None
    is_remote: bool
    posted_at: str | None
    apply_url: str
    score: int
    components: dict[str, int]
    matched: list[SkillMatch]
    missing_required: list[str]
    missing_preferred: list[str]
    reasons: list[str]


@dataclass(frozen=True)
class Requirement:
    skill: str
    importance: str


@dataclass(frozen=True)
class JobAnalysis:
    requirements: tuple[Requirement, ...]
    min_years: int | None


@dataclass(frozen=True)
class CandidateSkill:
    strength: float
    evidence: str


@dataclass
class Candidate:
    skills: dict[str, CandidateSkill]
    years: float


class _Vocabulary:
    def __init__(self, path: Path = SKILLS_PATH):
        self.by_alias: dict[str, str] = {}
        exact: list[str] = []
        loose: list[str] = []
        for item in yaml.safe_load(path.read_text(encoding="utf-8"))["skills"]:
            aliases = [item["name"], *item.get("aliases", [])]
            for alias in aliases:
                self.by_alias[alias.lower()] = item["name"]
            (exact if item.get("exact") else loose).extend(aliases)
        self.patterns = [self._compile(exact, 0), self._compile(loose, re.I)]

    @staticmethod
    def _compile(aliases: list[str], flags: int) -> re.Pattern:
        ordered = sorted(aliases, key=len, reverse=True)
        words = "|".join(r"\s+".join(re.escape(word) for word in alias.split()) for alias in ordered)
        return re.compile(rf"(?<![\w+#.-])(?:{words})(?![\w+#]|-\w)", flags)

    def find(self, text: str) -> set[str]:
        found = set()
        for pattern in self.patterns:
            for match in pattern.finditer(text):
                found.add(self.by_alias[" ".join(match.group().lower().split())])
        return found


@lru_cache(maxsize=1)
def vocabulary() -> _Vocabulary:
    return _Vocabulary()


def _is_heading(line: str) -> bool:
    return len(line) <= 60 and not line.endswith(".") and not line.startswith("•")


@lru_cache(maxsize=20000)
def analyze_job(title: str, description: str) -> JobAnalysis:
    """Find which skills a job asks for, and whether each one is required or only preferred."""
    vocab = vocabulary()
    importance: dict[str, str] = {skill: "required" for skill in vocab.find(title)}
    section = None
    saw_required_section = False

    for line in description.splitlines():
        text = line.strip()
        if not text:
            continue
        if _is_heading(text):
            if PREFERRED_RE.search(text):
                section = "preferred"
                continue
            if REQUIRED_HEADING_RE.search(text):
                section = "required"
                saw_required_section = True
                continue
            if OTHER_HEADING_RE.search(text):
                section = "other"
                continue
        level = "preferred" if section == "preferred" or PREFERRED_RE.search(text) else section
        for skill in vocab.find(text):
            if level == "required" or importance.get(skill) == "required":
                importance[skill] = "required"
            elif level is None:
                importance.setdefault(skill, "unclear")
            else:
                importance.setdefault(skill, "preferred")

    # Without a clear requirements section, skills named outside "about us" text are the best evidence.
    default = "preferred" if saw_required_section else "required"
    requirements = [
        Requirement(skill, default if level == "unclear" else level) for skill, level in sorted(importance.items())
    ]
    if requirements and not any(item.importance == "required" for item in requirements):
        requirements = [Requirement(item.skill, "required") for item in requirements]

    years = [int(match.group(1)) for match in YEARS_RE.finditer(description) if int(match.group(1)) <= 15]
    min_years = min(years) if years else None
    if min_years is None and SENIOR_RE.search(title):
        min_years = ASSUMED_SENIOR_YEARS
    return JobAnalysis(tuple(requirements), min_years)


def _months(fact_value: str, today: date) -> int:
    total = 0
    for match in DATE_RANGE_RE.finditer(fact_value):
        start_month, start_year, end_month, end_year, present = match.groups()
        start = int(start_year) * 12 + _month_index(start_month)
        end = today.year * 12 + today.month - 1 if present else int(end_year) * 12 + _month_index(end_month, 11)
        total += max(0, end - start + 1)
    return total


def _month_index(name: str | None, default: int = 0) -> int:
    return MONTHS.index(name.lower()) if name and name.lower() in MONTHS else default


def build_candidate(profile: CandidateProfile, today: date | None = None) -> Candidate:
    """Read only confirmed facts. Anything unreviewed or rejected cannot help a score."""
    vocab = vocabulary()
    today = today or date.today()
    skills: dict[str, CandidateSkill] = {}
    months = 0

    for fact in profile.facts:
        if fact.verification_status is not VerificationStatus.CONFIRMED:
            continue
        if fact.type is FactType.EXPERIENCE:
            months += _months(fact.value, today)
        if fact.type is FactType.SKILL:
            strength = LISTED_ONLY
        elif fact.type in (FactType.EXPERIENCE, FactType.PROJECT, FactType.ACHIEVEMENT):
            strength = USED_IN_WORK
        else:
            continue
        for skill in vocab.find(fact.value):
            if skill not in skills or strength > skills[skill].strength:
                skills[skill] = CandidateSkill(strength, fact.value)
    return Candidate(skills, months / 12)


def exclusion_reason(job: Job, preferences: Preferences) -> str | None:
    if preferences.remote_preference is RemotePreference.REMOTE and not job.is_remote:
        return "Not remote"
    if preferences.locations and not job.is_remote:
        location = (job.location or "").lower()
        if not any(place.lower() in location for place in preferences.locations):
            return "Outside your locations"
    return None


def _role_fit(job: Job, preferences: Preferences) -> float:
    if not preferences.target_roles:
        return UNKNOWN_FIT
    title = job.title.lower()
    return 1.0 if any(role.lower() in title for role in preferences.target_roles) else 0.5


def job_requirements(job: Job) -> list[Requirement]:
    # A company naming its own product, as Databricks does, is not asking for that skill.
    company = job.company.lower()
    return [item for item in analyze_job(job.title, job.description).requirements if item.skill.lower() not in company]


def score_job(job: Job, candidate: Candidate, preferences: Preferences) -> MatchResult | None:
    """Score one job, or return None when it is excluded or names no skill we can compare."""
    analysis = analyze_job(job.title, job.description)
    requirements = job_requirements(job)
    required = [item for item in requirements if item.importance == "required"]
    preferred = [item for item in requirements if item.importance == "preferred"]
    if not required or exclusion_reason(job, preferences):
        return None

    def coverage(items: list[Requirement]) -> float:
        if not items:
            return 1.0
        held = (candidate.skills.get(item.skill) for item in items)
        return sum(skill.strength for skill in held if skill) / len(items)

    required_fit = coverage(required)
    preferred_fit = coverage(preferred) if preferred else None
    experience_fit = UNKNOWN_FIT if not analysis.min_years else min(1.0, candidate.years / analysis.min_years)
    constraints_fit = _role_fit(job, preferences)

    fits = {
        "required": required_fit,
        "preferred": preferred_fit,
        "experience": experience_fit,
        "constraints": constraints_fit,
    }
    known = {name: fit for name, fit in fits.items() if fit is not None}
    score = 100 * sum(WEIGHTS[name] * fit for name, fit in known.items()) / sum(WEIGHTS[name] for name in known)
    if required_fit < 0.5:
        score = min(score, WEAK_MATCH_CAP)
    if experience_fit < 0.5:
        score = min(score, EXPERIENCE_GAP_CAP)
    if constraints_fit < UNKNOWN_FIT:
        score = min(score, ROLE_MISMATCH_CAP)
    # Matching one of very few readable skills proves little, so a thin job cannot score highly.
    score = min(score, THIN_JOB_BASE + THIN_JOB_STEP * len(required))

    matched = [
        SkillMatch(
            skill=item.skill,
            importance=item.importance,
            strength="Used in your work" if held.strength == USED_IN_WORK else "Listed in your CV only",
            evidence=held.evidence,
        )
        for item in requirements
        if (held := candidate.skills.get(item.skill))
    ]
    missing_required = [item.skill for item in required if item.skill not in candidate.skills]
    held_required = len(required) - len(missing_required)
    reasons = [f"You have {held_required} of {len(required)} required skills."]
    if analysis.min_years:
        reasons.append(f"Asks for about {analysis.min_years}+ years. Your confirmed roles show {candidate.years:.1f}.")
    if len(required) < THIN_JOB_SKILLS:
        reasons.append(f"Only {len(required)} required skills could be read from this job, so this score is less certain.")
    if preferences.target_roles and constraints_fit < 1:
        reasons.append("The title is not one of your target roles.")

    return MatchResult(
        job_id=job.id,
        title=job.title,
        company=job.company,
        location=job.location,
        is_remote=job.is_remote,
        posted_at=job.posted_at,
        apply_url=job.apply_url,
        score=round(score),
        components={
            "required": round(required_fit * 100),
            "preferred": round((preferred_fit or 0) * 100),
            "experience": round(experience_fit * 100),
            "constraints": round(constraints_fit * 100),
        },
        matched=matched,
        missing_required=missing_required,
        missing_preferred=[item.skill for item in preferred if item.skill not in candidate.skills],
        reasons=reasons,
    )


def rank_jobs(jobs: list[Job], profile: CandidateProfile, preferences: Preferences) -> list[MatchResult]:
    candidate = build_candidate(profile)
    results = [result for job in jobs if (result := score_job(job, candidate, preferences))]
    return sorted(results, key=lambda result: (-result.score, -(len(result.matched)), result.job_id))
