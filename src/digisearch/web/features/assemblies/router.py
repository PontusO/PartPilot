"""Assemblies routes: list/view assemblies, edit BOM lines, and import a CSV BOM."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...core.deps import require_role, require_user
from ..purchasing.service import resolve_bom_file
from . import repo
from .import_bom import apply_import_plan, build_import_plan

router = APIRouter(prefix="/assemblies")

# Roles allowed to edit a BOM (same as catalog writes).
ASSEMBLY_WRITE_ROLES = frozenset({"admin", "purchasing"})


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
def assemblies_list(request: Request, q: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    templates = request.app.state.templates
    search = (q or "").strip() or None
    return templates.TemplateResponse(
        request,
        "assemblies_list.html",
        {
            "assemblies": repo.list_assemblies(db, search),
            "summary": repo.summary(db),
            "q": search or "",
            "can_edit": user.role in ASSEMBLY_WRITE_ROLES,
        },
    )


def _parse_assembly(form) -> dict:
    return {
        "part_no": (form.get("part_no") or "").strip(),
        "value": (form.get("value") or "").strip() or None,
        "rev": (form.get("rev") or "").strip() or None,
        "category": (form.get("category") or "").strip().upper() or None,
        "description": (form.get("description") or "").strip() or None,
        "default_build_days": _int(form.get("default_build_days")),
    }


def _render_assembly_form(request, *, action, heading, submit_label, back_url, values,
                          hint=None, error=None, status=200):
    return request.app.state.templates.TemplateResponse(
        request, "assembly_form.html",
        {"action": action, "heading": heading, "submit_label": submit_label,
         "back_url": back_url, "values": values or {}, "hint": hint, "error": error},
        status_code=status,
    )


@router.get("/new", response_class=HTMLResponse)
def new_assembly_form(request: Request):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    return _render_assembly_form(
        request, action="/assemblies/new", heading="New assembly",
        submit_label="Create assembly", back_url="/assemblies", values={},
        hint="After creating, add components to its BOM from the assembly page.",
    )


@router.post("/new", response_class=HTMLResponse)
async def create_assembly(request: Request):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    form = await request.form()
    part = _parse_assembly(form)
    if not part["part_no"]:
        return _render_assembly_form(
            request, action="/assemblies/new", heading="New assembly",
            submit_label="Create assembly", back_url="/assemblies", values=dict(form),
            error="Part number is required.", status=400,
        )
    new_id = repo.create_assembly(request.app.state.database, part)
    return RedirectResponse(f"/assemblies/{new_id}", status_code=303)


@router.get("/{part_id}/edit", response_class=HTMLResponse)
def edit_assembly_form(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    a = repo.get_assembly(request.app.state.database, part_id)
    if a is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404)
    return _render_assembly_form(
        request, action=f"/assemblies/{part_id}/edit", heading=f"Edit — {a['part_no']}",
        submit_label="Save changes", back_url=f"/assemblies/{part_id}", values=a,
    )


@router.post("/{part_id}/edit", response_class=HTMLResponse)
async def update_assembly_route(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    if repo.get_assembly(db, part_id) is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404)
    form = await request.form()
    part = _parse_assembly(form)
    if not part["part_no"]:
        return _render_assembly_form(
            request, action=f"/assemblies/{part_id}/edit", heading="Edit assembly",
            submit_label="Save changes", back_url=f"/assemblies/{part_id}", values=dict(form),
            error="Part number is required.", status=400,
        )
    repo.update_assembly(db, part_id, part)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


def _render_detail(request: Request, part_id: int, user, error: str | None = None, status: int = 200):
    db = request.app.state.database
    templates = request.app.state.templates
    assembly = repo.get_assembly(db, part_id)
    if assembly is None:
        return templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404
        )
    can_edit = user.role in ASSEMBLY_WRITE_ROLES
    return templates.TemplateResponse(
        request,
        "assembly_detail.html",
        {
            "a": assembly,
            "can_edit": can_edit,
            "parts": repo.parts_for_picker(db, part_id) if can_edit else [],
            "error": error,
        },
        status_code=status,
    )


@router.get("/{part_id}", response_class=HTMLResponse)
def assembly_detail(request: Request, part_id: int):
    user = require_user(request)
    return _render_detail(request, part_id, user)


@router.post("/{part_id}/lines/add", response_class=HTMLResponse)
async def add_line(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    form = await request.form()
    child_id = _int(form.get("child_id"))
    qty_per = _num(form.get("qty_per")) or 1
    refdes = (form.get("refdes") or "").strip() or None
    if not child_id:
        return _render_detail(request, part_id, user, error="Choose a component to add.", status=400)
    try:
        repo.add_bom_line(request.app.state.database, part_id, child_id, qty_per, refdes)
    except ValueError as exc:
        return _render_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/lines/{line_id}/delete")
def delete_line(request: Request, part_id: int, line_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    repo.delete_bom_line(request.app.state.database, part_id, line_id)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/convert-to-component")
def convert_to_component(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    try:
        repo.convert_to_component(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_detail(request, part_id, user, error=str(exc), status=400)
    # It's now a component — its home is the catalog.
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


# ---- import a CSV BOM (reuses the purchasing tool's resolution) ----

@router.get("/{part_id}/import", response_class=HTMLResponse)
def import_form(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    templates = request.app.state.templates
    a = repo.get_assembly(db, part_id)
    if a is None:
        return templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404)
    return templates.TemplateResponse(request, "assembly_import.html", {"a": a, "error": None})


@router.post("/{part_id}/import", response_class=HTMLResponse)
async def import_resolve(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    templates = request.app.state.templates
    jobs_dir: Path = request.app.state.jobs_dir
    a = repo.get_assembly(db, part_id)
    if a is None:
        return templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404)

    form = await request.form()
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", ""):
        return templates.TemplateResponse(
            request, "assembly_import.html", {"a": a, "error": "Choose a CSV BOM file."},
            status_code=400)

    job_id = uuid.uuid4().hex
    bom_path = jobs_dir / f"asmimp-{job_id}-{Path(upload.filename).name}"
    bom_path.write_bytes(await upload.read())
    try:
        run = await run_in_threadpool(resolve_bom_file, bom_path, build_qty=1)
    except Exception as exc:  # missing creds, bad file, etc.
        return templates.TemplateResponse(
            request, "assembly_import.html", {"a": a, "error": f"Resolve failed: {exc}"},
            status_code=500)
    finally:
        bom_path.unlink(missing_ok=True)

    plan = build_import_plan(db, run)
    (jobs_dir / f"asmplan-{job_id}.json").write_text(json.dumps(plan))
    return templates.TemplateResponse(
        request, "assembly_import_review.html",
        {"a": a, "plan": plan, "job_id": job_id, "warnings": run.warnings,
         "source": Path(upload.filename).name},
    )


@router.post("/{part_id}/import/apply")
async def import_apply(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    job_id = (form.get("job_id") or "").strip()
    accepted = {int(v) for v in form.getlist("accept") if str(v).isdigit()}

    result = None
    if re.fullmatch(r"[0-9a-f]+", job_id):  # guard against path traversal
        plan_path = jobs_dir / f"asmplan-{job_id}.json"
        if plan_path.exists():
            result = apply_import_plan(db, part_id, json.loads(plan_path.read_text()), accepted)
            plan_path.unlink(missing_ok=True)

    # Nothing imported, or nothing needs a closer look -> straight back to the assembly.
    if result is None or not result.get("review"):
        return RedirectResponse(f"/assemblies/{part_id}", status_code=303)
    # Some created parts have an incomplete value notation: show them with edit links to finish.
    a = repo.get_assembly(db, part_id)
    return request.app.state.templates.TemplateResponse(
        request, "assembly_import_result.html", {"a": a, "result": result})
