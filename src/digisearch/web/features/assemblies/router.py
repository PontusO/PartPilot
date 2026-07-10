"""Assemblies routes: list/view assemblies, edit BOM lines, and import a CSV BOM."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from digisearch.devmgmt import DevmgmtConfig

from ...core.deps import require_role, require_user
from ..catalog import devmgmt_outbox, devmgmt_repo
from ..purchasing.service import resolve_bom_file
from . import repo
from .import_bom import apply_import_plan, build_import_plan

router = APIRouter(prefix="/assemblies")

# Roles allowed to edit a BOM (same as catalog writes).
ASSEMBLY_WRITE_ROLES = frozenset({"admin", "purchasing"})

# The three ways a component's factory firmware is written on the board (docs §5.2).
UPDATE_METHODS = ("ota_via_mcu", "local_serial", "local_usb")


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
    # devmgmt publish panel: the product (if any) linked to this assembly + its sync state.
    product = devmgmt_repo.product_for_assembly(db, part_id)
    sync = None
    if product and product.get("variant"):
        sync = devmgmt_outbox.status_for(db, "variant", product["variant"]["ref"])
    return templates.TemplateResponse(
        request,
        "assembly_detail.html",
        {
            "a": assembly,
            "can_edit": can_edit,
            "parts": repo.parts_for_picker(db, part_id) if can_edit else [],
            "error": error,
            "product": product,
            "sync": sync,
            "devmgmt_configured": DevmgmtConfig.from_env() is not None,
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


@router.post("/{part_id}/lines/{line_id}/edit")
async def edit_line(request: Request, part_id: int, line_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    form = await request.form()
    qty_per = _num(form.get("qty_per"))
    refdes = (form.get("refdes") or "").strip() or None
    if qty_per is None or qty_per <= 0:
        return _render_detail(request, part_id, user,
                              error="Quantity must be greater than zero.", status=400)
    repo.update_bom_line(request.app.state.database, part_id, line_id, qty_per, refdes)
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


# ---- publish this assembly to devmgmt (product = a model + a variant/SKU) ----

def _slug(text: str) -> str:
    """Normalize a part number / SKU into a wire-safe ref: collapse runs of non-alphanumerics to a
    single '-'. Case is preserved so the ref reads like the part number it came from."""
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip())).strip("-")


def _csv_list(text: str | None) -> list[str]:
    """Split a comma/newline-separated field into a clean, de-duplicated list (order preserved)."""
    out: list[str] = []
    for token in re.split(r"[,\n]", text or ""):
        token = token.strip()
        if token and token not in out:
            out.append(token)
    return out


def _parse_targets(form) -> list[dict]:
    """Flashable-target rows from the repeated ft_* fields; blank rows dropped."""
    components = form.getlist("ft_component")
    firmwares = form.getlist("ft_firmware")
    methods = form.getlist("ft_method")
    targets = []
    for i, component in enumerate(components):
        component = (component or "").strip()
        firmware = (firmwares[i] if i < len(firmwares) else "").strip()
        method = (methods[i] if i < len(methods) else "").strip()
        if not component and not firmware:      # an untouched / cleared row
            continue
        targets.append({"component": component, "factory_firmware_ref": firmware,
                        "update_method": method})
    return targets


def _devmgmt_defaults(a: dict) -> dict:
    """Sensible starting values when first publishing an assembly (derived from the part). The
    model ref is just the part number; the variant ref isn't asked for — it's the SKU (see POST)."""
    return {
        "editing": False,
        "model_ref": _slug(a["part_no"]), "model_name": a.get("value") or a["part_no"],
        "radio_capabilities": "", "board_revs": (a.get("rev") or "A"),
        "sku": a["part_no"],
        "enabled_radios": "", "radio_config": "",
        "targets": [{"component": "", "factory_firmware_ref": "", "update_method": "ota_via_mcu"}],
    }


def _devmgmt_values_from_product(product: dict) -> dict:
    """Pre-fill the form from an already-published product for editing."""
    model, variant = product["model"], product["variant"]
    return {
        "editing": True,
        "model_ref": model["ref"], "model_name": model["name"],
        "radio_capabilities": ", ".join(model["radio_capabilities"]),
        "board_revs": ", ".join(b["rev"] for b in model["board_revisions"]),
        "sku": variant["sku"],
        "enabled_radios": ", ".join(variant["enabled_radios"]),
        "radio_config": json.dumps(variant["radio_config"], indent=2) if variant["radio_config"] else "",
        "targets": variant["flashable_targets"] or [
            {"component": "", "factory_firmware_ref": "", "update_method": "ota_via_mcu"}],
    }


def _render_devmgmt_form(request, a, values, *, error=None, status=200):
    return request.app.state.templates.TemplateResponse(
        request, "devmgmt_product_form.html",
        {"a": a, "values": values, "methods": UPDATE_METHODS, "error": error},
        status_code=status,
    )


@router.get("/{part_id}/devmgmt", response_class=HTMLResponse)
def devmgmt_form(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    a = repo.get_assembly(db, part_id)
    if a is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404)
    product = devmgmt_repo.product_for_assembly(db, part_id)
    values = _devmgmt_values_from_product(product) if product else _devmgmt_defaults(a)
    return _render_devmgmt_form(request, a, values)


@router.post("/{part_id}/devmgmt", response_class=HTMLResponse)
async def devmgmt_publish(request: Request, part_id: int):
    require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    a = repo.get_assembly(db, part_id)
    if a is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Assembly not found."}, status_code=404)

    form = await request.form()
    model_ref = _slug(form.get("model_ref"))
    model_name = (form.get("model_name") or "").strip()
    sku = (form.get("sku") or "").strip()
    capabilities = _csv_list(form.get("radio_capabilities"))
    board_revs = _csv_list(form.get("board_revs"))
    enabled = _csv_list(form.get("enabled_radios"))
    targets = _parse_targets(form)
    raw_config = (form.get("radio_config") or "").strip()

    # A variant IS its SKU, so we don't ask for a separate ref: a new variant's ref is derived from
    # its SKU. On edit we keep the ORIGINAL ref (snapshot from the first SKU) so renaming the SKU
    # updates the same devmgmt variant instead of creating a new one.
    existing = devmgmt_repo.product_for_assembly(db, part_id)
    if existing and existing.get("variant"):
        variant_ref = existing["variant"]["ref"]
    else:
        variant_ref = _slug(sku)

    # Echo back exactly what was submitted (post-normalization) so an error re-render keeps it.
    values = {
        "editing": bool(existing and existing.get("variant")),
        "model_ref": model_ref, "model_name": model_name,
        "radio_capabilities": ", ".join(capabilities), "board_revs": ", ".join(board_revs),
        "sku": sku, "enabled_radios": ", ".join(enabled),
        "radio_config": raw_config,
        "targets": targets or [{"component": "", "factory_firmware_ref": "", "update_method": "ota_via_mcu"}],
    }

    def fail(msg, status=400):
        return _render_devmgmt_form(request, a, values, error=msg, status=status)

    if not (model_ref and model_name and sku):
        return fail("Model ref, model name and SKU are all required.")
    if not board_revs:
        return fail("Add at least one board revision (e.g. “C”) — devices reference it.")
    unknown = [r for r in enabled if r not in capabilities]
    if unknown:
        return fail(f"Enabled radio(s) {', '.join(unknown)} aren’t in the model’s capabilities.")
    for t in targets:
        if not t["component"] or not t["factory_firmware_ref"]:
            return fail("Each firmware target needs both a component and a firmware ref.")
        if t["update_method"] not in UPDATE_METHODS:
            return fail(f"Firmware target “{t['component']}” has an invalid update method.")
    radio_config = None
    if raw_config:
        try:
            radio_config = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            return fail(f"Radio config isn’t valid JSON: {exc}")
        if not isinstance(radio_config, dict):
            return fail("Radio config must be a JSON object (e.g. {\"lorawan\": {…}}).")

    board_revisions = [{"ref": f"{model_ref}-{rev}", "rev": rev} for rev in board_revs]
    try:
        devmgmt_repo.upsert_model(db, ref=model_ref, name=model_name,
                                  radio_capabilities=capabilities, board_revisions=board_revisions)
        devmgmt_repo.upsert_variant(db, ref=variant_ref, model_ref=model_ref, sku=sku,
                                    enabled_radios=enabled, radio_config=radio_config,
                                    assembly_id=part_id, flashable_targets=targets)
    except (ValueError, sqlite3.IntegrityError) as exc:
        # e.g. a ref/SKU already used by a different assembly's product.
        return fail(f"Couldn’t save: {exc}")
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/devmgmt/push")
def devmgmt_push_now(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    product = devmgmt_repo.product_for_assembly(db, part_id)
    if product is None or not product.get("variant"):
        return _render_detail(request, part_id, user,
                              error="Publish this assembly to devmgmt before pushing.", status=400)
    devmgmt_outbox.enqueue_product(db, product["model"]["ref"], product["variant"]["ref"])
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


def _variant_ref_or_error(request, part_id, user):
    """The published variant ref for this assembly, or a rendered 400 if there's no product."""
    product = devmgmt_repo.product_for_assembly(request.app.state.database, part_id)
    if product is None or not product.get("variant"):
        return None, _render_detail(request, part_id, user,
                                    error="This assembly isn’t published to devmgmt.", status=400)
    return product["variant"], None


