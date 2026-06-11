"""Planning calendar routes: the board, its events JSON, and drag/resize rescheduling."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ...core import iso, parse_date
from ...core.deps import require_role, require_user
from ..work_orders import repo as wo_repo
from . import repo

router = APIRouter(prefix="/planning")

# Who can re-plan (drag/resize). Anyone signed in can view the board.
PLANNING_ROLES = frozenset({"admin", "purchasing", "warehouse"})


@router.get("", response_class=HTMLResponse)
def calendar(request: Request):
    user = require_user(request)
    return request.app.state.templates.TemplateResponse(
        request, "calendar.html", {"can_edit": user.role in PLANNING_ROLES})


@router.get("/events")
def events(request: Request, start: str | None = None, end: str | None = None):
    require_user(request)
    return JSONResponse(repo.calendar_events(request.app.state.database, start, end))


@router.post("/reschedule")
async def reschedule(request: Request):
    require_role(request, PLANNING_ROLES)
    form = await request.form()
    wo_id = form.get("wo_id")
    if not (wo_id and str(wo_id).isdigit()):
        return JSONResponse({"ok": False, "error": "bad work order id"}, status_code=400)
    db, wo_id = request.app.state.database, int(wo_id)

    purchase = (form.get("purchase") or "").strip() or None
    try:
        if purchase:                                    # dragged the purchasing marker → its date only
            wo_repo.set_purchase_by(db, wo_id, purchase)
        else:                                           # dragged/resized the build bar → move the build
            kwargs = {"planned_start": (form.get("start") or "").strip() or None}
            end = (form.get("end") or "").strip() or None   # present only on resize (FC exclusive end)
            if end:
                due = parse_date(end)
                kwargs["due_date"] = iso(due - timedelta(days=1)) if due else None
            wo_repo.reschedule_work_order(db, wo_id, **kwargs)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return Response(status_code=204)
