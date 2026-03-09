"""
BibTeX utilities — parsing, author formatting, proceedings cleaning, and rendering.
"""
from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

import bibtexparser

if TYPE_CHECKING:
    from app.models.bibtex import BibEntry
    from app.models.paper import PaperProject

# ── Style defaults ────────────────────────────────────────────────────────────

DEFAULT_STYLE: dict = {
    "author_format": "full",       # "full" | "abbreviated" | "last_only"
    "max_authors": 0,              # 0 = all; N > 0 = show N then "et al."
    "include_doi": True,
    "include_url": False,
    "include_abstract": False,
    "use_crossref": False,
    "clean_proceedings": True,
}


def merge_style(saved: dict | None) -> dict:
    return {**DEFAULT_STYLE, **(saved or {})}


# ── Author formatting ─────────────────────────────────────────────────────────

# Compound last-name particles (lowercase connectors that are part of the last name)
_PARTICLES = {"van", "von", "de", "del", "della", "di", "du", "la", "le", "ten", "ter", "den"}


def _split_last_first(name: str) -> tuple[str, str]:
    """Split a single author name into (last, first).
    Handles 'Last, First', 'First Last', and braced groups like '{CERN}' or '{van den Berg}'.
    """
    name = name.strip()
    if "," in name:
        # "Last, First" or "Last, First Middle"
        parts = name.split(",", 1)
        return parts[0].strip(), parts[1].strip()
    # "First Last" — last word is last name, respecting particles
    tokens = name.split()
    if len(tokens) == 1:
        return tokens[0], ""
    # Collect trailing particle+last (e.g. "Mark van den Berg" → last="van den Berg")
    last_start = len(tokens) - 1
    while last_start > 0 and tokens[last_start - 1].lower() in _PARTICLES:
        last_start -= 1
    last = " ".join(tokens[last_start:])
    first = " ".join(tokens[:last_start])
    return last, first


def _abbreviate_first(first: str) -> str:
    """'John Michael' → 'J. M.'"""
    if not first:
        return ""
    parts = first.split()
    abbr = []
    for p in parts:
        clean = p.strip("{}.")
        if clean:
            abbr.append(clean[0].upper() + ".")
    return " ".join(abbr)


def format_single_author(name: str, fmt: str) -> str:
    """Format one author name string.
    fmt: 'full' | 'abbreviated' | 'last_only'
    Returns name in 'Last, First' style (or just 'Last').
    """
    name = name.strip()
    if not name:
        return name
    # Preserve fully-braced tokens like {CERN} or {{The Python Foundation}}
    if name.startswith("{") and name.endswith("}"):
        return name

    last, first = _split_last_first(name)

    if fmt == "last_only":
        return last
    if fmt == "abbreviated":
        abbr = _abbreviate_first(first)
        return f"{last}, {abbr}" if abbr else last
    # "full"
    return f"{last}, {first}" if first else last


def parse_author_list(raw: str) -> list[str]:
    """Split a BibTeX author field on ' and ' (respecting braces).
    Normalises whitespace first so 'and\\n' line-breaks don't cause mis-parses.
    """
    # Collapse all whitespace runs (incl. newlines) to a single space
    raw = re.sub(r"\s+", " ", raw).strip()
    authors = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "{":
            depth += 1
            current.append(c)
        elif c == "}":
            depth -= 1
            current.append(c)
        elif depth == 0 and raw[i : i + 5].lower() == " and ":
            authors.append("".join(current).strip())
            current = []
            i += 5
            continue
        else:
            current.append(c)
        i += 1
    if current:
        authors.append("".join(current).strip())
    return [a for a in authors if a]


def format_author_field(raw: str, style: dict) -> str:
    """Apply author_format and max_authors to a raw BibTeX author string."""
    if not raw:
        return raw
    fmt = style.get("author_format", "full")
    max_a = int(style.get("max_authors", 0))
    authors = parse_author_list(raw)
    if max_a > 0 and len(authors) > max_a:
        shown = authors[:max_a]
        formatted = [format_single_author(a, fmt) for a in shown]
        return " and ".join(formatted) + " and others"
    return " and ".join(format_single_author(a, fmt) for a in authors)


# ── Proceedings name cleaning ─────────────────────────────────────────────────