@router.post("/{part_id}/devmgmt/retire")
def devmgmt_retire(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    variant, err = _variant_ref_or_error(request, part_id, user)
    if err:
        return err
    devmgmt_repo.set_variant_retired(request.app.state.database, variant["ref"], True)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/devmgmt/unretire")
def devmgmt_unretire(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    variant, err = _variant_ref_or_error(request, part_id, user)
    if err:
        return err
    devmgmt_repo.set_variant_retired(request.app.state.database, variant["ref"], False)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/devmgmt/delete")
def devmgmt_delete(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    db = request.app.state.database
    variant, err = _variant_ref_or_error(request, part_id, user)
    if err:
        return err
    # Guards enforce the retire→delete discipline (docs §7); PartPilot is authoritative for both.
    if not variant.get("retired_at"):
        return _render_detail(request, part_id, user,
                              error="Retire this product before deleting it.", status=400)
    # The retire flag reaches devmgmt via a queued push; deleting before that push has flushed
    # would drop it (delete supersedes the queued upsert) and devmgmt would then refuse the DELETE
    # forever (its own retire-before-delete guard). Only relevant when devmgmt is actually
    # configured — otherwise there is no remote state to converge with.
    sync = devmgmt_outbox.status_for(db, "variant", variant["ref"])
    if sync and sync["status"] == "pending" and DevmgmtConfig.from_env() is not None:
        return _render_detail(
            request, part_id, user,
            error="This product still has a push queued to devmgmt (the retire flag). "
                  "Wait for the sync to run (about 20 s) and try again.",
            status=409)
    refs = devmgmt_repo.variant_device_count(db, variant["ref"])
    if refs:
        return _render_detail(
            request, part_id, user,
            error=f"{refs} device(s) still reference this SKU — remove them before deleting.",
            status=400)
    # Local hard-delete + queued devmgmt DELETE (no network in the request path).
    devmgmt_repo.delete_variant(db, variant["ref"])
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)
