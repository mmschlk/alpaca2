"""
Suggestion workflow: authenticated users propose new conferences,
conference editions, journals, and special issues for admin/moderator review.
"""
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conference import Conference, CORE_RANKS
from app.models.journal import Journal
from app.models.suggestion import Suggestion, SuggestionType

router = APIRouter(prefix="/suggest", tags=["suggestions"])


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": None, **kw}


# ── Conference ────────────────────────────────────────────────────────────────

@router.get("/conference", response_class=HTMLResponse)
async def suggest_conference_form(
    request: Request,
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse(
        request, "suggestions/form.html",
        _ctx(request, current_user,
             suggestion_type="conference",
             core_ranks=CORE_RANKS,
             conferences=[], journals=[]),
    )


@router.post("/conference")
async def suggest_conference(
    request: Request,
    name: str = Form(...),
    abbreviation: str = Form(...),
    core_rank: str = Form(default=""),
    website: str = Form(default=""),
    wikicfp_series_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    data = json.dumps({
        "name": name, "abbreviation": abbreviation,
        "core_rank": core_rank or None, "website": website or None,
        "wikicfp_series_id": wikicfp_series_id or None,
    })
    db.add(Suggestion(entity_type=SuggestionType.conference, data=data, submitted_by_id=current_user.id))
    await db.commit()
    return RedirectResponse("/suggest/submitted", 302)


# ── Conference Edition ────────────────────────────────────────────────────────

@router.get("/conference-edition", response_class=HTMLResponse)
async def suggest_edition_form(
    request: Request,
    conference_id: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    conferences = (await db.execute(
        select(Conference).order_by(Conference.name)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "suggestions/form.html",
        _ctx(request, current_user,
             suggestion_type="conference_edition",
             conferences=conferences, journals=[],
             preselect_conference_id=conference_id,
             core_ranks=[]),
    )


@router.post("/conference-edition")
async def suggest_edition(
    request: Request,
    conference_id: int = Form(...),
    year: int = Form(...),
    location: str = Form(default=""),
    abstract_deadline: str = Form(default=""),
    full_paper_deadline: str = Form(default=""),
    notification_date: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    data = json.dumps({
        "conference_id": conference_id, "year": year,
        "location": location or None,
        "abstract_deadline": abstract_deadline or None,
        "full_paper_deadline": full_paper_deadline or None,
        "notification_date": notification_date or None,
    })
    db.add(Suggestion(entity_type=SuggestionType.conference_edition, data=data, submitted_by_id=current_user.id))
    await db.commit()
    return RedirectResponse("/suggest/submitted", 302)


# ── Journal ───────────────────────────────────────────────────────────────────

@router.get("/journal", response_class=HTMLResponse)
async def suggest_journal_form(
    request: Request,
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse(
        request, "suggestions/form.html",
        _ctx(request, current_user,
             suggestion_type="journal",
             conferences=[], journals=[],
             core_ranks=[]),
    )


@router.post("/journal")
async def suggest_journal(
    request: Request,
    name: str = Form(...),
    abbreviation: str = Form(default=""),
    rank: str = Form(default=""),
    website: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    data = json.dumps({
        "name": name, "abbreviation": abbreviation or None,
        "rank": rank or None, "website": website or None,
    })
    db.add(Suggestion(entity_type=SuggestionType.journal, data=data, submitted_by_id=current_user.id))
    await db.commit()
    return RedirectResponse("/suggest/submitted", 302)


# ── Journal Special Issue ─────────────────────────────────────────────────────

@router.get("/journal-special-issue", response_class=HTMLResponse)
async def suggest_special_issue_form(
    request: Request,
    journal_id: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    journals = (await db.execute(
        select(Journal).order_by(Journal.name)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "suggestions/form.html",
        _ctx(request, current_user,
             suggestion_type="journal_special_issue",
             conferences=[], journals=journals,
             preselect_journal_id=journal_id,
             core_ranks=[]),
    )


@router.post("/journal-special-issue")
async def suggest_special_issue(
    request: Request,
    journal_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(default=""),
    submission_deadline: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    data = json.dumps({
        "journal_id": journal_id, "title": title,
        "description": description or None,
        "submission_deadline": submission_deadline or None,
    })
    db.add(Suggestion(entity_type=SuggestionType.journal_special_issue, data=data, submitted_by_id=current_user.id))
    await db.commit()
    return RedirectResponse("/suggest/submitted", 302)


# ── My suggestions ────────────────────────────────────────────────────────────

@router.get("/submitted", response_class=HTMLResponse)
async def submitted(request: Request, current_user=Depends(get_current_user)):
    if not current_user:
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse(
        request, "suggestions/submitted.html",
        _ctx(request, current_user),
    )


@router.get("/my", response_class=HTMLResponse)
async def my_suggestions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    suggestions = (await db.execute(
        select(Suggestion)
        .where(Suggestion.submitted_by_id == current_user.id)
        .order_by(Suggestion.submitted_at.desc())
    )).scalars().all()
    return templates.TemplateResponse(
        request, "suggestions/my.html",
        _ctx(request, current_user, suggestions=suggestions),
    )
