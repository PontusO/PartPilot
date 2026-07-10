"""Contacts routes: address book of suppliers/customers/other (list, add, edit)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...core.deps import require_role, require_user
from ..catalog import repo as catalog_repo
from . import repo

router = APIRouter(prefix="/contacts")

CONTACTS_WRITE_ROLES = frozenset({"admin", "purchasing"})


def _mirror_supplier(db, data: dict, *, minimrp_id) -> None:
    """Project a saved supplier-kind contact into the catalog ``suppliers`` table so it shows up
    in the part-edit and PO supplier dropdowns (those read ``suppliers``, not ``contacts``)."""
    if data.get("kind") != "supplier":
        return
    catalog_repo.upsert_supplier(
        db, name=data["name"], short_name=data.get("short_name"),
        url=data.get("website"), currency=data.get("currency"), minimrp_id=minimrp_id,
    )


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_contact(form) -> dict:
    def s(name):
        return (form.get(name) or "").strip() or None
    kind = (form.get("kind") or "").strip().lower()
    return {
        "kind": kind if kind in repo.KINDS else "supplier",
        "name": (form.get("name") or "").strip(),
        "short_name": s("short_name"), "contact": s("contact"), "email": s("email"),
        "phone": s("phone"), "phone2": s("phone2"), "fax": s("fax"),
        "address": s("address"), "postcode": s("postcode"), "country": s("country"),
        "org_no": s("org_no"), "website": s("website"),
        "currency": s("currency"), "discount": _num(form.get("discount")), "notes": s("notes"),
    }


def _parse_address(form) -> dict:
    def s(name):
        return (form.get(name) or "").strip() or None
    data = {f: s(f) for f in ("label", "company", "contact", "line1", "line2", "city",
                              "region", "postcode", "country", "phone", "email")}
    for flag in ("is_delivery", "is_invoice", "is_default_delivery", "is_default_invoice"):
        data[flag] = 1 if form.get(flag) is not None else 0
    return data


def _render_form(request, *, action, heading, submit_label, back_url, values, error=None, status=200):
    return request.app.state.templates.TemplateResponse(
        request, "contact_form.html",
        {"action": action, "heading": heading, "submit_label": submit_label,
         "back_url": back_url, "values": values or {}, "kinds": repo.KINDS, "error": error},
        status_code=status,
    )


@router.get("", response_class=HTMLResponse)
def contacts_list(request: Request, kind: str | None = None, q: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    kind = (kind or "").strip().lower() or None
    search = (q or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "contacts_list.html",
        {
            "contacts": repo.list_contacts(db, kind, search),
            "summary": repo.summary(db),
            "kinds": repo.KINDS, "kind": kind or "", "q": search or "",
            "can_edit": user.role in CONTACTS_WRITE_ROLES,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request):
    require_role(request, CONTACTS_WRITE_ROLES)
    kind = (request.query_params.get("kind") or "").strip().lower()
    return _render_form(
        request, action="/contacts/new", heading="New contact", submit_label="Create contact",
        back_url="/contacts", values={"kind": kind if kind in repo.KINDS else "supplier"},
    )


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request):
    require_role(request, CONTACTS_WRITE_ROLES)
    form = await request.form()
    data = _parse_contact(form)
    if not data["name"]:
        return _render_form(
            request, action="/contacts/new", heading="New contact",
            submit_label="Create contact", back_url="/contacts", values=data,
            error="Company name is required.", status=400,
        )
    db = request.app.state.database
    new_id = repo.create_contact(db, data)
    _mirror_supplier(db, data, minimrp_id=None)
    return RedirectResponse(f"/contacts/{new_id}", status_code=303)


@router.get("/{contact_id}", response_class=HTMLResponse)
def contact_detail(request: Request, contact_id: int):
    user = require_user(request)
    db = request.app.state.database
    c = repo.get_contact(db, contact_id)
    if c is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Contact not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "contact_detail.html",
        {"c": c, "addresses": repo.list_addresses(db, contact_id),
         "can_edit": user.role in CONTACTS_WRITE_ROLES},
    )


@router.get("/{contact_id}/addresses.json")
def addresses_json(request: Request, contact_id: int):
    require_user(request)
    rows = repo.list_addresses(request.app.state.database, contact_id)
    return JSONResponse([
        {"id": r["id"], "label": r["label"], "company": r["company"],
         "is_delivery": r["is_delivery"], "is_invoice": r["is_invoice"],
         "is_default_delivery": r["is_default_delivery"],
         "is_default_invoice": r["is_default_invoice"]}
        for r in rows
    ])


@router.get("/{contact_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, contact_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    c = repo.get_contact(request.app.state.database, contact_id)
    if c is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Contact not found."}, status_code=404)
    return _render_form(
        request, action=f"/contacts/{contact_id}/edit", heading=f"Edit — {c['name']}",
        submit_label="Save changes", back_url=f"/contacts/{contact_id}", values=c,
    )


@router.post("/{contact_id}/edit", response_class=HTMLResponse)
async def update(request: Request, contact_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    db = request.app.state.database
    if repo.get_contact(db, contact_id) is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Contact not found."}, status_code=404)
    form = await request.form()
    data = _parse_contact(form)
    if not data["name"]:
        return _render_form(
            request, action=f"/contacts/{contact_id}/edit", heading="Edit contact",
            submit_label="Save changes", back_url=f"/contacts/{contact_id}", values=data,
            error="Company name is required.", status=400,
        )
    repo.update_contact(db, contact_id, data)
    stored = repo.get_contact(db, contact_id)
    _mirror_supplier(db, data, minimrp_id=stored.get("minimrp_id") if stored else None)
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


# ---- structured addresses (delivery / invoice) ----

@router.post("/{contact_id}/addresses/add")
async def add_address(request: Request, contact_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    form = await request.form()
    repo.create_address(request.app.state.database, contact_id, _parse_address(form))
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


@router.post("/{contact_id}/addresses/{address_id}/edit")
async def edit_address(request: Request, contact_id: int, address_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    form = await request.form()
    repo.update_address(request.app.state.database, address_id, _parse_address(form))
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


@router.post("/{contact_id}/addresses/{address_id}/delete")
def delete_address(request: Request, contact_id: int, address_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    repo.delete_address(request.app.state.database, address_id)
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


@router.post("/{contact_id}/addresses/{address_id}/default")
async def default_address(request: Request, contact_id: int, address_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    form = await request.form()
    which = (form.get("which") or "").strip()
    if which in ("delivery", "invoice"):
        repo.set_default_address(request.app.state.database, address_id, which)
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)
