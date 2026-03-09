"""
BibTeX collections router — CRUD, entries, export, sharing.
"""
from __future__ import annotations

from io import StringIO

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.bibtex_utils import (
    DEFAULT_STYLE, generate_cite_key, merge_style,
    parse_bibtex_string, paper_to_entry_dict, render_collection, render_entry,
)
from app.models.bibtex import BibCollection, BibCollectionShare, BibCollectionWriteRevoke, BibEntry
from app.models.group import GroupMembership, GroupRole, ResearchGroup
from app.models.paper import PaperAuthor, PaperProject

router = APIRouter(prefix="/bibtex", tags=["bibtex"])
templates = Jinja2Templates(directory="app/templates")


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": "bibtex", **kw}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _user_groups(user_id: int, db: AsyncSession) -> list[ResearchGroup]:
    rows = (await db.execute(
        select(ResearchGroup)
        .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
        .where(GroupMembership.user_id == user_id)
        .order_by(ResearchGroup.name)
    )).scalars().all()
    return list(rows)


async def _user_admin_groups(user_id: int, db: AsyncSession) -> list[ResearchGroup]:
    """Groups where current user is an admin."""
    rows = (await db.execute(
        select(ResearchGroup)
        .join(GroupMembership, GroupMembership.group_id == ResearchGroup.id)
        .where(
            GroupMembership.user_id == user_id,
            GroupMembership.role == GroupRole.admin,
        )
        .order_by(ResearchGroup.name)
    )).scalars().all()
    return list(rows)


async def _get_collection(db: AsyncSession, collection_id: int) -> BibCollection | None:
    return (await db.execute(
        select(BibCollection)
        .where(BibCollection.id == collection_id)
        .options(
            selectinload(BibCollection.owner),
            selectinload(BibCollection.group),
            selectinload(BibCollection.entries),
            selectinload(BibCollection.shares).selectinload(BibCollectionShare.group),
            selectinload(BibCollection.write_revokes).selectinload(BibCollectionWriteRevoke.user),
        )
    )).scalar_one_or_none()


async def _can_view(collection: BibCollection, current_user, db: AsyncSession) -> bool:
    if collection.owner_id == current_user.id:
        return True
    my_group_ids = {g.id for g in await _user_groups(current_user.id, db)}
    if collection.group_id and collection.group_id in my_group_ids:
        return True
    shared_group_ids = {s.group_id for s in collection.shares}
    return bool(my_group_ids & shared_group_ids)


async def _can_write(collection: BibCollection, current_user, db: AsyncSession) -> bool:
    """Owner can always write. Group members can write unless revoked."""
    if collection.owner_id == current_user.id:
        return True
    if collection.group_id:
        my_group_ids = {g.id for g in await _user_groups(current_user.id, db)}
        if collection.group_id not in my_group_ids:
            return False
        revoked_ids = {r.user_id for r in collection.write_revokes}
        return current_user.id not in revoked_ids
    return False


async def _can_manage(collection: BibCollection, current_user, db: AsyncSession) -> bool:
    """Manage = edit settings, delete, change shares/revokes."""
    if current_user.is_admin:
        return True
    if collection.owner_id == current_user.id:
        return True
    if collection.group_id:
        m = (await db.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == collection.group_id,
                GroupMembership.user_id == current_user.id,
                GroupMembership.role == GroupRole.admin,
            )
        )).scalar_one_or_none()
        return m is not None
    return False


