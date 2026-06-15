"""Despatch routes: list, the ship-from-order review/apply, the note detail, and invoicing."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...core.deps import require_role, require_user
from . import fortnox_invoice, repo

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
    db = request.app.state.database
    desp_id = repo.create_despatch(db, order_id, selections, user.username)
    if desp_id:
        # Auto-invoice in Fortnox (only if connected). Despatch is already committed, so any
        # Fortnox problem is recorded on the despatch (retryable) and never fails the shipment.
        if fortnox_invoice.build_client(db) is not None:
            await run_in_threadpool(lambda: fortnox_invoice.invoice_despatch(db, desp_id))
        return RedirectResponse(f"/despatch/{desp_id}", status_code=303)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


# ---- despatch note ----

def _render_detail(request: Request, despatch_id: int, user, fortnox_result=None, status=200):
    db = request.app.state.database
    d = repo.get_despatch(db, despatch_id)
    if d is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Despatch not found."}, status_code=404)
    connected = fortnox_invoice.build_client(db) is not None
    # Show the create-customer confirmation when one was just requested, or when the despatch is
    # already sitting in the awaiting-confirmation state (so a plain page load offers it too).
    if fortnox_result and fortnox_result.get("status") == "needs_customer":
        preview = fortnox_result.get("customer_preview")
    elif connected:
        preview = fortnox_invoice.pending_customer_preview(db, despatch_id)
    else:
        preview = None
    return request.app.state.templates.TemplateResponse(
        request, "despatch_detail.html",
        {"d": d, "can_invoice": user.role in DESPATCH_ROLES,
         "fortnox_connected": connected, "fortnox_result": fortnox_result,
         "customer_preview": preview},
        status_code=status,
    )


@router.get("/{despatch_id}", response_class=HTMLResponse)
def despatch_detail(request: Request, despatch_id: int):
    user = require_user(request)
    return _render_detail(request, despatch_id, user)


@router.post("/{despatch_id}/fortnox-invoice", response_class=HTMLResponse)
async def fortnox_invoice_action(request: Request, despatch_id: int):
    user = require_role(request, DESPATCH_ROLES)
    db = request.app.state.database
    form = await request.form()
    confirm = (form.get("confirm") or "") == "1"
    result = await run_in_threadpool(
        lambda: fortnox_invoice.invoice_despatch(db, despatch_id, confirm_customer=confirm))
    return _render_detail(request, despatch_id, user, fortnox_result=result)


@router.post("/{despatch_id}/invoice")
async def invoice(request: Request, despatch_id: int):
    require_role(request, DESPATCH_ROLES)
    form = await request.form()
    repo.mark_invoiced(request.app.state.database, despatch_id,
                       (form.get("invoice_no") or "").strip() or None,
                       (form.get("invoice_date") or "").strip() or None)
    return RedirectResponse(f"/despatch/{despatch_id}", status_code=303)
