"""
Personal research notebook: private Markdown entries with tags,
paper/conference links, optional group sharing, and a vis-network mind map.
"""
import json

import bleach
import markdown as md
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conference import Conference
from app.models.group import GroupMembership, ResearchGroup
from app.models.notebook import (
    NotebookEdge,
    NotebookEntry,
    NotebookEntryShare,
    NotebookEntryTag,
    NotebookTag,
)
from app.models.paper import PaperProject

router = APIRouter(prefix="/notebook", tags=["notebook"])
templates = Jinja2Templates(directory="app/templates")

ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "p", "pre", "code", "h1", "h2", "h3", "h4", "h5", "ul", "ol", "li", "blockquote",
]


def _render_md(text: str) -> str:
    html = md.markdown(text, extensions=["fenced_code", "tables"])
    return bleach.clean(html, tags=ALLOWED_TAGS, strip=True)


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": "notebook", **kw}


async def _parse_tags(user_id: int, tag_string: str, db: AsyncSession) -> list[NotebookTag]:
    """Split comma-separated tag string, get-or-create NotebookTag rows."""
    names = [t.strip().lower() for t in tag_string.split(",") if t.strip()]
    tags = []
    for name in names:
        tag = (await db.execute(
            select(NotebookTag).where(NotebookTag.user_id == user_id, NotebookTag.name == name)
        )).scalar_one_or_none()
        if not tag:
            tag = NotebookTag(user_id=user_id, name=name)
            db.add(tag)
            await db.flush()
        tags.append(tag)
    return tags


async def _user_groups(user_id: int, db: AsyncSession) -> list[ResearchGroup]:
    """Return all research groups the user is a member of."""
    rows = (await db.execute(
        select(ResearchGroup)
        .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
        .where(GroupMembership.user_id == user_id)
        .order_by(ResearchGroup.name)
    )).scalars().all()
    return list(rows)


