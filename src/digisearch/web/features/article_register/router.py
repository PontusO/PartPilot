"""Article Register routes: browse/search internal numbers, view a running-number family, and
allocate (new product / single number) and retire numbers with a guided helper.

Reads are open to any signed-in user; writes are gated to Admin + Purchasing (``PURCHASE_ROLES``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...auth import PURCHASE_ROLES
from ...core.deps import require_role, require_user
from . import repo
from .repo import DuplicateNumber

router = APIRouter(prefix="/article-register")

WRITE_ROLES = PURCHASE_ROLES


def _s(form, name: str) -> str | None:
    return (form.get(name) or "").strip() or None


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _not_found(request):
    return request.app.state.templates.TemplateResponse(
        request, "error.html", {"message": "Article number not found."}, status_code=404)


@router.get("", response_class=HTMLResponse)
def register_list(request: Request, q: str | None = None, prefix: str | None = None,
                  category: str | None = None, retired: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    search = (q or "").strip() or None
    prefix = (prefix or "").strip() or None
    category = (category or "").strip() or None
    include_retired = retired in ("1", "on", "true")
    return request.app.state.templates.TemplateResponse(
        request, "article_register_list.html",
        {
            "entries": repo.list_entries(db, search=search, prefix=prefix, category=category,
                                         include_retired=include_retired),
            "summary": repo.summary(db),
            "prefixes": repo.list_prefixes(db),
            "categories": repo.CATEGORIES,
            "q": search or "", "prefix": prefix or "", "category": category or "",
            "include_retired": include_retired,
            "can_edit": user.role in WRITE_ROLES,
        },
    )


def _render_form(request, *, values, error=None, status=200):
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "article_register_form.html",
        {
            "groups": repo.prefixes_grouped(db),
            "next_running_no": repo.next_running_no(db),
            "values": values,
            "error": error,
        },
        status_code=status,
    )


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request, running_no: int | None = None):
    require_role(request, WRITE_ROLES)
    mode = "existing" if running_no else "new"
    return _render_form(request, values={
        "mode": mode, "running_no": running_no or "", "prefix": "", "suffix": "",
        "product": "", "created_by": "", "comment": "",
    })


@router.get("/api/next-suffix")
def api_next_suffix(request: Request, prefix: str, running_no: int):
    """Helper for the allocator's live suffix: the next free suffix for this prefix+running number."""
    require_user(request)
    db = request.app.state.database
    return JSONResponse({"suffix": repo.next_suffix(db, prefix.zfill(2), running_no)})


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    form = await request.form()
    mode = (form.get("mode") or "new").strip()
    prefix = _s(form, "prefix")
    product = _s(form, "product")
    created_by = _s(form, "created_by")
    comment = _s(form, "comment")

    values = {"mode": mode, "running_no": form.get("running_no") or "", "prefix": prefix or "",
              "suffix": form.get("suffix") or "", "product": product or "",
              "created_by": created_by or "", "comment": comment or ""}

    if not prefix:
        return _render_form(request, values=values, error="Pick a prefix (group).", status=400)

    if mode == "existing":
        running_no = _int(form.get("running_no"))
        if not running_no:
            return _render_form(request, values=values,
                                error="Enter the existing running number.", status=400)
    else:
        running_no = repo.next_running_no(db)  # recompute at submit to avoid a stale reservation

    suffix = _int(form.get("suffix")) or repo.next_suffix(db, prefix.zfill(2), running_no)

    try:
        repo.create_entry(db, prefix=prefix, running_no=running_no, suffix=suffix,
                          product=product, created_by=created_by, comment=comment)
    except DuplicateNumber as exc:
        return _render_form(request, values=values, error=str(exc), status=400)
    return RedirectResponse(f"/article-register/{running_no}", status_code=303)


def _render_product_form(request, *, values, error=None, status=200):
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "article_register_product.html",
        {"groups": repo.prefixes_grouped(db), "next_running_no": repo.next_running_no(db),
         "values": values, "error": error},
        status_code=status,
    )


@router.get("/product", response_class=HTMLResponse)
def new_product_form(request: Request):
    require_role(request, WRITE_ROLES)
    return _render_product_form(request, values={
        "product": "", "prefixes": [], "created_by": "", "comment": ""})


@router.post("/product", response_class=HTMLResponse)
async def create_product(request: Request):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    form = await request.form()
    product = _s(form, "product")
    prefixes = [p for p in form.getlist("prefixes") if p]
    values = {"product": product or "", "prefixes": prefixes,
              "created_by": form.get("created_by") or "", "comment": form.get("comment") or ""}
    if not prefixes:
        return _render_product_form(request, values=values,
                                    error="Tick at least one group.", status=400)
    running_no = repo.create_product(db, product=product, prefixes=prefixes,
                                     created_by=_s(form, "created_by"), comment=_s(form, "comment"))
    return RedirectResponse(f"/article-register/{running_no}", status_code=303)


@router.get("/{running_no}", response_class=HTMLResponse)
def family_detail(request: Request, running_no: int):
    user = require_user(request)
    db = request.app.state.database
    family = repo.get_family(db, running_no)
    if not family:
        return _not_found(request)
    return request.app.state.templates.TemplateResponse(
        request, "article_register_detail.html",
        {
            "running_no": running_no,
            "family": family,
            "groups": repo.prefixes_grouped(db),
            "can_edit": user.role in WRITE_ROLES,
        },
    )


@router.post("/{entry_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, entry_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    entry = repo.get_entry(db, entry_id)
    if entry is None:
        return _not_found(request)
    form = await request.form()
    repo.update_entry(db, entry_id, product=_s(form, "product"),
                      created_by=_s(form, "created_by"), comment=_s(form, "comment"))
    return RedirectResponse(f"/article-register/{entry['running_no']}", status_code=303)


@router.post("/{entry_id}/retire", response_class=HTMLResponse)
async def retire(request: Request, entry_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    entry = repo.get_entry(db, entry_id)
    if entry is None:
        return _not_found(request)
    form = await request.form()
    repo.set_retired(db, entry_id, (form.get("retired") or "1") != "0")
    return RedirectResponse(f"/article-register/{entry['running_no']}", status_code=303)
