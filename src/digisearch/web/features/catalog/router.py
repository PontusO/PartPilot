"""Catalog routes: browse parts, view a part, and add/edit components."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core.deps import require_role, require_user
from . import repo, stock

router = APIRouter(prefix="/catalog")

_PAGE_SIZE = 100

# Roles allowed to add/edit catalog data.
CATALOG_WRITE_ROLES = frozenset({"admin", "purchasing"})
# Roles allowed to move stock (receive/issue/adjust) — warehouse handles inventory.
STOCK_MOVE_ROLES = frozenset({"admin", "warehouse", "purchasing"})


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


def _get(lst: list, i: int):
    return lst[i] if i < len(lst) else None


def _positive_or_none(v: float | None) -> float | None:
    return v if (v is not None and v > 0) else None


# ---- shared form parsing / rendering (new + edit) ----

def _parse_part(form) -> dict:
    return {
        "part_no": (form.get("part_no") or "").strip(),
        "value": (form.get("value") or "").strip() or None,
        "description": (form.get("description") or "").strip() or None,
        "category": (form.get("category") or "").strip().upper() or None,
        "mfr_name": (form.get("mfr_name") or "").strip() or None,
        "mfr_pno": (form.get("mfr_pno") or "").strip() or None,
        "rev": (form.get("rev") or "").strip() or None,
        "min_qty": _num(form.get("min_qty")) or 0,
        "notes": (form.get("notes") or "").strip() or None,
        "unlimited_stock": 1 if form.get("unlimited_stock") else 0,
        "normally_stocked": 1 if form.get("normally_stocked") else 0,
        "exclude_from_bom_cost": 1 if form.get("exclude_from_bom_cost") else 0,
        # A per-part markup must be > 0 (0/negative would zero the part's sell prices); blank/invalid
        # -> None, which falls back to the Setup default.
        "markup": _positive_or_none(_num(form.get("markup"))),
    }


def _parse_sell_tiers(form) -> list[dict]:
    """Customer sell tiers from the flat repeating-row editor: parallel break-qty / unit-price
    lists. Rows missing either field are dropped."""
    qtys = form.getlist("sell_break_qty")
    prices = form.getlist("sell_unit_price")
    tiers = []
    for i in range(max(len(qtys), len(prices))):
        bq, up = _num(_get(qtys, i)), _num(_get(prices, i))
        if bq is None or up is None:
            continue
        tiers.append({"break_qty": bq, "unit_price": up})
    return tiers


def _supplier_map(db) -> dict[int, str]:
    return {s["id"]: s["name"] for s in repo.suppliers(db)}


def _parse_supplier_lines(form, sup_map: dict[int, str]) -> list[dict]:
    keys = form.getlist("row_key")
    sids = form.getlist("supplier_id")
    new_names = form.getlist("new_supplier_name")
    pnos = form.getlist("supplier_pno")
    prices = form.getlist("unit_price")
    reels = form.getlist("reel_qty")
    moqs = form.getlist("moq")
    leads = form.getlist("lead_time")
    default_key = form.get("default_key")

    lines = []
    for i in range(len(sids)):
        sid = (_get(sids, i) or "").strip()
        if sid == "__new__":
            name, supplier_id = (_get(new_names, i) or "").strip(), None
        elif sid:
            supplier_id = _int(sid)
            name = sup_map.get(supplier_id)
        else:
            name, supplier_id = None, None
        if not name:
            continue  # row with no supplier chosen
        lines.append({
            "supplier_id": supplier_id,
            "supplier_name": name,
            "supplier_pno": (_get(pnos, i) or "").strip() or None,
            "unit_price": _num(_get(prices, i)),
            "reel_qty": _num(_get(reels, i)) or 1,
            "moq": _num(_get(moqs, i)),
            "lead_time": _int(_get(leads, i)),
            "is_default": _get(keys, i) == default_key,
        })
    if lines and not any(line["is_default"] for line in lines):
        lines[0]["is_default"] = True
    return lines


def _parse_stock(form) -> dict:
    return {
        "qty": _num(form.get("stock_qty")),
        "location_id": _int(form.get("location_id")),
        "bin": (form.get("bin") or "").strip() or None,
    }


def _render_form(request: Request, *, action: str, heading: str, submit_label: str,
                 stock_heading: str, back_url: str, values: dict, supplier_rows: list[dict],
                 stock: dict, sell_tier_rows: list[dict] | None = None,
                 part_id: int | None = None, error: str | None = None,
                 notice: str | None = None):
    db = request.app.state.database
    templates = request.app.state.templates
    if supplier_rows and not any(r.get("is_default") for r in supplier_rows):
        supplier_rows[0]["is_default"] = True
    return templates.TemplateResponse(
        request,
        "part_form.html",
        {
            "action": action, "heading": heading, "submit_label": submit_label,
            "stock_heading": stock_heading, "back_url": back_url,
            "values": values or {}, "supplier_rows": supplier_rows or [{"is_default": True}],
            "stock": stock or {}, "error": error, "notice": notice,
            "sell_tier_rows": sell_tier_rows or [], "part_id": part_id,
            "categories": repo.categories(db), "suppliers": repo.suppliers(db),
            "locations": repo.locations(db),
        },
        status_code=400 if error else 200,
    )


def _render_edit_form(request: Request, part: dict, *, notice: str | None = None):
    """Render the edit form for an already-loaded part (shared by the GET edit page and the
    post-action re-renders like cost-tier refresh)."""
    part_id = part["id"]
    supplier_rows = [
        {"supplier_id": s["supplier_id"], "supplier_name": s["supplier_name"],
         "supplier_pno": s["supplier_pno"], "unit_price": s["unit_price"],
         "reel_qty": s["qty_per_uom"], "moq": s["moq"], "lead_time": s["lead_time"],
         "is_default": bool(s["is_default"]), "cost_tiers": s.get("cost_tiers") or []}
        for s in part["suppliers"]
    ]
    st = part["stock"][0] if part["stock"] else {}
    stock = {"qty": st.get("on_hand"), "location_id": st.get("location_id"), "bin": st.get("bin")}
    return _render_form(
        request, action=f"/catalog/{part_id}/edit", heading=f"Edit — {part['part_no']}",
        submit_label="Save changes", stock_heading="Stock on hand",
        back_url=f"/catalog/{part_id}", values=part,
        supplier_rows=supplier_rows or [{"is_default": True}], stock=stock,
        sell_tier_rows=part.get("sell_tiers") or [], part_id=part_id, notice=notice,
    )


# ---- list ----

@router.get("", response_class=HTMLResponse)
def parts_list(request: Request, q: str | None = None, category: str | None = None,
               stocked: bool = False, page: int = 1):
    user = require_user(request)
    db = request.app.state.database
    templates = request.app.state.templates

    page = max(1, page)
    search = (q or "").strip() or None
    category = (category or "").strip() or None
    parts, total = repo.list_parts(
        db, search=search, category=category, stocked_only=stocked,
        limit=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE
    )
    return templates.TemplateResponse(
        request,
        "parts_list.html",
        {
            "parts": parts, "total": total, "page": page, "page_size": _PAGE_SIZE,
            "has_prev": page > 1, "has_next": page * _PAGE_SIZE < total,
            "q": search or "", "category": category or "", "stocked": stocked,
            "categories": repo.categories(db), "summary": repo.summary(db),
            "can_add": user.role in CATALOG_WRITE_ROLES,
        },
    )


# ---- parts for a supplier ----

@router.get("/supplier", response_class=HTMLResponse)
def parts_by_supplier(request: Request, name: str | None = None):
    require_user(request)
    db = request.app.state.database
    supplier = (name or "").strip()
    return request.app.state.templates.TemplateResponse(
        request, "supplier_parts.html",
        {"supplier": supplier, "parts": repo.parts_for_supplier(db, supplier)},
    )


# ---- add ----

@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request, part_no: str | None = None):
    require_role(request, CATALOG_WRITE_ROLES)
    # part_no may be prefilled when returning from the Article Register allocator.
    values = {"part_no": part_no.strip()} if (part_no or "").strip() else {}
    return _render_form(
        request, action="/catalog/new", heading="Add component", submit_label="Save component",
        stock_heading="Opening stock", back_url="/catalog", values=values,
        supplier_rows=[{"is_default": True}], stock={"qty": 0},
    )


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request):
    require_role(request, CATALOG_WRITE_ROLES)
    db = request.app.state.database
    form = await request.form()
    part = _parse_part(form)
    part["sell_tiers"] = _parse_sell_tiers(form)
    lines = _parse_supplier_lines(form, _supplier_map(db))
    if not part["part_no"]:
        return _render_form(
            request, action="/catalog/new", heading="Add component", submit_label="Save component",
            stock_heading="Opening stock", back_url="/catalog", values=dict(form),
            supplier_rows=lines or [{"is_default": True}], stock=_parse_stock(form),
            sell_tier_rows=part["sell_tiers"], error="Part number is required.",
        )
    part_id = repo.create_part(db, part=part, supplier_lines=lines, opening=_parse_stock(form))
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


# ---- edit ----

@router.get("/{part_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, part_id: int):
    require_role(request, CATALOG_WRITE_ROLES)
    db = request.app.state.database
    part = repo.get_part(db, part_id)
    if part is None:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404
        )
    return _render_edit_form(request, part)


@router.post("/{part_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, part_id: int):
    require_role(request, CATALOG_WRITE_ROLES)
    db = request.app.state.database
    if repo.get_part(db, part_id) is None:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404
        )
    form = await request.form()
    part = _parse_part(form)
    sell_tiers = _parse_sell_tiers(form)
    lines = _parse_supplier_lines(form, _supplier_map(db))
    if not part["part_no"]:
        return _render_form(
            request, action=f"/catalog/{part_id}/edit", heading="Edit component",
            submit_label="Save changes", stock_heading="Stock on hand",
            back_url=f"/catalog/{part_id}", values=dict(form),
            supplier_rows=lines or [{"is_default": True}], stock=_parse_stock(form),
            sell_tier_rows=sell_tiers, part_id=part_id, error="Part number is required.",
        )
    repo.update_part(db, part_id, part=part, supplier_lines=lines, stock=_parse_stock(form))
    repo.replace_sell_tiers(db, part_id, sell_tiers)
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


@router.post("/{part_id}/generate-sell-tiers", response_class=HTMLResponse)
async def generate_sell_tiers(request: Request, part_id: int):
    """Generate customer sell tiers from the default supplier's captured cost breaks x markup
    (per-part markup if set, else the Setup default). Manual tiers are preserved. Uses the *saved*
    markup — save a changed markup before generating."""
    require_role(request, CATALOG_WRITE_ROLES)
    from ..setup import repo as setup_repo

    db = request.app.state.database
    if repo.get_part(db, part_id) is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404)
    repo.generate_sell_tiers_from_cost(db, part_id, setup_repo.get_default_markup(db))
    return RedirectResponse(f"/catalog/{part_id}/edit", status_code=303)


@router.post("/{part_id}/refresh-cost-tiers", response_class=HTMLResponse)
def refresh_cost_tiers(request: Request, part_id: int):
    """Re-query Digi-Key/Mouser for each of the part's distributor offers and overwrite their cost
    tiers with the current price breaks. A sync handler so FastAPI runs the (blocking) HTTP calls in
    a worker thread. Re-renders the edit form with a per-offer result summary."""
    require_role(request, CATALOG_WRITE_ROLES)
    from . import cost_refresh

    db = request.app.state.database
    if repo.get_part(db, part_id) is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404)
    result = cost_refresh.refresh_cost_tiers(db, part_id)
    parts = []
    if result["updated"]:
        parts.append("Updated — " + "; ".join(result["updated"]))
    if result["skipped"]:
        parts.append("Skipped — " + "; ".join(result["skipped"]))
    if result["errors"]:
        parts.append("Errors — " + "; ".join(result["errors"]))
    notice = " • ".join(parts) or "No Digi-Key or Mouser offers to refresh."
    # Reload so the freshly written tiers show, then render with the summary.
    return _render_edit_form(request, repo.get_part(db, part_id), notice=notice)


# ---- detail ----

@router.get("/{part_id}", response_class=HTMLResponse)
def part_detail(request: Request, part_id: int):
    user = require_user(request)
    db = request.app.state.database
    templates = request.app.state.templates
    part = repo.get_part(db, part_id)
    if part is None:
        return templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404
        )
    return templates.TemplateResponse(
        request, "part_detail.html",
        {"part": part, "can_edit": user.role in CATALOG_WRITE_ROLES,
         "can_move": user.role in STOCK_MOVE_ROLES,
         "movements": stock.movements_for_part(db, part_id),
         "locations": repo.locations(db)},
    )


# ---- stock movements (receive / issue / adjust) ----

@router.post("/{part_id}/stock/move")
async def move_stock(request: Request, part_id: int):
    user = require_role(request, STOCK_MOVE_ROLES)
    db = request.app.state.database
    part = repo.get_part(db, part_id)
    if part is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404)

    form = await request.form()
    action = (form.get("action") or "").strip()       # receive | issue | adjust
    qty = _num(form.get("qty"))
    location_id = _int(form.get("location_id"))
    note = (form.get("note") or "").strip() or None
    reference = (form.get("reference") or "").strip() or "manual"

    if action == "receive" and qty:
        delta, mtype = abs(qty), stock.RECEIVE
    elif action == "issue" and qty:
        delta, mtype = -abs(qty), stock.ISSUE
    elif action == "adjust" and qty is not None:       # qty is the new on-hand target
        delta, mtype = qty - (part["total_qty"] or 0), stock.ADJUST
    else:
        delta = None

    if delta is not None:
        stock.adjust_stock(db, part_id, delta=delta, mtype=mtype, reference=reference,
                           note=note, user=user.username, location_id=location_id)
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)
