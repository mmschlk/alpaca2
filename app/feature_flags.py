"""
In-memory feature flag cache.

Flags are loaded from the database at startup and refreshed whenever
an admin makes a change. All per-request checks are pure in-memory
lookups — no database calls needed during normal operation.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

# Known features seeded on first startup.
# Keys are stable identifiers referenced in templates and routers.
KNOWN_FEATURES: dict[str, dict] = {
    "bibtex": {
        "label": "BibTeX Collections",
        "description": "Import, export, and manage BibTeX bibliography collections.",
        "default_enabled": True,
    },
    "supervision": {
        "label": "Thesis Supervision",
        "description": "Track and manage student thesis supervision projects.",
        "default_enabled": True,
    },
    "notebook": {
        "label": "Notebook",
        "description": "Personal knowledge base with richly linked entries.",
        "default_enabled": True,
    },
    "wiki": {
        "label": "Wiki",
        "description": "Collaborative wiki for research group documentation.",
        "default_enabled": True,
    },
    "workflows": {
        "label": "Workflows",
        "description": "Custom paper submission workflows and step tracking.",
        "default_enabled": True,
    },
    "calendar": {
        "label": "Calendar",
        "description": "Personal academic calendar with deadline tracking.",
        "default_enabled": True,
    },
    "scholar": {
        "label": "Google Scholar Integration",
        "description": "Sync citation metrics from Google Scholar profiles.",
        "default_enabled": True,
    },
    "collaborators": {
        "label": "Collaborators",
        "description": "View and explore your co-author network.",
        "default_enabled": True,
    },
    "service": {
        "label": "Community Service",
        "description": "Track conference reviewing and editorial service records.",
        "default_enabled": True,
    },
}

# ── In-memory cache ──────────────────────────────────────────────────────────
_flag_cache: dict[str, bool] = {}        # feature_key -> globally_enabled
_user_overrides: dict[int, set[str]] = {}  # user_id -> {feature_keys with access}


async def populate_cache(db) -> None:
    """Load all feature flags (with user overrides) into memory."""
    from app.models.feature_flag import FeatureFlag

    flags = (await db.execute(
        select(FeatureFlag).options(selectinload(FeatureFlag.user_overrides))
    )).scalars().all()

    global _flag_cache, _user_overrides
    _flag_cache = {f.key: f.enabled for f in flags}
    _user_overrides = {}
    for flag in flags:
        for access in flag.user_overrides:
            _user_overrides.setdefault(access.user_id, set()).add(flag.key)


def invalidate_cache() -> None:
    """Call this after any admin change to force the next populate_cache."""
    _flag_cache.clear()
    _user_overrides.clear()


def get_user_feature_set(user_id: int | None, is_admin: bool) -> set[str]:
    """Return the set of feature keys the user may access."""
    if is_admin:
        return set(KNOWN_FEATURES.keys())
    enabled = {k for k, v in _flag_cache.items() if v}
    if user_id:
        enabled |= _user_overrides.get(user_id, set())
    return enabled


def user_has_feature(user_id: int | None, is_admin: bool, key: str) -> bool:
    """Single-flag check (faster than building the full set)."""
    if is_admin:
        return True
    if key not in _flag_cache:
        return True  # unknown features default to enabled
    if _flag_cache[key]:
        return True
    return bool(user_id and key in _user_overrides.get(user_id, set()))


def get_features_for_user(user) -> set[str]:
    """Template-callable helper — accepts a User ORM object or None."""
    if user is None:
        return set()
    return get_user_feature_set(user.id, user.is_admin)
