"""Setup & Tools — an admin hub of maintenance tools (first one: the miniMRP import).

The landing page renders ``TOOLS`` as a list, so adding a tool later is: append an entry
here and add its route. All routes are admin-only.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from digisearch.config import Settings

from ...core.deps import require_role
from ..assemblies.importer import import_boms
from ..catalog.importer import import_from_minimrp
from ..contacts.importer import import_contacts
from . import repo

SETUP_ROLES = frozenset({"admin"})

router = APIRouter(prefix="/setup")

# Tools shown on the Setup & Tools page — append here to add more later.
TOOLS = [
    {"label": "Import from miniMRP", "url": "/setup/import", "icon": "📥",
     "description": "Import or re-sync parts, suppliers, stock and the assembly BOM structure "
                    "from the miniMRP database."},
    {"label": "Company details", "url": "/setup/company", "icon": "🏢",
     "description": "Your company name and address used on purchase-order PDFs and ISO records."},
    {"label": "Production settings", "url": "/setup/production", "icon": "🏭",
     "description": "Production parameters such as the spillage/scrap margin added to each "
                    "work-order batch's component requirements."},
]


def _source_path() -> str | None:
    return Settings.load(None).minimrp_path


def _do_import(db, path) -> dict:
    stats = dict(import_from_minimrp(db, path))
    stats.update(import_boms(db, path))
    stats.update(import_contacts(db, path))
    return stats


@router.get("", response_class=HTMLResponse)
def index(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "setup_index.html", {"tools": TOOLS}
    )


@router.get("/company", response_class=HTMLResponse)
def company_page(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "company.html",
        {"company": repo.get_company(request.app.state.database), "saved": False},
    )


@router.post("/company", response_class=HTMLResponse)
async def save_company(request: Request):
    require_role(request, SETUP_ROLES)
    form = await request.form()
    data = {f: (form.get(f) or "").strip() for f in repo.COMPANY_FIELDS}
    repo.save_company(request.app.state.database, data)
    return request.app.state.templates.TemplateResponse(
        request, "company.html",
        {"company": repo.get_company(request.app.state.database), "saved": True},
    )


@router.get("/production", response_class=HTMLResponse)
def production_page(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "production.html",
        {"production": repo.get_production(request.app.state.database), "saved": False},
    )


@router.post("/production", response_class=HTMLResponse)
async def save_production(request: Request):
    require_role(request, SETUP_ROLES)
    form = await request.form()
    repo.save_production(request.app.state.database,
                         {"spillage_percent": form.get("spillage_percent"),
                          "min_margin_qty": form.get("min_margin_qty")})
    return request.app.state.templates.TemplateResponse(
        request, "production.html",
        {"production": repo.get_production(request.app.state.database), "saved": True},
    )


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    require_role(request, SETUP_ROLES)
    path = _source_path()
    return request.app.state.templates.TemplateResponse(
        request, "import.html",
        {"path": path, "ok": bool(path) and Path(path).exists(), "result": None, "error": None},
    )


@router.post("/import", response_class=HTMLResponse)
async def run_import(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    templates = request.app.state.templates
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    upload = form.get("dbfile")

    def page(ctx, status=200):
        ctx = {"path": _source_path(), "ok": True, "result": None, "source": None,
               "error": None, **ctx}
        return templates.TemplateResponse(request, "import.html", ctx, status_code=status)

    # A browsed/uploaded file wins; otherwise fall back to the configured path.
    tmp_path = None
    try:
        if upload is not None and getattr(upload, "filename", ""):
            tmp_path = jobs_dir / f"mrpimport-{uuid4().hex}-{Path(upload.filename).name}"
            tmp_path.write_bytes(await upload.read())
            path, source = str(tmp_path), Path(upload.filename).name
        else:
            path, source = _source_path(), _source_path()

        if not path or not Path(path).exists():
            return page({"ok": False,
                         "error": "No database selected, and no valid minimrp_path in settings."},
                        status=400)

        result = await run_in_threadpool(_do_import, db, path)
        return page({"result": result, "source": source})
    except Exception as exc:  # missing mdbtools, not an Access file, etc.
        return page({"error": f"Import failed: {exc}"}, status=500)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
