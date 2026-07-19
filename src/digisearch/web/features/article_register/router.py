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
from .codes import DESC_SEP, article_code
from .repo import DuplicateNumber

router = APIRouter(prefix="/article-register")

WRITE_ROLES = PURCHASE_ROLES
# Hard deletion is rare and irreversible — restrict it to admins. Retiring (the normal lifecycle
# action) stays open to WRITE_ROLES.
DELETE_ROLES = frozenset({"admin"})


def _s(form, name: str) -> str | None:
    return (form.get(name) or "").strip() or None


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _safe_return(url: str | None) -> str | None:
    """Sanitise a caller-supplied return path so allocation can bounce back to it.

    Only same-site absolute paths are honoured (``/catalog/new``, ``/assemblies/new``) — anything
    that could redirect off-site (scheme, host, protocol-relative ``//``) is rejected.
    """
    url = (url or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        return url
    return None


def _return_url(return_to: str, **params) -> str:
    """Build the bounce-back URL, appending the given truthy query params — the freshly-allocated
    ``part_no`` (the prefill the create forms read), plus optionally ``desc`` (name to prefill) and
    ``created`` (comma-separated stub codes the New-assembly dialog lists)."""
    from urllib.parse import urlencode
    q = urlencode({k: v for k, v in params.items() if v})
    if not q:
        return return_to
    sep = "&" if "?" in return_to else "?"
    return f"{return_to}{sep}{q}"


def _assembly_code_for(db, running_no: int) -> str | None:
    """The code to hand back to the New-assembly page after allocating a family: the assembly (``98``)
    line if the family has one, else the lowest-prefix coded line. ``None`` if the family is all
    reserved (no codes)."""
    coded = [e for e in repo.get_family(db, running_no) if e.get("code")]
    if not coded:
        return None
    return next((e["code"] for e in coded if e["prefix"] == "98"),
                sorted(coded, key=lambda e: (e["prefix"], e["suffix"]))[0]["code"])


def _family_product(family: list[dict]) -> str:
    """The base product name for a family, used to seed a fresh 'Apply template'. Prefer the assembly
    (``98``) line's product, else the lowest-prefix coded line's; strip any composed ``' – <label>'``
    suffix so we hand back the bare product (e.g. 'MB', not 'MB – PCB')."""
    coded = [e for e in family if e.get("code") and e.get("product")]
    if not coded:
        return ""
    chosen = next((e for e in coded if e["prefix"] == "98"),
                  sorted(coded, key=lambda e: (e["prefix"], e["suffix"]))[0])
    return (chosen["product"] or "").split(DESC_SEP)[0].strip()


def _create_stub_parts(db, running_no: int, skip_code: str) -> list[str]:
    """Create a bare catalog PART (no supplier / price / stock) for every coded family line except
    the assembly line handed back to the form. These are deliberately incomplete stubs — the user
    edits them before real use. Codes already in the catalog are left untouched (idempotent). Returns
    the codes actually created, in family order."""
    from ..catalog import repo as catalog_repo  # catalog is registered first — parts table exists
    created = []
    for e in repo.get_family(db, running_no):
        code = e.get("code")
        if not code or code == skip_code or catalog_repo.find_part_by_part_no(db, code):
            continue
        if _is_document_line(e):  # document-class lines become documents, not catalog parts
            continue
        # The product/description is stored in the part's `value` field (house convention), not
        # `description` — see the note on the from-template flow.
        catalog_repo.create_part(db, part={"part_no": code, "value": e.get("product")},
                                 supplier_lines=[])
        created.append(code)
    return created


def _is_document_line(entry: dict) -> bool:
    """A family line that belongs in the Documents feature, not the parts catalog: the document
    classes (50–59) plus 95 (software / source code). Matches the family-page Create-document rule."""
    return entry.get("category") == "document" or entry.get("prefix") == "95"


def _create_stub_documents(db, running_no: int) -> list[str]:
    """Create a bare document row (no revision yet — the user attaches the file/link afterwards) for
    every document-class family line that doesn't already have one. 95 (software) defaults to a link,
    everything else to a file. Idempotent. Returns the codes actually created, in family order."""
    from ..documents import repo as documents_repo  # documents is registered after article_register
    created = []
    for e in repo.get_family(db, running_no):
        code = e.get("code")
        if not code or not _is_document_line(e) or documents_repo.document_for_code(db, code):
            continue
        documents_repo.create_document(
            db, code=code, running_no=running_no, prefix=e["prefix"],
            title=e.get("product") or code,
            storage_kind="link" if e.get("prefix") == "95" else "file")
        created.append(code)
    return created


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
def new_form(request: Request, running_no: int | None = None, prefix: str | None = None,
             return_to: str | None = None):
    require_role(request, WRITE_ROLES)
    mode = "existing" if running_no else "new"
    return _render_form(request, values={
        "mode": mode, "running_no": running_no or "", "prefix": (prefix or "").strip(), "suffix": "",
        "product": "", "created_by": "", "comment": "", "return_to": _safe_return(return_to) or "",
    })


@router.get("/api/next-suffix")
def api_next_suffix(request: Request, prefix: str, running_no: int):
    """Helper for the allocator's live suffix: the next free suffix for this prefix+running number."""
    require_user(request)
    db = request.app.state.database
    return JSONResponse({"suffix": repo.next_suffix(db, prefix.zfill(2), running_no)})


@router.get("/api/unassigned")
def api_unassigned(request: Request, q: str | None = None, prefix: str | None = None):
    """Typeahead source for the part-number fields on Add-component / New-assembly.

    Returns live article numbers not yet tied to a catalog part/assembly, matching the typed text.
    Open to any signed-in user (read-only). ``prefix`` scopes to one category (e.g. ``98``).
    """
    require_user(request)
    db = request.app.state.database
    query = (q or "").strip() or None
    prefix = (prefix or "").strip() or None
    rows = repo.search_unassigned(db, query, prefix=prefix, limit=20)
    return JSONResponse({"results": rows})


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
    return_to = _safe_return(form.get("return_to"))

    values = {"mode": mode, "running_no": form.get("running_no") or "", "prefix": prefix or "",
              "suffix": form.get("suffix") or "", "product": product or "",
              "created_by": created_by or "", "comment": comment or "", "return_to": return_to or ""}

    if not prefix:
        return _render_form(request, values=values, error="Pick a prefix (group).", status=400)

    if mode == "existing":
        running_no = _int(form.get("running_no"))
        if not running_no:
            return _render_form(request, values=values,
                                error="Enter the existing running number.", status=400)
    else:
        running_no = repo.next_running_no(db)  # recompute at submit to avoid a stale reservation

    prefix = prefix.zfill(2)
    suffix = _int(form.get("suffix")) or repo.next_suffix(db, prefix, running_no)

    try:
        repo.create_entry(db, prefix=prefix, running_no=running_no, suffix=suffix,
                          product=product, created_by=created_by, comment=comment)
    except DuplicateNumber as exc:
        return _render_form(request, values=values, error=str(exc), status=400)
    if return_to:  # came from a create-part/assembly page — bounce back with the new code prefilled
        # `desc` carries the product name the user just typed, so the create form's name/value field
        # is seeded too (same param the from-template bounce uses).
        return RedirectResponse(
            _return_url(return_to, part_no=article_code(prefix, running_no, suffix), desc=product),
            status_code=303)
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
def new_product_form(request: Request, return_to: str | None = None):
    require_role(request, WRITE_ROLES)
    return _render_product_form(request, values={
        "product": "", "prefixes": [], "created_by": "", "comment": "",
        "return_to": _safe_return(return_to) or ""})


@router.post("/product", response_class=HTMLResponse)
async def create_product(request: Request):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    form = await request.form()
    product = _s(form, "product")
    prefixes = [p for p in form.getlist("prefixes") if p]
    return_to = _safe_return(form.get("return_to"))
    values = {"product": product or "", "prefixes": prefixes,
              "created_by": form.get("created_by") or "", "comment": form.get("comment") or "",
              "return_to": return_to or ""}
    if not prefixes:
        return _render_product_form(request, values=values,
                                    error="Tick at least one group.", status=400)
    running_no = repo.create_product(db, product=product, prefixes=prefixes,
                                     created_by=_s(form, "created_by"), comment=_s(form, "comment"))
    # Document-class lines (drawings, specs, software) become document items, not parts — same as the
    # from-template flow, so identical-looking families behave identically regardless of allocator.
    _create_stub_documents(db, running_no)
    if return_to:  # came from New Assembly — bounce back with the assembly code + product prefilled
        code = _assembly_code_for(db, running_no)
        if code:
            return RedirectResponse(_return_url(return_to, part_no=code, desc=product), status_code=303)
    return RedirectResponse(f"/article-register/{running_no}", status_code=303)


# ---- templates (product-structure blueprints) ----

def _parse_template_lines(form) -> list[dict]:
    """The line editor posts parallel arrays; rebuild ordered line dicts, dropping blank-prefix rows."""
    prefixes = form.getlist("line_prefix")
    suffixes = form.getlist("line_suffix")
    labels = form.getlist("line_label")
    lines = []
    for i, prefix in enumerate(prefixes):
        if not (prefix or "").strip():
            continue
        lines.append({
            "prefix": prefix,
            "suffix": _int(suffixes[i] if i < len(suffixes) else None, 1) or 1,
            "label": (labels[i] if i < len(labels) else "") or "",
        })
    return lines


def _render_template_form(request, *, tmpl, error=None, status=200):
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "article_register_template_form.html",
        {"tmpl": tmpl, "groups": repo.prefixes_grouped(db), "error": error},
        status_code=status,
    )


