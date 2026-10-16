"""Reports routes — read-only views over data other features write.

First report: the stock-movement ledger, browsable by date range and movement type.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...core.deps import require_user
from ..catalog import repo as catalog_repo
from . import repo

router = APIRouter(prefix="/reports")

# Cap a single view so a wide date range can't try to render the whole history at once.
LIMIT = 2000


@router.get("", response_class=HTMLResponse)
def reports_index(request: Request):
    require_user(request)
    return request.app.state.templates.TemplateResponse(request, "reports_index.html", {})


@router.get("/stock-movements", response_class=HTMLResponse)
def stock_movements(request: Request, start: str | None = None, end: str | None = None,
                    mtype: str | None = None, location_id: str | None = None):
    require_user(request)
    db = request.app.state.database
    # Default to the last 30 days so the page opens on something useful, not the whole ledger.
    today = date.today()
    start = (start or "").strip() or (today - timedelta(days=30)).isoformat()
    end = (end or "").strip() or today.isoformat()
    mtype = (mtype or "").strip() or None
    location_id = int(location_id) if (location_id or "").strip().isdigit() else None   # "" = all shelves

    rows = repo.stock_movements(db, start=start, end=end, mtype=mtype, location_id=location_id, limit=LIMIT)
    summary = repo.stock_movement_summary(db, start=start, end=end, mtype=mtype, location_id=location_id)
    return request.app.state.templates.TemplateResponse(
        request, "stock_movements.html",
        {"rows": rows, "summary": summary, "start": start, "end": end, "mtype": mtype or "",
         "location_id": location_id, "shelves": catalog_repo.locations(db, include_retired=True),
         "types": repo.MOVEMENT_TYPES, "limit": LIMIT, "truncated": len(rows) >= LIMIT},
    )
