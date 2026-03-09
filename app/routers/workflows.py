"""
Workflow router — CRUD for workflows, steps, triggers, shares,
manual apply, and personal todo management.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, or_, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.group import GroupMembership, ResearchGroup
from app.models.paper import PaperProject, TodoStatus
from app.models.personal_todo import PersonalTodo
from app.models.user import User
from app.models.workflow import (
    Workflow, WorkflowShare, WorkflowStep, WorkflowTrigger, WorkflowTriggerType,
)
from app.workflow_engine import apply_workflow_to_paper, apply_workflow_to_user

router = APIRouter(prefix="/workflows", tags=["workflows"])
templates = Jinja2Templates(directory="app/templates")

PAPER_STATUS_LABELS = {
    "planned": "Planned", "wip": "Work in Progress", "submitted": "Submitted",
    "under_review": "Under Review", "major_revision": "Major Revision",
    "minor_revision": "Minor Revision", "accepted": "Accepted",
    "published": "Published", "rejected": "Rejected",
}


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": "workflows", **kw}


async def _user_group_ids(db: AsyncSession, user_id: int) -> list[int]:
    rows = (await db.execute(
        select(GroupMembership.group_id).where(GroupMembership.user_id == user_id)
    )).all()
    return [r[0] for r in rows]


def _visible_filter(user_id: int, group_ids: list[int]):
    conds = [
        Workflow.owner_id == user_id,
        Workflow.is_public == True,  # noqa: E712
        exists(select(WorkflowShare.id).where(
            (WorkflowShare.workflow_id == Workflow.id) &
            (WorkflowShare.shared_with_user_id == user_id)
        )),
    ]
    if group_ids:
        conds.append(exists(select(WorkflowShare.id).where(
            (WorkflowShare.workflow_id == Workflow.id) &
            (WorkflowShare.shared_with_group_id.in_(group_ids))
        )))
    return or_(*conds)


async def _get_workflow(db, wf_id, user_id=None, owner_only=False):
    """Load workflow with all relationships. Returns None if not found or access denied."""
    q = (select(Workflow).where(Workflow.id == wf_id)
         .options(
             selectinload(Workflow.steps).selectinload(WorkflowStep.depends_on),
             selectinload(Workflow.triggers).selectinload(WorkflowTrigger.group),
             selectinload(Workflow.shares).selectinload(WorkflowShare.shared_with_user),
             selectinload(Workflow.shares).selectinload(WorkflowShare.shared_with_group),
             selectinload(Workflow.owner),
         ))
    wf = (await db.execute(q)).scalar_one_or_none()
    if wf is None:
        return None
    if owner_only and wf.owner_id != user_id:
        return None
    return wf


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_workflows(
    request: Request,
    tab: str = "mine",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    group_ids = await _user_group_ids(db, current_user.id)

    if tab == "shared":
        stmt = (select(Workflow)
                .where(
                    Workflow.owner_id != current_user.id,
                    exists(select(WorkflowShare.id).where(
                        (WorkflowShare.workflow_id == Workflow.id) & (
                            (WorkflowShare.shared_with_user_id == current_user.id) |
                            (group_ids and WorkflowShare.shared_with_group_id.in_(group_ids) or False)
                        )
                    ))
                ))
    elif tab == "public":
        stmt = select(Workflow).where(Workflow.is_public == True, Workflow.owner_id != current_user.id)  # noqa: E712
    else:  # mine
        stmt = select(Workflow).where(Workflow.owner_id == current_user.id)

    workflows = (await db.execute(
        stmt.options(selectinload(Workflow.steps), selectinload(Workflow.triggers))
        .order_by(Workflow.updated_at.desc())
    )).scalars().all()

    return templates.TemplateResponse(request, "workflows/list.html",
                                      _ctx(request, current_user, workflows=workflows, tab=tab))


# ── Create ────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_workflow_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse(request, "workflows/form.html",
                                      _ctx(request, current_user, workflow=None, action="/workflows"))


@router.post("", response_class=HTMLResponse)
async def create_workflow(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    is_public: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = Workflow(
        name=name, description=description or None,
        owner_id=current_user.id, is_public=bool(is_public),
    )
    db.add(wf)
    await db.commit()
    await db.refresh(wf)
    return RedirectResponse(f"/workflows/{wf.id}", 302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{wf_id}", response_class=HTMLResponse)
async def workflow_detail(
    request: Request, wf_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = await _get_workflow(db, wf_id)
    if not wf:
        return RedirectResponse("/workflows", 302)

    group_ids = await _user_group_ids(db, current_user.id)
    can_edit = (wf.owner_id == current_user.id or current_user.is_admin)

    # Data for add-trigger form
    all_groups = (await db.execute(
        select(ResearchGroup).order_by(ResearchGroup.name)
    )).scalars().all()
    all_users = (await db.execute(
        select(User).where(User.is_active == True).order_by(User.username)  # noqa: E712
    )).scalars().all()

    # Papers visible to the current user for the manual-apply dropdown
    from app.routers.papers import _visibility_filter
    visible_papers = (await db.execute(
        select(PaperProject)
        .where(_visibility_filter(current_user.id, current_user.author_id))
        .order_by(PaperProject.title)
    )).scalars().all()

    return templates.TemplateResponse(request, "workflows/detail.html", _ctx(
        request, current_user,
        workflow=wf, can_edit=can_edit,
        all_groups=all_groups, all_users=all_users,
        visible_papers=visible_papers,
        status_labels=PAPER_STATUS_LABELS,
        trigger_types=list(WorkflowTriggerType),
    ))


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/{wf_id}/edit", response_class=HTMLResponse)
async def edit_workflow_form(
    request: Request, wf_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = await _get_workflow(db, wf_id, current_user.id, owner_only=True)
    if not wf:
        return RedirectResponse("/workflows", 302)
    return templates.TemplateResponse(request, "workflows/form.html",
                                      _ctx(request, current_user, workflow=wf,
                                           action=f"/workflows/{wf_id}/edit"))


@router.post("/{wf_id}/edit")
async def update_workflow(
    wf_id: int,
    name: str = Form(...),
    description: str = Form(default=""),
    is_public: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        wf.name = name
        wf.description = description or None
        wf.is_public = bool(is_public)
        await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


@router.post("/{wf_id}/delete")
async def delete_workflow(
    wf_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        await db.delete(wf)
        await db.commit()
    return RedirectResponse("/workflows", 302)


# ── Steps ─────────────────────────────────────────────────────────────────────

@router.post("/{wf_id}/steps")
async def add_step(
    wf_id: int,
    title: str = Form(...),
    description: str = Form(default=""),
    due_offset_days: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        max_pos = (await db.execute(
            select(WorkflowStep.position)
            .where(WorkflowStep.workflow_id == wf_id)
            .order_by(WorkflowStep.position.desc()).limit(1)
        )).scalar_one_or_none() or 0
        db.add(WorkflowStep(
            workflow_id=wf_id, position=max_pos + 1,
            title=title, description=description or None,
            due_offset_days=int(due_offset_days) if due_offset_days.strip() else None,
        ))
        await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


@router.post("/{wf_id}/steps/{sid}/delete")
async def delete_step(
    wf_id: int, sid: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        step = (await db.execute(
            select(WorkflowStep).where(WorkflowStep.id == sid, WorkflowStep.workflow_id == wf_id)
        )).scalar_one_or_none()
        if step:
            await db.delete(step)
            await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


@router.post("/{wf_id}/steps/{sid}/depends-on")
async def set_step_dependency(
    wf_id: int, sid: int,
    depends_on_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Set or clear the depends_on for a workflow step."""
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        step = (await db.execute(
            select(WorkflowStep).where(WorkflowStep.id == sid, WorkflowStep.workflow_id == wf_id)
        )).scalar_one_or_none()
        if step:
            dep_id = int(depends_on_id) if depends_on_id.strip() else None
            # Prevent self-dependency
            step.depends_on_id = dep_id if dep_id != sid else None
            await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


