import bcrypt
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import require_admin, require_moderator
from app.models.author import Author
from app.models.claim import AuthorClaimRequest, ClaimStatus
from app.models.conference import Conference, ConferenceEdition
from app.models.group import ResearchGroup
from app.models.journal import Journal, JournalSpecialIssue
from app.models.paper import PaperProject
from app.models.suggestion import Suggestion, SuggestionStatus, SuggestionType
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _ctx(request, current_user, **kw):
    return {"request": request, "current_user": current_user, "active_page": "admin", **kw}


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


@router.get("", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    stats = {
        "users_total": (await db.execute(select(func.count(User.id)))).scalar_one(),
        "users_active": (await db.execute(
            select(func.count(User.id)).where(User.is_active == True)
        )).scalar_one(),
        "users_admin": (await db.execute(
            select(func.count(User.id)).where(User.is_admin == True)
        )).scalar_one(),
        "papers": (await db.execute(select(func.count(PaperProject.id)))).scalar_one(),
        "conferences": (await db.execute(select(func.count(Conference.id)))).scalar_one(),
        "editions": (await db.execute(select(func.count(ConferenceEdition.id)))).scalar_one(),
        "journals": (await db.execute(select(func.count(Journal.id)))).scalar_one(),
        "authors": (await db.execute(select(func.count(Author.id)))).scalar_one(),
        "groups": (await db.execute(select(func.count(ResearchGroup.id)))).scalar_one(),
        "pending_claims": (await db.execute(
            select(func.count(AuthorClaimRequest.id))
            .where(AuthorClaimRequest.status == ClaimStatus.pending)
        )).scalar_one(),
        "pending_suggestions": (await db.execute(
            select(func.count(Suggestion.id))
            .where(Suggestion.status == SuggestionStatus.pending)
        )).scalar_one(),
    }
    # Recent users
    recent_users = (await db.execute(
        select(User).order_by(User.created_at.desc()).limit(5)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "admin/index.html",
        _ctx(request, current_user, stats=stats, recent_users=recent_users),
    )


@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    users = (await db.execute(
        select(User)
        .options(selectinload(User.author))
        .order_by(User.username)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "admin/users.html",
        _ctx(request, current_user, users=users),
    )


@router.get("/users/new", response_class=HTMLResponse)
async def new_user_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    authors = (await db.execute(
        select(Author).order_by(Author.last_name, Author.given_name)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "admin/user_form.html",
        _ctx(request, current_user, edited_user=None, authors=authors, action="/admin/users"),
    )


@router.post("/users")
async def create_user(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(default=False),
    is_moderator: bool = Form(default=False),
    is_active: bool = Form(default=False),
    author_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    user = User(
        username=username,
        email=email,
        hashed_password=_hash(password),
        is_admin=is_admin,
        is_moderator=is_moderator,
        is_active=is_active,
        author_id=int(author_id) if author_id else None,
    )
    db.add(user)
    await db.commit()
    return RedirectResponse("/admin/users", 302)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_form(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    edited_user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not edited_user:
        return RedirectResponse("/admin/users", 302)
    authors = (await db.execute(
        select(Author).order_by(Author.last_name, Author.given_name)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "admin/user_form.html",
        _ctx(request, current_user, edited_user=edited_user, authors=authors,
             action=f"/admin/users/{user_id}/edit"),
    )


@router.post("/users/{user_id}/edit")
async def update_user(
    user_id: int,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(default=""),
    is_admin: bool = Form(default=False),
    is_moderator: bool = Form(default=False),
    is_active: bool = Form(default=False),
    author_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not user:
        return RedirectResponse("/admin/users", 302)
    user.username = username
    user.email = email
    # Prevent accidental self-lockout
    if user.id != current_user.id:
        user.is_admin = is_admin
        user.is_moderator = is_moderator
        user.is_active = is_active
    user.author_id = int(author_id) if author_id else None
    if password:
        user.hashed_password = _hash(password)
    await db.commit()
    return RedirectResponse("/admin/users", 302)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    if user_id == current_user.id:
        return RedirectResponse("/admin/users", 302)
    user = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if user:
        await db.delete(user)
        await db.commit()
    return RedirectResponse("/admin/users", 302)


# ── Author claim requests ────────────────────────────────────────────────────

@router.get("/claims", response_class=HTMLResponse)
async def list_claims(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    claims = (await db.execute(
        select(AuthorClaimRequest)
        .options(
            selectinload(AuthorClaimRequest.user),
            selectinload(AuthorClaimRequest.author),
            selectinload(AuthorClaimRequest.reviewer),
        )
        .order_by(AuthorClaimRequest.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse(
        request, "admin/claims.html",
        _ctx(request, current_user, claims=claims, ClaimStatus=ClaimStatus),
    )


@router.post("/claims/{claim_id}/approve")
async def approve_claim(
    claim_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    claim = (await db.execute(
        select(AuthorClaimRequest).where(AuthorClaimRequest.id == claim_id)
    )).scalar_one_or_none()
    if claim and claim.status == ClaimStatus.pending:
        claim.status = ClaimStatus.approved
        claim.reviewed_at = datetime.now(timezone.utc)
        claim.reviewed_by = current_user.id
        # Link author to user
        user = (await db.execute(select(User).where(User.id == claim.user_id))).scalar_one()
        user.author_id = claim.author_id
        await db.commit()
    return RedirectResponse("/admin/claims", 302)


@router.post("/claims/{claim_id}/reject")
async def reject_claim(
    claim_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_admin),
):
    claim = (await db.execute(
        select(AuthorClaimRequest).where(AuthorClaimRequest.id == claim_id)
    )).scalar_one_or_none()
    if claim and claim.status == ClaimStatus.pending:
        claim.status = ClaimStatus.rejected
        claim.reviewed_at = datetime.now(timezone.utc)
        claim.reviewed_by = current_user.id
        await db.commit()
    return RedirectResponse("/admin/claims", 302)


# ── Suggestions ──────────────────────────────────────────────────────────────

@router.get("/suggestions", response_class=HTMLResponse)
async def list_suggestions(
    request: Request,
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_moderator),
):
    filter_status = SuggestionStatus(status) if status in SuggestionStatus.__members__ else SuggestionStatus.pending
    suggestions = (await db.execute(
        select(Suggestion)
        .options(selectinload(Suggestion.submitted_by), selectinload(Suggestion.reviewed_by))
        .where(Suggestion.status == filter_status)
        .order_by(Suggestion.submitted_at.desc())
    )).scalars().all()
    return templates.TemplateResponse(
        request, "admin/suggestions.html",
        _ctx(request, current_user, suggestions=suggestions,
             SuggestionStatus=SuggestionStatus, filter_status=filter_status),
    )


@router.post("/suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: int,
    review_note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_moderator),
):
    suggestion = (await db.execute(
        select(Suggestion).where(Suggestion.id == suggestion_id)
    )).scalar_one_or_none()
    if not suggestion or suggestion.status != SuggestionStatus.pending:
        return RedirectResponse("/admin/suggestions", 302)

    data = suggestion.data_dict
    if suggestion.entity_type == SuggestionType.conference:
        entity = Conference(
            name=data["name"],
            abbreviation=data["abbreviation"],
            core_rank=data.get("core_rank"),
            website=data.get("website"),
            wikicfp_series_id=data.get("wikicfp_series_id"),
        )
        db.add(entity)
    elif suggestion.entity_type == SuggestionType.conference_edition:
        entity = ConferenceEdition(
            conference_id=data["conference_id"],
            year=data["year"],
            location=data.get("location"),
            abstract_deadline=date.fromisoformat(data["abstract_deadline"]) if data.get("abstract_deadline") else None,
            full_paper_deadline=date.fromisoformat(data["full_paper_deadline"]) if data.get("full_paper_deadline") else None,
            notification_date=date.fromisoformat(data["notification_date"]) if data.get("notification_date") else None,
        )
        db.add(entity)
    elif suggestion.entity_type == SuggestionType.journal:
        entity = Journal(
            name=data["name"],
            abbreviation=data.get("abbreviation"),
            rank=data.get("rank"),
            website=data.get("website"),
        )
        db.add(entity)
    elif suggestion.entity_type == SuggestionType.journal_special_issue:
        entity = JournalSpecialIssue(
            journal_id=data["journal_id"],
            title=data["title"],
            description=data.get("description"),
            submission_deadline=date.fromisoformat(data["submission_deadline"]) if data.get("submission_deadline") else None,
        )
        db.add(entity)

    suggestion.status = SuggestionStatus.approved
    suggestion.reviewed_at = datetime.now(timezone.utc)
    suggestion.reviewed_by_id = current_user.id
    suggestion.review_note = review_note or None
    await db.commit()
    return RedirectResponse("/admin/suggestions", 302)


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_id: int,
    review_note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_moderator),
):
    suggestion = (await db.execute(
        select(Suggestion).where(Suggestion.id == suggestion_id)
    )).scalar_one_or_none()
    if suggestion and suggestion.status == SuggestionStatus.pending:
        suggestion.status = SuggestionStatus.rejected
        suggestion.reviewed_at = datetime.now(timezone.utc)
        suggestion.reviewed_by_id = current_user.id
        suggestion.review_note = review_note or None
        await db.commit()
    return RedirectResponse("/admin/suggestions", 302)
