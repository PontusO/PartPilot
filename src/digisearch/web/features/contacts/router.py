"""Contacts routes: address book of suppliers/customers/other (list, add, edit)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...core.deps import require_role, require_user
from . import repo

router = APIRouter(prefix="/contacts")

CONTACTS_WRITE_ROLES = frozenset({"admin", "purchasing"})


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
        "address": s("address"), "postcode": s("postcode"), "website": s("website"),
        "currency": s("currency"), "discount": _num(form.get("discount")), "notes": s("notes"),
    }


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
    repo.create_contact(request.app.state.database, data)
    return RedirectResponse("/contacts", status_code=303)


@router.get("/{contact_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, contact_id: int):
    require_role(request, CONTACTS_WRITE_ROLES)
    c = repo.get_contact(request.app.state.database, contact_id)
    if c is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Contact not found."}, status_code=404)
    return _render_form(
        request, action=f"/contacts/{contact_id}/edit", heading=f"Edit — {c['name']}",
        submit_label="Save changes", back_url="/contacts", values=c,
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
            submit_label="Save changes", back_url="/contacts", values=data,
            error="Company name is required.", status=400,
        )
    repo.update_contact(db, contact_id, data)
    return RedirectResponse("/contacts", status_code=303)
