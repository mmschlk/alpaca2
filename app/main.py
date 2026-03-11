from contextlib import asynccontextmanager

import bcrypt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import User
from app.routers import (
    admin,
    auth,
    authors,
    affiliations,
    bibtex,
    calendar,
    collaborators,
    conferences,
    dashboard,
    groups,
    journals,
    papers,
    partials,
    scholar,
    service,
    suggestions,
    notebook,
    supervision,
    wiki,
    workflows,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.ADMIN_USERNAME and settings.ADMIN_EMAIL and settings.ADMIN_PASSWORD:
        async with AsyncSessionLocal() as db:
            existing = (await db.execute(
                select(User).where(User.username == settings.ADMIN_USERNAME)
            )).scalar_one_or_none()
            if not existing:
                db.add(User(
                    username=settings.ADMIN_USERNAME,
                    email=settings.ADMIN_EMAIL,
                    hashed_password=bcrypt.hashpw(
                        settings.ADMIN_PASSWORD.encode(), bcrypt.gensalt()
                    ).decode(),
                    is_admin=True,
                ))
                await db.commit()
    yield


app = FastAPI(
    title="Alpaca",
    description="Academic Administration, knowLedge base, Paper organization And Collaboration Assistant",
    lifespan=lifespan,
)

# ── Middleware ──────────────────────────────────────────────────────────────
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="alpaca_session",
    max_age=60 * 60 * 24 * 30,  # 30 days
    https_only=False,  # Set True in production
    same_site="lax",
)

# ── Static files ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(bibtex.router)
app.include_router(calendar.router)
app.include_router(dashboard.router)
app.include_router(papers.router)
app.include_router(conferences.router)
app.include_router(journals.router)
app.include_router(authors.router)
app.include_router(affiliations.router)
app.include_router(groups.router)
app.include_router(collaborators.router)
app.include_router(scholar.router)
app.include_router(service.router)
app.include_router(suggestions.router)
app.include_router(notebook.router)
app.include_router(supervision.router)
app.include_router(wiki.router)
app.include_router(workflows.router)
app.include_router(partials.router)
