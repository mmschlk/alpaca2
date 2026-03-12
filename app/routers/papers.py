import os
import re
import shutil
from datetime import date, datetime, timezone
from typing import Optional

import markdown as md
import bleach
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.workflow_engine import fire_paper_status_triggers
from app.models.affiliation import AuthorAffiliation
from app.models.author import Author
from app.models.conference import ConferenceEdition
from app.models.group import GroupMembership
from app.models.journal import Journal
from app.models.paper import (
    MILESTONE_TYPE_LABELS,
    PAPER_STATUS_COLORS,
    PAPER_STATUS_LABELS,
    MilestoneType,
    PaperAuthor,
    PaperChangeLog,
    PaperComment,
    PaperConferenceSubmission,
    PaperEventType,
    PaperGroupShare,
    PaperJournalSubmission,
    PaperMilestone,
    PaperProject,
    PaperResource,
    PaperResourceType,
    PaperStatus,
    SubmissionStatus,
    TodoItem,
    TodoStatus,
)
from app.models.scholar import ScholarPaperSnapshot
from app.models.workflow import PaperWorkflowSubscription as _PWS

router = APIRouter(prefix="/papers", tags=["papers"])
PAGE_SIZE = 20
UPLOAD_DIR = "static/uploads/papers"

ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + ["p", "pre", "code", "h1", "h2", "h3",
                                                        "h4", "h5", "ul", "ol", "li", "blockquote"]


def _render_md(text: str) -> str:
    html = md.markdown(text, extensions=["fenced_code", "tables"])
    return bleach.clean(html, tags=ALLOWED_TAGS, strip=True)


def _ctx(request, current_user, **kw):
    return {
        "request": request, "current_user": current_user, "active_page": "papers",
        "status_labels": PAPER_STATUS_LABELS, "status_colors": PAPER_STATUS_COLORS,
        "all_statuses": list(PaperStatus), **kw,
    }


async def _link_gs_snapshots(db: AsyncSession, paper_id: int, gs_id: str | None) -> None:
    if not gs_id:
        return
    from sqlalchemy import update
    await db.execute(
        update(ScholarPaperSnapshot)
        .where(ScholarPaperSnapshot.gs_paper_id == gs_id)
        .values(paper_id=paper_id)
    )


def _parse_authors(raw: str) -> list[tuple[str, str]]:
    entries = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "," in part:
            last, given = part.split(",", 1)
            entries.append((last.strip(), given.strip()))
        else:
            entries.append((part.strip(), ""))
    return entries


async def _resolve_authors(db: AsyncSession, raw: str) -> list[Author]:
    parsed = _parse_authors(raw)
    authors = []
    for last, given in parsed:
        result = await db.execute(
            select(Author).where(
                (func.lower(Author.last_name) == last.lower()) &
                (func.lower(Author.given_name) == given.lower())
            )
        )
        author = result.scalar_one_or_none()
        if not author:
            author = Author(last_name=last, given_name=given)
            db.add(author)
            await db.flush()
        authors.append(author)
    return authors


def _add_log(db, paper_id: int, user_id: int, event_type: PaperEventType, **kwargs) -> PaperChangeLog:
    entry = PaperChangeLog(paper_id=paper_id, event_type=event_type, created_by=user_id, **kwargs)
    db.add(entry)
    return entry


def _visibility_filter(user_id: int, author_id: Optional[int]):
    """
    Returns an OR clause that limits PaperProject rows to those visible to user:
      1. User created the paper
      2. User's author profile is listed as a co-author
      3. Paper is shared with a group the user belongs to
    """
    conds = [PaperProject.created_by == user_id]

    if author_id:
        conds.append(
            exists(
                select(PaperAuthor.id).where(
                    (PaperAuthor.paper_id == PaperProject.id) &
                    (PaperAuthor.author_id == author_id)
                )
            )
        )

    conds.append(
        exists(
            select(PaperGroupShare.paper_id)
            .join(GroupMembership, GroupMembership.group_id == PaperGroupShare.group_id)
            .where(
                (PaperGroupShare.paper_id == PaperProject.id) &
                (GroupMembership.user_id == user_id)
            )
        )
    )

    return or_(*conds)


