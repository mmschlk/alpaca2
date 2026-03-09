from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.author import Author
from app.models.group import GroupMembership, ResearchGroup
from app.models.paper import (
    PAPER_STATUS_COLORS, PAPER_STATUS_LABELS,
    PaperAuthor, PaperProject, PaperStatus, TodoItem, TodoStatus,
)
from app.models.personal_todo import PersonalTodo
from app.routers.papers import _visibility_filter

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    all_todos: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    vis = _visibility_filter(current_user.id, current_user.author_id)

    # Stats — scoped to visible papers only
    total_papers = (await db.execute(
        select(func.count(PaperProject.id)).where(vis)
    )).scalar_one()
    accepted = (await db.execute(
        select(func.count(PaperProject.id)).where(
            vis & PaperProject.status.in_([PaperStatus.accepted, PaperStatus.published])
        )
    )).scalar_one()
    submitted = (await db.execute(
        select(func.count(PaperProject.id)).where(
            vis & PaperProject.status.in_([PaperStatus.submitted, PaperStatus.under_review])
        )
    )).scalar_one()
    total_authors = (await db.execute(select(func.count(Author.id)))).scalar_one()

    # Recent papers — scoped to visible papers only
    result = await db.execute(
        select(PaperProject)
        .options(selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author))
        .where(vis)
        .order_by(PaperProject.updated_at.desc())
        .limit(8)
    )
    recent_papers = result.scalars().all()

    # My groups
    my_groups: list[ResearchGroup] = []
    if current_user.author_id:
        gm_result = await db.execute(
            select(ResearchGroup)
            .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
            .where(GroupMembership.user_id == current_user.id)
            .limit(5)
        )
        my_groups = gm_result.scalars().all()

    # Todos on visible papers (open + in_progress), sorted by due_date nulls last
    todo_stmt = (
        select(TodoItem)
        .join(PaperProject, PaperProject.id == TodoItem.paper_id)
        .options(
            selectinload(TodoItem.paper),
            selectinload(TodoItem.assigned_user),
            selectinload(TodoItem.source_workflow),
            selectinload(TodoItem.blocked_by),
        )
        .where(vis)
        .where(TodoItem.status != TodoStatus.done)
    )
    if not all_todos:
        todo_stmt = todo_stmt.where(TodoItem.assigned_to == current_user.id)
    todo_stmt = todo_stmt.order_by(
        func.isnull(TodoItem.due_date).asc(),
        TodoItem.due_date.asc(),
        TodoItem.created_at.asc(),
    ).limit(30)
    todos = (await db.execute(todo_stmt)).scalars().all()

    # Personal todos (open + in_progress), sorted by due_date nulls last
    personal_todos = (await db.execute(
        select(PersonalTodo)
        .where(
            PersonalTodo.user_id == current_user.id,
            PersonalTodo.status != TodoStatus.done,
        )
        .order_by(
            func.isnull(PersonalTodo.due_date).asc(),
            PersonalTodo.due_date.asc(),
            PersonalTodo.created_at.asc(),
        )
        .limit(30)
    )).scalars().all()

    return templates.TemplateResponse(
        request,
        "dashboard/index.html",
        {
            "active_page": "dashboard",
            "current_user": current_user,
            "stats": {
                "papers": total_papers,
                "accepted": accepted,
                "submitted": submitted,
                "authors": total_authors,
            },
            "recent_papers": recent_papers,
            "my_groups": my_groups,
            "todos": todos,
            "all_todos": bool(all_todos),
            "personal_todos": personal_todos,
            "today_date": date.today(),
            "status_labels": PAPER_STATUS_LABELS,
            "status_colors": PAPER_STATUS_COLORS,
            "todo_statuses": list(TodoStatus),
        },
    )
