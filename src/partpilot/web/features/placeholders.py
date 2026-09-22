"""Placeholder categories — nav entries for areas not yet built.

Each is a real (if empty) feature module: it registers a nav entry (shown with a "soon"
tag) and a route that renders a generic "coming soon" page. Building one out later means
replacing its router/templates/migrations — the nav slot is already there.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..core import Feature, NavItem
from ..core.deps import require_role, require_user


def make_placeholder(
    name: str, label: str, icon: str, order: int, roles: frozenset[str] | None = None
) -> Feature:
    router = APIRouter(prefix=f"/{name}")

    @router.get("", response_class=HTMLResponse)
    def page(request: Request):
        require_role(request, roles) if roles else require_user(request)
        templates = request.app.state.templates
        return templates.TemplateResponse(request, "placeholder.html", {"title": label})

    return Feature(
        name=name,
        router=router,
        nav=NavItem(label=label, url=f"/{name}", roles=roles, icon=icon, order=order,
                    placeholder=True),
    )