def _style_from_form(form: dict) -> dict:
    return {
        "author_format": form.get("author_format", "full"),
        "max_authors": int(form.get("max_authors") or 0),
        "include_doi": "include_doi" in form,
        "include_url": "include_url" in form,
        "include_abstract": "include_abstract" in form,
        "use_crossref": "use_crossref" in form,
        "clean_proceedings": "clean_proceedings" in form,
    }


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_collections(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    _load_opts = [
        selectinload(BibCollection.entries),
        selectinload(BibCollection.shares).selectinload(BibCollectionShare.group),
        selectinload(BibCollection.group),
        selectinload(BibCollection.owner),
    ]

    # Personal collections (owned by me)
    my = (await db.execute(
        select(BibCollection)
        .where(BibCollection.owner_id == current_user.id)
        .options(*_load_opts)
        .order_by(BibCollection.name)
    )).scalars().all()

    # Group-owned collections (groups I'm in)
    my_group_ids = [g.id for g in await _user_groups(current_user.id, db)]
    group_collections: list[BibCollection] = []
    if my_group_ids:
        group_collections = (await db.execute(
            select(BibCollection)
            .where(BibCollection.group_id.in_(my_group_ids))
            .options(*_load_opts)
            .order_by(BibCollection.name)
        )).scalars().all()

    # User-owned collections shared with my groups (excluding my own)
    shared: list[BibCollection] = []
    if my_group_ids:
        shared = (await db.execute(
            select(BibCollection)
            .join(BibCollectionShare, BibCollectionShare.collection_id == BibCollection.id)
            .where(
                BibCollectionShare.group_id.in_(my_group_ids),
                BibCollection.owner_id != current_user.id,
                BibCollection.group_id.is_(None),
            )
            .options(*_load_opts)
            .order_by(BibCollection.name)
        )).scalars().all()

    # Groups where I'm an admin (for creating group collections)
    admin_groups = await _user_admin_groups(current_user.id, db)

    return templates.TemplateResponse(
        request, "bibtex/list.html",
        _ctx(request, current_user,
             my_collections=my,
             group_collections=group_collections,
             shared_collections=shared,
             admin_groups=admin_groups),
    )


# ── New / Create ──────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_collection_form(
    request: Request,
    group_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    admin_groups = await _user_admin_groups(current_user.id, db)
    return templates.TemplateResponse(
        request, "bibtex/form.html",
        _ctx(request, current_user,
             collection=None,
             style=DEFAULT_STYLE,
             action="/bibtex",
             admin_groups=admin_groups,
             preselect_group_id=group_id),
    )


@router.post("", response_class=HTMLResponse)
async def create_collection(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse("/bibtex/new", 302)
    description = str(form.get("description", "")).strip() or None
    style = _style_from_form(form)

    # Determine ownership: personal or group
    group_id_str = str(form.get("group_id", "")).strip()
    group_id: int | None = None
    if group_id_str and group_id_str.isdigit():
        gid = int(group_id_str)
        # Verify user is admin of that group
        admin_groups = await _user_admin_groups(current_user.id, db)
        if any(g.id == gid for g in admin_groups):
            group_id = gid

    collection = BibCollection(
        name=name,
        description=description,
        owner_id=None if group_id else current_user.id,
        group_id=group_id,
        style=style,
    )
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    return RedirectResponse(f"/bibtex/{collection.id}", 302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{collection_id}", response_class=HTMLResponse)
async def collection_detail(
    collection_id: int,
    request: Request,
    tab: str = "entries",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_view(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)

    style = merge_style(collection.style)
    can_write = await _can_write(collection, current_user, db)
    can_manage = await _can_manage(collection, current_user, db)

    # Export preview: first 15 entries rendered
    preview_bib = ""
    if tab == "export" and collection.entries:
        preview_bib = render_collection(collection.entries[:15], style)

    # Sharing tab data
    shareable_groups: list[ResearchGroup] = []
    group_members: list[GroupMembership] = []
    revoked_user_ids: set[int] = set()

    if tab == "sharing":
        if collection.group_id:
            # Group-owned: show members + write-revoke management
            group_members = (await db.execute(
                select(GroupMembership)
                .where(GroupMembership.group_id == collection.group_id)
                .options(selectinload(GroupMembership.user))
                .order_by(GroupMembership.role, GroupMembership.joined_at)
            )).scalars().all()
            revoked_user_ids = {r.user_id for r in collection.write_revokes}
        elif can_manage:
            # User-owned: show group share management
            already_shared_ids = {s.group_id for s in collection.shares}
            shareable_groups = [
                g for g in await _user_groups(current_user.id, db)
                if g.id not in already_shared_ids
            ]

    return templates.TemplateResponse(
        request, "bibtex/detail.html",
        _ctx(request, current_user,
             collection=collection,
             style=style,
             can_write=can_write,
             can_manage=can_manage,
             tab=tab,
             preview_bib=preview_bib,
             shareable_groups=shareable_groups,
             group_members=group_members,
             revoked_user_ids=revoked_user_ids,
             entry_type_colors={
                 "article": "success",
                 "inproceedings": "primary",
                 "proceedings": "info",
                 "book": "warning",
                 "misc": "secondary",
             }),
    )


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/{collection_id}/edit", response_class=HTMLResponse)
async def edit_collection_form(
    collection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_manage(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)
    style = merge_style(collection.style)
    admin_groups = await _user_admin_groups(current_user.id, db)
    return templates.TemplateResponse(
        request, "bibtex/form.html",
        _ctx(request, current_user,
             collection=collection, style=style,
             action=f"/bibtex/{collection_id}/edit",
             admin_groups=admin_groups,
             preselect_group_id=None),
    )


@router.post("/{collection_id}/edit", response_class=HTMLResponse)
async def update_collection(
    collection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_manage(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        collection.name = name
    collection.description = str(form.get("description", "")).strip() or None
    collection.style = _style_from_form(form)
    await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}", 302)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/{collection_id}/delete", response_class=HTMLResponse)
async def delete_collection(
    collection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if collection and await _can_manage(collection, current_user, db):
        await db.delete(collection)
        await db.commit()
    return RedirectResponse("/bibtex", 302)


# ── Export ────────────────────────────────────────────────────────────────────

@router.get("/{collection_id}/export")
async def export_collection(
    collection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_view(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)
    style = merge_style(collection.style)
    bib_str = render_collection(collection.entries, style)
    safe_name = "".join(c for c in collection.name if c.isalnum() or c in " _-").strip().replace(" ", "_")
    filename = f"{safe_name or 'collection'}.bib"
    return StreamingResponse(
        StringIO(bib_str),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Regenerate cite keys ───────────────────────────────────────────────────────

@router.post("/{collection_id}/regenerate-keys", response_class=HTMLResponse)
async def regenerate_keys(
    collection_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)

    used: set[str] = set()
    for entry in sorted(collection.entries, key=lambda e: e.position):
        new_key = generate_cite_key(
            entry.entry_type,
            entry.authors_raw,
            entry.year,
            entry.fields_json or {},
            used,
        )
        entry.cite_key = new_key
        used.add(new_key)

    await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=entries&regen=1", 302)


# ── Entries: add (paste/upload) ───────────────────────────────────────────────

@router.post("/{collection_id}/entries/add", response_class=HTMLResponse)
async def add_entries(
    collection_id: int,
    request: Request,
    bibtex_file: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)

    form = await request.form()
    raw_text = str(form.get("bibtex_text", "")).strip()

    # Prefer uploaded file over pasted text
    if bibtex_file and bibtex_file.filename:
        content_bytes = await bibtex_file.read()
        raw_text = content_bytes.decode("utf-8", errors="replace")

    added = 0
    skipped_keys: list[str] = []

    if raw_text:
        parsed, _ = parse_bibtex_string(raw_text)
        existing_keys = {e.cite_key for e in collection.entries}
        max_pos = max((e.position for e in collection.entries), default=-1) + 1

        for i, entry in enumerate(parsed):
            key = entry["key"]
            if key in existing_keys:
                skipped_keys.append(key)
                continue
            be = BibEntry(
                collection_id=collection_id,
                entry_type=entry["type"],
                cite_key=key,
                title=entry.get("title"),
                year=entry.get("year"),
                authors_raw=entry.get("authors_raw"),
                fields_json=entry.get("fields") or {},
                position=max_pos + i,
            )
            db.add(be)
            existing_keys.add(key)
            added += 1

        await db.commit()

    return RedirectResponse(
        f"/bibtex/{collection_id}?tab=entries&added={added}&skipped={len(skipped_keys)}", 302
    )


# ── Entries: import from papers ───────────────────────────────────────────────

@router.get("/{collection_id}/entries/import-papers", response_class=HTMLResponse)
async def import_papers_form(
    collection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)

    papers = (await db.execute(
        select(PaperProject)
        .options(
            selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author),
            selectinload(PaperProject.journal_submissions),
            selectinload(PaperProject.conference_submissions),
        )
        .order_by(PaperProject.title)
    )).scalars().all()

    existing_titles = {e.title.lower() for e in collection.entries if e.title}

    return templates.TemplateResponse(
        request, "bibtex/import_papers.html",
        _ctx(request, current_user,
             collection=collection,
             papers=papers,
             existing_titles=existing_titles),
    )


@router.post("/{collection_id}/entries/import-papers", response_class=HTMLResponse)
async def import_papers_apply(
    collection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)

    form = await request.form()
    paper_ids = [int(v) for k, v in form.multi_items() if k == "paper_id"]

    if paper_ids:
        papers = (await db.execute(
            select(PaperProject)
            .where(PaperProject.id.in_(paper_ids))
            .options(
                selectinload(PaperProject.paper_authors).selectinload(PaperAuthor.author),
                selectinload(PaperProject.journal_submissions),
                selectinload(PaperProject.conference_submissions),
            )
        )).scalars().all()

        existing_keys = {e.cite_key for e in collection.entries}
        max_pos = max((e.position for e in collection.entries), default=-1) + 1

        for i, paper in enumerate(papers):
            entry_dict = paper_to_entry_dict(paper)
            # Generate key using new format with disambiguation
            key = generate_cite_key(
                entry_dict["type"],
                entry_dict.get("authors_raw"),
                entry_dict.get("year"),
                entry_dict.get("fields") or {},
                existing_keys,
            )
            existing_keys.add(key)

            be = BibEntry(
                collection_id=collection_id,
                entry_type=entry_dict["type"],
                cite_key=key,
                title=entry_dict.get("title"),
                year=entry_dict.get("year"),
                authors_raw=entry_dict.get("authors_raw"),
                fields_json=entry_dict.get("fields") or {},
                position=max_pos + i,
            )
            db.add(be)

        await db.commit()

    return RedirectResponse(f"/bibtex/{collection_id}?tab=entries", 302)


# ── Entry edit ────────────────────────────────────────────────────────────────

@router.get("/{collection_id}/entries/{entry_id}/edit", response_class=HTMLResponse)
async def edit_entry_form(
    collection_id: int,
    entry_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)
    entry = next((e for e in collection.entries if e.id == entry_id), None)
    if not entry:
        return RedirectResponse(f"/bibtex/{collection_id}", 302)

    style = merge_style(collection.style)
    raw_bib = render_entry(entry, style)

    return templates.TemplateResponse(
        request, "bibtex/entry_form.html",
        _ctx(request, current_user,
             collection=collection,
             entry=entry,
             raw_bib=raw_bib,
             action=f"/bibtex/{collection_id}/entries/{entry_id}/edit"),
    )


@router.post("/{collection_id}/entries/{entry_id}/edit", response_class=HTMLResponse)
async def update_entry(
    collection_id: int,
    entry_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)
    entry = next((e for e in collection.entries if e.id == entry_id), None)
    if not entry:
        return RedirectResponse(f"/bibtex/{collection_id}", 302)

    form = await request.form()
    mode = str(form.get("mode", "form"))

    if mode == "raw":
        raw = str(form.get("raw_bib", "")).strip()
        if raw:
            parsed, _ = parse_bibtex_string(raw)
            if parsed:
                p = parsed[0]
                new_key = p["key"]
                conflict = any(
                    e.cite_key == new_key and e.id != entry_id
                    for e in collection.entries
                )
                if not conflict:
                    entry.cite_key = new_key
                entry.entry_type = p["type"]
                entry.title = p.get("title")
                entry.year = p.get("year")
                entry.authors_raw = p.get("authors_raw")
                entry.fields_json = p.get("fields") or {}
    else:
        new_key = str(form.get("cite_key", "")).strip()
        if new_key and not any(
            e.cite_key == new_key and e.id != entry_id for e in collection.entries
        ):
            entry.cite_key = new_key
        entry.entry_type = str(form.get("entry_type", entry.entry_type)).strip()
        entry.title = str(form.get("title", "")).strip() or None
        year_str = str(form.get("year", "")).strip()
        entry.year = int(year_str) if year_str.isdigit() else None
        entry.authors_raw = str(form.get("authors_raw", "")).strip() or None

        extra: dict[str, str] = {}
        keys = form.getlist("field_key")
        vals = form.getlist("field_val")
        for k, v in zip(keys, vals):
            k = k.strip()
            v = v.strip()
            if k and v:
                extra[k.lower()] = v
        entry.fields_json = extra

    await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=entries", 302)


# ── Entry delete ──────────────────────────────────────────────────────────────

@router.post("/{collection_id}/entries/{entry_id}/delete", response_class=HTMLResponse)
async def delete_entry(
    collection_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if not collection or not await _can_write(collection, current_user, db):
        return RedirectResponse("/bibtex", 302)
    entry = next((e for e in collection.entries if e.id == entry_id), None)
    if entry:
        await db.delete(entry)
        await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=entries", 302)


# ── Sharing: user-owned collection → share with group ─────────────────────────

@router.post("/{collection_id}/shares/add", response_class=HTMLResponse)
async def add_share(
    collection_id: int,
    group_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if collection and await _can_manage(collection, current_user, db):
        existing = (await db.execute(
            select(BibCollectionShare).where(
                BibCollectionShare.collection_id == collection_id,
                BibCollectionShare.group_id == group_id,
            )
        )).scalar_one_or_none()
        if not existing:
            db.add(BibCollectionShare(collection_id=collection_id, group_id=group_id))
            await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=sharing", 302)


@router.post("/{collection_id}/shares/{share_id}/delete", response_class=HTMLResponse)
async def delete_share(
    collection_id: int,
    share_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if collection and await _can_manage(collection, current_user, db):
        share = (await db.execute(
            select(BibCollectionShare).where(BibCollectionShare.id == share_id)
        )).scalar_one_or_none()
        if share and share.collection_id == collection_id:
            await db.delete(share)
            await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=sharing", 302)


# ── Sharing: group-owned collection → revoke/grant member write access ────────

@router.post("/{collection_id}/revoke/{user_id}", response_class=HTMLResponse)
async def revoke_write(
    collection_id: int,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if collection and await _can_manage(collection, current_user, db):
        existing = (await db.execute(
            select(BibCollectionWriteRevoke).where(
                BibCollectionWriteRevoke.collection_id == collection_id,
                BibCollectionWriteRevoke.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not existing:
            db.add(BibCollectionWriteRevoke(collection_id=collection_id, user_id=user_id))
            await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=sharing", 302)


@router.post("/{collection_id}/unrevoke/{user_id}", response_class=HTMLResponse)
async def unrevoke_write(
    collection_id: int,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    collection = await _get_collection(db, collection_id)
    if collection and await _can_manage(collection, current_user, db):
        revoke = (await db.execute(
            select(BibCollectionWriteRevoke).where(
                BibCollectionWriteRevoke.collection_id == collection_id,
                BibCollectionWriteRevoke.user_id == user_id,
            )
        )).scalar_one_or_none()
        if revoke:
            await db.delete(revoke)
            await db.commit()
    return RedirectResponse(f"/bibtex/{collection_id}?tab=sharing", 302)
