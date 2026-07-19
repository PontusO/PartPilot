"""Customer order routes: list / create / edit a header, manage its lines."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...core.deps import require_role, require_user
from ..despatch import repo as despatch_repo
from ..despatch.router import DESPATCH_ROLES
from ..work_orders import repo as wo_repo
from ..work_orders.router import WORK_ORDER_ROLES
from . import export, repo

router = APIRouter(prefix="/customer-orders")

# Roles allowed to raise/amend customer orders (same as the other write features for now).
CUSTOMER_ORDER_WRITE_ROLES = frozenset({"admin", "purchasing"})
# Roles allowed to reserve/free stock against an order.
ALLOCATE_ROLES = frozenset({"admin", "purchasing", "warehouse"})


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
def orders_list(request: Request, status: str | None = None, q: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    status = status if status in repo.STATUSES else None
    search = (q or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "customer_orders_list.html",
        {"orders": repo.list_orders(db, status, search), "summary": repo.summary(db),
         "statuses": repo.STATUSES, "status": status or "", "q": search or "",
         "can_edit": user.role in CUSTOMER_ORDER_WRITE_ROLES},
    )


def _parse_order(form) -> dict:
    return {
        "order_ref": (form.get("order_ref") or "").strip() or None,
        "customer_id": _int(form.get("customer_id")),
        "customer_po": (form.get("customer_po") or "").strip() or None,
        "status": (form.get("status") or "draft").strip() or "draft",
        "order_date": (form.get("order_date") or "").strip() or None,
        "required_date": (form.get("required_date") or "").strip() or None,
        "currency": (form.get("currency") or "").strip() or None,
        "discount_rate": _num(form.get("discount_rate")),
        "delivery_charge": _num(form.get("delivery_charge")),
        "tax_rate": _num(form.get("tax_rate")),
        "notes": (form.get("notes") or "").strip() or None,
        "delivery_address_id": _int(form.get("delivery_address_id")),
        "invoice_address_id": _int(form.get("invoice_address_id")),
    }


def _render_form(request, *, action, heading, submit_label, back_url, values,
                 error=None, status=200, statuses=None):
    return request.app.state.templates.TemplateResponse(
        request, "customer_order_form.html",
        {"action": action, "heading": heading, "submit_label": submit_label,
         "back_url": back_url, "values": values or {}, "error": error,
         "customers": repo.customers(request.app.state.database),
         # Only the manually-settable statuses are offered; a shipped/complete/cancelled order shows
         # its status fixed (those transitions belong to the despatch/invoice/cancel actions).
         "statuses": statuses or repo.MANUAL_STATUSES},
        status_code=status,
    )


@router.get("/new", response_class=HTMLResponse)
def new_order_form(request: Request):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    return _render_form(request, action="/customer-orders/new", heading="New customer order",
                        submit_label="Create order", back_url="/customer-orders",
                        values={"status": "draft", "order_date": date.today().isoformat(),
                                "order_ref": repo.next_order_ref(request.app.state.database)})


@router.post("/new", response_class=HTMLResponse)
async def create_order(request: Request):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    form = await request.form()
    data = _parse_order(form)
    if not data["customer_id"]:
        return _render_form(request, action="/customer-orders/new", heading="New customer order",
                            submit_label="Create order", back_url="/customer-orders",
                            values=dict(form), error="Choose a customer.", status=400)
    new_id = repo.create_order(request.app.state.database, data)
    return RedirectResponse(f"/customer-orders/{new_id}", status_code=303)


@router.get("/{order_id}/edit", response_class=HTMLResponse)
def edit_order_form(request: Request, order_id: int):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    order = repo.get_order(request.app.state.database, order_id)
    if order is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Order not found."}, status_code=404)
    return _render_form(request, action=f"/customer-orders/{order_id}/edit",
                        heading=f"Edit order {order.get('order_ref') or '#' + str(order_id)}",
                        submit_label="Save changes", back_url=f"/customer-orders/{order_id}",
                        values=order, statuses=repo.allowed_statuses(order.get("status")))


@router.post("/{order_id}/edit", response_class=HTMLResponse)
async def update_order_route(request: Request, order_id: int):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    db = request.app.state.database
    order = repo.get_order(db, order_id)
    if order is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Order not found."}, status_code=404)
    form = await request.form()
    data = _parse_order(form)
    allowed = repo.allowed_statuses(order.get("status"))
    if not data["customer_id"]:
        return _render_form(request, action=f"/customer-orders/{order_id}/edit",
                            heading="Edit customer order", submit_label="Save changes",
                            back_url=f"/customer-orders/{order_id}", values=dict(form),
                            error="Choose a customer.", status=400, statuses=allowed)
    try:
        repo.update_order(db, order_id, data)
    except ValueError as exc:  # illegal manual status transition
        return _render_form(request, action=f"/customer-orders/{order_id}/edit",
                            heading="Edit customer order", submit_label="Save changes",
                            back_url=f"/customer-orders/{order_id}", values=dict(form),
                            error=str(exc), status=400, statuses=allowed)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


def _render_detail(request, order_id, user, error=None, status=200):
    db = request.app.state.database
    order = repo.get_order(db, order_id)
    if order is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Order not found."}, status_code=404)
    can_edit = user.role in CUSTOMER_ORDER_WRITE_ROLES
    proposals = wo_repo.fulfilment_proposals(db, order_id)
    return request.app.state.templates.TemplateResponse(
        request, "customer_order_detail.html",
        {"o": order, "can_edit": can_edit,
         "parts": repo.parts_for_picker(db) if can_edit else [], "error": error,
         "can_build": user.role in WORK_ORDER_ROLES,
         "can_allocate": user.role in ALLOCATE_ROLES,
         "can_ship": user.role in DESPATCH_ROLES,
         "despatches": despatch_repo.despatches_for_order(db, order_id),
         "downstream": repo.order_downstream(db, order_id),
         "wos_by_line": wo_repo.work_orders_for_order(db, order_id),
         "acknowledgements": repo.documents_for_order(db, order_id),
         "build_short_count": sum(1 for p in proposals if p["category"] == "build")},
        status_code=status,
    )


@router.get("/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int):
    user = require_user(request)
    return _render_detail(request, order_id, user)


@router.post("/{order_id}/lines/add", response_class=HTMLResponse)
async def add_line(request: Request, order_id: int):
    user = require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    form = await request.form()
    part_id = _int(form.get("part_id"))
    if not part_id:
        return _render_detail(request, order_id, user, error="Choose a product to add.", status=400)
    try:
        repo.add_line(request.app.state.database, order_id, part_id,
                      _num(form.get("qty")) or 1, _num(form.get("unit_price")),
                      _num(form.get("discount")))
    except ValueError as exc:
        return _render_detail(request, order_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/lines/{line_id}/update")
async def update_line(request: Request, order_id: int, line_id: int):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    form = await request.form()
    repo.update_line(request.app.state.database, order_id, line_id,
                     _num(form.get("qty")) or 0, _num(form.get("unit_price")),
                     _num(form.get("discount")))
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/lines/{line_id}/reprice")
def reprice_line(request: Request, order_id: int, line_id: int):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    repo.reprice_line(request.app.state.database, order_id, line_id)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/lines/{line_id}/delete")
def delete_line(request: Request, order_id: int, line_id: int):
    require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    repo.delete_line(request.app.state.database, order_id, line_id)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/allocate")
def allocate(request: Request, order_id: int):
    require_role(request, ALLOCATE_ROLES)
    repo.allocate_order(request.app.state.database, order_id)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/release")
def release(request: Request, order_id: int):
    require_role(request, ALLOCATE_ROLES)
    repo.release_order_allocations(request.app.state.database, order_id)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/cancel")
def cancel(request: Request, order_id: int):
    user = require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    try:
        repo.cancel_order(request.app.state.database, order_id)
    except ValueError as exc:
        return _render_detail(request, order_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.post("/{order_id}/acknowledge")
def acknowledge(request: Request, order_id: int):
    user = require_role(request, CUSTOMER_ORDER_WRITE_ROLES)
    try:
        repo.acknowledge_order(request.app.state.database, order_id, user.username)
    except ValueError as exc:
        return _render_detail(request, order_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/customer-orders/{order_id}", status_code=303)


@router.get("/{order_id}/acknowledgement.pdf")
def acknowledgement_pdf(request: Request, order_id: int):
    """Serve the archived acknowledgement if one has been issued, else a live preview of the
    current order so it can be reviewed before acknowledging."""
    require_user(request)
    db = request.app.state.database
    order = repo.get_order(db, order_id)
    if order is None:
        return Response("not found", status_code=404)
    doc = repo.get_document(db, order_id, "pdf")
    content = doc["content"] if doc else export.ack_pdf(order, export._company(db))
    filename = doc["filename"] if doc else f"OA-{order.get('order_ref') or 'CO-' + str(order_id)}.pdf"
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