@router.get("", response_class=HTMLResponse)
async def list_papers(
    request: Request,
    page: int = 1, q: str = "", status: str = "",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    vis = _visibility_filter(current_user.id, current_user.author_id)

    # Per-status counts for tab badges (respects search query)
    count_base = select(PaperProject.status, func.count(PaperProject.id)).where(vis)
    if q:
        count_base = count_base.where(PaperProject.title.ilike(f"%{q}%"))
    count_rows = (await db.execute(count_base.group_by(PaperProject.status))).all()
    counts_by_status = {(r[0].value if hasattr(r[0], "value") else r[0]): r[1] for r in count_rows}

    stmt = select(PaperProject).options(
        selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author),
        selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.affiliation),
    ).where(vis)
    if q:
        stmt = stmt.where(PaperProject.title.ilike(f"%{q}%"))
    if status:
        stmt = stmt.where(PaperProject.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(
        stmt.order_by(PaperProject.updated_at.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    )).scalars().all()
    query_params = {}
    if q:
        query_params["q"] = q
    if status:
        query_params["status"] = status
    return templates.TemplateResponse(
        request, "papers/list.html",
        _ctx(request, current_user, papers=items, total=total, page=page,
             total_pages=(total + PAGE_SIZE - 1) // PAGE_SIZE, q=q, status=status,
             counts_by_status=counts_by_status, query_params=query_params),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_paper_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    all_authors = (await db.execute(
        select(Author).order_by(Author.last_name, Author.given_name)
    )).scalars().all()
    return templates.TemplateResponse(request, "papers/form.html",
                                      _ctx(request, current_user, paper=None, action="/papers",
                                           all_authors=all_authors))


@router.post("", response_class=HTMLResponse)
async def create_paper(
    request: Request,
    title: str = Form(...),
    description: str = Form(default=""),
    status: str = Form(default="planned"),
    authors_raw: str = Form(default=""),
    overleaf_url: str = Form(default=""),
    github_url: str = Form(default=""),
    google_scholar_paper_id: str = Form(default=""),
    published_date: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    from datetime import date as _date
    ps = PaperStatus(status)
    pub_date = _date.fromisoformat(published_date) if published_date else None
    paper = PaperProject(
        title=title, description=description or None,
        status=ps,
        overleaf_url=overleaf_url or None,
        github_url=github_url or None,
        google_scholar_paper_id=google_scholar_paper_id or None,
        published_date=pub_date,
        created_by=current_user.id,
    )
    db.add(paper)
    await db.flush()
    if authors_raw.strip():
        authors = await _resolve_authors(db, authors_raw)
        for pos, author in enumerate(authors):
            aff_result = await db.execute(
                select(AuthorAffiliation)
                .where((AuthorAffiliation.author_id == author.id) & (AuthorAffiliation.end_date.is_(None)))
                .order_by(AuthorAffiliation.start_date.desc())
                .limit(1)
            )
            aa = aff_result.scalar_one_or_none()
            db.add(PaperAuthor(paper_id=paper.id, author_id=author.id,
                               position=pos, affiliation_id=aa.affiliation_id if aa else None))
    # Log initial status
    _add_log(db, paper.id, current_user.id, PaperEventType.status_change, new_status=ps.value)
    # Log initial resources
    if overleaf_url:
        res = PaperResource(paper_id=paper.id, label="Overleaf", url=overleaf_url,
                            resource_type=PaperResourceType.overleaf, created_by=current_user.id)
        db.add(res)
        await db.flush()
        _add_log(db, paper.id, current_user.id, PaperEventType.resource_added,
                 resource_id=res.id, note="Overleaf link added")
    if github_url:
        res = PaperResource(paper_id=paper.id, label="GitHub", url=github_url,
                            resource_type=PaperResourceType.github, created_by=current_user.id)
        db.add(res)
        await db.flush()
        _add_log(db, paper.id, current_user.id, PaperEventType.resource_added,
                 resource_id=res.id, note="GitHub repository added")
    await _link_gs_snapshots(db, paper.id, google_scholar_paper_id or None)
    await db.commit()
    return RedirectResponse(f"/papers/{paper.id}", 302)


@router.get("/{paper_id}", response_class=HTMLResponse)
async def paper_detail(
    request: Request, paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(PaperProject)
        .options(
            selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author),
            selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.affiliation),
            selectinload(PaperProject.comments).selectinload(PaperComment.user),
            selectinload(PaperProject.todos).selectinload(TodoItem.assigned_user),
            selectinload(PaperProject.todos).selectinload(TodoItem.source_workflow),
            selectinload(PaperProject.todos).selectinload(TodoItem.blocked_by),
            selectinload(PaperProject.workflow_subscriptions).selectinload(_PWS.workflow),
            selectinload(PaperProject.milestones),
            selectinload(PaperProject.group_shares).selectinload(PaperGroupShare.group),
            selectinload(PaperProject.resources).selectinload(PaperResource.creator),
            selectinload(PaperProject.change_log).selectinload(PaperChangeLog.creator),
            selectinload(PaperProject.change_log).selectinload(PaperChangeLog.edition)
            .selectinload(ConferenceEdition.conference),
            selectinload(PaperProject.change_log).selectinload(PaperChangeLog.journal),
            selectinload(PaperProject.change_log).selectinload(PaperChangeLog.special_issue),
            selectinload(PaperProject.change_log).selectinload(PaperChangeLog.resource),
        )
        .where(
            (PaperProject.id == paper_id) &
            _visibility_filter(current_user.id, current_user.author_id)
        )
    )
    paper = result.scalar_one_or_none()
    if not paper:
        return RedirectResponse("/papers", 302)

    gs_snapshots = []
    if paper.google_scholar_paper_id:
        gs_snapshots = (await db.execute(
            select(ScholarPaperSnapshot)
            .where(ScholarPaperSnapshot.gs_paper_id == paper.google_scholar_paper_id)
            .order_by(ScholarPaperSnapshot.date)
        )).scalars().all()

    rendered_comments = [(c, _render_md(c.content)) for c in paper.comments]

    editions = (await db.execute(
        select(ConferenceEdition)
        .options(selectinload(ConferenceEdition.conference))
        .order_by(ConferenceEdition.year.desc())
    )).scalars().all()
    journals = (await db.execute(select(Journal).order_by(Journal.name))).scalars().all()
    from app.models.user import User
    users = (await db.execute(select(User).where(User.is_active == True))).scalars().all()

    # Render change-log notes as markdown
    rendered_log = [
        (entry, _render_md(entry.note) if entry.note and entry.event_type == PaperEventType.note else None)
        for entry in paper.change_log
    ]

    # Workflows visible to this user (for manual apply dropdown)
    from app.models.workflow import Workflow, WorkflowShare
    from sqlalchemy import or_, exists as sqla_exists
    group_ids = [r[0] for r in (await db.execute(
        select(GroupMembership.group_id).where(GroupMembership.user_id == current_user.id)
    )).all()]
    wf_filter = or_(
        Workflow.owner_id == current_user.id,
        Workflow.is_public == True,  # noqa: E712
        sqla_exists(select(WorkflowShare.id).where(
            (WorkflowShare.workflow_id == Workflow.id) &
            (WorkflowShare.shared_with_user_id == current_user.id)
        )),
        *(
            [sqla_exists(select(WorkflowShare.id).where(
                (WorkflowShare.workflow_id == Workflow.id) &
                (WorkflowShare.shared_with_group_id.in_(group_ids))
            ))] if group_ids else []
        ),
    )
    visible_workflows = (await db.execute(
        select(Workflow).where(wf_filter).order_by(Workflow.name)
    )).scalars().all()

    subscribed_wf_ids = {s.workflow_id for s in paper.workflow_subscriptions}

    return templates.TemplateResponse(
        request, "papers/detail.html",
        _ctx(request, current_user, paper=paper, gs_snapshots=gs_snapshots,
             rendered_comments=rendered_comments, rendered_log=rendered_log,
             editions=editions, journals=journals, users=users, today=date.today(),
             submission_statuses=list(SubmissionStatus), todo_statuses=list(TodoStatus),
             resource_types=list(PaperResourceType), event_types=PaperEventType,
             milestone_types=list(MilestoneType), milestone_type_labels=MILESTONE_TYPE_LABELS,
             visible_workflows=visible_workflows,
             subscribed_wf_ids=subscribed_wf_ids),
    )


@router.get("/{paper_id}/edit", response_class=HTMLResponse)
async def edit_paper_form(
    request: Request, paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(PaperProject)
        .options(selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author))
        .where(
            (PaperProject.id == paper_id) &
            _visibility_filter(current_user.id, current_user.author_id)
        )
    )
    paper = result.scalar_one_or_none()
    if not paper:
        return RedirectResponse("/papers", 302)
    author_str = "; ".join(f"{pa.author.last_name}, {pa.author.given_name}"
                           for pa in paper.paper_authors)
    all_authors = (await db.execute(
        select(Author).order_by(Author.last_name, Author.given_name)
    )).scalars().all()
    return templates.TemplateResponse(request, "papers/form.html",
                                      _ctx(request, current_user, paper=paper,
                                           author_str=author_str,
                                           action=f"/papers/{paper_id}/edit",
                                           all_authors=all_authors))


