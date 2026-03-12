"""Shared Jinja2Templates instance used by all routers.

Registering globals here means every template has access to them
without any per-request or per-router setup.
"""
from fastapi.templating import Jinja2Templates

from app.feature_flags import get_features_for_user

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["get_user_features"] = get_features_for_user
