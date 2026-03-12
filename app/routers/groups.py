import os

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.group import (
    GroupMembership, GroupReviewAssignment, GroupReviewBalance,
    GroupReviewRequest, GroupReviewRequestStatus, GroupRole, ResearchGroup,
)
from app.models.paper import PaperAuthor, PaperGroupShare, PaperProject, PAPER_STATUS_LABELS, PAPER_STATUS_COLORS
from app.models.user import User
from app.models.wiki import WikiPage
from app.models.bibtex import BibCollection, BibCollectionShare
from app.models.workflow import Workflow, WorkflowShare, WorkflowTrigger
from app.routers.papers import _visibility_filter
from app.workflow_engine import fire_group_join_triggers

router = APIRouter(prefix="/groups", tags=["groups"])
PAGE_SIZE = 25

_LOGO_DIR = "static/uploads/group_logos"
_ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/svg+xml"}
_LOGO_EXTS = {"image/png": ".png", "image/jpeg": ".jpg", "image/svg+xml": ".svg"}


async def _save_logo(logo: UploadFile, group_id: int) -> str | None:
    if not logo or not logo.filename or logo.content_type not in _ALLOWED_LOGO_TYPES:
        return None
    os.makedirs(_LOGO_DIR, exist_ok=True)
    ext = _LOGO_EXTS[logo.content_type]
    path = os.path.join(_LOGO_DIR, f"{group_id}{ext}")
    # Remove old files with any extension before saving new one
    for old_ext in _LOGO_EXTS.values():
        old_path = os.path.join(_LOGO_DIR, f"{group_id}{old_ext}")
        if os.path.exists(old_path):
            os.remove(old_path)
    content = await logo.read()
    with open(path, "wb") as f:
        f.write(content)
    return f"/static/uploads/group_logos/{group_id}{ext}"


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": "groups", **kw}


async def _check_group_admin(db: AsyncSession, group_id: int, current_user) -> bool:
    """Return True if current_user is a site admin or a group admin of group_id."""
    if current_user.is_admin:
        return True
    m = (await db.execute(
        select(GroupMembership).where(
            (GroupMembership.group_id == group_id) &
            (GroupMembership.user_id == current_user.id) &
            (GroupMembership.role == GroupRole.admin)
        )
    )).scalar_one_or_none()
    return m is not None


@router.get("", response_class=HTMLResponse)
async def list_groups(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(ResearchGroup)
        .options(
            selectinload(ResearchGroup.memberships).selectinload(GroupMembership.user),
            selectinload(ResearchGroup.subgroups),
            selectinload(ResearchGroup.parent),
        )
        .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
        .where(GroupMembership.user_id == current_user.id)
        .order_by(ResearchGroup.name)
    )
    groups = result.scalars().all()
    return templates.TemplateResponse(request, "groups/list.html",
                                      _ctx(request, current_user, groups=groups))


@router.get("/new", response_class=HTMLResponse)
async def new_group_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    all_groups = (await db.execute(
        select(ResearchGroup)
        .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
        .where(GroupMembership.user_id == current_user.id)
        .order_by(ResearchGroup.name)
    )).scalars().all()
    return templates.TemplateResponse(request, "groups/form.html",
                                      _ctx(request, current_user, group=None,
                                           all_groups=all_groups, action="/groups"))


@router.post("")
async def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    parent_group_id: str = Form(default=""),
    logo: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group = ResearchGroup(
        name=name, description=description or None,
        parent_group_id=int(parent_group_id) if parent_group_id else None,
    )
    db.add(group)
    await db.flush()
    logo_path = await _save_logo(logo, group.id)
    if logo_path:
        group.logo_path = logo_path
    # Creator becomes admin
    db.add(GroupMembership(group_id=group.id, user_id=current_user.id, role=GroupRole.admin))
    await db.commit()
    return RedirectResponse(f"/groups/{group.id}", 302)


