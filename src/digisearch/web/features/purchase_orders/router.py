"""Purchase order routes: list, the shortage→suggested-PO automation, manual PO entry,
the order/receive/cancel lifecycle, and goods receiving — every automated step operator-confirmed.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ...core.deps import require_role, require_user
from . import export, repo

router = APIRouter(prefix="/purchase-orders")

PO_WRITE_ROLES = frozenset({"admin", "purchasing"})            # raise / amend / place / cancel
PO_RECEIVE_ROLES = frozenset({"admin", "purchasing", "warehouse"})  # book goods in


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("", response_class=HTMLResponse)
def po_list(request: Request, status: str | None = None, q: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    status = status if status in repo.STATUSES else None
    search = (q or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "purchase_orders_list.html",
        {"pos": repo.list_pos(db, status, search), "summary": repo.summary(db),
         "shortages": len(repo.shortage_suggestions(db)), "statuses": repo.STATUSES,
         "status": status or "", "q": search or "", "can_edit": user.role in PO_WRITE_ROLES},
    )


# ---- shortage → suggested POs (the automation) ----

@router.get("/suggestions", response_class=HTMLResponse)
def suggestions(request: Request):
    require_role(request, PO_WRITE_ROLES)
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "purchase_order_suggestions.html",
        {"groups": repo.shortage_suggestions_grouped(db)},
    )


@router.post("/suggestions")
async def suggestions_apply(request: Request):
    user = require_role(request, PO_WRITE_ROLES)
    form = await request.form()
    selected = {int(v) for v in form.getlist("buy") if str(v).isdigit()}
    selections = {}
    for part_id in selected:
        qty = _num(form.get(f"qty_{part_id}"))
        if qty and qty > 0:
            selections[part_id] = qty
    # May query Digi-Key/Mouser to tier-price each line — run off the event loop.
    await run_in_threadpool(
        repo.create_pos_from_suggestions, request.app.state.database, selections, user.username)
    return RedirectResponse("/purchase-orders", status_code=303)


# ---- manual new PO ----

@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request):
    require_role(request, PO_WRITE_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "purchase_order_form.html",
        {"values": {}, "suppliers": repo.suppliers(request.app.state.database), "error": None},
    )


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request):
    require_role(request, PO_WRITE_ROLES)
    form = await request.form()
    data = {
        "po_no": (form.get("po_no") or "").strip() or None,
        "supplier_id": _int(form.get("supplier_id")),
        "order_date": (form.get("order_date") or "").strip() or None,
        "required_date": (form.get("required_date") or "").strip() or None,
        "currency": (form.get("currency") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
    }
    if not data["supplier_id"]:
        return request.app.state.templates.TemplateResponse(
            request, "purchase_order_form.html",
            {"values": dict(form), "suppliers": repo.suppliers(request.app.state.database),
             "error": "Choose a supplier."}, status_code=400)
    po_id = repo.create_po(request.app.state.database, data)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


# ---- detail + lines + lifecycle ----

def _render_detail(request, po_id, user, error=None, status=200):
    db = request.app.state.database
    po = repo.get_po(db, po_id)
    if po is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Purchase order not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "purchase_order_detail.html",
        {"po": po, "can_edit": user.role in PO_WRITE_ROLES,
         "can_receive": user.role in PO_RECEIVE_ROLES,
         "parts": repo.parts_for_picker(db) if user.role in PO_WRITE_ROLES else [],
         "receipts": repo.receipts_for_po(db, po_id),
         "documents": repo.documents_for_po(db, po_id), "error": error},
        status_code=status,
    )


@router.get("/{po_id}", response_class=HTMLResponse)
def po_detail(request: Request, po_id: int):
    user = require_user(request)
    return _render_detail(request, po_id, user)


def _filename(po, ext):
    return f"{(po.get('po_no') or 'PO-' + str(po['id']))}.{ext}"


def _serve(po, db, po_id, kind, media_type, generate):
    """Serve the archived document if the PO has one (placed), else a live preview (draft)."""
    doc = repo.get_document(db, po_id, kind)
    content = doc["content"] if doc else generate()
    filename = doc["filename"] if doc else _filename(po, kind)
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{po_id}/export.csv")
def export_csv(request: Request, po_id: int):
    require_user(request)
    db = request.app.state.database
    po = repo.get_po(db, po_id)
    if po is None:
        return Response("not found", status_code=404)
    return _serve(po, db, po_id, "csv", "text/csv", lambda: export.po_csv(po).encode("utf-8"))


@router.get("/{po_id}/export.pdf")
def export_pdf(request: Request, po_id: int):
    require_user(request)
    db = request.app.state.database
    po = repo.get_po(db, po_id)
    if po is None:
        return Response("not found", status_code=404)
    return _serve(po, db, po_id, "pdf", "application/pdf",
                  lambda: export.po_pdf(po, export._company(db)))


@router.post("/{po_id}/lines/add", response_class=HTMLResponse)
async def add_line(request: Request, po_id: int):
    user = require_role(request, PO_WRITE_ROLES)
    form = await request.form()
    part_id = _int(form.get("part_id"))
    if not part_id:
        return _render_detail(request, po_id, user, error="Choose a part to add.", status=400)
    try:
        # A distributor-sourced line with no explicit price is tier-priced via a live lookup.
        await run_in_threadpool(
            repo.add_line, request.app.state.database, po_id, part_id,
            _num(form.get("qty")) or 1, _num(form.get("unit_price")))
    except ValueError as exc:
        return _render_detail(request, po_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@router.post("/{po_id}/lines/{line_id}/update")
async def update_line(request: Request, po_id: int, line_id: int):
    user = require_role(request, PO_WRITE_ROLES)
    form = await request.form()
    try:
        repo.update_line(request.app.state.database, po_id, line_id, _num(form.get("qty")),
                         _num(form.get("unit_price")))
    except ValueError as exc:
        return _render_detail(request, po_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@router.post("/{po_id}/lines/{line_id}/delete")
def delete_line(request: Request, po_id: int, line_id: int):
    require_role(request, PO_WRITE_ROLES)
    repo.delete_line(request.app.state.database, po_id, line_id)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@router.post("/{po_id}/order")
def place_order(request: Request, po_id: int):
    user = require_role(request, PO_WRITE_ROLES)
    try:
        repo.mark_ordered(request.app.state.database, po_id, user.username)
    except ValueError as exc:
        return _render_detail(request, po_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@router.post("/{po_id}/delete")
def delete_po(request: Request, po_id: int):
    user = require_role(request, PO_WRITE_ROLES)
    try:
        repo.delete_po(request.app.state.database, po_id)
    except ValueError as exc:
        return _render_detail(request, po_id, user, error=str(exc), status=400)
    return RedirectResponse("/purchase-orders", status_code=303)


@router.post("/{po_id}/cancel")
def cancel(request: Request, po_id: int):
    user = require_role(request, PO_WRITE_ROLES)
    try:
        repo.cancel_po(request.app.state.database, po_id)
    except ValueError as exc:
        return _render_detail(request, po_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)


@router.post("/{po_id}/receive")
async def receive(request: Request, po_id: int):
    user = require_role(request, PO_RECEIVE_ROLES)
    form = await request.form()
    db = request.app.state.database
    po = repo.get_po(db, po_id)
    if po is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Purchase order not found."}, status_code=404)
    receipts = {}
    for ln in po["lines"]:
        qty = _num(form.get(f"recv_{ln['id']}"))
        if qty and qty > 0:
            receipts[ln["id"]] = qty
    advice_no = (form.get("advice_no") or "").strip() or None
    try:
        grn_id = repo.receive_po(db, po_id, receipts, user.username, advice_no)
    except ValueError as exc:
        return _render_detail(request, po_id, user, error=str(exc), status=400)
    if grn_id:
        return RedirectResponse(f"/goods-receipts/{grn_id}", status_code=303)
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)
