"""
Collaborators overview: all authors the logged-in user's author profile
has co-authored papers with.
"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.affiliation import Affiliation, AuthorAffiliation
from app.models.author import Author
from app.models.paper import PaperAuthor

router = APIRouter(prefix="/collaborators", tags=["collaborators"])


@router.get("", response_class=HTMLResponse)
async def collaborators(
    request: Request,
    view: str = "list",  # list | affiliation | country | graph
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    if not current_user.author_id:
        return templates.TemplateResponse(
            request, "collaborators/index.html",
            {"request": request, "current_user": current_user, "active_page": "collaborators",
             "no_author": True, "collaborators": [], "view": view,
             "graph_nodes_json": "[]", "graph_edges_json": "[]"},
        )

    # Find all papers this user's author is on
    my_paper_ids = [
        r[0] for r in (await db.execute(
            select(PaperAuthor.paper_id).where(PaperAuthor.author_id == current_user.author_id)
        )).all()
    ]

    if not my_paper_ids:
        return templates.TemplateResponse(
            request, "collaborators/index.html",
            {"request": request, "current_user": current_user, "active_page": "collaborators",
             "collaborators": [], "view": view,
             "graph_nodes_json": "[]", "graph_edges_json": "[]"},
        )

    # Find all co-authors on those papers (excluding self)
    collab_authors = (await db.execute(
        select(Author)
        .join(PaperAuthor, PaperAuthor.author_id == Author.id)
        .options(
            selectinload(Author.author_affiliations).selectinload(AuthorAffiliation.affiliation)
        )
        .where(
            PaperAuthor.paper_id.in_(my_paper_ids),
            Author.id != current_user.author_id,
        )
        .distinct()
        .order_by(Author.last_name, Author.given_name)
    )).scalars().all()

    # ── Graph data ─────────────────────────────────────────────────────────────
    # Build per-paper author sets across all my papers
    all_pa_rows = (await db.execute(
        select(PaperAuthor.paper_id, PaperAuthor.author_id)
        .where(PaperAuthor.paper_id.in_(my_paper_ids))
    )).all()
    paper_author_sets: dict[int, set[int]] = {}
    for paper_id, author_id in all_pa_rows:
        paper_author_sets.setdefault(paper_id, set()).add(author_id)

    collab_id_set = {a.id for a in collab_authors}
    collab_by_id = {a.id: a for a in collab_authors}

    # Edge weight: number of papers shared between every pair of nodes
    # (pairs involving the user are user.author_id ↔ collab_id;
    #  pairs between two collaborators only count papers where the user is also present)
    edge_weights: dict[tuple[int, int], int] = {}
    for paper_id, author_ids in paper_author_sets.items():
        present_collabs = author_ids & collab_id_set
        # user ↔ each collaborator on this paper
        for cid in present_collabs:
            key = (current_user.author_id, cid)
            edge_weights[key] = edge_weights.get(key, 0) + 1
        # collaborator ↔ collaborator (only on papers where the user is also present)
        if current_user.author_id in author_ids:
            collab_list = sorted(present_collabs)
            for i, a in enumerate(collab_list):
                for b in collab_list[i + 1:]:
                    key = (a, b)
                    edge_weights[key] = edge_weights.get(key, 0) + 1

    # Load user's own author record for the label
    my_author = (await db.execute(
        select(Author).where(Author.id == current_user.author_id)
    )).scalar_one_or_none()

    graph_nodes = [{"id": current_user.author_id, "label": my_author.full_name if my_author else "Me", "group": "self"}]
    for a in collab_authors:
        graph_nodes.append({"id": a.id, "label": a.full_name, "group": "collab"})

    graph_edges = [
        {"from": a, "to": b, "value": w, "title": f"{w} joint paper{'s' if w != 1 else ''}"}
        for (a, b), w in edge_weights.items()
    ]

    # ── List/group views ───────────────────────────────────────────────────────
    by_affiliation: dict[str, list] = {}
    by_country: dict[str, list] = {}
    for author in collab_authors:
        current_affs = [aa for aa in author.author_affiliations if aa.end_date is None]
        if current_affs:
            for aa in current_affs:
                by_affiliation.setdefault(aa.affiliation.name, []).append(author)
                by_country.setdefault(aa.affiliation.country or "Unknown", []).append(author)
        else:
            by_affiliation.setdefault("No Affiliation", []).append(author)
            by_country.setdefault("Unknown", []).append(author)

    return templates.TemplateResponse(
        request, "collaborators/index.html",
        {
            "request": request, "current_user": current_user, "active_page": "collaborators",
            "collaborators": collab_authors,
            "by_affiliation": dict(sorted(by_affiliation.items())),
            "by_country": dict(sorted(by_country.items())),
            "view": view,
            "graph_nodes_json": json.dumps(graph_nodes),
            "graph_edges_json": json.dumps(graph_edges),
        },
    )
