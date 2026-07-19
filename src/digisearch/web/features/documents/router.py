"""Documents feature routes.

Reads are open to any signed-in user; writes are gated to Admin + Purchasing (``WRITE_ROLES``), and
hard-delete to Admin only — matching the Article Register conventions. A document's number is
allocated through the Article Register allocator (``article_register.repo``); files are stored on
disk under ``app.state.documents_dir`` with the traversal-guarded download pattern from Purchasing.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...auth import PURCHASE_ROLES
from ...core.deps import require_role, require_user
from ..article_register import repo as ar_repo
from ..article_register.codes import article_code
from . import repo
from .repo import SOFTWARE_PREFIX

router = APIRouter(prefix="/documents")

WRITE_ROLES = PURCHASE_ROLES
DELETE_ROLES = frozenset({"admin"})

# Uploaded documents are arbitrary content (CAD, gerbers, binaries, zips), so there is no extension
# allow-list; safety comes from never executing the bytes, forcing an attachment download, sanitizing
# the stored name, and this hard size cap.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_INCOMING = "_incoming"  # staging dir for create uploads (before a document id exists)


def _s(form, name: str) -> str | None:
    return (form.get(name) or "").strip() or None


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _not_found(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "error.html", {"message": "Document not found."}, status_code=404)


def _error(request: Request, message: str, status: int = 400):
    return request.app.state.templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=status)


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").name) or "upload.bin"


def _suggest_rev(doc: dict | None) -> str:
    """Suggest the next revision label: A, B, C … then numeric once past Z."""
    n = len(doc["revisions"]) if doc else 0
    return chr(ord("A") + n) if n < 26 else str(n + 1)


def _stream_to_file(src, dst: Path, max_bytes: int) -> int:
    """Copy a file-like to ``dst`` in chunks, enforcing ``max_bytes``. Raises ValueError (and removes
    the partial file) if exceeded. Returns the byte size."""
    size = 0
    try:
        with open(dst, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("upload exceeds size limit")
                out.write(chunk)
    except ValueError:
        dst.unlink(missing_ok=True)
        raise
    return size


def _save_upload(upload, target_dir: Path, documents_dir: Path) -> tuple[str, str, int, str | None]:
    """Stream an ``UploadFile`` into ``target_dir`` under a collision-proof name. Returns
    (rel_path, original_filename, byte_size, content_type). Runs in a threadpool (blocking IO)."""
    safe = _sanitize(upload.filename)
    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / f"{uuid4().hex}_{safe}"
    size = _stream_to_file(upload.file, dst, MAX_UPLOAD_BYTES)
    return str(dst.relative_to(documents_dir)), safe, size, getattr(upload, "content_type", None)


def _move_into_doc(documents_dir: Path, tmp_rel: str, document_id: int) -> str:
    """Move a staged (_incoming) upload into the document's own folder; return the new rel_path."""
    src = documents_dir / tmp_rel
    doc_dir = documents_dir / str(document_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    dst = doc_dir / src.name
    src.replace(dst)
    return str(dst.relative_to(documents_dir))


# ---- list ----

@router.get("", response_class=HTMLResponse)
def list_page(request: Request, q: str | None = None, prefix: str | None = None,
              kind: str | None = None, retired: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    include_retired = bool(retired)
    docs = repo.list_documents(db, search=(q or "").strip() or None,
                               prefix=(prefix or "").strip() or None,
                               kind=(kind or "").strip() or None, include_retired=include_retired)
    return request.app.state.templates.TemplateResponse(
        request, "documents_list.html",
        {"documents": docs, "prefixes": repo.document_prefixes(db),
         "q": q or "", "prefix": prefix or "", "kind": kind or "",
         "include_retired": include_retired, "can_edit": user.role in WRITE_ROLES})


# ---- create ----

def _render_form(request: Request, *, values: dict, error=None, status=200):
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "document_form.html",
        {"prefixes": repo.document_prefixes(db), "next_running_no": ar_repo.next_running_no(db),
         "software_prefix": SOFTWARE_PREFIX, "values": values, "error": error},
        status_code=status)


_CODE_RE = re.compile(r"(\d{2})-(\d{5})-(\d+)")


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request, running_no: int | None = None, prefix: str | None = None,
             code: str | None = None, title: str | None = None):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    code = (code or "").strip()
    values = {
        "mode": "existing" if running_no else "new", "running_no": running_no or "",
        "prefix": (prefix or "").strip(), "code": code, "title": (title or "").strip(), "doc_type": "",
        "storage_kind": "file", "external_url": "", "ext_ref": "", "ext_path": "", "rev": "A",
        "created_by": "", "notes": ""}
    if code:
        # Bound to an already-allocated article number (from the running-number page): a document
        # already there → jump straight to editing it; otherwise fix the class/family from the code.
        existing = repo.document_for_code(db, code)
        if existing:
            return RedirectResponse(f"/documents/{existing['id']}/edit", status_code=303)
        m = _CODE_RE.fullmatch(code)
        if m:
            values["prefix"] = m.group(1)
            values["running_no"] = int(m.group(2))
        if values["prefix"] == SOFTWARE_PREFIX:
            values["storage_kind"] = "link"
    return _render_form(request, values=values)


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request):
    user = require_role(request, WRITE_ROLES)
    db = request.app.state.database
    documents_dir: Path = request.app.state.documents_dir
    form = await request.form()

    bound_code = _s(form, "code")
    prefix = _s(form, "prefix")
    prefix = prefix.zfill(2) if prefix else None
    title = _s(form, "title")
    doc_type = _s(form, "doc_type")
    mode = (form.get("mode") or "new").strip()
    kind = (form.get("storage_kind") or "file").strip()
    created_by = _s(form, "created_by") or user.username
    notes = _s(form, "notes")
    external_url = _s(form, "external_url")
    ext_ref = _s(form, "ext_ref")
    ext_path = _s(form, "ext_path")
    rev = _s(form, "rev") or "A"
    upload = form.get("file")

    values = {"mode": mode, "running_no": form.get("running_no") or "", "prefix": prefix or "",
              "code": bound_code or "", "title": title or "", "doc_type": doc_type or "",
              "storage_kind": kind, "external_url": external_url or "", "ext_ref": ext_ref or "",
              "ext_path": ext_path or "", "rev": rev, "created_by": created_by or "",
              "notes": notes or ""}

    # Identity: either bound to an existing article number (from the running-number page — no new
    # allocation), or freshly allocated from the class + family pickers.
    if bound_code:
        m = _CODE_RE.fullmatch(bound_code)
        if not m:
            return _render_form(request, values=values, error="That article number looks malformed.")
        prefix, running_no = m.group(1), int(m.group(2))
        values["prefix"] = prefix
        if not repo.article_code_exists(db, bound_code):
            return _render_form(request, values=values,
                                error=f"{bound_code} is not an allocated article number.")
        if repo.document_for_code(db, bound_code):
            return _render_form(request, values=values,
                                error=f"A document already exists for {bound_code}.")
        code = bound_code
        allocate = False
    else:
        allowed = {p["code"] for p in repo.document_prefixes(db)}
        if not prefix or prefix not in allowed:
            return _render_form(request, values=values, error="Pick a document class (prefix).")
        if mode == "existing":
            running_no = _int(form.get("running_no"))
            if not running_no:
                return _render_form(request, values=values, error="Enter the existing running number.")
            suffix = ar_repo.next_suffix(db, prefix, running_no)
        else:
            running_no = ar_repo.next_running_no(db)  # recompute at submit; avoid a stale reservation
            suffix = 1
        code = article_code(prefix, running_no, suffix)
        allocate = True

    if prefix == SOFTWARE_PREFIX:  # source code is always a link, never a stored file
        kind = values["storage_kind"] = "link"
    if not title:
        return _render_form(request, values=values, error="Give the document a title.")

    has_file = bool(upload and getattr(upload, "filename", ""))
    if kind == "file" and not has_file:
        return _render_form(request, values=values, error="Choose a file to upload.")
    if kind == "link" and not external_url:
        return _render_form(request, values=values, error="Enter the document's URL.")

    # Stage a file upload BEFORE allocating, so an oversize file doesn't leave an orphan number/row.
    staged = None
    if kind == "file":
        try:
            staged = await run_in_threadpool(_save_upload, upload, documents_dir / _INCOMING, documents_dir)
        except ValueError:
            return _render_form(request, values=values, error="File exceeds the 100 MB limit.")

    if allocate:
        try:
            ar_repo.create_entry(db, prefix=prefix, running_no=running_no, suffix=suffix,
                                 product=title, created_by=created_by, source="manual")
        except ar_repo.DuplicateNumber as exc:
            if staged:
                (documents_dir / staged[0]).unlink(missing_ok=True)
            return _render_form(request, values=values, error=str(exc))

    doc_id = repo.create_document(db, code=code, running_no=running_no, prefix=prefix, title=title,
                                  storage_kind=kind, doc_type=doc_type, created_by=created_by, notes=notes)

    if kind == "file":
        rel_path = await run_in_threadpool(_move_into_doc, documents_dir, staged[0], doc_id)
        repo.add_file_revision(db, doc_id, rev=rev, filename=staged[1], rel_path=rel_path,
                               byte_size=staged[2], content_type=staged[3], uploaded_by=created_by,
                               notes=notes)
    else:
        repo.add_link_revision(db, doc_id, rev=rev, external_url=external_url, ext_ref=ext_ref,
                               ext_path=ext_path, uploaded_by=created_by, notes=notes)
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