@router.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request):
    user = require_user(request)
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "article_register_templates.html",
        {"templates": repo.list_templates(db, active_only=False),
         "can_edit": user.role in WRITE_ROLES},
    )


@router.get("/templates/new", response_class=HTMLResponse)
def template_new(request: Request):
    require_role(request, WRITE_ROLES)
    return _render_template_form(request, tmpl={"id": None, "name": "", "notes": "", "lines": []})


@router.post("/templates/new", response_class=HTMLResponse)
async def template_create(request: Request):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    form = await request.form()
    name = _s(form, "name")
    lines = _parse_template_lines(form)
    if not name:
        return _render_template_form(
            request, tmpl={"id": None, "name": "", "notes": form.get("notes") or "", "lines": lines},
            error="Give the template a name.", status=400)
    tid = repo.create_template(db, name=name, notes=_s(form, "notes"))
    repo.save_template(db, tid, name=name, notes=_s(form, "notes"), lines=lines)
    return RedirectResponse("/article-register/templates", status_code=303)


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
def template_edit(request: Request, template_id: int):
    require_role(request, WRITE_ROLES)
    tmpl = repo.get_template(request.app.state.database, template_id)
    if not tmpl:
        return _not_found(request)
    return _render_template_form(request, tmpl=tmpl)


