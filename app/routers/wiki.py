"""
Group wiki: collaboratively-editable Markdown pages with pessimistic edit locking
and full revision history.
"""
import re
from datetime import datetime, timezone

import bleach
import markdown as md
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.group import GroupMembership, GroupRole, ResearchGroup
from app.models.wiki import LOCK_TTL, WikiPage, WikiPageRevision

router = APIRouter(prefix="/groups", tags=["wiki"])
templates = Jinja2Templates(directory="app/templates")

ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "p", "pre", "code", "h1", "h2", "h3", "h4", "h5", "ul", "ol", "li", "blockquote",
]


def _render_md(text: str) -> str:
    html = md.markdown(text, extensions=["fenced_code", "tables"])
    return bleach.clean(html, tags=ALLOWED_TAGS, strip=True)


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": "groups", **kw}


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "page"


async def _unique_slug(group_id: int, base: str, db: AsyncSession, exclude_id: int | None = None) -> str:
    slug = base
    n = 2
    while True:
        existing = (await db.execute(
            select(WikiPage).where(WikiPage.group_id == group_id, WikiPage.slug == slug)
        )).scalar_one_or_none()
        if not existing or (exclude_id and existing.id == exclude_id):
            return slug
        slug = f"{base}-{n}"
        n += 1


async def _require_member(group_id: int, current_user, db: AsyncSession):
    """Return (group, is_admin) or None if not a member."""
    group = (await db.execute(
        select(ResearchGroup)
        .options(selectinload(ResearchGroup.memberships))
        .where(ResearchGroup.id == group_id)
    )).scalar_one_or_none()
    if not group:
        return None, False
    is_admin = current_user.is_admin or any(
        m.user_id == current_user.id and m.role == GroupRole.admin
        for m in group.memberships
    )
    is_member = is_admin or any(m.user_id == current_user.id for m in group.memberships)
    if not is_member:
        return None, False
    return group, is_admin


async def _get_page(group_id: int, slug: str, db: AsyncSession) -> WikiPage | None:
    return (await db.execute(
        select(WikiPage)
        .options(
            selectinload(WikiPage.created_by),
            selectinload(WikiPage.locked_by),
            selectinload(WikiPage.revisions).selectinload(WikiPageRevision.edited_by),
        )
        .where(WikiPage.group_id == group_id, WikiPage.slug == slug)
    )).scalar_one_or_none()


async def _save_revision(page: WikiPage, user_id: int, edit_note: str, db: AsyncSession) -> None:
    db.add(WikiPageRevision(
        page_id=page.id,
        body=page.body,
        edited_by_id=user_id,
        edit_note=edit_note or None,
    ))


# ── Wiki index ────────────────────────────────────────────────────────────────