_PROC_PREFIX = re.compile(
    r"^(?:In\s+)?(?:Proceedings\s+of\s+(?:the\s+)?|"
    r"Proc\.\s+(?:of\s+(?:the\s+)?)?|"
    r"Advances\s+in\s+|"
    r"Workshop\s+on\s+)",
    re.IGNORECASE,
)


def clean_venue_name(name: str) -> str:
    return _PROC_PREFIX.sub("", name).strip()


# ── Cite-key generation ───────────────────────────────────────────────────────

_STOP_WORDS = {"the", "a", "an", "of", "in", "on", "at", "for", "and", "or", "with", "to", "its"}


def _venue_abbrev(entry_type: str, fields_json: dict) -> str:
    """Extract a short venue abbreviation for cite-key generation.

    Format priority:
    1. arXiv / CoRR journal → "arxiv"
    2. All-uppercase word(s) in the cleaned venue name (e.g. ICML, NeurIPS is handled below)
    3. CamelCase acronym heuristic (uppercase letters from each word)
    4. Initials of significant words
    """
    fields = fields_json or {}

    # arXiv / CoRR
    journal = fields.get("journal", "")
    if re.search(r"\b(?:corr|arxiv)\b", journal, re.IGNORECASE):
        return "arxiv"
    eprint = fields.get("eprint", fields.get("archivePrefix", ""))
    if "arxiv" in eprint.lower():
        return "arxiv"

    # Choose the venue text
    if entry_type in ("inproceedings", "proceedings"):
        venue_text = clean_venue_name(fields.get("booktitle", ""))
    else:
        venue_text = fields.get("journal", "")

    if not venue_text:
        return "misc"

    # 1) Find an all-uppercase token ≥ 2 chars (e.g. "ICML", "AAAI", "ICLR")
    #    Skip pure year-like numbers
    tokens = re.findall(r"\b([A-Z][A-Z0-9]{1,7})\b", venue_text)
    tokens = [t for t in tokens if not t.isdigit()]
    if tokens:
        return tokens[0].lower()

    # 2) CamelCase token: extract uppercase letters (e.g. "NeurIPS" → "nips")
    camel = re.search(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b", venue_text)
    if camel:
        word = camel.group(1)
        abbr = "".join(c for c in word if c.isupper()).lower()
        if len(abbr) >= 2:
            return abbr

    # 3) Initials of significant words (drop stop words, ordinal numbers)
    words = [
        w for w in venue_text.split()
        if w.lower() not in _STOP_WORDS and w[0].isalpha() and not re.match(r"^\d", w)
    ]
    if words:
        return "".join(w[0].lower() for w in words[:6])

    return "misc"


def generate_cite_key(
    entry_type: str,
    authors_raw: str | None,
    year: int | None,
    fields_json: dict | None,
    existing_keys: set[str],
) -> str:
    """Generate a cite key: {lastname}-{venue}{yy}{letter}.

    Letter starts at 'a' and increments for disambiguation within the existing_keys set.
    For arXiv/CoRR papers venue is always 'arxiv'.
    """
    # Last name of first author
    lastname = "unknown"
    if authors_raw:
        authors = parse_author_list(authors_raw)
        if authors:
            last, _ = _split_last_first(authors[0])
            slug = _slugify(last)
            if slug:
                lastname = slug.lower()

    venue = _venue_abbrev(entry_type, fields_json or {})
    yy = str(year)[-2:] if year else ""
    base = f"{lastname}-{venue}{yy}"

    for letter in "abcdefghijklmnopqrstuvwxyz":
        key = f"{base}{letter}"
        if key not in existing_keys:
            return key

    # Extremely unlikely: all 26 letters taken
    for n in range(2, 1000):
        key = f"{base}{n}"
        if key not in existing_keys:
            return key

    return base


# ── BibTeX entry rendering ────────────────────────────────────────────────────

# Preferred field order for output
_FIELD_ORDER = [
    "author", "title", "booktitle", "journal", "year", "volume", "number",
    "pages", "publisher", "address", "editor", "series", "edition",
    "month", "doi", "url", "isbn", "issn", "note", "abstract",
    "crossref",
]


def _brace(value: str) -> str:
    """Wrap value in braces if not already wrapped."""
    v = value.strip()
    if v.startswith("{") and v.endswith("}"):
        return v
    return "{" + v + "}"


def render_entry(
    entry: "BibEntry",
    style: dict,
    crossref_map: dict[str, str] | None = None,
) -> str:
    """Render a BibEntry to a BibTeX string with style applied.

    crossref_map: maps (clean_booktitle, year) → proceedings_cite_key.
    When use_crossref=True and entry is @inproceedings with a match, the
    booktitle/publisher/address/year fields are replaced by crossref={key}.
    """
    fields: dict[str, str] = {}

    # Author
    if entry.authors_raw:
        fields["author"] = format_author_field(entry.authors_raw, style)

    # Title
    if entry.title:
        fields["title"] = entry.title

    # Extra fields from JSON
    extra: dict = entry.fields_json or {}

    # Determine crossref substitution
    use_crossref_for_this = False
    crossref_key = None
    if (
        crossref_map
        and entry.entry_type == "inproceedings"
        and "booktitle" in extra
    ):
        booktitle = extra.get("booktitle", "")
        cleaned_bt = clean_venue_name(booktitle) if style.get("clean_proceedings") else booktitle
        year = str(entry.year or extra.get("year", ""))
        lookup = (cleaned_bt.lower(), year)
        if lookup in crossref_map:
            crossref_key = crossref_map[lookup]
            use_crossref_for_this = True

    crossref_skip = {"booktitle", "publisher", "address", "year"} if use_crossref_for_this else set()

    for k, v in extra.items():
        if not v:
            continue
        k_lower = k.lower()
        if k_lower in crossref_skip:
            continue
        if k_lower == "doi" and not style.get("include_doi"):
            continue
        if k_lower == "url" and not style.get("include_url"):
            continue
        if k_lower == "abstract" and not style.get("include_abstract"):
            continue
        val = v.strip()
        if k_lower == "booktitle" and style.get("clean_proceedings"):
            val = clean_venue_name(val)
        fields[k_lower] = val

    # Year (at top level takes precedence)
    if entry.year and "year" not in fields and not use_crossref_for_this:
        fields["year"] = str(entry.year)

    if use_crossref_for_this:
        fields["crossref"] = crossref_key

    # Sort fields in preferred order
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for k in _FIELD_ORDER:
        if k in fields:
            ordered.append((k, fields[k]))
            seen.add(k)
    for k, v in fields.items():
        if k not in seen:
            ordered.append((k, v))

    lines = [f"@{entry.entry_type}{{{entry.cite_key},"]
    for k, v in ordered:
        lines.append(f"  {k:<14} = {_brace(v)},")
    lines.append("}")
    return "\n".join(lines)


def render_collection(entries: list["BibEntry"], style: dict) -> str:
    """Render all entries to a .bib file string."""
    if not entries:
        return "% Empty collection\n"

    output_parts: list[str] = []
    crossref_map: dict[tuple[str, str], str] = {}

    if style.get("use_crossref"):
        # Group @inproceedings by (clean_booktitle, year) to build @proceedings stubs
        from collections import defaultdict
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for e in entries:
            if e.entry_type == "inproceedings":
                extra = e.fields_json or {}
                bt = extra.get("booktitle", "")
                if bt:
                    clean_bt = clean_venue_name(bt) if style.get("clean_proceedings") else bt
                    year = str(e.year or extra.get("year", ""))
                    groups[(clean_bt.lower(), year)].append((clean_bt, year, extra))

        for (bt_lower, year), items in groups.items():
            clean_bt, yr, sample_extra = items[0]
            # Generate a proceedings cite key: e.g. "ICML2023"
            abbr = re.sub(r"[^A-Za-z0-9]", "", clean_bt)[:20]
            proc_key = f"{abbr}{yr}"
            crossref_map[(bt_lower, year)] = proc_key

            # Build @proceedings entry
            proc_fields: list[str] = [f"  {'booktitle':<14} = {_brace(clean_bt)},"]
            if yr:
                proc_fields.append(f"  {'year':<14} = {_brace(yr)},")
            for f_key in ("publisher", "address", "editor", "series"):
                val = sample_extra.get(f_key, "")
                if val:
                    proc_fields.append(f"  {f_key:<14} = {_brace(val)},")
            output_parts.append(
                "@proceedings{" + proc_key + ",\n" + "\n".join(proc_fields) + "\n}"
            )

        output_parts.append("")  # blank line after proceedings block

    for entry in entries:
        output_parts.append(render_entry(entry, style, crossref_map or None))

    return "\n\n".join(p for p in output_parts if p is not None) + "\n"


# ── BibTeX parsing (bibtexparser v1) ─────────────────────────────────────────

def parse_bibtex_string(raw: str) -> tuple[list[dict], list[str]]:
    """Parse a BibTeX string using bibtexparser v1.

    Returns (entries, warnings) where each entry is:
    {type, key, title, year, authors_raw, fields: {field: value, ...}}
    Warnings list any entries that were skipped.
    """
    try:
        db = bibtexparser.loads(raw)
    except Exception as exc:
        return [], [f"Parse error: {exc}"]

    results = []
    warnings = []
    for raw_entry in db.entries:
        etype = raw_entry.get("ENTRYTYPE", "misc").lower()
        key = raw_entry.get("ID", "")
        if not key:
            warnings.append("Skipped entry with no cite key.")
            continue

        title = raw_entry.get("title", "").strip()
        year_str = raw_entry.get("year", "").strip()
        try:
            year = int(year_str) if year_str.isdigit() else None
        except (ValueError, AttributeError):
            year = None

        authors_raw = raw_entry.get("author", "").strip() or None

        # All remaining fields (excluding ENTRYTYPE, ID, title, year, author)
        skip = {"ENTRYTYPE", "ID", "title", "year", "author"}
        fields = {k: str(v).strip() for k, v in raw_entry.items() if k not in skip and v}

        results.append({
            "type": etype,
            "key": key,
            "title": title or None,
            "year": year,
            "authors_raw": authors_raw,
            "fields": fields,
        })

    return results, warnings


# ── Import from PaperProject ──────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Create a safe ASCII slug for cite keys."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]", "", text)


def paper_to_entry_dict(paper: "PaperProject") -> dict:
    """Convert a PaperProject to a BibTeX entry dict ready for insertion.

    Strategy:
    - Accepted/published journal submission → @article
    - Conference submission → @inproceedings
    - Otherwise → @misc
    """
    from app.models.paper import PaperStatus, SubmissionStatus

    title = paper.title or ""
    year = paper.published_date.year if paper.published_date else None

    # Authors from paper_authors (sorted by position)
    author_names = []
    for pa in sorted(paper.paper_authors, key=lambda x: x.position):
        a = pa.author
        author_names.append(f"{a.last_name}, {a.given_name}")
    authors_raw = " and ".join(author_names) if author_names else None

    # Generate cite key: FirstAuthorLastYearN
    base_key = ""
    if author_names:
        first_last = author_names[0].split(",")[0].strip()
        base_key = _slugify(first_last)
    base_key += str(year) if year else ""
    cite_key = base_key or "entry"

    fields: dict = {}

    # Determine type from submissions
    entry_type = "misc"
    accepted_statuses = {SubmissionStatus.accepted}
    published = paper.status in (PaperStatus.accepted, PaperStatus.published)

    # Journal: pick accepted/published submission first
    j_sub = next(
        (s for s in paper.journal_submissions if s.status in accepted_statuses),
        next(iter(paper.journal_submissions), None),
    )
    c_sub = next(
        (s for s in paper.conference_submissions if s.status in accepted_statuses),
        next(iter(paper.conference_submissions), None),
    )

    if j_sub and (j_sub.status in accepted_statuses or published):
        entry_type = "article"
        j = j_sub.journal
        if j:
            fields["journal"] = j.name
        if j_sub.special_issue:
            fields["note"] = f"Special issue: {j_sub.special_issue.title}"
    elif c_sub:
        entry_type = "inproceedings"
        edition = c_sub.edition if hasattr(c_sub, "edition") else None
        if edition and edition.conference:
            conf = edition.conference
            fields["booktitle"] = f"Proceedings of {conf.name}"
            if not year and edition.year:
                year = edition.year
    elif paper.status in (PaperStatus.published, PaperStatus.accepted):
        entry_type = "article"

    if year:
        fields["year"] = str(year)

    # DOI / URL from paper fields
    if paper.google_scholar_paper_id:
        fields["note"] = fields.get("note", "") or f"Google Scholar: {paper.google_scholar_paper_id}"

    return {
        "type": entry_type,
        "key": cite_key,
        "title": title or None,
        "year": year,
        "authors_raw": authors_raw,
        "fields": fields,
    }