@router.post("/{wf_id}/steps/{sid}/move")
async def move_step(
    wf_id: int, sid: int,
    direction: str = Form(...),   # "up" | "down"
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if not wf:
        return RedirectResponse(f"/workflows/{wf_id}", 302)

    steps = (await db.execute(
        select(WorkflowStep)
        .where(WorkflowStep.workflow_id == wf_id)
        .order_by(WorkflowStep.position)
    )).scalars().all()

    idx = next((i for i, s in enumerate(steps) if s.id == sid), None)
    if idx is None:
        return RedirectResponse(f"/workflows/{wf_id}", 302)

    swap = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap < len(steps):
        steps[idx].position, steps[swap].position = steps[swap].position, steps[idx].position
        await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


# ── Triggers ──────────────────────────────────────────────────────────────────

@router.post("/{wf_id}/triggers")
async def add_trigger(
    wf_id: int,
    trigger_type: str = Form(...),
    target_status: str = Form(default=""),
    group_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        db.add(WorkflowTrigger(
            workflow_id=wf_id,
            trigger_type=WorkflowTriggerType(trigger_type),
            target_status=target_status or None,
            group_id=int(group_id) if group_id.strip() else None,
        ))
        await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


@router.post("/{wf_id}/triggers/{tid}/delete")
async def delete_trigger(
    wf_id: int, tid: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        t = (await db.execute(
            select(WorkflowTrigger).where(WorkflowTrigger.id == tid, WorkflowTrigger.workflow_id == wf_id)
        )).scalar_one_or_none()
        if t:
            await db.delete(t)
            await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


# ── Shares ────────────────────────────────────────────────────────────────────

@router.post("/{wf_id}/shares")
async def add_share(
    wf_id: int,
    share_user_id: str = Form(default=""),
    share_group_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf and (share_user_id.strip() or share_group_id.strip()):
        uid = int(share_user_id) if share_user_id.strip() else None
        gid = int(share_group_id) if share_group_id.strip() else None
        existing = (await db.execute(
            select(WorkflowShare).where(
                WorkflowShare.workflow_id == wf_id,
                WorkflowShare.shared_with_user_id == uid,
                WorkflowShare.shared_with_group_id == gid,
            )
        )).scalar_one_or_none()
        if not existing:
            db.add(WorkflowShare(
                workflow_id=wf_id,
                shared_with_user_id=uid,
                shared_with_group_id=gid,
            ))
            await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


@router.post("/{wf_id}/shares/{shid}/delete")
async def delete_share(
    wf_id: int, shid: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    wf = (await db.execute(select(Workflow).where(
        Workflow.id == wf_id, Workflow.owner_id == current_user.id
    ))).scalar_one_or_none()
    if wf:
        sh = (await db.execute(
            select(WorkflowShare).where(WorkflowShare.id == shid, WorkflowShare.workflow_id == wf_id)
        )).scalar_one_or_none()
        if sh:
            await db.delete(sh)
            await db.commit()
    return RedirectResponse(f"/workflows/{wf_id}", 302)


# ── Manual apply ──────────────────────────────────────────────────────────────

@router.post("/{wf_id}/apply/paper/{paper_id}")
async def apply_to_paper(
    wf_id: int, paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    await apply_workflow_to_paper(db, wf_id, paper_id)
    await db.commit()
    return RedirectResponse(f"/papers/{paper_id}", 302)


@router.post("/{wf_id}/apply/me")
async def apply_to_me(
    request: Request,
    wf_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    await apply_workflow_to_user(db, wf_id, current_user.id)
    await db.commit()
    referer = request.headers.get("referer", "/workflows")
    return RedirectResponse(referer, 302)


# ── Personal todos ────────────────────────────────────────────────────────────

@router.post("/personal-todos")
async def create_personal_todo(
    title: str = Form(...),
    due_date: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    from datetime import date as _date
    db.add(PersonalTodo(
        user_id=current_user.id,
        title=title,
        due_date=_date.fromisoformat(due_date) if due_date else None,
    ))
    await db.commit()
    return RedirectResponse("/", 302)


@router.post("/personal-todos/{todo_id}/status")
async def toggle_personal_todo_status(
    todo_id: int,
    status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    todo = (await db.execute(
        select(PersonalTodo).where(PersonalTodo.id == todo_id, PersonalTodo.user_id == current_user.id)
    )).scalar_one_or_none()
    if todo:
        todo.status = TodoStatus(status)
        await db.commit()
    return RedirectResponse("/", 302)


@router.post("/personal-todos/{todo_id}/delete")
async def delete_personal_todo(
    todo_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    todo = (await db.execute(
        select(PersonalTodo).where(PersonalTodo.id == todo_id, PersonalTodo.user_id == current_user.id)
    )).scalar_one_or_none()
    if todo:
        await db.delete(todo)
        await db.commit()
    return RedirectResponse("/", 302)
