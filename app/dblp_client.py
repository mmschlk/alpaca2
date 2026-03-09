"""
DBLP API client (no auth required for public records).

Fetches author info and publications from DBLP.
Also provides helpers for linking to existing Alpaca records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

DBLP_AUTHOR_API = "https://dblp.org/search/author/api"
DBLP_PUBL_API = "https://dblp.org/search/publ/api"
_HEADERS = {"Accept": "application/json"}

_PID_RE = re.compile(r'dblp\.(?:org|uni-trier\.de)/(?:pid|homepages)/(.+?)(?:\.html?|\.xml)?$')


def extract_dblp_pid(url_or_pid: str) -> str | None:
    """Extract DBLP PID from a profile URL, or return as-is if it looks like a PID."""
    s = url_or_pid.strip().rstrip("/")
    m = _PID_RE.search(s)
    if m:
        return m.group(1)
    # Raw PID: contains a slash, no http
    if "/" in s and not s.startswith("http"):
        return s
    return None


def dblp_url(pid: str) -> str:
    return f"https://dblp.org/pid/{pid}"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DblpAuthorHit:
    pid: str
    name: str
    url: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class DblpCoAuthor:
    name: str
    pid: Optional[str]


@dataclass
class DblpWork:
    title: str
    year: Optional[int]
    venue: Optional[str]
    work_type: str          # "conference" | "journal" | "other"
    doi: Optional[str]
    dblp_key: str
    co_authors: list[DblpCoAuthor] = field(default_factory=list)


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _work_type(info: dict) -> str:
    t = (info.get("type") or "").lower()
    if "conference" in t or "workshop" in t:
        return "conference"
    if "journal" in t:
        return "journal"
    return "other"


def _doi_from_ee(ee) -> str | None:
    if not ee:
        return None
    urls = ee if isinstance(ee, list) else [ee]
    for u in urls:
        if "doi.org/" in u:
            return u.split("doi.org/", 1)[-1]
    return None


def _parse_authors(info: dict, main_pid: str | None) -> list[DblpCoAuthor]:
    raw = info.get("authors", {}).get("author", [])
    if isinstance(raw, dict):
        raw = [raw]
    result = []
    for a in raw:
        pid = a.get("@pid") or None
        name = a.get("text") or ""
        if name and pid != main_pid:
            result.append(DblpCoAuthor(name=name, pid=pid))
    return result


# ── API calls ─────────────────────────────────────────────────────────────────

async def search_dblp_authors(query: str, limit: int = 10) -> list[DblpAuthorHit]:
    """Search for DBLP authors by name."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(
                DBLP_AUTHOR_API,
                params={"q": query, "format": "json", "h": limit},
                headers=_HEADERS,
            )
        except httpx.RequestError:
            return []
        if r.status_code != 200:
            return []

        hits = r.json().get("result", {}).get("hits", {}).get("hit") or []
        results = []
        for h in hits:
            info = h.get("info", {})
            url = info.get("url") or ""
            pid = extract_dblp_pid(url) or url
            aliases_raw = (info.get("aliases") or {}).get("alias") or []
            aliases = [aliases_raw] if isinstance(aliases_raw, str) else list(aliases_raw)
            results.append(DblpAuthorHit(
                pid=pid,
                name=info.get("author") or "",
                url=url,
                aliases=aliases,
            ))
        return results


async def fetch_dblp_works(
    author_name: str,
    author_pid: str | None = None,
    limit: int = 500,
) -> tuple[list[DblpWork], str | None]:
    """Fetch all publications for a DBLP author by their canonical name.

    Returns (works, error_message). Works are deduplicated by lowercased title.
    """
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            r = await client.get(
                DBLP_PUBL_API,
                params={
                    "q": f"author:{author_name}:",
                    "format": "json",
                    "h": limit,
                    "c": "0",
                },
                headers=_HEADERS,
            )
        except httpx.TimeoutException:
            return [], "Request timed out. DBLP may be slow — please try again."
        except httpx.RequestError as exc:
            return [], f"Network error: {exc}"

        if r.status_code != 200:
            return [], f"DBLP returned status {r.status_code}."

        hits = r.json().get("result", {}).get("hits", {}).get("hit") or []
        works = []
        seen: set[str] = set()
        for h in hits:
            info = h.get("info", {})
            title = info.get("title") or ""
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            year_str = info.get("year")
            year = int(year_str) if year_str and str(year_str).isdigit() else None
            works.append(DblpWork(
                title=title,
                year=year,
                venue=info.get("venue") or None,
                work_type=_work_type(info),
                doi=_doi_from_ee(info.get("ee")),
                dblp_key=info.get("key") or "",
                co_authors=_parse_authors(info, author_pid),
            ))
        return works, None
