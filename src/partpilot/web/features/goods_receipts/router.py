"""Goods Receipts routes — a read-only register of received deliveries (GRNs)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from ...core.deps import require_user
from . import export, repo

router = APIRouter(prefix="/goods-receipts")


def _filename(g: dict, ext: str) -> str:
    return f"{(g.get('grn_no') or 'GRN-' + str(g['id']))}.{ext}"


@router.get("", response_class=HTMLResponse)
def receipts_list(request: Request, q: str | None = None):
    require_user(request)
    db = request.app.state.database
    search = (q or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "goods_receipts_list.html",
        {"receipts": repo.list_receipts(db, search), "summary": repo.summary(db), "q": search or ""},
    )


@router.get("/{grn_id}", response_class=HTMLResponse)
def receipt_detail(request: Request, grn_id: int):
    require_user(request)
    g = repo.get_receipt(request.app.state.database, grn_id)
    if g is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Goods receipt not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(request, "goods_receipt_detail.html", {"g": g})


@router.get("/{grn_id}/export.pdf")
def export_pdf(request: Request, grn_id: int):
    require_user(request)
    db = request.app.state.database
    g = repo.get_receipt(db, grn_id)
    if g is None:
        return Response("not found", status_code=404)
    return Response(
        content=export.grn_pdf(g, export.company_profile(db)), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_filename(g, "pdf")}"'},
    )


@router.get("/{grn_id}/export.csv")
def export_csv(request: Request, grn_id: int):
    require_user(request)
    db = request.app.state.database
    g = repo.get_receipt(db, grn_id)
    if g is None:
        return Response("not found", status_code=404)
    return Response(
        content=export.grn_csv(g).encode("utf-8"), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_filename(g, "csv")}"'},
    )
