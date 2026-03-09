"""
Workflow engine — fires triggers and creates todos.

Called from:
  - app/routers/papers.py  (update_paper, update_status)
  - app/routers/groups.py  (add_member)
"""
from datetime import date, timedelta
from typing import Sequence

from sqlalchemy import select, or_, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.personal_todo import PersonalTodo
from app.models.paper import TodoItem, TodoStatus
from app.models.workflow import (
    PaperWorkflowSubscription, Workflow, WorkflowShare, WorkflowTrigger, WorkflowTriggerType,
)


# ── Visibility filter ─────────────────────────────────────────────────────────

def _visible_to(user_id: int, group_ids: Sequence[int]):
    """SQLAlchemy OR filter: workflow is owned by user, shared with user,
    shared with any of user's groups, or is public."""
    conds = [
        Workflow.owner_id == user_id,
        Workflow.is_public == True,  # noqa: E712
        exists(
            select(WorkflowShare.id).where(
                (WorkflowShare.workflow_id == Workflow.id) &
                (WorkflowShare.shared_with_user_id == user_id)
            )
        ),
    ]
    if group_ids:
        conds.append(
            exists(
                select(WorkflowShare.id).where(
                    (WorkflowShare.workflow_id == Workflow.id) &
                    (WorkflowShare.shared_with_group_id.in_(group_ids))
                )
            )
        )
    return or_(*conds)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_steps_to_paper(
    wf: Workflow,
    paper_id: int,
    existing_titles: set,
    today: date,
) -> list:
    """Create TodoItem objects for each step in wf not already present.
    Returns newly created items. Wires blocked_by from step depends_on."""
    step_to_todo: dict[int, TodoItem] = {}
    new_todos: list[TodoItem] = []

    for step in wf.steps:
        if step.title.lower() in existing_titles:
            continue
        due = (today + timedelta(days=step.due_offset_days)) if step.due_offset_days else None
        todo = TodoItem(
            paper_id=paper_id,
            title=step.title,
            description=step.description,
            status=TodoStatus.open,
            due_date=due,
            source_workflow_id=wf.id,
        )
        step_to_todo[step.id] = todo
        new_todos.append(todo)
        existing_titles.add(step.title.lower())

    # Wire blocked_by after all todos are built
    for step in wf.steps:
        if (
            step.depends_on_id
            and step.id in step_to_todo
            and step.depends_on_id in step_to_todo
        ):
            step_to_todo[step.id].blocked_by = step_to_todo[step.depends_on_id]

    return new_todos


# ── Paper status trigger ──────────────────────────────────────────────────────

async def fire_paper_status_triggers(
    db: AsyncSession,
    paper_id: int,
    new_status: str,
    user_id: int,
    user_group_ids: Sequence[int],
) -> int:
    """Create TodoItems on paper_id for every visible workflow triggered by new_status.
    Also fires workflows that the paper is explicitly subscribed to.
    Idempotent: skips steps whose title already exists as a todo on that paper."""

    # 1. Global workflows with a matching paper_status trigger, visible to the user
    result = await db.execute(
        select(Workflow)
        .join(WorkflowTrigger, WorkflowTrigger.workflow_id == Workflow.id)
        .where(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.paper_status,
            WorkflowTrigger.target_status == new_status,
            _visible_to(user_id, user_group_ids),
        )
        .options(selectinload(Workflow.steps))
        .distinct()
    )
    global_workflows = result.scalars().all()

    # 2. Paper-specific subscriptions whose workflow has a matching trigger
    sub_result = await db.execute(
        select(Workflow)
        .join(PaperWorkflowSubscription, PaperWorkflowSubscription.workflow_id == Workflow.id)
        .join(WorkflowTrigger, WorkflowTrigger.workflow_id == Workflow.id)
        .where(
            PaperWorkflowSubscription.paper_id == paper_id,
            WorkflowTrigger.trigger_type == WorkflowTriggerType.paper_status,
            WorkflowTrigger.target_status == new_status,
        )
        .options(selectinload(Workflow.steps))
        .distinct()
    )
    sub_workflows = sub_result.scalars().all()

    # Merge, deduplicate by id
    seen_ids: set[int] = set()
    workflows: list[Workflow] = []
    for wf in list(global_workflows) + list(sub_workflows):
        if wf.id not in seen_ids:
            seen_ids.add(wf.id)
            workflows.append(wf)

    if not workflows:
        return 0

    existing_titles = {
        row[0].lower() for row in (await db.execute(
            select(TodoItem.title).where(TodoItem.paper_id == paper_id)
        )).all()
    }

    today = date.today()
    created = 0
    for wf in workflows:
        new_todos = _apply_steps_to_paper(wf, paper_id, existing_titles, today)
        for todo in new_todos:
            db.add(todo)
        created += len(new_todos)

    if created:
        await db.flush()
    return created