# ---- detail / edit ----

@router.get("/{document_id}", response_class=HTMLResponse)
def detail(request: Request, document_id: int):
    user = require_user(request)
    doc = repo.get_document(request.app.state.database, document_id)
    if doc is None:
        return _not_found(request)
    return request.app.state.templates.TemplateResponse(
        request, "document_detail.html",
        {"doc": doc, "suggest_rev": _suggest_rev(doc),
         "can_edit": user.role in WRITE_ROLES, "can_delete": user.role in DELETE_ROLES})


@router.get("/{document_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, document_id: int):
    require_role(request, WRITE_ROLES)
    doc = repo.get_document(request.app.state.database, document_id)
    if doc is None:
        return _not_found(request)
    return request.app.state.templates.TemplateResponse(
        request, "document_edit.html", {"doc": doc})


@router.post("/{document_id}/edit", response_class=HTMLResponse)
async def save_edit(request: Request, document_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    doc = repo.get_document(db, document_id)
    if doc is None:
        return _not_found(request)
    form = await request.form()
    title = _s(form, "title")
    if not title:
        return request.app.state.templates.TemplateResponse(
            request, "document_edit.html", {"doc": doc, "error": "Title can't be empty."},
            status_code=400)
    repo.update_document(db, document_id, title=title, doc_type=_s(form, "doc_type"),
                         notes=_s(form, "notes"))
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


# ---- revisions ----

@router.post("/{document_id}/revisions", response_class=HTMLResponse)
async def upload_revision(request: Request, document_id: int):
    user = require_role(request, WRITE_ROLES)
    db = request.app.state.database
    documents_dir: Path = request.app.state.documents_dir
    doc = repo.get_document(db, document_id)
    if doc is None:
        return _not_found(request)
    if doc["storage_kind"] != "file":
        return _error(request, "This is a link document — update its link instead of uploading a file.")
    form = await request.form()
    upload = form.get("file")
    if not (upload and getattr(upload, "filename", "")):
        return _error(request, "Choose a file to upload.")
    rev = _s(form, "rev") or _suggest_rev(doc)
    notes = _s(form, "notes")
    by = _s(form, "uploaded_by") or user.username
    try:
        rel_path, filename, size, ctype = await run_in_threadpool(
            _save_upload, upload, documents_dir / str(document_id), documents_dir)
    except ValueError:
        return _error(request, "File exceeds the 100 MB limit.")
    repo.add_file_revision(db, document_id, rev=rev, filename=filename, rel_path=rel_path,
                           byte_size=size, content_type=ctype, uploaded_by=by, notes=notes)
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/{document_id}/link", response_class=HTMLResponse)
async def update_link(request: Request, document_id: int):
    user = require_role(request, WRITE_ROLES)
    db = request.app.state.database
    doc = repo.get_document(db, document_id)
    if doc is None:
        return _not_found(request)
    if doc["storage_kind"] != "link":
        return _error(request, "This is a file document — upload a new revision instead.")
    form = await request.form()
    external_url = _s(form, "external_url")
    if not external_url:
        return _error(request, "Enter the document's URL.")
    rev = _s(form, "rev") or _suggest_rev(doc)
    repo.add_link_revision(db, document_id, rev=rev, external_url=external_url,
                           ext_ref=_s(form, "ext_ref"), ext_path=_s(form, "ext_path"),
                           uploaded_by=_s(form, "uploaded_by") or user.username, notes=_s(form, "notes"))
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/{document_id}/revisions/{revision_id}/set-current", response_class=HTMLResponse)
async def set_current(request: Request, document_id: int, revision_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    if not repo.set_current_revision(db, document_id, revision_id):
        return _not_found(request)
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.get("/{document_id}/revisions/{revision_id}/download")
def download(request: Request, document_id: int, revision_id: int):
    require_user(request)
    documents_dir: Path = request.app.state.documents_dir
    rev = repo.get_revision(request.app.state.database, document_id, revision_id)
    if rev is None or not rev["rel_path"]:  # missing, or a link revision (no file)
        return HTMLResponse("Not found", status_code=404)
    target = (documents_dir / rev["rel_path"]).resolve()
    if not target.is_relative_to(documents_dir.resolve()) or not target.is_file():
        return HTMLResponse("Not found", status_code=404)
    # Force a download (never inline) with a neutral type, so an uploaded HTML/SVG can't render.
    return FileResponse(
        target, filename=rev["filename"] or target.name, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{rev["filename"] or target.name}"'})


# ---- lifecycle ----

@router.post("/{document_id}/retire", response_class=HTMLResponse)
async def retire(request: Request, document_id: int):
    require_role(request, WRITE_ROLES)
    db = request.app.state.database
    doc = repo.get_document(db, document_id)
    if doc is None:
        return _not_found(request)
    repo.set_retired(db, document_id, not doc["retired"])
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/{document_id}/delete", response_class=HTMLResponse)
async def delete(request: Request, document_id: int):
    require_role(request, DELETE_ROLES)
    db = request.app.state.database
    documents_dir: Path = request.app.state.documents_dir
    doc = repo.get_document(db, document_id)
    if doc is None:
        return _not_found(request)
    repo.delete_document(db, document_id)  # revision rows cascade
    # Remove the on-disk folder after the DB delete; a failure here is non-fatal (orphan bytes only).
    await run_in_threadpool(shutil.rmtree, documents_dir / str(document_id), True)
    return RedirectResponse("/documents", status_code=303)
