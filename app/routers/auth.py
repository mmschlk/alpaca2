from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from app.templating import templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import bcrypt

from app.database import get_db
from app.dependencies import get_current_user
from app.models.author import Author
from app.models.claim import AuthorClaimRequest, ClaimStatus
from app.models.user import User

router = APIRouter()


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _tpl(request: Request, name: str, **ctx):
    ctx.setdefault("current_user", request.session.get("user_id") and None)
    return templates.TemplateResponse(request, name, ctx)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "auth/login.html", {"next": next, "current_user": None})


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not _verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Invalid username or password.", "next": next, "current_user": None},
            status_code=401,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Account is disabled.", "next": next, "current_user": None},
            status_code=403,
        )
    request.session["user_id"] = user.id
    return RedirectResponse(url=next or "/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "auth/register.html", {"current_user": None})


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # Check uniqueness
    existing = await db.execute(
        select(User).where((User.username == username) | (User.email == email))
    )
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"error": "Username or email already taken.", "current_user": None},
            status_code=400,
        )
    user = User(
        username=username,
        email=email,
        hashed_password=_hash_password(password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=302)


# ── User profile ─────────────────────────────────────────────────────────────

async def _profile_ctx(db: AsyncSession, current_user) -> dict:
    """Build the full context dict needed to render profile.html."""
    user = (await db.execute(
        select(User).options(selectinload(User.author)).where(User.id == current_user.id)
    )).scalar_one()
    # Pending or rejected claim for this user
    pending_claim = (await db.execute(
        select(AuthorClaimRequest)
        .options(selectinload(AuthorClaimRequest.author))
        .where(
            (AuthorClaimRequest.user_id == current_user.id) &
            (AuthorClaimRequest.status == ClaimStatus.pending)
        )
    )).scalar_one_or_none()
    last_rejected = None
    if not pending_claim and not user.author_id:
        last_rejected = (await db.execute(
            select(AuthorClaimRequest)
            .options(selectinload(AuthorClaimRequest.author))
            .where(
                (AuthorClaimRequest.user_id == current_user.id) &
                (AuthorClaimRequest.status == ClaimStatus.rejected)
            )
            .order_by(AuthorClaimRequest.reviewed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
    # Authors not already linked to any user (excluding already-claimed one)
    claimable_authors = []
    if not user.author_id and not pending_claim:
        claimable_authors = (await db.execute(
            select(Author)
            .where(~Author.id.in_(
                select(User.author_id).where(User.author_id.isnot(None))
            ))
            .order_by(Author.last_name, Author.given_name)
        )).scalars().all()
    return {"current_user": user, "active_page": None,
            "pending_claim": pending_claim, "last_rejected": last_rejected,
            "claimable_authors": claimable_authors}


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    return templates.TemplateResponse(
        request, "auth/profile.html", await _profile_ctx(db, current_user),
    )


@router.post("/profile/email", response_class=HTMLResponse)
async def update_email(
    request: Request,
    new_email: str = Form(...),
    current_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    def _err(msg):
        return templates.TemplateResponse(
            request, "auth/profile.html",
            {"current_user": current_user, "active_page": None, "email_error": msg},
            status_code=400,
        )

    if not _verify_password(current_password, current_user.hashed_password):
        return _err("Current password is incorrect.")
    existing = (await db.execute(
        select(User).where((User.email == new_email) & (User.id != current_user.id))
    )).scalar_one_or_none()
    if existing:
        return _err("That email address is already in use.")
    current_user.email = new_email
    await db.commit()
    return templates.TemplateResponse(
        request, "auth/profile.html",
        {"current_user": current_user, "active_page": None, "email_success": True},
    )


@router.post("/profile/password", response_class=HTMLResponse)
async def update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)

    def _err(msg):
        return templates.TemplateResponse(
            request, "auth/profile.html",
            {"current_user": current_user, "active_page": None, "password_error": msg},
            status_code=400,
        )

    if not _verify_password(current_password, current_user.hashed_password):
        return _err("Current password is incorrect.")
    if len(new_password) < 8:
        return _err("New password must be at least 8 characters.")
    if new_password != confirm_password:
        return _err("Passwords do not match.")
    current_user.hashed_password = _hash_password(new_password)
    await db.commit()
    return templates.TemplateResponse(
        request, "auth/profile.html",
        {"current_user": current_user, "active_page": None, "password_success": True},
    )


@router.post("/profile/claim")
async def submit_claim(
    author_id: int = Form(...),
    message: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user or current_user.author_id:
        return RedirectResponse("/profile", 302)
    # Cancel any previous pending claim
    old = (await db.execute(
        select(AuthorClaimRequest).where(
            (AuthorClaimRequest.user_id == current_user.id) &
            (AuthorClaimRequest.status == ClaimStatus.pending)
        )
    )).scalar_one_or_none()
    if old:
        await db.delete(old)
    db.add(AuthorClaimRequest(
        user_id=current_user.id,
        author_id=author_id,
        message=message or None,
        status=ClaimStatus.pending,
    ))
    await db.commit()
    return RedirectResponse("/profile", 302)


@router.post("/profile/claim/cancel")
async def cancel_claim(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/login", 302)
    claim = (await db.execute(
        select(AuthorClaimRequest).where(
            (AuthorClaimRequest.user_id == current_user.id) &
            (AuthorClaimRequest.status == ClaimStatus.pending)
        )
    )).scalar_one_or_none()
    if claim:
        await db.delete(claim)
        await db.commit()
    return RedirectResponse("/profile", 302)


# ── Theme preference ───────────────────────────────────────────────────────────

_VALID_THEMES = {"light", "dark", "solarized", "high-contrast", "ocean", "forest"}

@router.post("/profile/theme")
async def update_theme(
    theme: str = Form(...),
    primary: str = Form(default=""),
    body_bg: str = Form(default=""),
    card_bg: str = Form(default=""),
    font_size: str = Form(default="md"),
    radius: str = Form(default="md"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return JSONResponse({"ok": False}, status_code=401)
    current_user.theme = theme if theme in _VALID_THEMES else "light"
    custom: dict = {}
    if primary:           custom["primary"]   = primary
    if body_bg:           custom["body_bg"]   = body_bg
    if card_bg:           custom["card_bg"]   = card_bg
    if font_size != "md": custom["font_size"] = font_size
    if radius != "md":    custom["radius"]    = radius
    current_user.theme_custom = custom or None
    await db.commit()
    return JSONResponse({"ok": True})
