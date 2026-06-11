"""Goods Receipts routes — a read-only register of received deliveries (GRNs)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core.deps import require_user
from . import repo

router = APIRouter(prefix="/goods-receipts")


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
