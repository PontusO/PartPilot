"""Help routes: the user manual, readable by every signed-in user."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core.deps import require_user
from . import manual

router = APIRouter(prefix="/help")


@router.get("", response_class=HTMLResponse)
def help_index(request: Request):
    require_user(request)
    body, toc = manual.load()
    return request.app.state.templates.TemplateResponse(
        request, "help.html", {"body": body, "toc": toc}
    )