@router.post("/templates/{template_id}/edit", response_class=HTMLResponse)
async def template_save(request: Request, template_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    tmpl = repo.get_template(db, template_id)
    if not tmpl:
        return _not_found(request)
    form = await request.form()
    name = _s(form, "name")
    lines = _parse_template_lines(form)
    if not name:
        return _render_template_form(
            request, tmpl={**tmpl, "name": "", "notes": form.get("notes") or "", "lines": lines},
            error="Give the template a name.", status=400)
    repo.save_template(db, template_id, name=name, notes=_s(form, "notes"), lines=lines)
    return RedirectResponse("/article-register/templates", status_code=303)


@router.post("/templates/{template_id}/delete", response_class=HTMLResponse)
async def template_delete(request: Request, template_id: int):
    require_role(request, WRITE_ROLES)
    repo.delete_template(request.app.state.database, template_id)
    return RedirectResponse("/article-register/templates", status_code=303)


def _render_apply(request, *, values, error=None, status=200):
    db = request.app.state.database
    # When appending to an existing family, the preview tags template lines whose exact code is already
    # present (they'll be skipped, not duplicated). Empty for New Product (fresh number, no lines).
    running_no = _int(values.get("running_no")) if values.get("mode") == "existing" else None
    existing_codes = repo.family_codes(db, running_no) if running_no else []
    return request.app.state.templates.TemplateResponse(
        request, "article_register_from_template.html",
        {"templates": repo.list_templates(db, active_only=True),
         "next_running_no": repo.next_running_no(db), "family_codes": existing_codes,
         "values": values, "error": error},
        status_code=status,
    )


@router.get("/from-template", response_class=HTMLResponse)
def from_template_form(request: Request, running_no: int | None = None,
                       product: str | None = None, return_to: str | None = None):
    require_role(request, WRITE_ROLES)
    return _render_apply(request, values={
        "template_id": "", "product": (product or "").strip(), "created_by": "", "comment": "",
        "running_no": running_no or "", "mode": "existing" if running_no else "new",
        "return_to": _safe_return(return_to) or ""})


@router.post("/from-template", response_class=HTMLResponse)
async def from_template_apply(request: Request):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    form = await request.form()
    template_id = _int(form.get("template_id"))
    product = _s(form, "product")
    mode = (form.get("mode") or "new").strip()
    return_to = _safe_return(form.get("return_to"))
    values = {"template_id": form.get("template_id") or "", "product": product or "",
              "created_by": form.get("created_by") or "", "comment": form.get("comment") or "",
              "running_no": form.get("running_no") or "", "mode": mode, "return_to": return_to or ""}
    if not template_id:
        return _render_apply(request, values=values, error="Pick a template.", status=400)
    if not product:
        return _render_apply(request, values=values, error="Enter the product name.", status=400)
    running_no = None
    if mode == "existing":
        running_no = _int(form.get("running_no"))
        if not running_no:
            return _render_apply(request, values=values,
                                 error="Enter the existing running number.", status=400)
    try:
        running_no = repo.apply_template(db, template_id, product=product,
                                         created_by=_s(form, "created_by"),
                                         comment=_s(form, "comment"), running_no=running_no)
    except (ValueError, DuplicateNumber) as exc:
        return _render_apply(request, values=values, error=str(exc), status=400)
    # Document-class lines (drawings, specs, software) become document items, not parts — do this in
    # both the plain and the New-Assembly flows.
    created_docs = _create_stub_documents(db, running_no)
    if return_to:  # came from New Assembly — materialise the family's parts, then bounce back
        code = _assembly_code_for(db, running_no)
        if code:
            created = _create_stub_parts(db, running_no, skip_code=code)
            # Carry both what became BOM parts (``created``) and what became documents (``docs``) so
            # the new-assembly screen can show the documents too — they're made but not added to the BOM.
            return RedirectResponse(
                _return_url(return_to, part_no=code, desc=product,
                            created=",".join(created), docs=",".join(created_docs)),
                status_code=303)
    return RedirectResponse(f"/article-register/{running_no}", status_code=303)


@router.get("/{running_no}", response_class=HTMLResponse)
def family_detail(request: Request, running_no: int):
    user = require_user(request)
    db = request.app.state.database
    family = repo.get_family(db, running_no)
    if not family:
        return _not_found(request)
    # A family splits into physical/BOM items (assembly + parts) and its deliverables (documents).
    # Document-class lines live only under the Documents section — each carries its materialised
    # document record (id/kind/rev), or None when the number is allocated but no document exists yet.
    docs_by_code = {d["code"]: d for d in repo.list_family_documents(db, running_no)}
    part_lines, doc_lines = [], []
    for e in family:
        if _is_document_line(e):
            row = dict(e)
            row["document"] = docs_by_code.get(e.get("code"))
            doc_lines.append(row)
        else:
            part_lines.append(e)
    return request.app.state.templates.TemplateResponse(
        request, "article_register_detail.html",
        {
            "running_no": running_no,
            "family": family,            # kept for the family-size count in the header
            "part_lines": part_lines,    # assembly + component numbers → the items table
            "doc_lines": doc_lines,      # document-class numbers → the Documents section (only here)
            "product": _family_product(family),
            "groups": repo.prefixes_grouped(db),
            "can_edit": user.role in WRITE_ROLES,
            "can_delete": user.role in DELETE_ROLES,
        },
    )


@router.get("/{entry_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, entry_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    entry = repo.get_entry(db, entry_id)
    if entry is None:
        return _not_found(request)
    return request.app.state.templates.TemplateResponse(
        request, "article_register_edit.html", {"entry": entry, "error": None})


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


@router.post("/{entry_id}/duplicate", response_class=HTMLResponse)
async def duplicate(request: Request, entry_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    entry = repo.get_entry(db, entry_id)
    if entry is None:
        return _not_found(request)
    repo.duplicate_entry(db, entry_id)
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


@router.post("/{entry_id}/delete", response_class=HTMLResponse)
async def delete_entry(request: Request, entry_id: int):
    require_role(request, DELETE_ROLES)
    db = request.app.state.database
    entry = repo.get_entry(db, entry_id)
    if entry is None:
        return _not_found(request)
    running_no = entry["running_no"]
    repo.delete_entry(db, entry_id)
    return RedirectResponse(f"/article-register/{running_no}", status_code=303)
