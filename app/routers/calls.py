from datetime import date
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conference import ConferenceEdition
from app.models.journal import JournalSpecialIssue
from app.models.paper import PaperAuthor, PaperProject, PaperSubmissionPlan
from app.templating import templates

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("", response_class=HTMLResponse)
async def calls_overview(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    today = date.today()

    # Load all plans with all needed relationships eagerly
    plans_result = await db.execute(
        select(PaperSubmissionPlan)
        .options(
            selectinload(PaperSubmissionPlan.paper)
            .selectinload(PaperProject.paper_authors)
            .selectinload(PaperAuthor.author),
            selectinload(PaperSubmissionPlan.edition)
            .selectinload(ConferenceEdition.conference),
            selectinload(PaperSubmissionPlan.journal),
            selectinload(PaperSubmissionPlan.special_issue)
            .selectinload(JournalSpecialIssue.journal),
        )
    )
    all_plans = plans_result.scalars().all()

    # Group by conference edition using SimpleNamespace for reliable attribute access in templates
    edition_groups: dict[int, SimpleNamespace] = {}
    for plan in all_plans:
        if plan.conference_edition_id and plan.edition:
            eid = plan.conference_edition_id
            if eid not in edition_groups:
                edition_groups[eid] = SimpleNamespace(edition=plan.edition, plans=[])
            edition_groups[eid].plans.append(plan)

    forthcoming_editions = []
    past_editions = []
    for grp in edition_groups.values():
        if grp.edition.next_deadline and grp.edition.next_deadline >= today:
            forthcoming_editions.append(grp)
        else:
            past_editions.append(grp)
    forthcoming_editions.sort(key=lambda g: g.edition.next_deadline)

    # Group by journal (direct, no special issue)
    journal_groups: dict[int, SimpleNamespace] = {}
    for plan in all_plans:
        if plan.journal_id and not plan.journal_special_issue_id and plan.journal:
            jid = plan.journal_id
            if jid not in journal_groups:
                journal_groups[jid] = SimpleNamespace(journal=plan.journal, plans=[])
            journal_groups[jid].plans.append(plan)
    journals_with_plans = sorted(journal_groups.values(), key=lambda g: g.journal.name)

    # Group by special issue
    si_groups: dict[int, SimpleNamespace] = {}
    for plan in all_plans:
        if plan.journal_special_issue_id and plan.special_issue:
            siid = plan.journal_special_issue_id
            if siid not in si_groups:
                si_groups[siid] = SimpleNamespace(special_issue=plan.special_issue, plans=[])
            si_groups[siid].plans.append(plan)
    special_issues_with_plans = sorted(
        si_groups.values(),
        key=lambda g: g.special_issue.submission_deadline or date.max,
    )

    return templates.TemplateResponse(
        request, "calls/index.html",
        {
            "request": request,
            "current_user": current_user,
            "active_page": "calls",
            "today": today,
            "forthcoming_editions": forthcoming_editions,
            "past_editions": past_editions,
            "journals_with_plans": journals_with_plans,
            "special_issues_with_plans": special_issues_with_plans,
        },
    )
