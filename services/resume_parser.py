"""Turn a CV PDF into a candidate profile where every fact points back to its exact source text."""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from models.schemas import (
    CandidateProfile,
    Evidence,
    Fact,
    FactType,
    Gap,
    SourceType,
    VerificationStatus,
)

MIN_TEXT_CHARS = 50
BULLET_CHARS = "•●▪◦‣·*-–—○■►➢"
WRAP_ENDINGS = (",", "&", "-", " and", " or", " of", " with", " the", " to", " in", " for", " using")

SECTION_ALIASES = {
    "summary": {
        "summary", "profile", "professional summary", "career summary", "objective",
        "career objective", "about", "about me",
    },
    "contact": {"contact", "contact information", "contact details", "personal details"},
    "education": {"education", "academic background", "academic qualifications", "qualifications"},
    "skills": {
        "skills", "technical skills", "core skills", "key skills", "technologies", "tech stack",
        "tools", "skills & tools", "core competencies",
    },
    "experience": {
        "experience", "work experience", "professional experience", "employment",
        "employment history", "work history", "internships", "internship experience",
    },
    "projects": {"projects", "academic projects", "personal projects", "key projects", "selected projects"},
    "certifications": {
        "certifications", "certificates", "licenses & certifications", "courses", "training",
    },
    "other": {
        "languages", "interests", "hobbies", "references", "awards", "achievements",
        "publications", "activities", "volunteering", "volunteer experience",
    },
}
HEADING_TO_SECTION = {alias: name for name, aliases in SECTION_ALIASES.items() for alias in aliases}

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_RE = re.compile(rf"\b(?:{MONTH}\s*)?(?:19|20)\d{{2}}\b|\b\d{{1,2}}/(?:19|20)\d{{2}}\b|\bpresent\b", re.I)
YEAR = rf"(?:{MONTH}\s*)?(?:19|20)\d{{2}}"
HEADING_DATE_RE = re.compile(rf"\s+({YEAR}(?:\s*[-–—]\s*(?:{YEAR}|present))?)$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
LINK_RE = re.compile(r"(?:https?://|www\.|(?:linkedin|github)\.com)\S+", re.I)
WRAPPED_HYPHEN_RE = re.compile(r"(?<=\w)-\n(?=[a-z])")
SKILL_CATEGORY_RE = re.compile(r"\s*([A-Za-z][A-Za-z &/-]{1,30}):\s*")
SKILL_TOKEN_RE = re.compile(r"[^,;|()•·]+(?:\([A-Z0-9/-]{2,8}\))?")


class CvParseError(ValueError):
    """The file cannot be turned into a profile. The message is safe to show the user."""


class InvalidPdfError(CvParseError):
    pass


class ScannedPdfError(CvParseError):
    pass


@dataclass
class TextLine:
    text: str
    start: int
    end: int
    page: int
    bold: bool = False


@dataclass
class ParsedDocument:
    raw_text: str
    lines: list[TextLine]
    page_count: int

    def page_at(self, offset: int) -> int:
        index = bisect.bisect_right([line.start for line in self.lines], offset) - 1
        return self.lines[max(index, 0)].page


@dataclass
class Section:
    name: str
    lines: list[TextLine] = field(default_factory=list)


@dataclass
class Entry:
    start: int | None = None
    end: int | None = None
    has_date: bool = False
    bullets: list[list[int]] = field(default_factory=list)


@dataclass
class ParseResult:
    profile: CandidateProfile
    rejected: list[Fact]


@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool = False

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _page_boxes(page: pymupdf.Page) -> list[_Box]:
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
    boxes = []
    for block in page.get_text("dict", flags=flags)["blocks"]:
        for line in block.get("lines", []):
            text = " ".join("".join(span["text"] for span in line["spans"]).split())
            if text:
                spans = [span for span in line["spans"] if span["text"].strip()]
                bold = all(span["flags"] & pymupdf.TEXT_FONT_BOLD for span in spans)
                boxes.append(_Box(*line["bbox"], text, bold))
    return boxes


def _rows(boxes: list[_Box]) -> list[tuple[str, bool]]:
    """Join boxes sitting on one visual row, so a right-aligned date stays with its job title."""
    rows: list[list[_Box]] = []
    for box in sorted(boxes, key=lambda item: item.y_mid):
        if rows and abs(box.y_mid - rows[-1][0].y_mid) < (box.y1 - box.y0) / 2:
            rows[-1].append(box)
        else:
            rows.append([box])
    return [
        (" ".join(box.text for box in sorted(row, key=lambda item: item.x0)), all(box.bold for box in row))
        for row in rows
    ]


def _find_gutter(boxes: list[_Box], width: float) -> float | None:
    """Find a vertical gap with real text on both sides. Sidebar layouts put it off-centre."""
    total_chars = sum(len(box.text) for box in boxes)
    best: tuple[int, float] | None = None
    for x in range(int(width * 0.25), int(width * 0.75), 4):
        crossing = sum(box.x0 < x < box.x1 for box in boxes)
        left = sum(len(box.text) for box in boxes if box.x1 <= x)
        right = sum(len(box.text) for box in boxes if box.x0 >= x)
        if min(left, right) < total_chars * 0.2:
            continue
        if best is None or crossing < best[0]:
            best = (crossing, x)
    if best is None or best[0] > max(2, len(boxes) * 0.1):
        return None
    return best[1]


def _reading_order(boxes: list[_Box], width: float) -> list[tuple[str, bool]]:
    gutter = _find_gutter(boxes, width)
    if gutter is None:
        return _rows(boxes)

    spanning = sorted((b for b in boxes if b.x0 < gutter < b.x1), key=lambda b: b.y0)
    columns = [b for b in boxes if not b.x0 < gutter < b.x1]
    ordered: list[tuple[str, bool]] = []
    top = float("-inf")
    for separator in [*spanning, None]:
        bottom = separator.y0 if separator else float("inf")
        band = [b for b in columns if top <= b.y_mid < bottom]
        ordered += _rows([b for b in band if b.x1 <= gutter])
        ordered += _rows([b for b in band if b.x1 > gutter])
        if separator:
            ordered.append((separator.text, separator.bold))
            top = separator.y1
    return ordered


def extract_document(pdf_bytes: bytes) -> ParsedDocument:
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise InvalidPdfError("This file could not be opened as a PDF.") from exc

    with doc:
        if doc.needs_pass:
            raise InvalidPdfError("This PDF is password protected. Please upload an unlocked copy.")
        lines: list[TextLine] = []
        offset = 0
        for page_number, page in enumerate(doc, start=1):
            for text, bold in _reading_order(_page_boxes(page), page.rect.width):
                lines.append(TextLine(text, offset, offset + len(text), page_number, bold))
                offset += len(text) + 1
        page_count = doc.page_count

    raw_text = "\n".join(line.text for line in lines)
    if len(raw_text) < MIN_TEXT_CHARS:
        raise ScannedPdfError(
            "No readable text was found. This looks like a scanned CV, which is not supported yet. "
            "Please upload a text-based PDF."
        )
    return ParsedDocument(raw_text, lines, page_count)


def _heading_section(text: str) -> str | None:
    if len(text) > 40:
        return None
    key = " ".join(re.sub(r"[^a-z& ]", " ", text.lower()).split())
    return HEADING_TO_SECTION.get(key)


def _split_heading(line: TextLine) -> tuple[str | None, TextLine | None]:
    """Find a section heading. A right-aligned date can share its row, and belongs to the first entry."""
    name = _heading_section(line.text)
    if name:
        return name, None
    date = HEADING_DATE_RE.search(line.text)
    name = _heading_section(line.text[: date.start()]) if date else None
    if not name:
        return None, None
    return name, TextLine(date.group(1), line.start + date.start(1), line.end, line.page, line.bold)


def _is_contact_line(text: str) -> bool:
    if EMAIL_RE.fullmatch(text) or LINK_RE.fullmatch(text):
        return True
    return bool(PHONE_RE.fullmatch(text)) and sum(c.isdigit() for c in text) >= 10


def _looks_like_name(text: str) -> bool:
    return 2 <= len(text.split()) <= 5 and not re.search(r"[@\d/|]", text)


def split_sections(document: ParsedDocument) -> list[Section]:
    header = Section("header")
    sections = [header]
    for line in document.lines:
        name, first_line = _split_heading(line)
        if name:
            sections.append(Section(name, [first_line] if first_line else []))
        elif _is_contact_line(line.text):
            # Sidebar layouts can place the name and contact block after another section.
            current = sections[-1]
            if current is not header and not header.lines and current.lines and _looks_like_name(current.lines[-1].text):
                header.lines.append(current.lines.pop())
            header.lines.append(line)
        else:
            sections[-1].lines.append(line)
    return sections


def _strip_bullet(line: TextLine) -> tuple[bool, int]:
    """Return whether the line is a bullet, and where its content starts."""
    text = line.text
    if text[0] in BULLET_CHARS and (len(text) == 1 or text[1] == " "):
        return True, line.start + (len(text) - len(text[1:].lstrip()))
    return False, line.start


def _is_continuation(previous: str, line: str, longest: int) -> bool:
    if line[0].islower() or previous.endswith(WRAP_ENDINGS):
        return True
    return len(previous) >= longest * 0.75 and not previous.endswith(".") and not DATE_RE.search(line)


def group_entries(lines: list[TextLine]) -> list[Entry]:
    """Group a section's lines into entries, each a header followed by its bullets."""
    entries: list[Entry] = []
    current: Entry | None = None
    previous: TextLine | None = None
    bullet_pending = False
    longest = max((len(line.text) for line in lines), default=0)

    for line in lines:
        is_bullet, content_start = _strip_bullet(line)
        if is_bullet and content_start >= line.end:
            bullet_pending = True  # Some PDFs put the glyph on its own line.
            continue
        if is_bullet or bullet_pending:
            if current is None:
                current = Entry()
                entries.append(current)
            current.bullets.append([content_start, line.end])
            bullet_pending = False
        elif current and current.bullets and previous and _is_continuation(previous.text, line.text, longest):
            current.bullets[-1][1] = line.end
        elif current is None or current.bullets or (current.has_date and DATE_RE.search(line.text)):
            current = Entry(start=line.start, end=line.end, has_date=bool(DATE_RE.search(line.text)))
            entries.append(current)
        else:
            current.end = line.end
            current.has_date = current.has_date or bool(DATE_RE.search(line.text))
        previous = line
    return entries


def _has_bold_titles(lines: list[TextLine]) -> bool:
    has_bullet = any(_strip_bullet(line)[0] for line in lines)
    return not has_bullet and any(line.bold for line in lines) and not all(line.bold for line in lines)


def group_paragraphs(lines: list[TextLine]) -> list[Entry]:
    """Group entries written as a bold title followed by a plain paragraph, with no bullets."""
    entries: list[Entry] = []
    for line in lines:
        current = entries[-1] if entries else None
        if line.bold and current and not current.bullets:
            current.end = line.end
        elif line.bold or current is None:
            entries.append(Entry(start=line.start, end=line.end))
        elif current.bullets:
            current.bullets[-1][1] = line.end
        else:
            current.bullets.append([line.start, line.end])
    return entries


class _ProfileBuilder:
    def __init__(self, document: ParsedDocument):
        self.document = document
        self.facts: list[Fact] = []
        self.counts: dict[str, int] = {}

    def evidence(self, start: int, end: int, section: str) -> Evidence:
        return Evidence(
            source_type=SourceType.CV,
            section=section,
            page=self.document.page_at(start),
            char_start=start,
            char_end=end,
            quote=self.document.raw_text[start:end],
        )

    def add(
        self,
        fact_type: FactType,
        start: int,
        end: int,
        section: str,
        *,
        label: str | None = None,
        parent_key: str | None = None,
    ) -> Fact:
        self.counts[fact_type.value] = self.counts.get(fact_type.value, 0) + 1
        value = " ".join(WRAPPED_HYPHEN_RE.sub("-", self.document.raw_text[start:end]).split())
        fact = Fact(
            key=f"{fact_type.value}-{self.counts[fact_type.value]}",
            parent_key=parent_key,
            type=fact_type,
            label=label,
            value=value,
            normalized_value=normalize(value),
            evidence=[self.evidence(start, end, section)],
        )
        self.facts.append(fact)
        return fact

    def add_contact(self, lines: list[TextLine]) -> None:
        found: set[str] = set()
        for line in lines:
            for label, pattern in (("email", EMAIL_RE), ("phone", PHONE_RE), ("link", LINK_RE)):
                for match in pattern.finditer(line.text):
                    if label == "phone" and sum(c.isdigit() for c in match.group()) < 10:
                        continue
                    if label in found and label != "link":
                        continue
                    found.add(label)
                    self.add(
                        FactType.CONTACT,
                        line.start + match.start(),
                        line.start + match.end(),
                        "contact",
                        label=label,
                    )

    def add_name(self, lines: list[TextLine]) -> None:
        for line in lines[:5]:
            if _looks_like_name(line.text):
                self.add(FactType.CONTACT, line.start, line.end, "contact", label="name")
                return

    def add_skills(self, lines: list[TextLine]) -> None:
        seen: dict[str, Fact] = {}
        for line in lines:
            _, offset = _strip_bullet(line)
            content_at = offset - line.start
            label = None
            category = SKILL_CATEGORY_RE.match(line.text, content_at)
            if category:
                label = category.group(1).strip()
                content_at = category.end()
            for match in SKILL_TOKEN_RE.finditer(line.text, content_at):
                token = match.group()
                lead = len(token) - len(token.lstrip(" "))
                inner = token.strip(" .")
                if inner.lower().startswith("and "):
                    lead += 4
                    inner = inner[4:]
                if not inner or len(inner.split()) > 5:
                    continue
                start = line.start + match.start() + lead
                end = start + len(inner)
                existing = seen.get(normalize(inner))
                if existing:
                    existing.evidence.append(self.evidence(start, end, "skills"))
                else:
                    seen[normalize(inner)] = self.add(FactType.SKILL, start, end, "skills", label=label)

    def add_entries(self, fact_type: FactType, section: Section) -> None:
        paragraphs = fact_type is not FactType.EDUCATION and _has_bold_titles(section.lines)
        for entry in (group_paragraphs if paragraphs else group_entries)(section.lines):
            parent = None
            if entry.start is not None:
                parent = self.add(fact_type, entry.start, entry.end, section.name)
            for start, end in entry.bullets:
                self.add(
                    FactType.ACHIEVEMENT,
                    start,
                    end,
                    section.name,
                    parent_key=parent.key if parent else None,
                )

    def add_certifications(self, section: Section) -> None:
        for line in section.lines:
            _, start = _strip_bullet(line)
            if start < line.end:
                self.add(FactType.CERTIFICATION, start, line.end, section.name)

    def link_skill_usage(self, sections: list[Section]) -> None:
        """Add evidence wherever a listed skill is actually used in experience or project text."""
        for section in sections:
            if section.name not in ("experience", "projects") or not section.lines:
                continue
            base = section.lines[0].start
            text = self.document.raw_text[base : section.lines[-1].end]
            for fact in self.facts:
                if fact.type is not FactType.SKILL:
                    continue
                pattern = r"\s+".join(re.escape(word) for word in fact.value.split())
                flags = re.I if len(fact.value) > 2 else 0
                for match in re.finditer(rf"(?<![\w+#.]){pattern}(?![\w+#])", text, flags):
                    fact.evidence.append(self.evidence(base + match.start(), base + match.end(), section.name))


def verify_evidence(raw_text: str, evidence: Evidence) -> bool:
    """Check a CV quote really sits at its stated position. User-stated evidence has no CV position."""
    if evidence.source_type is SourceType.USER_STATED:
        return bool(evidence.quote.strip())
    if evidence.char_start is None or evidence.char_end is None or not evidence.quote:
        return False
    return raw_text[evidence.char_start : evidence.char_end] == evidence.quote


def verify_facts(raw_text: str, facts: list[Fact]) -> tuple[list[Fact], list[Fact]]:
    """Split facts into verified and rejected. Any future extractor, LLM included, must pass this."""
    verified, rejected = [], []
    for fact in facts:
        valid = [item for item in fact.evidence if verify_evidence(raw_text, item)]
        if valid:
            verified.append(fact.model_copy(update={"evidence": valid}))
        else:
            rejected.append(fact)
    return verified, rejected


def parse_cv(pdf_bytes: bytes, cv_path: str | None = None) -> ParseResult:
    document = extract_document(pdf_bytes)
    sections = split_sections(document)

    builder = _ProfileBuilder(document)
    header = next(section for section in sections if section.name == "header")
    contact_lines = list(header.lines)
    for section in sections:
        if section.name == "contact":
            contact_lines += section.lines
    builder.add_name(contact_lines)
    builder.add_contact(contact_lines)

    for section in sections:
        if not section.lines:
            continue
        if section.name == "summary":
            builder.add(FactType.SUMMARY, section.lines[0].start, section.lines[-1].end, "summary")
        elif section.name == "education":
            builder.add_entries(FactType.EDUCATION, section)
        elif section.name == "skills":
            builder.add_skills(section.lines)
        elif section.name == "experience":
            builder.add_entries(FactType.EXPERIENCE, section)
        elif section.name == "projects":
            builder.add_entries(FactType.PROJECT, section)
        elif section.name == "certifications":
            builder.add_certifications(section)
    builder.link_skill_usage(sections)

    verified, rejected = verify_facts(document.raw_text, builder.facts)
    profile = CandidateProfile(
        cv_sha256=sha256_bytes(pdf_bytes),
        cv_path=cv_path,
        raw_text=document.raw_text,
        facts=verified,
    )
    return ParseResult(profile, rejected)


def find_gaps(profile: CandidateProfile) -> list[Gap]:
    """List missing details and claims the CV does not back up."""
    facts = [f for f in profile.facts if f.verification_status is not VerificationStatus.REJECTED]
    gaps: list[Gap] = []

    for label in ("name", "email", "phone"):
        if not any(f.type is FactType.CONTACT and f.label == label for f in facts):
            gaps.append(Gap(kind="missing_contact", message=f"No {label} was found."))

    required = {
        FactType.EDUCATION: "education",
        FactType.SKILL: "skills",
    }
    for fact_type, name in required.items():
        if not any(f.type is fact_type for f in facts):
            gaps.append(Gap(kind="missing_section", message=f"No {name} section was found."))
    if not any(f.type in (FactType.EXPERIENCE, FactType.PROJECT) for f in facts):
        gaps.append(Gap(kind="missing_section", message="No experience or projects were found."))

    parents_with_bullets = {f.parent_key for f in facts if f.parent_key}
    for fact in facts:
        if fact.type in (FactType.EXPERIENCE, FactType.EDUCATION) and not DATE_RE.search(fact.value):
            gaps.append(Gap(kind="missing_dates", message=f"No dates for: {fact.value}", fact_key=fact.key))
        elif fact.type is FactType.EXPERIENCE and fact.key not in parents_with_bullets:
            gaps.append(Gap(kind="no_details", message=f"No details for: {fact.value}", fact_key=fact.key))
        elif fact.type is FactType.ACHIEVEMENT and fact.parent_key is None:
            gaps.append(
                Gap(kind="orphan_bullet", message=f"Not linked to a role or project: {fact.value}", fact_key=fact.key)
            )
        elif fact.type is FactType.SKILL and not any(
            item.section in ("experience", "projects") or item.source_type is SourceType.USER_STATED
            for item in fact.evidence
        ):
            gaps.append(
                Gap(
                    kind="unsupported_skill",
                    message=f"'{fact.value}' is listed but not shown in any experience or project.",
                    fact_key=fact.key,
                )
            )
    return gaps


def render_evidence(pdf_path: str | Path, page: int, quote: str, zoom: float = 1.5) -> bytes:
    """Render a CV page as PNG with the quoted text highlighted."""
    with pymupdf.open(pdf_path) as doc:
        target = doc[page - 1]
        for line in quote.splitlines():
            for rect in target.search_for(line):
                target.add_highlight_annot(rect)
        return target.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True).tobytes("png")