@router.post("/{paper_id}/edit")
async def update_paper(
    paper_id: int,
    title: str = Form(...),
    description: str = Form(default=""),
    status: str = Form(default="planned"),
    authors_raw: str = Form(default=""),
    overleaf_url: str = Form(default=""),
    github_url: str = Form(default=""),
    google_scholar_paper_id: str = Form(default=""),
    published_date: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    from datetime import date as _date
    result = await db.execute(
        select(PaperProject).options(selectinload(PaperProject.paper_authors))
        .where(
            (PaperProject.id == paper_id) &
            _visibility_filter(current_user.id, current_user.author_id)
        )
    )
    paper = result.scalar_one_or_none()
    if not paper:
        return RedirectResponse("/papers", 302)

    new_status = PaperStatus(status)
    status_changed = paper.status != new_status
    if status_changed:
        _add_log(db, paper.id, current_user.id, PaperEventType.status_change,
                 old_status=paper.status.value, new_status=new_status.value)

    old_overleaf = paper.overleaf_url
    old_github = paper.github_url

    paper.title = title
    paper.description = description or None
    paper.status = new_status
    paper.overleaf_url = overleaf_url or None
    paper.github_url = github_url or None
    paper.google_scholar_paper_id = google_scholar_paper_id or None
    paper.published_date = _date.fromisoformat(published_date) if published_date else None

    if authors_raw.strip():
        for pa in paper.paper_authors:
            await db.delete(pa)
        await db.flush()
        authors = await _resolve_authors(db, authors_raw)
        for pos, author in enumerate(authors):
            aff_result = await db.execute(
                select(AuthorAffiliation)
                .where((AuthorAffiliation.author_id == author.id) & (AuthorAffiliation.end_date.is_(None)))
                .limit(1)
            )
            aa = aff_result.scalar_one_or_none()
            db.add(PaperAuthor(paper_id=paper.id, author_id=author.id,
                               position=pos, affiliation_id=aa.affiliation_id if aa else None))

    # Log new resource links
    if overleaf_url and overleaf_url != old_overleaf:
        res = PaperResource(paper_id=paper.id, label="Overleaf", url=overleaf_url,
                            resource_type=PaperResourceType.overleaf, created_by=current_user.id)
        db.add(res)
        await db.flush()
        _add_log(db, paper.id, current_user.id, PaperEventType.resource_added,
                 resource_id=res.id, note="Overleaf link added")
    if github_url and github_url != old_github:
        res = PaperResource(paper_id=paper.id, label="GitHub", url=github_url,
                            resource_type=PaperResourceType.github, created_by=current_user.id)
        db.add(res)
        await db.flush()
        _add_log(db, paper.id, current_user.id, PaperEventType.resource_added,
                 resource_id=res.id, note="GitHub repository added")

    await _link_gs_snapshots(db, paper.id, google_scholar_paper_id or None)
    if status_changed:
        group_ids = [r[0] for r in (await db.execute(
            select(GroupMembership.group_id).where(GroupMembership.user_id == current_user.id)
        )).all()]
        await fire_paper_status_triggers(db, paper.id, new_status.value, current_user.id, group_ids)
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}", 302)