@router.get("/{group_id}", response_class=HTMLResponse)
async def group_detail(
    request: Request, group_id: int,
    tab: str = "overview",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(ResearchGroup)
        .options(
            selectinload(ResearchGroup.memberships).selectinload(GroupMembership.user),
            selectinload(ResearchGroup.subgroups),
            selectinload(ResearchGroup.paper_shares).selectinload(PaperGroupShare.paper)
            .selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author),
        )
        .where(ResearchGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        return RedirectResponse("/groups", 302)

    is_member = any(m.user_id == current_user.id for m in group.memberships)
    if not is_member and not current_user.is_admin:
        return RedirectResponse("/groups", 302)

    is_admin = current_user.is_admin or any(
        m.user_id == current_user.id and m.role == GroupRole.admin
        for m in group.memberships
    )
    all_users = (await db.execute(select(User).where(User.is_active == True))).scalars().all()
    all_papers = (await db.execute(
        select(PaperProject)
        .where(_visibility_filter(current_user.id, current_user.author_id))
        .order_by(PaperProject.title)
    )).scalars().all()

    # Review exchange data
    review_requests = (await db.execute(
        select(GroupReviewRequest)
        .options(
            selectinload(GroupReviewRequest.requester),
            selectinload(GroupReviewRequest.paper),
            selectinload(GroupReviewRequest.assignment).selectinload(GroupReviewAssignment.reviewer),
        )
        .where(GroupReviewRequest.group_id == group_id)
        .where(GroupReviewRequest.status != GroupReviewRequestStatus.cancelled)
        .order_by(GroupReviewRequest.created_at.desc())
    )).scalars().all()

    my_balance_row = (await db.execute(
        select(GroupReviewBalance).where(
            GroupReviewBalance.group_id == group_id,
            GroupReviewBalance.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    my_balance = my_balance_row.balance if my_balance_row else 0

    wiki_pages = (await db.execute(
        select(WikiPage)
        .where(WikiPage.group_id == group_id)
        .order_by(WikiPage.is_pinned.desc(), WikiPage.updated_at.desc())
    )).scalars().all()

    # Workflows shared with this group
    group_workflows = (await db.execute(
        select(Workflow)
        .join(WorkflowShare, WorkflowShare.workflow_id == Workflow.id)
        .where(WorkflowShare.shared_with_group_id == group_id)
        .options(
            selectinload(Workflow.steps),
            selectinload(Workflow.triggers).selectinload(WorkflowTrigger.group),
            selectinload(Workflow.owner),
        )
        .order_by(Workflow.name)
        .distinct()
    )).scalars().all()

    # BibTeX collections shared with this group
    group_bibtex = (await db.execute(
        select(BibCollection)
        .join(BibCollectionShare, BibCollectionShare.collection_id == BibCollection.id)
        .where(BibCollectionShare.group_id == group_id)
        .options(
            selectinload(BibCollection.owner),
            selectinload(BibCollection.entries),
        )
        .order_by(BibCollection.name)
    )).scalars().all()

    return templates.TemplateResponse(
        request, "groups/detail.html",
        _ctx(request, current_user, group=group, is_admin=is_admin,
             all_users=all_users, all_papers=all_papers,
             status_labels=PAPER_STATUS_LABELS, status_colors=PAPER_STATUS_COLORS,
             review_requests=review_requests, my_balance=my_balance,
             wiki_pages=wiki_pages, group_workflows=group_workflows,
             group_bibtex=group_bibtex,
             active_tab=tab),
    )


@router.get("/{group_id}/edit", response_class=HTMLResponse)
async def edit_group_form(
    request: Request, group_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(ResearchGroup)
        .options(selectinload(ResearchGroup.memberships))
        .where(ResearchGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        return RedirectResponse("/groups", 302)
    is_admin = current_user.is_admin or any(
        m.user_id == current_user.id and m.role == GroupRole.admin for m in group.memberships
    )
    if not is_admin:
        return RedirectResponse(f"/groups/{group_id}", 302)
    all_groups = (await db.execute(
        select(ResearchGroup)
        .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
        .where(GroupMembership.user_id == current_user.id)
        .where(ResearchGroup.id != group_id)
        .order_by(ResearchGroup.name)
    )).scalars().all()
    return templates.TemplateResponse(request, "groups/form.html",
                                      _ctx(request, current_user, group=group,
                                           all_groups=all_groups,
                                           action=f"/groups/{group_id}/edit"))


@router.post("/{group_id}/edit")
async def update_group(
    group_id: int,
    name: str = Form(...),
    description: str = Form(default=""),
    parent_group_id: str = Form(default=""),
    logo: UploadFile = File(default=None),
    remove_logo: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(ResearchGroup).options(selectinload(ResearchGroup.memberships))
        .where(ResearchGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        return RedirectResponse("/groups", 302)
    is_admin = current_user.is_admin or any(
        m.user_id == current_user.id and m.role == GroupRole.admin for m in group.memberships
    )
    if not is_admin:
        return RedirectResponse(f"/groups/{group_id}", 302)
    group.name = name
    group.description = description or None
    group.parent_group_id = int(parent_group_id) if parent_group_id else None
    if remove_logo:
        for ext in _LOGO_EXTS.values():
            p = os.path.join(_LOGO_DIR, f"{group_id}{ext}")
            if os.path.exists(p):
                os.remove(p)
        group.logo_path = None
    else:
        logo_path = await _save_logo(logo, group_id)
        if logo_path:
            group.logo_path = logo_path
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/delete")
async def delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    result = await db.execute(
        select(ResearchGroup).options(selectinload(ResearchGroup.memberships))
        .where(ResearchGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if group:
        is_admin = current_user.is_admin or any(
            m.user_id == current_user.id and m.role == GroupRole.admin for m in group.memberships
        )
        if is_admin:
            for ext in _LOGO_EXTS.values():
                p = os.path.join(_LOGO_DIR, f"{group_id}{ext}")
                if os.path.exists(p):
                    os.remove(p)
            await db.delete(group)
            await db.commit()
    return RedirectResponse("/groups", 302)


@router.post("/{group_id}/members/add")
async def add_member(
    group_id: int,
    user_id: int = Form(...),
    role: str = Form(default="member"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    if not await _check_group_admin(db, group_id, current_user):
        return RedirectResponse(f"/groups/{group_id}", 302)
    existing = (await db.execute(
        select(GroupMembership).where(
            (GroupMembership.group_id == group_id) & (GroupMembership.user_id == user_id)
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(GroupMembership(group_id=group_id, user_id=user_id, role=GroupRole(role)))
        await db.commit()
        # Fire group_join workflow triggers for the new member
        member_group_ids = [r[0] for r in (await db.execute(
            select(GroupMembership.group_id).where(GroupMembership.user_id == user_id)
        )).all()]
        await fire_group_join_triggers(db, group_id, user_id, member_group_ids)
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/members/{user_id}/remove")
async def remove_member(
    group_id: int, user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    if not await _check_group_admin(db, group_id, current_user):
        return RedirectResponse(f"/groups/{group_id}", 302)
    result = await db.execute(
        select(GroupMembership).where(
            (GroupMembership.group_id == group_id) & (GroupMembership.user_id == user_id)
        )
    )
    m = result.scalar_one_or_none()
    if m:
        await db.delete(m)
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/papers/add")
async def share_paper(
    group_id: int,
    paper_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    if not await _check_group_admin(db, group_id, current_user):
        return RedirectResponse(f"/groups/{group_id}", 302)
    existing = (await db.execute(
        select(PaperGroupShare).where(
            (PaperGroupShare.group_id == group_id) & (PaperGroupShare.paper_id == paper_id)
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(PaperGroupShare(group_id=group_id, paper_id=paper_id))
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/papers/{paper_id}/remove")
async def unshare_paper(
    group_id: int, paper_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    if not await _check_group_admin(db, group_id, current_user):
        return RedirectResponse(f"/groups/{group_id}", 302)
    result = await db.execute(
        select(PaperGroupShare).where(
            (PaperGroupShare.group_id == group_id) & (PaperGroupShare.paper_id == paper_id)
        )
    )
    share = result.scalar_one_or_none()
    if share:
        await db.delete(share)
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


# ── Review exchange helpers ────────────────────────────────────────────────────

async def _get_or_create_balance(db: AsyncSession, group_id: int, user_id: int) -> GroupReviewBalance:
    row = (await db.execute(
        select(GroupReviewBalance).where(
            GroupReviewBalance.group_id == group_id,
            GroupReviewBalance.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not row:
        row = GroupReviewBalance(group_id=group_id, user_id=user_id, balance=0)
        db.add(row)
        await db.flush()
    return row


async def _get_review_request(db: AsyncSession, group_id: int, request_id: int) -> GroupReviewRequest | None:
    return (await db.execute(
        select(GroupReviewRequest)
        .options(selectinload(GroupReviewRequest.assignment))
        .where(GroupReviewRequest.id == request_id, GroupReviewRequest.group_id == group_id)
    )).scalar_one_or_none()


# ── Review exchange endpoints ──────────────────────────────────────────────────

@router.post("/{group_id}/reviews/request")
async def request_review(
    group_id: int,
    paper_id: str = Form(default=""),
    notes: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    # Must be a group member
    membership = (await db.execute(
        select(GroupMembership).where(
            GroupMembership.group_id == group_id,
            GroupMembership.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not membership:
        return RedirectResponse(f"/groups/{group_id}", 302)

    balance = await _get_or_create_balance(db, group_id, current_user.id)
    balance.balance -= 1
    db.add(GroupReviewRequest(
        group_id=group_id,
        requester_id=current_user.id,
        paper_id=int(paper_id) if paper_id else None,
        notes=notes.strip() or None,
        status=GroupReviewRequestStatus.open,
    ))
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/reviews/{request_id}/accept")
async def accept_review(
    group_id: int, request_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    req = await _get_review_request(db, group_id, request_id)
    if not req or req.status != GroupReviewRequestStatus.open or req.requester_id == current_user.id:
        return RedirectResponse(f"/groups/{group_id}", 302)

    req.status = GroupReviewRequestStatus.assigned
    db.add(GroupReviewAssignment(request_id=req.id, reviewer_id=current_user.id))
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/reviews/{request_id}/complete")
async def complete_review(
    group_id: int, request_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    req = await _get_review_request(db, group_id, request_id)
    if not req or req.status != GroupReviewRequestStatus.assigned:
        return RedirectResponse(f"/groups/{group_id}", 302)
    if not req.assignment or req.assignment.reviewer_id != current_user.id:
        return RedirectResponse(f"/groups/{group_id}", 302)

    from datetime import datetime
    req.assignment.completed_at = datetime.utcnow()
    req.status = GroupReviewRequestStatus.completed
    balance = await _get_or_create_balance(db, group_id, current_user.id)
    balance.balance += 1
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)


@router.post("/{group_id}/reviews/{request_id}/cancel")
async def cancel_review_request(
    group_id: int, request_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    req = await _get_review_request(db, group_id, request_id)
    if not req or req.requester_id != current_user.id:
        return RedirectResponse(f"/groups/{group_id}", 302)
    if req.status not in (GroupReviewRequestStatus.open, GroupReviewRequestStatus.assigned):
        return RedirectResponse(f"/groups/{group_id}", 302)

    # Refund the balance point
    balance = await _get_or_create_balance(db, group_id, current_user.id)
    balance.balance += 1
    # If already assigned, the reviewer loses their pending review
    req.status = GroupReviewRequestStatus.cancelled
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}", 302)
