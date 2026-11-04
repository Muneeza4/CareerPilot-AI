"""Storage for the candidate profile. Shares the Repository connection and schema."""

from __future__ import annotations

import json

from models.schemas import (
    CandidateProfile,
    Evidence,
    Fact,
    FactType,
    Preferences,
    RemotePreference,
    SourceType,
    VerificationStatus,
)

# The first release is single-user, so every profile belongs to this candidate.
CANDIDATE_ID = 1


def _normalized(value: str) -> str:
    return " ".join(value.lower().split())


class ProfileStore:
    def __init__(self, conn):
        self.conn = conn

    def _ensure_candidate(self) -> None:
        self.conn.execute("INSERT OR IGNORE INTO candidate (id) VALUES (?)", (CANDIDATE_ID,))

    def save_profile(self, profile: CandidateProfile) -> int:
        """Make this CV the active profile. Re-uploading a known CV keeps its reviews and edits."""
        existing = self.conn.execute(
            "SELECT id FROM profile_version WHERE candidate_id = ? AND cv_sha256 = ?",
            (CANDIDATE_ID, profile.cv_sha256),
        ).fetchone()

        with self.conn:
            self._ensure_candidate()
            if existing:
                version_id = existing["id"]
                self._refresh_facts(version_id, profile)
            else:
                version_id = self.conn.execute(
                    "INSERT INTO profile_version (candidate_id, cv_sha256, cv_path, raw_text) VALUES (?, ?, ?, ?)",
                    (CANDIDATE_ID, profile.cv_sha256, profile.cv_path, profile.raw_text),
                ).lastrowid
                for fact in profile.facts:
                    self._insert_fact(version_id, fact)
            self.conn.execute(
                "UPDATE candidate SET name = ?, email = ?, active_version_id = ? WHERE id = ?",
                (profile.contact("name"), profile.contact("email"), version_id, CANDIDATE_ID),
            )
        return version_id

    def _refresh_facts(self, version_id: int, profile: CandidateProfile) -> None:
        """Re-read a known CV. A fact with the same source text keeps its review; a new one needs review."""
        rows = self.conn.execute(
            """SELECT f.id, f.value, f.verification_status,
                      (SELECT e.quote FROM evidence e WHERE e.fact_id = f.id AND e.source_type = 'cv'
                       ORDER BY e.id LIMIT 1) AS quote
               FROM fact f WHERE f.profile_version_id = ?""",
            (version_id,),
        ).fetchall()
        reviewed = {_normalized(row["quote"]): row for row in rows if row["quote"]}

        for row in reviewed.values():
            self.conn.execute("DELETE FROM evidence WHERE fact_id = ?", (row["id"],))
            self.conn.execute("DELETE FROM fact WHERE id = ?", (row["id"],))
        self.conn.execute(
            "UPDATE profile_version SET raw_text = ?, cv_path = ? WHERE id = ?",
            (profile.raw_text, profile.cv_path, version_id),
        )
        for fact in profile.facts:
            earlier = reviewed.get(_normalized(fact.evidence[0].quote))
            if earlier:
                fact = fact.model_copy(
                    update={
                        "value": earlier["value"],
                        "normalized_value": _normalized(earlier["value"]),
                        "verification_status": VerificationStatus(earlier["verification_status"]),
                    }
                )
            self._insert_fact(version_id, fact)

    def _insert_fact(self, version_id: int, fact: Fact) -> int:
        fact_id = self.conn.execute(
            """INSERT INTO fact (profile_version_id, key, parent_key, type, label, value,
                                 normalized_value, verification_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                fact.key,
                fact.parent_key,
                fact.type.value,
                fact.label,
                fact.value,
                fact.normalized_value,
                fact.verification_status.value,
            ),
        ).lastrowid
        for item in fact.evidence:
            self.conn.execute(
                """INSERT INTO evidence (fact_id, source_type, section, page, char_start, char_end, quote)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    fact_id,
                    item.source_type.value,
                    item.section,
                    item.page,
                    item.char_start,
                    item.char_end,
                    item.quote,
                ),
            )
        return fact_id

    def load_profile(self) -> CandidateProfile | None:
        row = self.conn.execute(
            """SELECT v.* FROM profile_version v JOIN candidate c ON c.active_version_id = v.id
               WHERE c.id = ?""",
            (CANDIDATE_ID,),
        ).fetchone()
        if row is None:
            return None

        evidence_by_fact: dict[int, list[Evidence]] = {}
        for item in self.conn.execute(
            """SELECT e.* FROM evidence e JOIN fact f ON f.id = e.fact_id
               WHERE f.profile_version_id = ? ORDER BY e.id""",
            (row["id"],),
        ):
            evidence_by_fact.setdefault(item["fact_id"], []).append(_to_evidence(item))

        facts = [
            _to_fact(item, evidence_by_fact.get(item["id"], []))
            for item in self.conn.execute(
                "SELECT * FROM fact WHERE profile_version_id = ? ORDER BY id", (row["id"],)
            )
        ]
        return CandidateProfile(
            version_id=row["id"],
            cv_sha256=row["cv_sha256"],
            cv_path=row["cv_path"],
            raw_text=row["raw_text"],
            facts=facts,
        )

    def get_fact(self, fact_id: int) -> Fact | None:
        row = self.conn.execute("SELECT * FROM fact WHERE id = ?", (fact_id,)).fetchone()
        if row is None:
            return None
        evidence = self.conn.execute("SELECT * FROM evidence WHERE fact_id = ? ORDER BY id", (fact_id,))
        return _to_fact(row, [_to_evidence(item) for item in evidence])

    def update_fact(
        self,
        fact_id: int,
        *,
        value: str | None = None,
        status: VerificationStatus | None = None,
    ) -> None:
        with self.conn:
            if value is not None:
                self.conn.execute(
                    "UPDATE fact SET value = ?, normalized_value = ? WHERE id = ?",
                    (value, _normalized(value), fact_id),
                )
            if status is not None:
                self.conn.execute(
                    "UPDATE fact SET verification_status = ? WHERE id = ?", (status.value, fact_id)
                )

    def add_user_fact(self, version_id: int, fact_type: FactType, value: str) -> int:
        """Add something the user stated directly. It carries user_stated evidence, not a CV quote."""
        next_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM fact").fetchone()[0]
        fact = Fact(
            key=f"user-{next_id}",
            type=fact_type,
            value=value,
            normalized_value=_normalized(value),
            verification_status=VerificationStatus.CONFIRMED,
            evidence=[Evidence(source_type=SourceType.USER_STATED, quote=value)],
        )
        with self.conn:
            return self._insert_fact(version_id, fact)

    def save_preferences(self, preferences: Preferences) -> None:
        with self.conn:
            self._ensure_candidate()
            self.conn.execute(
                """INSERT INTO preferences (candidate_id, target_roles, locations, remote_preference,
                                            salary_min, salary_currency)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (candidate_id) DO UPDATE SET
                       target_roles = excluded.target_roles,
                       locations = excluded.locations,
                       remote_preference = excluded.remote_preference,
                       salary_min = excluded.salary_min,
                       salary_currency = excluded.salary_currency""",
                (
                    CANDIDATE_ID,
                    json.dumps(preferences.target_roles),
                    json.dumps(preferences.locations),
                    preferences.remote_preference.value,
                    preferences.salary_min,
                    preferences.salary_currency,
                ),
            )

    def load_preferences(self) -> Preferences:
        row = self.conn.execute(
            "SELECT * FROM preferences WHERE candidate_id = ?", (CANDIDATE_ID,)
        ).fetchone()
        if row is None:
            return Preferences()
        return Preferences(
            target_roles=json.loads(row["target_roles"]),
            locations=json.loads(row["locations"]),
            remote_preference=RemotePreference(row["remote_preference"]),
            salary_min=row["salary_min"],
            salary_currency=row["salary_currency"],
        )


def _to_evidence(row) -> Evidence:
    return Evidence(
        source_type=SourceType(row["source_type"]),
        section=row["section"],
        page=row["page"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        quote=row["quote"],
    )


def _to_fact(row, evidence: list[Evidence]) -> Fact:
    return Fact(
        id=row["id"],
        key=row["key"],
        parent_key=row["parent_key"],
        type=FactType(row["type"]),
        label=row["label"],
        value=row["value"],
        normalized_value=row["normalized_value"],
        verification_status=VerificationStatus(row["verification_status"]),
        evidence=evidence,
    )