@router.post("/{paper_id}/status")
async def update_status(
    request: Request,
    paper_id: int,
    status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(PaperProject).where(
            (PaperProject.id == paper_id) &
            _visibility_filter(current_user.id, current_user.author_id)
        )
    )
    paper = result.scalar_one_or_none()
    if paper:
        new_status = PaperStatus(status)
        if new_status != paper.status:
            _add_log(db, paper_id, current_user.id, PaperEventType.status_change,
                     old_status=paper.status, new_status=new_status)
            paper.status = new_status
            group_ids = [r[0] for r in (await db.execute(
                select(GroupMembership.group_id).where(GroupMembership.user_id == current_user.id)
            )).all()]
            await fire_paper_status_triggers(db, paper_id, new_status.value, current_user.id, group_ids)
        await db.commit()
    referer = request.headers.get("referer", f"/papers/{paper_id}")
    return RedirectResponse(referer, 302)


@router.post("/bulk-delete")
async def bulk_delete_papers(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    form = await request.form()
    raw_ids = form.getlist("paper_ids")
    ids = [int(v) for v in raw_ids if v.isdigit()]
    if ids:
        vis = _visibility_filter(current_user.id, current_user.author_id)
        papers = (await db.execute(
            select(PaperProject).where(PaperProject.id.in_(ids)).where(vis)
        )).scalars().all()
        for paper in papers:
            await db.delete(paper)
        await db.commit()
    redirect_to = (form.get("redirect_to") or "/papers").strip() or "/papers"
    return RedirectResponse(redirect_to, 302)


@router.post("/{paper_id}/delete")
async def delete_paper(
    paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(PaperProject).where(
            (PaperProject.id == paper_id) &
            _visibility_filter(current_user.id, current_user.author_id)
        )
    )
    paper = result.scalar_one_or_none()
    if paper:
        await db.delete(paper)
        await db.commit()
    return RedirectResponse("/papers", 302)


# ── Comments ──

@router.post("/{paper_id}/comments")
async def add_comment(
    paper_id: int,
    content: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    db.add(PaperComment(paper_id=paper_id, user_id=current_user.id, content=content))
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#comments", 302)


@router.post("/{paper_id}/comments/{comment_id}/delete")
async def delete_comment(
    paper_id: int, comment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(select(PaperComment).where(PaperComment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment and comment.user_id == current_user.id:
        await db.delete(comment)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#comments", 302)


# ── Todos ──

@router.post("/{paper_id}/todos")
async def add_todo(
    paper_id: int,
    title: str = Form(...),
    description: str = Form(default=""),
    assigned_to: Optional[int] = Form(default=None),
    due_date: str = Form(default=""),
    blocked_by_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    from datetime import date as _date
    parsed_due = None
    if due_date:
        try:
            parsed_due = _date.fromisoformat(due_date)
        except ValueError:
            pass
    blocker_id = int(blocked_by_id) if blocked_by_id.strip() else None
    db.add(TodoItem(
        paper_id=paper_id, title=title, description=description or None,
        assigned_to=assigned_to, due_date=parsed_due, status=TodoStatus.open,
        blocked_by_id=blocker_id,
    ))
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#todos", 302)


@router.post("/{paper_id}/todos/{todo_id}/status")
async def update_todo_status(
    paper_id: int, todo_id: int,
    status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(select(TodoItem).where(TodoItem.id == todo_id))
    todo = result.scalar_one_or_none()
    if todo:
        todo.status = TodoStatus(status)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#todos", 302)


@router.post("/{paper_id}/todos/{todo_id}/delete")
async def delete_todo(
    paper_id: int, todo_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(select(TodoItem).where(TodoItem.id == todo_id))
    todo = result.scalar_one_or_none()
    if todo:
        await db.delete(todo)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#todos", 302)


# ── Workflow subscriptions ─────────────────────────────────────────────────────

@router.post("/{paper_id}/workflow-subscriptions")
async def subscribe_workflow(
    paper_id: int,
    workflow_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Subscribe this paper to a workflow (paper-specific trigger scope)."""
    if not current_user:
        return RedirectResponse("/login", 302)
    from app.models.workflow import PaperWorkflowSubscription
    wf_id = int(workflow_id)
    existing = (await db.execute(
        select(PaperWorkflowSubscription).where(
            PaperWorkflowSubscription.paper_id == paper_id,
            PaperWorkflowSubscription.workflow_id == wf_id,
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(PaperWorkflowSubscription(paper_id=paper_id, workflow_id=wf_id))
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#workflows", 302)


@router.post("/{paper_id}/workflow-subscriptions/{wf_id}/delete")
async def unsubscribe_workflow(
    paper_id: int, wf_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    from app.models.workflow import PaperWorkflowSubscription
    sub = (await db.execute(
        select(PaperWorkflowSubscription).where(
            PaperWorkflowSubscription.paper_id == paper_id,
            PaperWorkflowSubscription.workflow_id == wf_id,
        )
    )).scalar_one_or_none()
    if sub:
        await db.delete(sub)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#workflows", 302)


# ── Milestones ──

@router.post("/{paper_id}/milestones")
async def add_milestone(
    paper_id: int,
    title: str = Form(...),
    milestone_type: str = Form(default="internal_deadline"),
    due_date: str = Form(...),
    description: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    from datetime import date as _date
    try:
        parsed_due = _date.fromisoformat(due_date)
    except ValueError:
        return RedirectResponse(f"/papers/{paper_id}?tab=activity#milestones", 302)
    db.add(PaperMilestone(
        paper_id=paper_id, title=title,
        milestone_type=MilestoneType(milestone_type),
        due_date=parsed_due,
        description=description or None,
    ))
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#milestones", 302)


@router.post("/{paper_id}/milestones/{milestone_id}/toggle")
async def toggle_milestone(
    paper_id: int, milestone_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    m = (await db.execute(select(PaperMilestone).where(PaperMilestone.id == milestone_id))).scalar_one_or_none()
    if m:
        m.is_done = not m.is_done
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#milestones", 302)


@router.post("/{paper_id}/milestones/{milestone_id}/delete")
async def delete_milestone(
    paper_id: int, milestone_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    m = (await db.execute(select(PaperMilestone).where(PaperMilestone.id == milestone_id))).scalar_one_or_none()
    if m:
        await db.delete(m)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=activity#milestones", 302)


# ── Resources ──

@router.post("/{paper_id}/resources")
async def add_resource(
    paper_id: int,
    label: str = Form(...),
    url: str = Form(default=""),
    resource_type: str = Form(default="link"),
    file: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    rtype = PaperResourceType(resource_type)
    file_path = None
    if file and file.filename:
        upload_dir = os.path.join(UPLOAD_DIR, str(paper_id))
        os.makedirs(upload_dir, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-]", "_", file.filename)
        dest = os.path.join(upload_dir, safe_name)
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        file_path = dest
        rtype = PaperResourceType.file
    res = PaperResource(
        paper_id=paper_id, label=label,
        url=url or None, file_path=file_path,
        resource_type=rtype, created_by=current_user.id,
    )
    db.add(res)
    await db.flush()
    _add_log(db, paper_id, current_user.id, PaperEventType.resource_added,
             resource_id=res.id, note=f"{label} added")
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)


@router.post("/{paper_id}/resources/{res_id}/delete")
async def delete_resource(
    paper_id: int, res_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(select(PaperResource).where(PaperResource.id == res_id))
    res = result.scalar_one_or_none()
    if res:
        _add_log(db, paper_id, current_user.id, PaperEventType.resource_removed,
                 note=f"{res.label} removed")
        if res.file_path and os.path.exists(res.file_path):
            os.remove(res.file_path)
        await db.delete(res)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)


# ── Change log notes ──

@router.post("/{paper_id}/log/note")
async def add_log_note(
    paper_id: int,
    note: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    _add_log(db, paper_id, current_user.id, PaperEventType.note, note=note)
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)


# ── Submissions ──

@router.post("/{paper_id}/submit/conference")
async def submit_to_conference(
    paper_id: int,
    conference_edition_id: int = Form(...),
    submitted_at: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    parsed_date = datetime.strptime(submitted_at, "%Y-%m-%d") if submitted_at else datetime.now(timezone.utc)
    db.add(PaperConferenceSubmission(
        paper_id=paper_id,
        conference_edition_id=conference_edition_id,
        submitted_at=parsed_date,
        status=SubmissionStatus.submitted,
    ))
    _add_log(db, paper_id, current_user.id, PaperEventType.submitted_conference,
             conference_edition_id=conference_edition_id)
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)


@router.post("/{paper_id}/submit/journal")
async def submit_to_journal(
    paper_id: int,
    journal_id: int = Form(...),
    special_issue_id: Optional[int] = Form(default=None),
    submitted_at: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    parsed_date = datetime.strptime(submitted_at, "%Y-%m-%d") if submitted_at else datetime.now(timezone.utc)
    db.add(PaperJournalSubmission(
        paper_id=paper_id, journal_id=journal_id,
        special_issue_id=special_issue_id or None,
        submitted_at=parsed_date,
        status=SubmissionStatus.submitted,
    ))
    _add_log(db, paper_id, current_user.id, PaperEventType.submitted_journal,
             journal_id=journal_id, special_issue_id=special_issue_id or None)
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)


@router.post("/{paper_id}/submissions/conference/{sub_id}/status")
async def update_conf_submission_status(
    paper_id: int, sub_id: int,
    status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(select(PaperConferenceSubmission).where(PaperConferenceSubmission.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub:
        sub.status = SubmissionStatus(status)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)


@router.post("/{paper_id}/submissions/conference/{sub_id}/delete")
async def delete_conf_submission(
    paper_id: int, sub_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(select(PaperConferenceSubmission).where(PaperConferenceSubmission.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub:
        await db.delete(sub)
        await db.commit()
    return RedirectResponse(f"/papers/{paper_id}?tab=timeline", 302)
