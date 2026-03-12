import os
import re
import shutil
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.paper import TodoStatus
from app.models.supervision import (
    DOCUMENT_TYPE_LABELS,
    PROJECT_TYPE_LABELS,
    SupervisionDocument,
    SupervisionDocumentType,
    SupervisionProject,
    SupervisionProjectType,
    SupervisionStatus,
    SupervisionTodo,
    SupervisionTypeWorkflowConfig,
)
from app.models.workflow import Workflow
from app.routers.auth import get_current_user

router = APIRouter(prefix="/supervision", tags=["supervision"])

UPLOAD_DIR = "static/uploads/supervision"

_STATUS_ORDER = [
    SupervisionStatus.incubating,
    SupervisionStatus.ongoing,
    SupervisionStatus.submitted,
    SupervisionStatus.archived,
]

_TODO_NEXT = {
    TodoStatus.open:        TodoStatus.in_progress,
    TodoStatus.in_progress: TodoStatus.done,
    TodoStatus.done:        TodoStatus.open,
}


def _ctx(request: Request, current_user, **kw):
    return {
        "request": request,
        "current_user": current_user,
        "active_page": "supervision",
        "project_types": list(SupervisionProjectType),
        "project_type_labels": PROJECT_TYPE_LABELS,
        "statuses": list(SupervisionStatus),
        "doc_types": list(SupervisionDocumentType),
        "doc_type_labels": DOCUMENT_TYPE_LABELS,
        "todo_statuses": list(TodoStatus),
        **kw,
    }


async def _get_project(project_id: int, user_id: int, db: AsyncSession):
    """Load a project with all eager-loaded relations, verify ownership."""
    result = await db.execute(
        select(SupervisionProject)
        .options(
            selectinload(SupervisionProject.todos).selectinload(
                SupervisionTodo.source_workflow_step
            ),
            selectinload(SupervisionProject.documents),
        )
        .where(
            SupervisionProject.id == project_id,
            SupervisionProject.supervisor_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _seed_todos(project: SupervisionProject, user_id: int, db: AsyncSession):
    """Populate todos from the configured workflow for this project type (if any)."""
    cfg_result = await db.execute(
        select(SupervisionTypeWorkflowConfig).where(
            SupervisionTypeWorkflowConfig.user_id == user_id,
            SupervisionTypeWorkflowConfig.project_type == project.project_type,
        )
    )
    cfg = cfg_result.scalar_one_or_none()
    if not cfg or not cfg.workflow_id:
        return

    wf_result = await db.execute(
        select(Workflow)
        .options(selectinload(Workflow.steps))
        .where(Workflow.id == cfg.workflow_id)
    )
    wf = wf_result.scalar_one_or_none()
    if not wf:
        return

    for i, step in enumerate(sorted(wf.steps, key=lambda s: s.position)):
        db.add(SupervisionTodo(
            project_id=project.id,
            title=step.title,
            description=step.description,
            position=i,
            source_workflow_step_id=step.id,
        ))


# ── List ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_supervision(
    request: Request,
    tab: str = "ongoing",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    projects = (await db.execute(
        select(SupervisionProject)
        .options(
            selectinload(SupervisionProject.todos),
            selectinload(SupervisionProject.documents),
        )
        .where(SupervisionProject.supervisor_id == current_user.id)
        .order_by(SupervisionProject.updated_at.desc())
    )).scalars().all()

    by_status: dict[SupervisionStatus, list] = {s: [] for s in _STATUS_ORDER}
    for p in projects:
        by_status[p.status].append(p)

    return templates.TemplateResponse(
        request, "supervision/list.html",
        _ctx(request, current_user, by_status=by_status, tab=tab),
    )


# ── Settings ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def supervision_settings_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    configs = (await db.execute(
        select(SupervisionTypeWorkflowConfig)
        .options(selectinload(SupervisionTypeWorkflowConfig.workflow))
        .where(SupervisionTypeWorkflowConfig.user_id == current_user.id)
    )).scalars().all()
    config_map = {c.project_type: c for c in configs}

    workflows = (await db.execute(
        select(Workflow)
        .options(selectinload(Workflow.steps))
        .where(Workflow.owner_id == current_user.id)
        .order_by(Workflow.name)
    )).scalars().all()

    return templates.TemplateResponse(
        request, "supervision/settings.html",
        _ctx(request, current_user, config_map=config_map, workflows=workflows),
    )


