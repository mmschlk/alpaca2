"""
ORCID public API client (v3.0, no auth required for public records).

Fetches and parses employments, works, and peer-reviews from ORCID.
Also provides fuzzy matching helpers for linking to existing Alpaca records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

import httpx

ORCID_BASE = "https://pub.orcid.org/v3.0"
_HEADERS = {"Accept": "application/json"}

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.IGNORECASE)


def validate_orcid(raw: str) -> str | None:
    """Return cleaned ORCID iD (XXXX-XXXX-XXXX-XXXX) or None if invalid."""
    cleaned = raw.strip().upper()
    # Accept with or without dashes
    digits = re.sub(r"[-\s]", "", cleaned)
    if len(digits) == 16:
        formatted = f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:16]}"
        if ORCID_RE.match(formatted):
            return formatted
    if ORCID_RE.match(cleaned):
        return cleaned
    return None


def orcid_url(orcid: str) -> str:
    return f"https://orcid.org/{orcid}"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class OrcidEmployment:
    org_name: str
    role: str
    start_year: int | None
    end_year: int | None
    country: str


@dataclass
class OrcidWork:
    title: str
    year: int | None
    doi: str | None
    journal_name: str | None
    work_type: str  # JOURNAL_ARTICLE, CONFERENCE_PAPER, etc.


@dataclass
class OrcidReview:
    venue_name: str
    year: int | None
    role: str           # reviewer, editor, etc.
    review_group_id: str | None


@dataclass
class OrcidRecord:
    orcid: str
    display_name: str
    employments: list[OrcidEmployment] = field(default_factory=list)
    works: list[OrcidWork] = field(default_factory=list)
    reviews: list[OrcidReview] = field(default_factory=list)
    error: str | None = None


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _year(obj) -> int | None:
    try:
        return int(obj["year"]["value"]) if obj and obj.get("year") else None
    except (TypeError, KeyError, ValueError):
        return None


def _val(obj) -> str | None:
    try:
        return str(obj["value"]) if obj and obj.get("value") else None
    except (TypeError, KeyError):
        return None


# ── Main fetch ────────────────────────────────────────────────────────────────

async def fetch_orcid_record(orcid: str) -> OrcidRecord:
    """Fetch employments, works, and peer-reviews from ORCID public API."""
    display_name = orcid
    employments: list[OrcidEmployment] = []
    works: list[OrcidWork] = []
    reviews: list[OrcidReview] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            # ── Name ──────────────────────────────────────────────────────────
            r = await client.get(f"{ORCID_BASE}/{orcid}/person", headers=_HEADERS)
            if r.status_code == 200:
                data = r.json()
                nm = data.get("name") or {}
                given = _val(nm.get("given-names")) or ""
                family = _val(nm.get("family-name")) or ""
                display_name = f"{given} {family}".strip() or orcid
            elif r.status_code == 404:
                return OrcidRecord(orcid=orcid, display_name=orcid,
                                   error="ORCID record not found or not public.")

            # ── Employments ───────────────────────────────────────────────────
            r = await client.get(f"{ORCID_BASE}/{orcid}/employments", headers=_HEADERS)
            if r.status_code == 200:
                for grp in r.json().get("affiliation-group", []):
                    for sw in grp.get("summaries", []):
                        s = sw.get("employment-summary", {})
                        org = s.get("organization") or {}
                        org_name = org.get("name") or ""
                        if not org_name:
                            continue
                        employments.append(OrcidEmployment(
                            org_name=org_name,
                            role=s.get("role-title") or "",
                            start_year=_year(s.get("start-date")),
                            end_year=_year(s.get("end-date")),
                            country=(org.get("address") or {}).get("country") or "",
                        ))

            # ── Works ─────────────────────────────────────────────────────────
            r = await client.get(f"{ORCID_BASE}/{orcid}/works", headers=_HEADERS)
            if r.status_code == 200:
                seen: set[str] = set()
                for grp in r.json().get("group", []):
                    summaries = grp.get("work-summary", [])
                    if not summaries:
                        continue
                    s = summaries[0]  # preferred summary
                    title = _val((s.get("title") or {}).get("title")) or ""
                    if not title or title.lower() in seen:
                        continue
                    seen.add(title.lower())
                    doi = None
                    for eid in (s.get("external-ids") or {}).get("external-id", []):
                        if eid.get("external-id-type") == "doi":
                            doi = eid.get("external-id-value")
                            break
                    works.append(OrcidWork(
                        title=title,
                        year=_year(s.get("publication-date")),
                        doi=doi,
                        journal_name=_val(s.get("journal-title")),
                        work_type=(s.get("type") or "other").upper(),
                    ))

            # ── Peer reviews ──────────────────────────────────────────────────
            r = await client.get(f"{ORCID_BASE}/{orcid}/peer-reviews", headers=_HEADERS)
            if r.status_code == 200:
                for grp in r.json().get("group", []):
                    for s in grp.get("peer-review-summary", []):
                        org = s.get("convening-organization") or {}
                        venue = org.get("name") or s.get("review-group-id") or ""
                        if not venue:
                            continue
                        reviews.append(OrcidReview(
                            venue_name=venue,
                            year=_year(s.get("completion-date")),
                            role=(s.get("role") or "reviewer").lower().replace("_", " "),
                            review_group_id=s.get("review-group-id"),
                        ))

        except httpx.TimeoutException:
            return OrcidRecord(orcid=orcid, display_name=display_name,
                               error="Request timed out. ORCID may be slow — please try again.")
        except httpx.RequestError as exc:
            return OrcidRecord(orcid=orcid, display_name=display_name,
                               error=f"Network error: {exc}")

    return OrcidRecord(orcid=orcid, display_name=display_name,
                       employments=employments, works=works, reviews=reviews)


# ── Fuzzy matching helpers ────────────────────────────────────────────────────

def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_matches(
    query: str,
    candidates: list[str],
    threshold: float = 0.40,
    top: int = 5,
) -> list[tuple[float, int]]:
    """Return (score, index) pairs from candidates sorted best-first."""
    scored = [(_sim(query, c), i) for i, c in enumerate(candidates)]
    return sorted([(s, i) for s, i in scored if s >= threshold], reverse=True)[:top]


def top_match(query: str, candidates: list[str], threshold: float = 0.50) -> int | None:
    """Return index of the best match above threshold, or None."""
    hits = best_matches(query, candidates, threshold=threshold, top=1)
    return hits[0][1] if hits else None


# ── ORCID role → ServiceRole mapping ─────────────────────────────────────────

_ORCID_ROLE_MAP: dict[str, str] = {
    "reviewer": "reviewer",
    "review": "reviewer",
    "editor": "associate_editor",
    "associate editor": "associate_editor",
    "editor-in-chief": "editor_in_chief",
    "editor in chief": "editor_in_chief",
    "senior editor": "associate_editor",
    "area chair": "area_chair",
    "meta-reviewer": "area_chair",
    "senior program committee": "senior_program_committee",
    "senior pc": "senior_program_committee",
    "program chair": "program_chair",
    "workshop organizer": "workshop_organizer",
    "editorial board": "editorial_board",
    "board member": "editorial_board",
}


def map_orcid_role(role: str) -> str:
    """Map an ORCID peer-review role string to a ServiceRole value."""
    key = role.lower().strip()
    return _ORCID_ROLE_MAP.get(key, "reviewer")


# ── Work type helpers ─────────────────────────────────────────────────────────

CONFERENCE_TYPES = {"CONFERENCE_PAPER", "CONFERENCE_POSTER", "CONFERENCE_ABSTRACT"}
JOURNAL_TYPES = {"JOURNAL_ARTICLE", "REVIEW"}

def work_venue_type(work_type: str) -> str:
    """Return 'journal', 'conference', or 'other'."""
    t = work_type.upper()
    if t in JOURNAL_TYPES:
        return "journal"
    if t in CONFERENCE_TYPES:
        return "conference"
    return "other"