def _entry_selectinload():
    return (
        select(NotebookEntry)
        .options(
            selectinload(NotebookEntry.entry_tags).selectinload(NotebookEntryTag.tag),
            selectinload(NotebookEntry.shared_groups).selectinload(NotebookEntryShare.group),
            selectinload(NotebookEntry.paper),
            selectinload(NotebookEntry.conference),
        )
    )


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_entries(
    request: Request,
    q: str = "",
    tag: str = "",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    stmt = _entry_selectinload().where(NotebookEntry.user_id == current_user.id)
    if q:
        stmt = stmt.where(
            or_(NotebookEntry.title.ilike(f"%{q}%"), NotebookEntry.body.ilike(f"%{q}%"))
        )
    if tag:
        stmt = stmt.join(NotebookEntryTag, NotebookEntryTag.entry_id == NotebookEntry.id)\
                   .join(NotebookTag, NotebookTag.id == NotebookEntryTag.tag_id)\
                   .where(NotebookTag.name == tag.lower())

    entries = (await db.execute(stmt.order_by(NotebookEntry.updated_at.desc()))).scalars().all()

    # All tags for filter sidebar
    all_tags = (await db.execute(
        select(NotebookTag).where(NotebookTag.user_id == current_user.id).order_by(NotebookTag.name)
    )).scalars().all()

    return templates.TemplateResponse(
        request, "notebook/list.html",
        _ctx(request, current_user, entries=entries, all_tags=all_tags, q=q, active_tag=tag),
    )


# ── Shared with me ────────────────────────────────────────────────────────────

@router.get("/shared", response_class=HTMLResponse)
async def shared_entries(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    # Entries shared with any group the user belongs to (excluding own entries)
    my_group_ids = [g.id for g in await _user_groups(current_user.id, db)]
    if my_group_ids:
        stmt = (
            _entry_selectinload()
            .join(NotebookEntryShare, NotebookEntryShare.entry_id == NotebookEntry.id)
            .where(
                NotebookEntryShare.group_id.in_(my_group_ids),
                NotebookEntry.user_id != current_user.id,
            )
            .order_by(NotebookEntry.updated_at.desc())
        )
        entries = (await db.execute(stmt)).scalars().unique().all()
    else:
        entries = []

    return templates.TemplateResponse(
        request, "notebook/shared.html",
        _ctx(request, current_user, entries=entries),
    )


# ── Mind map ─────────────────────────────────────────────────────────────────

@router.get("/map", response_class=HTMLResponse)
async def mind_map(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    entries = (await db.execute(
        select(NotebookEntry).where(NotebookEntry.user_id == current_user.id)
    )).scalars().all()

    edges = (await db.execute(
        select(NotebookEdge)
        .join(NotebookEntry, NotebookEntry.id == NotebookEdge.source_id)
        .where(NotebookEntry.user_id == current_user.id)
    )).scalars().all()

    nodes_json = json.dumps([
        {
            "id": e.id, "label": e.title,
            "x": e.map_x, "y": e.map_y,
            "physics": e.map_x is None,
        }
        for e in entries
    ])
    edges_json = json.dumps([
        {"id": ed.id, "from": ed.source_id, "to": ed.target_id, "label": ed.label or ""}
        for ed in edges
    ])

    return templates.TemplateResponse(
        request, "notebook/map.html",
        _ctx(request, current_user, nodes_json=nodes_json, edges_json=edges_json),
    )


@router.post("/map/positions")
async def save_positions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    for item in body:
        entry = (await db.execute(
            select(NotebookEntry).where(
                NotebookEntry.id == item["id"],
                NotebookEntry.user_id == current_user.id,
            )
        )).scalar_one_or_none()
        if entry:
            entry.map_x = item.get("x")
            entry.map_y = item.get("y")
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/map/edges")
async def create_edge(
    source_id: int = Form(...),
    target_id: int = Form(...),
    label: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return JSONResponse({"ok": False}, status_code=401)
    # Verify both entries belong to this user
    src = (await db.execute(
        select(NotebookEntry).where(NotebookEntry.id == source_id, NotebookEntry.user_id == current_user.id)
    )).scalar_one_or_none()
    tgt = (await db.execute(
        select(NotebookEntry).where(NotebookEntry.id == target_id, NotebookEntry.user_id == current_user.id)
    )).scalar_one_or_none()
    if src and tgt:
        edge = NotebookEdge(source_id=source_id, target_id=target_id, label=label or None)
        db.add(edge)
        await db.commit()
        await db.refresh(edge)
        return JSONResponse({"ok": True, "id": edge.id})
    return JSONResponse({"ok": False}, status_code=400)


@router.post("/map/edges/{edge_id}/delete")
async def delete_edge(
    edge_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return JSONResponse({"ok": False}, status_code=401)
    edge = (await db.execute(
        select(NotebookEdge)
        .join(NotebookEntry, NotebookEntry.id == NotebookEdge.source_id)
        .where(NotebookEdge.id == edge_id, NotebookEntry.user_id == current_user.id)
    )).scalar_one_or_none()
    if edge:
        await db.delete(edge)
        await db.commit()
    return JSONResponse({"ok": True})


# ── Create ────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_entry_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    papers = (await db.execute(select(PaperProject).order_by(PaperProject.title))).scalars().all()
    conferences = (await db.execute(select(Conference).order_by(Conference.name))).scalars().all()
    groups = await _user_groups(current_user.id, db)
    return templates.TemplateResponse(
        request, "notebook/form.html",
        _ctx(request, current_user, entry=None, papers=papers, conferences=conferences,
             groups=groups, action="/notebook"),
    )


@router.post("")
async def create_entry(
    title: str = Form(...),
    body: str = Form(default=""),
    tags: str = Form(default=""),
    paper_id: str = Form(default=""),
    conference_id: str = Form(default=""),
    is_shared: bool = Form(default=False),
    shared_group_ids: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    entry = NotebookEntry(
        user_id=current_user.id,
        title=title,
        body=body,
        is_shared=is_shared,
        paper_id=int(paper_id) if paper_id else None,
        conference_id=int(conference_id) if conference_id else None,
    )
    db.add(entry)
    await db.flush()

    tag_objs = await _parse_tags(current_user.id, tags, db)
    for tag in tag_objs:
        db.add(NotebookEntryTag(entry_id=entry.id, tag_id=tag.id))

    if is_shared:
        for gid in shared_group_ids:
            db.add(NotebookEntryShare(entry_id=entry.id, group_id=gid))

    await db.commit()
    return RedirectResponse(f"/notebook/{entry.id}", 302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{entry_id}", response_class=HTMLResponse)
async def detail_entry(
    request: Request,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    entry = (await db.execute(
        _entry_selectinload().where(NotebookEntry.id == entry_id)
    )).scalar_one_or_none()
    if not entry:
        return RedirectResponse("/notebook", 302)

    # Access check: own entry, or shared with one of user's groups
    if entry.user_id != current_user.id:
        my_group_ids = {g.id for g in await _user_groups(current_user.id, db)}
        shared_group_ids = {s.group_id for s in entry.shared_groups}
        if not (entry.is_shared and my_group_ids & shared_group_ids):
            return RedirectResponse("/notebook", 302)

    rendered = _render_md(entry.body) if entry.body else ""
    return templates.TemplateResponse(
        request, "notebook/detail.html",
        _ctx(request, current_user, entry=entry, rendered=rendered),
    )


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/{entry_id}/edit", response_class=HTMLResponse)
async def edit_entry_form(
    request: Request,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    entry = (await db.execute(
        _entry_selectinload().where(NotebookEntry.id == entry_id)
    )).scalar_one_or_none()
    if not entry or entry.user_id != current_user.id:
        return RedirectResponse("/notebook", 302)
    papers = (await db.execute(select(PaperProject).order_by(PaperProject.title))).scalars().all()
    conferences = (await db.execute(select(Conference).order_by(Conference.name))).scalars().all()
    groups = await _user_groups(current_user.id, db)
    return templates.TemplateResponse(
        request, "notebook/form.html",
        _ctx(request, current_user, entry=entry, papers=papers, conferences=conferences,
             groups=groups, action=f"/notebook/{entry_id}/edit"),
    )


@router.post("/{entry_id}/edit")
async def update_entry(
    entry_id: int,
    title: str = Form(...),
    body: str = Form(default=""),
    tags: str = Form(default=""),
    paper_id: str = Form(default=""),
    conference_id: str = Form(default=""),
    is_shared: bool = Form(default=False),
    shared_group_ids: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    entry = (await db.execute(
        _entry_selectinload().where(NotebookEntry.id == entry_id)
    )).scalar_one_or_none()
    if not entry or entry.user_id != current_user.id:
        return RedirectResponse("/notebook", 302)

    entry.title = title
    entry.body = body
    entry.is_shared = is_shared
    entry.paper_id = int(paper_id) if paper_id else None
    entry.conference_id = int(conference_id) if conference_id else None

    # Replace tags
    for et in list(entry.entry_tags):
        await db.delete(et)
    await db.flush()
    tag_objs = await _parse_tags(current_user.id, tags, db)
    for tag in tag_objs:
        db.add(NotebookEntryTag(entry_id=entry.id, tag_id=tag.id))

    # Replace shares
    for share in list(entry.shared_groups):
        await db.delete(share)
    await db.flush()
    if is_shared:
        for gid in shared_group_ids:
            db.add(NotebookEntryShare(entry_id=entry.id, group_id=gid))

    await db.commit()
    return RedirectResponse(f"/notebook/{entry_id}", 302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{entry_id}/delete")
async def delete_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    entry = (await db.execute(
        select(NotebookEntry).where(
            NotebookEntry.id == entry_id, NotebookEntry.user_id == current_user.id
        )
    )).scalar_one_or_none()
    if entry:
        await db.delete(entry)
        await db.commit()
    return RedirectResponse("/notebook", 302)
