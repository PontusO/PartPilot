"""Despatch routes: list, the ship-from-order review/apply, the note detail, and invoicing."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core.deps import require_role, require_user
from . import repo

router = APIRouter(prefix="/despatch")

DESPATCH_ROLES = frozenset({"admin", "purchasing", "warehouse", "shipping"})


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("", response_class=HTMLResponse)
def despatch_list(request: Request, q: str | None = None):
    require_user(request)
    db = request.app.state.database
    search = (q or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "despatch_list.html",
        {"despatches": repo.list_despatches(db, search), "summary": repo.summary(db), "q": search or ""},
    )


# ---- ship from a customer order ----

@router.get("/from-order/{order_id}", response_class=HTMLResponse)
def ship_from_order(request: Request, order_id: int):
    require_role(request, DESPATCH_ROLES)
    db = request.app.state.database
    order = repo.order_header(db, order_id)
    if order is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Customer order not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "despatch_from_order.html",
        {"order": order, "lines": repo.shippable_lines(db, order_id)},
    )


@router.post("/from-order/{order_id}")
async def ship_from_order_apply(request: Request, order_id: int):
    user = require_role(request, DESPATCH_ROLES)
    form = await request.form()
    selected = {int(v) for v in form.getlist("ship") if str(v).isdigit()}
    selections = {}
    for line_id in selected:
        qty = _num(form.get(f"qty_{line_id}"))
        if qty and qty > 0:
            selections[line_id] = qty
    desp_id = repo.create_despatch(request.app.state.database, order_id, selections, user.username)
    if desp_id:
        return RedirectResponse(f"/despatch/{desp_id}", status_code=303)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


# ---- despatch note ----

@router.get("/{despatch_id}", response_class=HTMLResponse)
def despatch_detail(request: Request, despatch_id: int):
    user = require_user(request)
    d = repo.get_despatch(request.app.state.database, despatch_id)
    if d is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Despatch not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "despatch_detail.html", {"d": d, "can_invoice": user.role in DESPATCH_ROLES})


@router.post("/{despatch_id}/invoice")
async def invoice(request: Request, despatch_id: int):
    require_role(request, DESPATCH_ROLES)
    form = await request.form()
    repo.mark_invoiced(request.app.state.database, despatch_id,
                       (form.get("invoice_no") or "").strip() or None,
                       (form.get("invoice_date") or "").strip() or None)
    return RedirectResponse(f"/despatch/{despatch_id}", status_code=303)