@router.get("/{group_id}/wiki", response_class=HTMLResponse)
async def wiki_index(
    request: Request,
    group_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, is_admin = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)

    pages = (await db.execute(
        select(WikiPage)
        .options(selectinload(WikiPage.created_by), selectinload(WikiPage.locked_by))
        .where(WikiPage.group_id == group_id)
        .order_by(WikiPage.is_pinned.desc(), WikiPage.updated_at.desc())
    )).scalars().all()

    return templates.TemplateResponse(
        request, "wiki/index.html",
        _ctx(request, current_user, group=group, pages=pages, is_admin=is_admin),
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.get("/{group_id}/wiki/new", response_class=HTMLResponse)
async def new_page_form(
    request: Request,
    group_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, is_admin = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    return templates.TemplateResponse(
        request, "wiki/form.html",
        _ctx(request, current_user, group=group, page=None, is_admin=is_admin,
             action=f"/groups/{group_id}/wiki"),
    )


@router.post("/{group_id}/wiki")
async def create_page(
    group_id: int,
    title: str = Form(...),
    body: str = Form(default=""),
    edit_note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, _ = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)

    slug = await _unique_slug(group_id, _slugify(title), db)
    page = WikiPage(
        group_id=group_id, title=title, slug=slug, body=body,
        created_by_id=current_user.id,
    )
    db.add(page)
    await db.flush()
    await _save_revision(page, current_user.id, edit_note or "Initial version", db)
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)


# ── View ──────────────────────────────────────────────────────────────────────

@router.get("/{group_id}/wiki/{slug}", response_class=HTMLResponse)
async def view_page(
    request: Request,
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, is_admin = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    page = await _get_page(group_id, slug, db)
    if not page:
        return RedirectResponse(f"/groups/{group_id}/wiki", 302)

    rendered = _render_md(page.body) if page.body else ""
    latest_rev = page.revisions[0] if page.revisions else None
    return templates.TemplateResponse(
        request, "wiki/view.html",
        _ctx(request, current_user, group=group, page=page, rendered=rendered,
             latest_rev=latest_rev, is_admin=is_admin, LOCK_TTL=LOCK_TTL),
    )


# ── Edit (acquire lock) ───────────────────────────────────────────────────────

@router.get("/{group_id}/wiki/{slug}/edit", response_class=HTMLResponse)
async def edit_page_form(
    request: Request,
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, is_admin = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    page = await _get_page(group_id, slug, db)
    if not page:
        return RedirectResponse(f"/groups/{group_id}/wiki", 302)

    locked_by_other = page.locked_by_other(current_user.id)

    if not locked_by_other:
        # Acquire or refresh lock
        page.locked_by_id = current_user.id
        page.locked_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(page)

    return templates.TemplateResponse(
        request, "wiki/form.html",
        _ctx(request, current_user, group=group, page=page, is_admin=is_admin,
             locked_by_other=locked_by_other,
             action=f"/groups/{group_id}/wiki/{slug}/edit"),
    )


@router.post("/{group_id}/wiki/{slug}/edit")
async def save_page(
    group_id: int,
    slug: str,
    body: str = Form(default=""),
    edit_note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, _ = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    page = await _get_page(group_id, slug, db)
    if not page:
        return RedirectResponse(f"/groups/{group_id}/wiki", 302)

    # Only save if we hold the lock (or lock expired)
    if page.locked_by_id not in (None, current_user.id) and page.is_locked:
        return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)

    page.body = body
    page.locked_by_id = None
    page.locked_at = None
    await _save_revision(page, current_user.id, edit_note, db)
    await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)


@router.post("/{group_id}/wiki/{slug}/cancel-edit")
async def cancel_edit(
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    page = (await db.execute(
        select(WikiPage).where(WikiPage.group_id == group_id, WikiPage.slug == slug)
    )).scalar_one_or_none()
    if page and page.locked_by_id == current_user.id:
        page.locked_by_id = None
        page.locked_at = None
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)


# ── Force unlock (admin) ──────────────────────────────────────────────────────

@router.post("/{group_id}/wiki/{slug}/force-unlock")
async def force_unlock(
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    _, is_admin = await _require_member(group_id, current_user, db)
    if not is_admin:
        return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)
    page = (await db.execute(
        select(WikiPage).where(WikiPage.group_id == group_id, WikiPage.slug == slug)
    )).scalar_one_or_none()
    if page:
        page.locked_by_id = None
        page.locked_at = None
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)


# ── Pin toggle ────────────────────────────────────────────────────────────────

@router.post("/{group_id}/wiki/{slug}/pin")
async def toggle_pin(
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    _, is_admin = await _require_member(group_id, current_user, db)
    if not is_admin:
        return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)
    page = (await db.execute(
        select(WikiPage).where(WikiPage.group_id == group_id, WikiPage.slug == slug)
    )).scalar_one_or_none()
    if page:
        page.is_pinned = not page.is_pinned
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{group_id}/wiki/{slug}/delete")
async def delete_page(
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    _, is_admin = await _require_member(group_id, current_user, db)
    if not is_admin:
        return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)
    page = (await db.execute(
        select(WikiPage).where(WikiPage.group_id == group_id, WikiPage.slug == slug)
    )).scalar_one_or_none()
    if page:
        await db.delete(page)
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki", 302)


# ── Revision history ──────────────────────────────────────────────────────────

@router.get("/{group_id}/wiki/{slug}/history", response_class=HTMLResponse)
async def page_history(
    request: Request,
    group_id: int,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, is_admin = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    page = await _get_page(group_id, slug, db)
    if not page:
        return RedirectResponse(f"/groups/{group_id}/wiki", 302)
    return templates.TemplateResponse(
        request, "wiki/history.html",
        _ctx(request, current_user, group=group, page=page, is_admin=is_admin),
    )


@router.get("/{group_id}/wiki/{slug}/history/{rev_id}", response_class=HTMLResponse)
async def view_revision(
    request: Request,
    group_id: int,
    slug: str,
    rev_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, is_admin = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    page = await _get_page(group_id, slug, db)
    if not page:
        return RedirectResponse(f"/groups/{group_id}/wiki", 302)

    revision = (await db.execute(
        select(WikiPageRevision)
        .options(selectinload(WikiPageRevision.edited_by))
        .where(WikiPageRevision.id == rev_id, WikiPageRevision.page_id == page.id)
    )).scalar_one_or_none()
    if not revision:
        return RedirectResponse(f"/groups/{group_id}/wiki/{slug}/history", 302)

    rendered = _render_md(revision.body) if revision.body else ""
    return templates.TemplateResponse(
        request, "wiki/revision.html",
        _ctx(request, current_user, group=group, page=page, revision=revision,
             rendered=rendered, is_admin=is_admin),
    )


@router.post("/{group_id}/wiki/{slug}/history/{rev_id}/restore")
async def restore_revision(
    group_id: int,
    slug: str,
    rev_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    group, _ = await _require_member(group_id, current_user, db)
    if not group:
        return RedirectResponse("/groups", 302)
    page = await _get_page(group_id, slug, db)
    if not page:
        return RedirectResponse(f"/groups/{group_id}/wiki", 302)

    # Can only restore if not locked by someone else
    if page.locked_by_other(current_user.id):
        return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)

    revision = (await db.execute(
        select(WikiPageRevision).where(
            WikiPageRevision.id == rev_id, WikiPageRevision.page_id == page.id
        )
    )).scalar_one_or_none()
    if revision:
        page.body = revision.body
        page.locked_by_id = None
        page.locked_at = None
        await _save_revision(page, current_user.id, f"Restored revision #{rev_id}", db)
        await db.commit()
    return RedirectResponse(f"/groups/{group_id}/wiki/{slug}", 302)
