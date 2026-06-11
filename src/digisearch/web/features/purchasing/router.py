"""Purchasing feature routes: upload a BOM, resolve it, download the outputs."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from digisearch.config import Settings
from digisearch.models import Status

from ...auth import PURCHASE_ROLES
from ...core.deps import require_role, require_user
from .service import run_purchase

router = APIRouter(prefix="/purchasing")

_ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}

# Status -> CSS badge class, for colouring the results table.
STATUS_CLASS = {
    Status.RESOLVED: "ok",
    Status.IN_STOCK: "stock",
    Status.REVIEW: "warn",
    Status.NOT_FOUND: "bad",
    Status.MANUAL: "bad",
    Status.ERROR: "bad",
    Status.DNP: "muted",
    Status.NON_ORDERABLE: "muted",
}


@router.get("", response_class=HTMLResponse)
def upload_page(request: Request):
    user = require_user(request)
    settings = Settings.load(None)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "can_purchase": user.role in PURCHASE_ROLES,
            "default_build_qty": settings.build_qty,
            "default_currency": settings.currency or "",
            "stock_available": bool(settings.minimrp_path),
        },
    )


@router.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    file: UploadFile = File(...),
    build_qty: int = Form(...),
    check_stock: bool = Form(False),
):
    user = require_role(request, PURCHASE_ROLES)
    templates = request.app.state.templates
    jobs_dir: Path = request.app.state.jobs_dir

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"Unsupported file type '{suffix}'. Upload a CSV or Excel BOM."},
            status_code=400,
        )

    job_id = uuid.uuid4().hex
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    bom_path = job_dir / Path(file.filename).name
    bom_path.write_bytes(await file.read())

    try:
        result = await run_in_threadpool(
            run_purchase, bom_path, job_dir, build_qty=build_qty, check_stock=check_stock
        )
    except Exception as exc:  # surface engine/credential errors to the user
        return templates.TemplateResponse(
            request, "error.html", {"message": f"Run failed: {exc}"}, status_code=500
        )

    downloads = [("Report (Excel)", result.report_path.name)]
    downloads += [(f"{label} cart", path.name) for label, path in result.cart_paths.items()]

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "job_id": job_id,
            "result": result,
            "downloads": downloads,
            "status_class": STATUS_CLASS,
            "source_name": Path(file.filename).name,
        },
    )


@router.get("/download/{job_id}/{name}")
def download(request: Request, job_id: str, name: str):
    require_user(request)
    jobs_dir: Path = request.app.state.jobs_dir
    target = (jobs_dir / job_id / name).resolve()
    if not target.is_relative_to(jobs_dir.resolve()) or not target.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(target, filename=name)