# ── Group join trigger ────────────────────────────────────────────────────────

async def fire_group_join_triggers(
    db: AsyncSession,
    group_id: int,
    new_member_id: int,
    new_member_group_ids: Sequence[int],
) -> int:
    """Create PersonalTodos for new_member_id for every visible workflow triggered
    by joining group_id. Idempotent: skips if (title, source_workflow_id) already exists."""

    result = await db.execute(
        select(Workflow)
        .join(WorkflowTrigger, WorkflowTrigger.workflow_id == Workflow.id)
        .where(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.group_join,
            WorkflowTrigger.group_id == group_id,
            _visible_to(new_member_id, new_member_group_ids),
        )
        .options(selectinload(Workflow.steps))
        .distinct()
    )
    workflows = result.scalars().all()
    if not workflows:
        return 0

    existing = {
        (row[0].lower(), row[1]) for row in (await db.execute(
            select(PersonalTodo.title, PersonalTodo.source_workflow_id)
            .where(PersonalTodo.user_id == new_member_id)
        )).all()
    }

    today = date.today()
    created = 0
    for wf in workflows:
        for step in wf.steps:
            key = (step.title.lower(), wf.id)
            if key in existing:
                continue
            due = (today + timedelta(days=step.due_offset_days)) if step.due_offset_days else None
            db.add(PersonalTodo(
                user_id=new_member_id,
                title=step.title,
                description=step.description,
                status=TodoStatus.open,
                due_date=due,
                source_workflow_id=wf.id,
            ))
            existing.add(key)
            created += 1

    if created:
        await db.flush()
    return created


# ── Manual apply ─────────────────────────────────────────────────────────────

async def apply_workflow_to_paper(
    db: AsyncSession,
    workflow_id: int,
    paper_id: int,
) -> int:
    """Manually apply a workflow's steps as paper todos. Idempotent."""
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
        .options(selectinload(Workflow.steps))
    )
    wf = result.scalar_one_or_none()
    if not wf:
        return 0

    existing_titles = {
        row[0].lower() for row in (await db.execute(
            select(TodoItem.title).where(TodoItem.paper_id == paper_id)
        )).all()
    }

    today = date.today()
    new_todos = _apply_steps_to_paper(wf, paper_id, existing_titles, today)
    for todo in new_todos:
        db.add(todo)

    if new_todos:
        await db.flush()
    return len(new_todos)


async def apply_workflow_to_user(
    db: AsyncSession,
    workflow_id: int,
    user_id: int,
) -> int:
    """Manually apply a workflow's steps as personal todos. Idempotent."""
    result = await db.execute(
        select(Workflow).where(Workflow.id == workflow_id)
        .options(selectinload(Workflow.steps))
    )
    wf = result.scalar_one_or_none()
    if not wf:
        return 0

    existing = {
        (row[0].lower(), row[1]) for row in (await db.execute(
            select(PersonalTodo.title, PersonalTodo.source_workflow_id)
            .where(PersonalTodo.user_id == user_id)
        )).all()
    }

    today = date.today()
    created = 0
    for step in wf.steps:
        key = (step.title.lower(), wf.id)
        if key in existing:
            continue
        due = (today + timedelta(days=step.due_offset_days)) if step.due_offset_days else None
        db.add(PersonalTodo(
            user_id=user_id,
            title=step.title,
            description=step.description,
            status=TodoStatus.open,
            due_date=due,
            source_workflow_id=wf.id,
        ))
        existing.add(key)
        created += 1

    if created:
        await db.flush()
    return created