@router.post("/settings")
async def supervision_settings_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    form = await request.form()
    for pt in SupervisionProjectType:
        raw = form.get(f"workflow_{pt.value}", "")
        wf_id = int(raw) if raw else None

        existing = (await db.execute(
            select(SupervisionTypeWorkflowConfig).where(
                SupervisionTypeWorkflowConfig.user_id == current_user.id,
                SupervisionTypeWorkflowConfig.project_type == pt,
            )
        )).scalar_one_or_none()

        if existing:
            existing.workflow_id = wf_id
        else:
            db.add(SupervisionTypeWorkflowConfig(
                user_id=current_user.id, project_type=pt, workflow_id=wf_id,
            ))

    await db.commit()
    return RedirectResponse("/supervision/settings", 302)


# ── New / Create ─────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_supervision_form(
    request: Request,
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse(
        request, "supervision/form.html",
        _ctx(request, current_user, project=None, action="/supervision"),
    )


@router.post("", response_class=HTMLResponse)
async def create_supervision(
    request: Request,
    title: str = Form(...),
    project_type: str = Form(...),
    status: str = Form(default="incubating"),
    student_name: str = Form(default=""),
    student_email: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    github_url: str = Form(default=""),
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = SupervisionProject(
        supervisor_id=current_user.id,
        title=title,
        project_type=SupervisionProjectType(project_type),
        status=SupervisionStatus(status),
        student_name=student_name or None,
        student_email=student_email or None,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
        github_url=github_url or None,
        notes=notes or None,
    )
    db.add(project)
    await db.flush()
    await _seed_todos(project, current_user.id, db)
    await db.commit()
    return RedirectResponse(f"/supervision/{project.id}", 302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}", response_class=HTMLResponse)
async def supervision_detail(
    request: Request,
    project_id: int,
    tab: str = "overview",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = await _get_project(project_id, current_user.id, db)
    if not project:
        return RedirectResponse("/supervision", 302)

    done_count = sum(1 for t in project.todos if t.status == TodoStatus.done)
    docs_by_type: dict[SupervisionDocumentType, list] = {
        dt: [] for dt in SupervisionDocumentType
    }
    for doc in project.documents:
        docs_by_type[doc.document_type].append(doc)

    return templates.TemplateResponse(
        request, "supervision/detail.html",
        _ctx(
            request, current_user,
            project=project, tab=tab,
            done_count=done_count, total_count=len(project.todos),
            docs_by_type=docs_by_type,
        ),
    )


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/{project_id}/edit", response_class=HTMLResponse)
async def edit_supervision_form(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = await _get_project(project_id, current_user.id, db)
    if not project:
        return RedirectResponse("/supervision", 302)

    return templates.TemplateResponse(
        request, "supervision/form.html",
        _ctx(request, current_user, project=project,
             action=f"/supervision/{project_id}/edit"),
    )


@router.post("/{project_id}/edit")
async def update_supervision(
    project_id: int,
    title: str = Form(...),
    project_type: str = Form(...),
    status: str = Form(default="incubating"),
    student_name: str = Form(default=""),
    student_email: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    github_url: str = Form(default=""),
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = await _get_project(project_id, current_user.id, db)
    if not project:
        return RedirectResponse("/supervision", 302)

    project.title = title
    project.project_type = SupervisionProjectType(project_type)
    project.status = SupervisionStatus(status)
    project.student_name = student_name or None
    project.student_email = student_email or None
    project.start_date = date.fromisoformat(start_date) if start_date else None
    project.end_date = date.fromisoformat(end_date) if end_date else None
    project.github_url = github_url or None
    project.notes = notes or None
    await db.commit()
    return RedirectResponse(f"/supervision/{project_id}", 302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{project_id}/delete")
async def delete_supervision(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = await _get_project(project_id, current_user.id, db)
    if project:
        # Remove uploaded files
        upload_dir = os.path.join(UPLOAD_DIR, str(project_id))
        if os.path.isdir(upload_dir):
            shutil.rmtree(upload_dir)
        await db.delete(project)
        await db.commit()
    return RedirectResponse("/supervision", 302)


# ── Todos ─────────────────────────────────────────────────────────────────────

@router.post("/{project_id}/todos")
async def add_todo(
    project_id: int,
    title: str = Form(...),
    due_date: str = Form(default=""),
    description: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = await _get_project(project_id, current_user.id, db)
    if not project:
        return RedirectResponse("/supervision", 302)

    # Position = next after existing
    position = len(project.todos)
    db.add(SupervisionTodo(
        project_id=project_id,
        title=title,
        description=description or None,
        due_date=date.fromisoformat(due_date) if due_date else None,
        position=position,
    ))
    await db.commit()
    return RedirectResponse(f"/supervision/{project_id}?tab=todos", 302)


@router.post("/{project_id}/todos/{todo_id}/toggle")
async def toggle_todo(
    project_id: int,
    todo_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    todo = (await db.execute(
        select(SupervisionTodo).where(
            SupervisionTodo.id == todo_id,
            SupervisionTodo.project_id == project_id,
        )
    )).scalar_one_or_none()

    if todo:
        todo.status = _TODO_NEXT[todo.status]
        await db.commit()

    return RedirectResponse(f"/supervision/{project_id}?tab=todos", 302)


@router.post("/{project_id}/todos/{todo_id}/delete")
async def delete_todo(
    project_id: int,
    todo_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    todo = (await db.execute(
        select(SupervisionTodo).where(
            SupervisionTodo.id == todo_id,
            SupervisionTodo.project_id == project_id,
        )
    )).scalar_one_or_none()

    if todo:
        await db.delete(todo)
        await db.commit()

    return RedirectResponse(f"/supervision/{project_id}?tab=todos", 302)


# ── Documents ─────────────────────────────────────────────────────────────────

@router.post("/{project_id}/documents")
async def add_document(
    project_id: int,
    label: str = Form(...),
    document_type: str = Form(default="other"),
    url: str = Form(default=""),
    file: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    project = await _get_project(project_id, current_user.id, db)
    if not project:
        return RedirectResponse("/supervision", 302)

    file_path = None
    if file and file.filename:
        upload_dir = os.path.join(UPLOAD_DIR, str(project_id))
        os.makedirs(upload_dir, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-]", "_", file.filename)
        dest = os.path.join(upload_dir, safe_name)
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        file_path = dest

    db.add(SupervisionDocument(
        project_id=project_id,
        label=label,
        document_type=SupervisionDocumentType(document_type),
        file_path=file_path,
        url=url or None,
        uploaded_by=current_user.id,
    ))
    await db.commit()
    return RedirectResponse(f"/supervision/{project_id}?tab=documents", 302)


@router.post("/{project_id}/documents/{doc_id}/delete")
async def delete_document(
    project_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    # Verify ownership via project
    project = await _get_project(project_id, current_user.id, db)
    if not project:
        return RedirectResponse("/supervision", 302)

    doc = (await db.execute(
        select(SupervisionDocument).where(
            SupervisionDocument.id == doc_id,
            SupervisionDocument.project_id == project_id,
        )
    )).scalar_one_or_none()

    if doc:
        if doc.file_path and os.path.exists(doc.file_path):
            os.remove(doc.file_path)
        await db.delete(doc)
        await db.commit()

    return RedirectResponse(f"/supervision/{project_id}?tab=documents", 302)
