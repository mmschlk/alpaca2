from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conference import Conference, ConferenceEdition
from app.models.journal import Journal
from app.models.service import SERVICE_ROLE_COLORS, SERVICE_ROLE_LABELS, ServiceRecord, ServiceRole

router = APIRouter(prefix="/service", tags=["service"])


def _ctx(request, current_user, **kw):
    return {
        "request": request, "current_user": current_user, "active_page": "service",
        "role_labels": SERVICE_ROLE_LABELS, "role_colors": SERVICE_ROLE_COLORS,
        "all_roles": list(ServiceRole), **kw,
    }


@router.get("", response_class=HTMLResponse)
async def service_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    records = (await db.execute(
        select(ServiceRecord)
        .options(
            selectinload(ServiceRecord.conference_edition)
            .selectinload(ConferenceEdition.conference),
            selectinload(ServiceRecord.journal),
        )
        .where(ServiceRecord.user_id == current_user.id)
        .order_by(ServiceRecord.year.desc(), ServiceRecord.created_at.desc())
    )).scalars().all()

    # Group by year for display
    by_year: dict[int, list[ServiceRecord]] = {}
    for r in records:
        by_year.setdefault(r.year, []).append(r)

    # Stats
    total_papers = sum(r.num_papers for r in records if r.num_papers)

    conferences = (await db.execute(
        select(Conference).order_by(Conference.abbreviation)
    )).scalars().all()
    journals = (await db.execute(
        select(Journal).order_by(Journal.name)
    )).scalars().all()

    return templates.TemplateResponse(
        request, "service/list.html",
        _ctx(request, current_user,
             by_year=by_year,
             total_records=len(records),
             total_papers=total_papers,
             conferences=conferences,
             journals=journals),
    )


@router.get("/editions/{conf_id}", response_class=HTMLResponse)
async def conference_editions_fragment(
    conf_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """HTMX: return <option> elements for a conference's editions."""
    if not current_user:
        return HTMLResponse("")
    editions = (await db.execute(
        select(ConferenceEdition)
        .where(ConferenceEdition.conference_id == conf_id)
        .order_by(ConferenceEdition.year.desc())
    )).scalars().all()
    options = "".join(
        f'<option value="{e.id}">{e.year}</option>' for e in editions
    )
    return HTMLResponse(f'<option value="">— select year —</option>{options}')


@router.post("", response_class=HTMLResponse)
async def create_service_record(
    request: Request,
    service_type: str = Form(...),
    conference_edition_id: str = Form(default=""),
    journal_id: str = Form(default=""),
    year: str = Form(default=""),
    role: str = Form(...),
    num_papers: str = Form(default=""),
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    # Resolve year
    record_year: int | None = None
    conf_edition_id: int | None = None
    jnl_id: int | None = None

    if service_type == "conference" and conference_edition_id:
        conf_edition_id = int(conference_edition_id)
        edition = (await db.execute(
            select(ConferenceEdition).where(ConferenceEdition.id == conf_edition_id)
        )).scalar_one_or_none()
        record_year = edition.year if edition else None
    elif service_type == "journal" and journal_id:
        jnl_id = int(journal_id)
        try:
            record_year = int(year)
        except (ValueError, TypeError):
            record_year = None

    if record_year is None:
        return RedirectResponse("/service", 302)

    db.add(ServiceRecord(
        user_id=current_user.id,
        conference_edition_id=conf_edition_id,
        journal_id=jnl_id,
        year=record_year,
        role=ServiceRole(role),
        num_papers=int(num_papers) if num_papers.strip() else None,
        notes=notes.strip() or None,
    ))
    await db.commit()
    return RedirectResponse("/service", 302)


@router.post("/{record_id}/delete")
async def delete_service_record(
    record_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    rec = (await db.execute(
        select(ServiceRecord).where(
            ServiceRecord.id == record_id,
            ServiceRecord.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if rec:
        await db.delete(rec)
        await db.commit()
    return RedirectResponse("/service", 302)
