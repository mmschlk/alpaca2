"""
Scholar data ingestion router.
Provides endpoints to accept crawler output (from the Google Scholar crawler)
and to display scholar data.
"""
import json
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.author import Author
from app.models.paper import PaperAuthor, PaperProject, PaperStatus
from app.models.scholar import ScholarAuthorSnapshot, ScholarPaperSnapshot

router = APIRouter(prefix="/scholar", tags=["scholar"])


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": None, **kw}


# ── Ingestion API ──────────────────────────────────────────────────────────────

@router.post("/ingest/author/{author_id}", response_class=JSONResponse)
async def ingest_author_stats(
    request: Request,
    author_id: int,
    snap_date: str = Form(default=""),
    citations: str = Form(default=""),
    h_index: str = Form(default=""),
    i10_index: str = Form(default=""),
    gs_entries: str = Form(default=""),
    current_year_citations: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """
    Accept a Google Scholar author stats snapshot.
    Can also be called with JSON body:
      {"citations": "302", "h-index": "7", "i10-index": "7", "gs_entries": 21, "current_year_citations": "14"}
    """
    # Accept both form and JSON
    body = None
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        snap_date = body.get("date", snap_date)
        citations = str(body.get("citations", citations))
        h_index = str(body.get("h-index", h_index))
        i10_index = str(body.get("i10-index", i10_index))
        gs_entries = str(body.get("gs_entries", gs_entries))
        current_year_citations = str(body.get("current_year_citations", current_year_citations))

    record_date = date.fromisoformat(snap_date) if snap_date else date.today()

    def _int(v):
        try:
            return int(v) if v else None
        except (ValueError, TypeError):
            return None

    snap = ScholarAuthorSnapshot(
        author_id=author_id,
        date=record_date,
        citations=_int(citations),
        h_index=_int(h_index),
        i10_index=_int(i10_index),
        gs_entries=_int(gs_entries),
        current_year_citations=_int(current_year_citations),
    )
    db.add(snap)
    await db.commit()
    return {"status": "ok", "id": snap.id}


@router.post("/ingest/papers/{author_id}", response_class=JSONResponse)
async def ingest_author_papers(
    request: Request,
    author_id: int,
    snap_date: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """
    Accept a JSON list of papers scraped from an author's Google Scholar profile.
    POST with Content-Type: application/json and body = the array from the crawler.
    """
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        papers = await request.json()
    else:
        raw = await request.body()
        papers = json.loads(raw)

    record_date = date.fromisoformat(snap_date) if snap_date else date.today()

    def _int(v):
        try:
            return int(v) if v else None
        except (ValueError, TypeError):
            return None

    # Try to match paper to an existing PaperProject by google_scholar_paper_id
    created = 0
    for entry in papers:
        gs_paper_id = entry.get("paper_id", "")
        if not gs_paper_id:
            continue
        # Look up linked paper project
        paper_result = await db.execute(
            select(PaperProject).where(PaperProject.google_scholar_paper_id == gs_paper_id)
        )
        paper = paper_result.scalar_one_or_none()
        snap = ScholarPaperSnapshot(
            paper_id=paper.id if paper else None,
            gs_paper_id=gs_paper_id,
            date=record_date,
            num_citations=_int(entry.get("num_citations")),
            title=entry.get("paper_title", "")[:512],
            year=str(entry.get("year", ""))[:8],
            venue=re.sub(r"<[^>]+>", "", str(entry.get("venue", "")))[:512],
            author_list=entry.get("author_list", ""),
        )
        db.add(snap)
        created += 1

    await db.commit()
    return {"status": "ok", "created": created}


# ── UI views ───────────────────────────────────────────────────────────────────

@router.get("/authors/{author_id}", response_class=HTMLResponse)
async def scholar_author_history(
    request: Request, author_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    author = (await db.execute(select(Author).where(Author.id == author_id))).scalar_one_or_none()
    if not author:
        return RedirectResponse("/authors", 302)
    snaps = (await db.execute(
        select(ScholarAuthorSnapshot)
        .where(ScholarAuthorSnapshot.author_id == author_id)
        .order_by(ScholarAuthorSnapshot.date.desc())
    )).scalars().all()
    return templates.TemplateResponse(
        request, "scholar/author_history.html",
        _ctx(request, current_user, author=author, snapshots=snaps),
    )


# ── GS import ─────────────────────────────────────────────────────────────────

_GS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


async def _scrape_gs_papers(gs_user_id: str) -> list[dict]:
    """Fetch and parse the paper list from a Google Scholar author profile."""
    url = (
        f"https://scholar.google.com/citations"
        f"?user={gs_user_id}&sortby=pubdate&pagesize=100"
    )
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_GS_HEADERS) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    papers = []
    for row in soup.select("tr.gsc_a_tr"):
        title_el = row.select_one("a.gsc_a_at")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        gs_paper_id = href.split("citation_for_view=")[-1] if "citation_for_view=" in href else None

        gray = row.select("div.gs_gray")
        author_list = gray[0].get_text(strip=True) if gray else ""
        venue = gray[1].get_text(strip=True) if len(gray) > 1 else ""

        year_el = row.select_one("span.gsc_a_h")
        year = year_el.get_text(strip=True) if year_el else ""

        papers.append({
            "title": title,
            "gs_paper_id": gs_paper_id,
            "author_list": author_list,
            "venue": venue,
            "year": year,
        })
    return papers


@router.get("/import", response_class=HTMLResponse)
async def import_papers_page(
    request: Request,
    author_id: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    authors = (await db.execute(
        select(Author)
        .where(Author.google_scholar_id.isnot(None))
        .order_by(Author.last_name, Author.given_name)
    )).scalars().all()
    selected_author = None
    if author_id:
        selected_author = (await db.execute(
            select(Author).where(Author.id == author_id)
        )).scalar_one_or_none()
    return templates.TemplateResponse(
        request, "scholar/import.html",
        _ctx(request, current_user, authors=authors, selected_author=selected_author),
    )


@router.get("/import/preview", response_class=HTMLResponse)
async def import_papers_preview(
    request: Request,
    gs_id: str = "",
    author_id: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """HTMX: fetch GS profile and return paper checklist."""
    if not current_user or not gs_id:
        return HTMLResponse("")

    try:
        papers = await _scrape_gs_papers(gs_id)
    except Exception as exc:
        return HTMLResponse(
            f'<div class="alert alert-danger">Failed to fetch Google Scholar profile: {exc}</div>'
        )

    if not papers:
        return HTMLResponse('<div class="alert alert-warning">No papers found on this profile.</div>')

    # Find which gs_paper_ids are already imported
    existing_ids = {
        r[0] for r in (await db.execute(
            select(PaperProject.google_scholar_paper_id)
            .where(PaperProject.google_scholar_paper_id.isnot(None))
        )).all()
    }

    new_papers = [p for p in papers if p["gs_paper_id"] not in existing_ids]
    already_imported = [p for p in papers if p["gs_paper_id"] in existing_ids]

    return templates.TemplateResponse(
        request, "scholar/import_preview.html",
        _ctx(request, current_user,
             new_papers=new_papers,
             already_imported=already_imported,
             gs_id=gs_id,
             author_id=author_id),
    )


def _parse_gs_author_list(author_list_str: str) -> list[tuple[str, str]]:
    """Parse a GS author string like 'F Hutter, M Feurer, J Smith' into
    a list of (given_name, last_name) tuples. Handles abbreviated first names."""
    # Remove trailing ellipsis (GS truncates long lists)
    raw = author_list_str.rstrip("…").rstrip("...").strip()
    result = []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        parts = name.split()
        if len(parts) == 1:
            result.append(("", parts[0]))
        else:
            result.append((" ".join(parts[:-1]), parts[-1]))
    return result


async def _find_or_create_author(
    db: AsyncSession,
    given: str,
    last: str,
    prefer_id: int | None = None,
) -> Author:
    """Return an existing Author or create a new one.

    Matching strategy: same last name (case-insensitive) AND first character
    of given name matches (handles abbreviated vs full first names).
    If prefer_id is set and that author's last name matches, return it directly.
    """
    if prefer_id:
        preferred = (await db.execute(
            select(Author).where(Author.id == prefer_id)
        )).scalar_one_or_none()
        if preferred and preferred.last_name.lower() == last.lower():
            return preferred

    candidates = (await db.execute(
        select(Author).where(func.lower(Author.last_name) == last.lower())
    )).scalars().all()

    for a in candidates:
        if not given or not a.given_name:
            return a
        if a.given_name[0].lower() == given[0].lower():
            return a

    # No match — create a new author
    author = Author(given_name=given or "?", last_name=last)
    db.add(author)
    await db.flush()
    return author


@router.post("/import")
async def do_import_papers(
    request: Request,
    gs_id: str = Form(...),
    author_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    form = await request.form()
    selected_ids = form.getlist("gs_paper_ids")
    if not selected_ids:
        return RedirectResponse(f"/scholar/import?gs_id={gs_id}", 302)

    # Re-fetch so we have title/year/venue/author_list for each selected id
    try:
        all_papers = await _scrape_gs_papers(gs_id)
    except Exception:
        return RedirectResponse("/scholar/import", 302)

    papers_by_id = {p["gs_paper_id"]: p for p in all_papers if p["gs_paper_id"]}
    linked_author_id = int(author_id) if author_id else None

    imported = 0
    for gs_paper_id in selected_ids:
        entry = papers_by_id.get(gs_paper_id)
        if not entry:
            continue
        # Skip if already exists
        existing = (await db.execute(
            select(PaperProject).where(PaperProject.google_scholar_paper_id == gs_paper_id)
        )).scalar_one_or_none()
        if existing:
            continue

        # Build description from venue
        description = entry["venue"] or None

        paper = PaperProject(
            title=entry["title"][:512],
            description=description,
            status=PaperStatus.published,
            google_scholar_paper_id=gs_paper_id,
            created_by=current_user.id,
        )
        db.add(paper)
        await db.flush()

        # Parse and create/link all co-authors
        parsed_names = _parse_gs_author_list(entry.get("author_list", ""))
        seen_author_ids: set[int] = set()
        for position, (given, last) in enumerate(parsed_names, start=1):
            if not last:
                continue
            author = await _find_or_create_author(
                db, given, last, prefer_id=linked_author_id
            )
            if author.id in seen_author_ids:
                continue
            seen_author_ids.add(author.id)
            db.add(PaperAuthor(paper_id=paper.id, author_id=author.id, position=position))

        # If a linked author was specified but not found in the parsed list, add them at end
        if linked_author_id and linked_author_id not in seen_author_ids:
            db.add(PaperAuthor(
                paper_id=paper.id,
                author_id=linked_author_id,
                position=len(parsed_names) + 1,
            ))

        imported += 1

    await db.commit()
    return RedirectResponse(f"/papers?imported={imported}", 302)
