"""In-memory ScimagoJR data store, populated from the annual CSV download.

Download the file at https://www.scimagojr.com/journalrank.php (click "Download data").
The file is semicolon-delimited and uses European decimal format (comma as decimal point).
"""
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

CSV_PATH = Path("static/uploads/scimago.csv")


@dataclass
class ScimagoEntry:
    source_id: str
    title: str
    issns: list[str]
    sjr: Optional[float]
    best_quartile: Optional[str]
    h_index: Optional[int]
    categories: list[tuple[str, str]]  # [(category_name, quartile), ...]
    country: str
    publisher: str

    @property
    def scimago_url(self) -> str:
        return f"https://www.scimagojr.com/journalsearch.php?q={self.source_id}&tip=sid"


# ── Module-level store ───────────────────────────────────────────────────────
_by_id: dict[str, ScimagoEntry] = {}
_by_issn: dict[str, str] = {}          # normalised ISSN → source_id
_entries: list[ScimagoEntry] = []
_meta: dict = {"loaded_at": None, "row_count": 0}


def is_loaded() -> bool:
    return bool(_entries)


def get_meta() -> dict:
    return dict(_meta)


# ── Parsers ──────────────────────────────────────────────────────────────────

def _float(val: str) -> Optional[float]:
    try:
        return float(val.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return None


def _int(val: str) -> Optional[int]:
    try:
        return int(val.strip())
    except (ValueError, AttributeError):
        return None


def _parse_issns(val: str) -> list[str]:
    return [i.strip() for i in val.split(",") if i.strip()]


def _parse_categories(raw: str) -> list[tuple[str, str]]:
    """Parse 'Foo (Q1); Bar (Q2)' → [('Foo', 'Q1'), ('Bar', 'Q2')]."""
    result = []
    for item in raw.split(";"):
        item = item.strip()
        m = re.match(r"^(.+?)\s*\((Q[1-4])\)\s*$", item)
        if m:
            result.append((m.group(1).strip(), m.group(2)))
    return result


# ── Public API ───────────────────────────────────────────────────────────────

def load(path: Path = CSV_PATH) -> int:
    """Parse the CSV and populate the in-memory store. Returns entry count."""
    global _by_id, _by_issn, _entries, _meta

    if not path.exists():
        return 0

    new_by_id: dict[str, ScimagoEntry] = {}
    new_by_issn: dict[str, str] = {}
    new_entries: list[ScimagoEntry] = []

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            sid = row.get("Sourceid", "").strip()
            if not sid:
                continue
            issns = _parse_issns(row.get("Issn", ""))
            entry = ScimagoEntry(
                source_id=sid,
                title=row.get("Title", "").strip(),
                issns=issns,
                sjr=_float(row.get("SJR", "")),
                best_quartile=row.get("SJR Best Quartile", "").strip() or None,
                h_index=_int(row.get("H index", "")),
                categories=_parse_categories(row.get("Categories", "")),
                country=row.get("Country", "").strip(),
                publisher=row.get("Publisher", "").strip(),
            )
            new_by_id[sid] = entry
            for issn in issns:
                new_by_issn[issn.replace("-", "")] = sid
            new_entries.append(entry)

    _by_id = new_by_id
    _by_issn = new_by_issn
    _entries = new_entries
    _meta = {"loaded_at": datetime.now(), "row_count": len(new_entries)}
    return len(new_entries)


def lookup_by_id(source_id: str) -> Optional[ScimagoEntry]:
    return _by_id.get(str(source_id).strip())


def lookup_by_issn(issn: str) -> Optional[ScimagoEntry]:
    sid = _by_issn.get(issn.replace("-", ""))
    return _by_id.get(sid) if sid else None


def search(query: str, limit: int = 10) -> list[ScimagoEntry]:
    """Substring search on title; prefix matches rank first."""
    q = query.lower().strip()
    if not q or not _entries:
        return []
    hits = [e for e in _entries if q in e.title.lower()]
    hits.sort(key=lambda e: (not e.title.lower().startswith(q), e.title.lower()))
    return hits[:limit]
