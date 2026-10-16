"""Work order routes: list / create, and drive the build lifecycle."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core.deps import require_role, require_user
from ..catalog import repo as catalog_repo
from . import repo

router = APIRouter(prefix="/work-orders")

# Planning + production + warehouse can run work orders.
WORK_ORDER_ROLES = frozenset({"admin", "purchasing", "warehouse"})

_ACTIONS = {
    "issue": repo.issue_work_order,
    "finish": repo.finish_work_order,
    "flush": repo.flush_work_order,
    "regenerate-bom": repo.regenerate_bom,
}


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("", response_class=HTMLResponse)
def work_orders_list(request: Request, status: str | None = None, q: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    status = status if status in repo.STATUSES else None
    search = (q or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "work_orders_list.html",
        {"work_orders": repo.list_work_orders(db, status, search), "summary": repo.summary(db),
         "statuses": repo.STATUSES, "status": status or "", "q": search or "",
         "can_edit": user.role in WORK_ORDER_ROLES},
    )


def _render_form(request, *, values, error=None, status=200):
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "work_order_form.html",
        {"values": values or {}, "assemblies": repo.assemblies(db),
         "shelves": catalog_repo.pickable_shelves(db) + catalog_repo.locations(db, kind="customer"),
         "shelf_selected": (values or {}).get("location_id") or catalog_repo.default_shelf_id(db),
         "error": error},
        status_code=status,
    )


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request):
    require_role(request, WORK_ORDER_ROLES)
    return _render_form(request, values={"qty": 1})


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request):
    require_role(request, WORK_ORDER_ROLES)
    form = await request.form()
    data = {
        "wo_no": (form.get("wo_no") or "").strip() or None,
        "assembly_id": _int(form.get("assembly_id")),
        "qty": _num(form.get("qty")) or 1,
        "location_id": _int(form.get("location_id")),
        "build_date": (form.get("build_date") or "").strip() or None,
        "due_date": (form.get("due_date") or "").strip() or None,
        "duration_days": _int(form.get("duration_days")),
        "notes": (form.get("notes") or "").strip() or None,
    }
    if not data["assembly_id"]:
        return _render_form(request, values=dict(form), error="Choose an assembly to build.", status=400)
    try:
        wo_id = repo.create_work_order(request.app.state.database, data)
    except ValueError as exc:
        return _render_form(request, values=dict(form), error=str(exc), status=400)
    return RedirectResponse(f"/work-orders/{wo_id}", status_code=303)


# ---- build to fulfil a customer order ----

@router.get("/from-order/{order_id}", response_class=HTMLResponse)
def build_from_order(request: Request, order_id: int):
    require_role(request, WORK_ORDER_ROLES)
    db = request.app.state.database
    order = repo.order_header(db, order_id)
    if order is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Customer order not found."}, status_code=404)
    cust = order.get("customer_id")
    return request.app.state.templates.TemplateResponse(
        request, "work_order_from_order.html",
        {"order": order, "proposals": repo.fulfilment_proposals(db, order_id),
         "existing": repo.work_orders_for_order(db, order_id),
         "shelves": catalog_repo.pickable_shelves(db, customer_id=cust),
         "shelf_selected": catalog_repo.preferred_shelf_id(db, customer_id=cust, order_id=order_id),
         "customer_has_shelf": bool(catalog_repo.shelves_for_customer(db, cust)) if cust else True},
    )


@router.post("/from-order/{order_id}")
async def build_from_order_apply(request: Request, order_id: int):
    user = require_role(request, WORK_ORDER_ROLES)
    form = await request.form()
    selected = {int(v) for v in form.getlist("build") if str(v).isdigit()}
    selections = {}
    for line_id in selected:
        qty = _num(form.get(f"qty_{line_id}"))
        if qty and qty > 0:
            selections[line_id] = qty
    try:
        repo.create_work_orders_for_order(request.app.state.database, order_id, selections,
                                          user.username, location_id=_int(form.get("location_id")))
    except ValueError as exc:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=400)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


def _render_detail(request, wo_id, user, error=None, status=200):
    db = request.app.state.database
    wo = repo.get_work_order(db, wo_id)
    if wo is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Work order not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "work_order_detail.html",
        {"w": wo, "can_edit": user.role in WORK_ORDER_ROLES,
         "buy_by": repo.buy_by_for_wo(db, wo_id), "error": error},
        status_code=status,
    )


@router.get("/{wo_id}", response_class=HTMLResponse)
def work_order_detail(request: Request, wo_id: int):
    user = require_user(request)
    return _render_detail(request, wo_id, user)


@router.post("/{wo_id}/reschedule")
async def reschedule(request: Request, wo_id: int):
    user = require_role(request, WORK_ORDER_ROLES)
    form = await request.form()
    try:
        repo.reschedule_work_order(
            request.app.state.database, wo_id,
            due_date=(form.get("due_date") or "").strip() or None,
            duration_days=_int(form.get("duration_days")),
        )
    except ValueError as exc:
        return _render_detail(request, wo_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/work-orders/{wo_id}", status_code=303)


@router.post("/{wo_id}/{action}")
async def run_action(request: Request, wo_id: int, action: str):
    user = require_role(request, WORK_ORDER_ROLES)
    db = request.app.state.database
    if action == "cancel":
        try:
            repo.cancel_work_order(db, wo_id)
        except ValueError as exc:
            return _render_detail(request, wo_id, user, error=str(exc), status=400)
        return RedirectResponse(f"/work-orders/{wo_id}", status_code=303)
    fn = _ACTIONS.get(action)
    if fn is None:
        return _render_detail(request, wo_id, user, error="Unknown action.", status=400)
    try:
        fn(db, wo_id, user.username)
    except ValueError as exc:
        return _render_detail(request, wo_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/work-orders/{wo_id}", status_code=303)
