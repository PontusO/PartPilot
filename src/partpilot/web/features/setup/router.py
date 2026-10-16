"""Setup & Tools — an admin hub of maintenance tools (part-number cleanup, Fortnox, webshop, …).

The landing page renders ``TOOLS`` as a list, so adding a tool later is: append an entry
here and add its route. All routes are admin-only.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from partpilot.config import Settings

from partpilot.fortnox import FortnoxError
from partpilot.fortnox.client import authorize_url, exchange_code
from partpilot.woocommerce import WooClient, WooError

from ...auth import ROLES
from ...core.deps import require_role
from ..catalog import woo_sync
from ..assemblies import repo as asm_repo
from ..catalog import repo as catalog_repo
from ..catalog.import_parts import apply_parts_plan, build_import_plan
from ..purchasing.service import resolve_bom_file
from . import import_parts as ip
from . import repo
from .part_cleanup import build_clients, recover

SETUP_ROLES = frozenset({"admin"})

router = APIRouter(prefix="/setup")

# Tools shown on the Setup & Tools page — append here to add more later.
TOOLS = [
    {"label": "Import parts from BOM", "url": "/setup/import-parts", "icon": "📥",
     "description": "Upload any CSV/Excel BOM or part list, map its columns to PartPilot fields, "
                    "resolve each row against stock and Digi-Key/Mouser, and tick the parts to "
                    "create in the catalog. Mappings are remembered per file layout."},
    {"label": "Stock shelves", "url": "/catalog/shelves", "icon": "🗄️",
     "description": "Our shelves and customer-owned shelves: who owns what stock, where it sits, "
                    "and what it is worth. Create, rename and retire shelves."},
    {"label": "Part-number cleanup", "url": "/setup/part-cleanup", "icon": "🧹",
     "description": "Find parts whose part number is really a distributor order code (no "
                    "manufacturer P/N captured), look up the manufacturer P/N at Digi-Key/Mouser, "
                    "and promote it — moving the supplier code into the supplier row."},
    {"label": "Company details", "url": "/setup/company", "icon": "🏢",
     "description": "Your company name and address used on purchase-order PDFs and ISO records."},
    {"label": "Production settings", "url": "/setup/production", "icon": "🏭",
     "description": "Production parameters such as the spillage/scrap margin added to each "
                    "work-order batch's component requirements."},
    {"label": "Order settings", "url": "/setup/orders", "icon": "🧾",
     "description": "How customer orders behave — e.g. whether acknowledging an order also "
                    "confirms it."},
    {"label": "Pricing settings", "url": "/setup/pricing", "icon": "💰",
     "description": "The default sell markup applied to a part's cost when it has no explicit sell "
                    "tiers and no per-part markup override."},
    {"label": "Users", "url": "/setup/users", "icon": "👤",
     "description": "Add people, set the group (role) they belong to, reset passwords and "
                    "deactivate accounts that should no longer log in."},
    {"label": "Webshop settings", "url": "/setup/webshop", "icon": "🛒",
     "description": "Connect the WooCommerce webshop (store URL + read-only API keys) used to "
                    "pull product stock into PartPilot."},
    {"label": "Sync webshop", "url": "/setup/webshop/sync", "icon": "🔄",
     "description": "Pull products from the webshop: match by part number (SKU) and update stock, "
                    "creating any parts/assemblies that don't exist yet. Webshop is authoritative."},
    {"label": "Fortnox", "url": "/setup/fortnox", "icon": "🧮",
     "description": "Connect Fortnox accounting and set invoicing defaults. Despatches then create "
                    "draft customer invoices in Fortnox."},
]




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


# ----- Users -----

def _users_page(request: Request, *, error: str | None = None,
                saved: str | None = None, status: int = 200):
    store = request.app.state.store
    return request.app.state.templates.TemplateResponse(
        request, "users.html",
        {"users": store.list_users(), "roles": ROLES, "error": error, "saved": saved,
         "me": request.app.state.store.get(request.session.get("user_id"))},
        status_code=status,
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    require_role(request, SETUP_ROLES)
    return _users_page(request)


@router.post("/users", response_class=HTMLResponse)
async def add_user(request: Request):
    require_role(request, SETUP_ROLES)
    store = request.app.state.store
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    full_name = (form.get("full_name") or "").strip()
    role = (form.get("role") or "").strip()
    if not username or not password:
        return _users_page(request, error="Username and password are required.", status=400)
    if role not in ROLES:
        return _users_page(request, error=f"Unknown group {role!r}.", status=400)
    try:
        store.create_user(username, password, role=role, full_name=full_name)
    except sqlite3.IntegrityError:
        return _users_page(request, error=f"A user named {username!r} already exists.", status=400)
    return _users_page(request, saved=f"User {username!r} added.")


@router.post("/users/{user_id}/role", response_class=HTMLResponse)
async def change_role(request: Request, user_id: int):
    me = require_role(request, SETUP_ROLES)
    store = request.app.state.store
    target = store.get_any(user_id)
    form = await request.form()
    role = (form.get("role") or "").strip()
    if role not in ROLES:
        return _users_page(request, error=f"Unknown group {role!r}.", status=400)
    if target is None:
        return _users_page(request, error="No such user.", status=404)
    if target.id == me.id and role != "admin":
        return _users_page(request, error="You can't change your own group.", status=400)
    if target.role == "admin" and role != "admin" and store.count_active_admins() <= 1:
        return _users_page(request, error="Can't change the last admin's group — "
                           "promote someone else to admin first.", status=400)
    store.update_role(user_id, role)
    return _users_page(request, saved=f"{target.username} is now in the {role} group.")


@router.post("/users/{user_id}/password", response_class=HTMLResponse)
async def reset_password(request: Request, user_id: int):
    require_role(request, SETUP_ROLES)
    store = request.app.state.store
    target = store.get_any(user_id)
    form = await request.form()
    password = form.get("password") or ""
    if target is None:
        return _users_page(request, error="No such user.", status=404)
    if not password:
        return _users_page(request, error="A new password is required.", status=400)
    store.set_password(user_id, password)
    return _users_page(request, saved=f"Password reset for {target.username}.")


@router.post("/users/{user_id}/active", response_class=HTMLResponse)
async def toggle_active(request: Request, user_id: int):
    me = require_role(request, SETUP_ROLES)
    store = request.app.state.store
    target = store.get_any(user_id)
    form = await request.form()
    active = (form.get("active") or "") == "1"
    if target is None:
        return _users_page(request, error="No such user.", status=404)
    if not active:  # deactivating
        if target.id == me.id:
            return _users_page(request, error="You can't deactivate your own account.", status=400)
        if target.role == "admin" and store.count_active_admins() <= 1:
            return _users_page(request, error="Can't deactivate the last admin.", status=400)
    store.set_active(user_id, active)
    verb = "reactivated" if active else "deactivated"
    return _users_page(request, saved=f"{target.username} {verb}.")


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(request: Request, user_id: int):
    me = require_role(request, SETUP_ROLES)
    store = request.app.state.store
    target = store.get_any(user_id)
    if target is None:
        return _users_page(request, error="No such user.", status=404)
    if target.id == me.id:
        return _users_page(request, error="You can't delete your own account.", status=400)
    if store.has_logged_in(user_id):
        return _users_page(request, error=f"{target.username} has activity on record — "
                           "deactivate instead of deleting to preserve history.", status=400)
    store.delete(user_id)
    return _users_page(request, saved=f"User {target.username!r} deleted.")


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "orders.html",
        {"orders": repo.get_orders(request.app.state.database), "saved": False},
    )


@router.post("/orders", response_class=HTMLResponse)
async def save_orders(request: Request):
    require_role(request, SETUP_ROLES)
    form = await request.form()
    repo.save_orders(request.app.state.database,
                     {"ack_confirms": form.get("ack_confirms") is not None})
    return request.app.state.templates.TemplateResponse(
        request, "orders.html",
        {"orders": repo.get_orders(request.app.state.database), "saved": True},
    )


@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "pricing.html",
        {"pricing": repo.get_pricing(request.app.state.database), "saved": False},
    )


@router.post("/pricing", response_class=HTMLResponse)
async def save_pricing(request: Request):
    require_role(request, SETUP_ROLES)
    form = await request.form()
    repo.save_pricing(request.app.state.database,
                      {"default_markup": form.get("default_markup")})
    return request.app.state.templates.TemplateResponse(
        request, "pricing.html",
        {"pricing": repo.get_pricing(request.app.state.database), "saved": True},
    )


# ----- Webshop (WooCommerce) -----

def _build_woo_client(settings: dict) -> WooClient:
    return WooClient(settings["base_url"], settings["consumer_key"], settings["consumer_secret"],
                     currency=settings.get("currency") or None)


@router.get("/webshop", response_class=HTMLResponse)
def webshop_page(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "webshop.html",
        {"webshop": repo.get_webshop(request.app.state.database), "saved": False,
         "tested": None, "error": None},
    )


@router.post("/webshop", response_class=HTMLResponse)
async def save_webshop(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    templates = request.app.state.templates
    form = await request.form()
    data = {f: form.get(f) for f in repo.WEBSHOP_FIELDS}
    repo.save_webshop(db, data)
    repo.set_webshop_time(db, form.get("sync_at_time"))

    tested, error = None, None
    if (form.get("action") or "") == "test":
        settings = repo.get_webshop(db)
        if not settings["configured"]:
            error = "Fill in the store URL and both API keys before testing."
        else:
            try:
                await run_in_threadpool(_build_woo_client(settings).ping)
                tested = "Connected to the webshop successfully."
            except WooError as exc:
                error = str(exc)
    return templates.TemplateResponse(
        request, "webshop.html",
        {"webshop": repo.get_webshop(db), "saved": True, "tested": tested, "error": error},
        status_code=400 if error else 200,
    )


@router.get("/webshop/sync", response_class=HTMLResponse)
def webshop_sync_page(request: Request):
    require_role(request, SETUP_ROLES)
    return request.app.state.templates.TemplateResponse(
        request, "webshop_sync.html",
        {"webshop": repo.get_webshop(request.app.state.database), "report": None,
         "dry_run": None, "error": None},
    )


@router.post("/webshop/sync", response_class=HTMLResponse)
async def webshop_sync_run(request: Request):
    me = require_role(request, SETUP_ROLES)
    db = request.app.state.database
    templates = request.app.state.templates
    form = await request.form()
    dry_run = (form.get("action") or "preview") != "apply"
    settings = repo.get_webshop(db)

    def page(report=None, error=None, status=200):
        return templates.TemplateResponse(
            request, "webshop_sync.html",
            {"webshop": repo.get_webshop(db), "report": report, "dry_run": dry_run,
             "error": error}, status_code=status,
        )

    if not settings["configured"]:
        return page(error="The webshop isn't configured yet — set the URL and API keys first.",
                    status=400)
    try:
        client = _build_woo_client(settings)
        report = await run_in_threadpool(
            lambda: woo_sync.sync_from_woo(db, list(client.iter_products()),
                                           client=client, user=me.username, dry_run=dry_run))
    except WooError as exc:
        return page(error=str(exc), status=502)
    if not dry_run:
        repo.set_webshop_synced(db, datetime.now().isoformat(timespec="seconds"))
    return page(report=report)


# ----- Fortnox (accounting / invoicing) -----

def _fortnox_page(request: Request, *, saved=False, error=None, connected_now=False, status=200):
    return request.app.state.templates.TemplateResponse(
        request, "fortnox.html",
        {"fortnox": repo.get_fortnox(request.app.state.database), "saved": saved,
         "error": error, "connected_now": connected_now}, status_code=status,
    )


@router.get("/fortnox", response_class=HTMLResponse)
def fortnox_page(request: Request):
    require_role(request, SETUP_ROLES)
    return _fortnox_page(request, connected_now=bool(request.query_params.get("connected")))


@router.post("/fortnox", response_class=HTMLResponse)
async def save_fortnox(request: Request):
    require_role(request, SETUP_ROLES)
    form = await request.form()
    repo.save_fortnox(request.app.state.database,
                      {f: form.get(f) for f in repo.FORTNOX_FIELDS})
    return _fortnox_page(request, saved=True)


@router.get("/fortnox/connect")
def fortnox_connect(request: Request):
    require_role(request, SETUP_ROLES)
    cfg = repo.get_fortnox(request.app.state.database)
    if not cfg["configured"]:
        return _fortnox_page(request, error="Save the Client ID, secret and redirect URL first.",
                             status=400)
    state = secrets.token_urlsafe(24)
    request.session["fortnox_oauth_state"] = state
    return RedirectResponse(
        authorize_url(cfg["client_id"], cfg["redirect_uri"], state), status_code=303)


@router.get("/fortnox/callback", response_class=HTMLResponse)
def fortnox_callback(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    params = request.query_params
    expected = request.session.pop("fortnox_oauth_state", None)
    if params.get("error"):
        return _fortnox_page(request, error=f"Fortnox declined: {params.get('error')}", status=400)
    if not params.get("code") or not expected or params.get("state") != expected:
        return _fortnox_page(request, error="Authorisation failed or expired — try connecting again.",
                             status=400)
    cfg = repo.get_fortnox(db)
    try:
        tokens = exchange_code(cfg["client_id"], cfg["client_secret"],
                               params["code"], cfg["redirect_uri"])
    except FortnoxError as exc:
        return _fortnox_page(request, error=str(exc), status=502)
    repo.save_fortnox_tokens(db, tokens)
    return RedirectResponse("/setup/fortnox?connected=1", status_code=303)


@router.post("/fortnox/disconnect", response_class=HTMLResponse)
async def fortnox_disconnect(request: Request):
    require_role(request, SETUP_ROLES)
    repo.clear_fortnox_tokens(request.app.state.database)
    return _fortnox_page(request, saved=True)


# ----- Part-number cleanup -----

def _cleanup_form_values(form) -> dict[int, dict]:
    """Pull the editable MPN / manufacturer fields the page submitted, keyed by part id."""
    values: dict[int, dict] = {}
    for key in form.keys():
        for prefix, field in (("mpn_", "mpn"), ("mfr_", "mfr")):
            if key.startswith(prefix) and key[len(prefix):].isdigit():
                values.setdefault(int(key[len(prefix):]), {})[field] = (form.get(key) or "").strip()
    return values


def _cleanup_page(request: Request, *, proposals=None, form_values=None,
                  summary=None, error=None, status=200):
    db = request.app.state.database
    proposals = proposals or {}
    form_values = form_values or {}
    suspects = repo.find_suspect_parts(db)
    for s in suspects:
        prop = proposals.get(s["id"])
        fv = form_values.get(s["id"], {})
        s["mpn"] = (prop.mpn if prop and prop.mpn else fv.get("mpn", "")) or ""
        s["mfr"] = (prop.manufacturer if prop and prop.manufacturer else fv.get("mfr", "")) or ""
        s["source"] = prop.source if prop else None
        s["note"] = prop.note if prop else None
    return request.app.state.templates.TemplateResponse(
        request, "part_cleanup.html",
        {"suspects": suspects, "summary": summary, "error": error}, status_code=status)


# ---- Import parts from any BOM ------------------------------------------------------------
# upload -> map columns -> resolve (shared engine) -> review -> create. Files live in jobs_dir
# between steps: pimp-<job><suffix> (the upload), pimeta-<job>.json (source name, headers,
# build qty), piplan-<job>.json (the review plan). Apply replaces all three with
# piresult-<job>.json (rows + outcome), which backs the result page and the optional "wrap into
# an assembly" step, and is removed when the assembly is created or the admin clicks Done.

_JOB_RE = re.compile(r"[0-9a-f]+")


def _ip_paths(jobs_dir: Path, job_id: str, suffix: str = "") -> tuple[Path, Path, Path]:
    return (jobs_dir / f"pimp-{job_id}{suffix}", jobs_dir / f"pimeta-{job_id}.json",
            jobs_dir / f"piplan-{job_id}.json")


def _ip_result_path(jobs_dir: Path, job_id: str) -> Path | None:
    """The applied-import record (plan rows + outcome), or None for an unknown/expired job."""
    if not _JOB_RE.fullmatch(job_id or ""):
        return None
    p = jobs_dir / f"piresult-{job_id}.json"
    return p if p.exists() else None


def meta_source(upload) -> str:
    return Path(getattr(upload, "filename", "") or "file").name


def _ip_shelves(db) -> dict:
    return {"shelves": catalog_repo.locations(db), "shelf_selected": catalog_repo.default_shelf_id(db)}


def _ip_upload_page(request: Request, error: str | None = None, status: int = 200):
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_upload.html",
        {"error": error, **_ip_shelves(request.app.state.database)}, status_code=status)


def _ip_map_page(request: Request, *, job_id: str, meta: dict, samples: list[list[str]],
                 selected: list[str], error: str | None = None, status: int = 200):
    """``selected`` is the target per column (parallel to headers), ``"ignore"`` for none."""
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_map.html",
        {"job_id": job_id, "source": meta["source"], "headers": meta["headers"],
         "samples": samples, "selected": selected, "targets": ip.TARGETS,
         "build_qty": meta.get("build_qty"), "stock_shelf": meta.get("stock_shelf_name"),
         "error": error}, status_code=status)


def _ip_selected_from_mapping(mapping: dict[str, str], headers: list[str]) -> list[str]:
    by_header = {h: f for f, h in mapping.items()}
    return [by_header.get(h, ip.IGNORE) for h in headers]


def _ip_selected_from_form(chosen: dict[str, str], headers: list[str]) -> list[str]:
    """Exactly what the user picked per column — duplicates included — for re-rendering."""
    selected = [ip.IGNORE] * len(headers)
    for k, v in chosen.items():
        if k[4:].isdigit() and int(k[4:]) < len(headers) and v in ip.TARGET_FIELDS:
            selected[int(k[4:])] = v
    return selected


def _ip_load_job(jobs_dir: Path, job_id: str):
    """(bom_path, meta_path, plan_path, meta) or None if the job is unknown/expired."""
    if not _JOB_RE.fullmatch(job_id or ""):
        return None
    _, meta_path, plan_path = _ip_paths(jobs_dir, job_id)
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    bom_path = _ip_paths(jobs_dir, job_id, meta.get("suffix", ""))[0]
    return bom_path, meta_path, plan_path, meta


@router.get("/import-parts", response_class=HTMLResponse)
def import_parts_form(request: Request):
    require_role(request, SETUP_ROLES)
    return _ip_upload_page(request)


@router.post("/import-parts", response_class=HTMLResponse)
async def import_parts_upload(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", ""):
        return _ip_upload_page(request, "Choose a CSV or Excel file.", 400)
    raw_qty = (form.get("build_qty") or "").strip()
    build_qty = int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else None
    # "Check stock on shelf": a customer's shelf sees only their stock, so rows we hold on Main still
    # go to the distributors; "none" prices every row.
    try:
        check_stock, stock_location_id = catalog_repo.parse_stock_shelf(db, form.get("stock_shelf"))
    except ValueError as exc:
        return _ip_upload_page(request, str(exc), 400)
    shelf = catalog_repo.get_location(db, stock_location_id) if stock_location_id else None
    stock_shelf_name = shelf["name"] if shelf else None

    job_id = uuid4().hex
    suffix = Path(upload.filename).suffix.lower()
    bom_path, meta_path, _ = _ip_paths(jobs_dir, job_id, suffix)
    bom_path.write_bytes(await upload.read())
    try:
        headers, samples = ip.read_headers(bom_path)
    except Exception as exc:  # unsupported suffix, no header row, corrupt/not-really-Excel, ...
        bom_path.unlink(missing_ok=True)
        return _ip_upload_page(request, f"Could not read {meta_source(upload)}: {exc}", 400)
    meta = {"source": Path(upload.filename).name, "headers": headers, "build_qty": build_qty,
            "suffix": suffix, "check_stock": check_stock, "stock_location_id": stock_location_id,
            "stock_shelf_name": stock_shelf_name}
    meta_path.write_text(json.dumps(meta))
    return _ip_map_page(request, job_id=job_id, meta=meta, samples=samples,
                        selected=_ip_selected_from_mapping(ip.prefill_mapping(db, headers), headers))


@router.post("/import-parts/resolve", response_class=HTMLResponse)
async def import_parts_resolve(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    job_id = (form.get("job_id") or "").strip()
    job = _ip_load_job(jobs_dir, job_id)
    if job is None or not job[0].exists():
        return _ip_upload_page(request, "Upload expired — please upload the file again.", 400)
    bom_path, meta_path, plan_path, meta = job
    headers = meta["headers"]
    _, samples = ip.read_headers(bom_path)
    chosen = {k: str(v) for k, v in form.items() if k.startswith("map_")}
    selected = _ip_selected_from_form(chosen, headers)
    try:
        mapping = ip.parse_mapping(chosen, headers)
    except ValueError as exc:
        return _ip_map_page(request, job_id=job_id, meta=meta, samples=samples,
                            selected=selected, error=str(exc), status=400)
    ip.remember_mapping(db, headers, mapping)

    try:
        run = await run_in_threadpool(
            resolve_bom_file, bom_path, build_qty=meta.get("build_qty"), database=db,
            check_stock=meta.get("check_stock", True), stock_location_id=meta.get("stock_location_id"),
            mappings=ip.run_mappings(mapping))
    except Exception as exc:  # missing creds, network, bad rows
        # The upload is kept so the user can fix the mapping and retry without re-uploading.
        return _ip_map_page(request, job_id=job_id, meta=meta, samples=samples,
                            selected=selected, error=f"Resolve failed: {exc}", status=500)

    plan = build_import_plan(db, run, meta.get("stock_location_id"))
    plan_path.write_text(json.dumps(plan))
    meta["stock_shelf"] = run.stock_shelf   # what actually happened, shown on the result page
    meta_path.write_text(json.dumps(meta))
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_review.html",
        {"plan": plan, "job_id": job_id, "warnings": run.warnings, "source": meta["source"],
         "stock_shelf": run.stock_shelf})


@router.post("/import-parts/apply")
async def import_parts_apply(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    job_id = (form.get("job_id") or "").strip()
    accepted = {int(v) for v in form.getlist("accept") if str(v).isdigit()}
    job = _ip_load_job(jobs_dir, job_id)
    if job is None or not job[2].exists():
        return RedirectResponse("/setup/import-parts", status_code=303)
    bom_path, meta_path, plan_path, meta = job
    plan = json.loads(plan_path.read_text())
    result = apply_parts_plan(db, plan, accepted)
    for p in (bom_path, meta_path, plan_path):
        p.unlink(missing_ok=True)
    rows = _ip_outcome_rows(plan, result, accepted)
    (jobs_dir / f"piresult-{job_id}.json").write_text(json.dumps({
        "source": meta["source"], "rows": rows,
        "stock_shelf": meta.get("stock_shelf"),
        "counts": {"created": result["created"], "linked": result["linked"],
                   "skipped": result["skipped"]},
        "review": result["review"],
    }))
    return RedirectResponse(f"/setup/import-parts/{job_id}/result", status_code=303)


# Per-row outcome labels on the result page.
CREATED = "created"                 # new catalog part made by this import
LINKED = "already in catalog"       # row points at a pre-existing part
NOT_TICKED = "not ticked"           # could have been created, admin left it out
NOT_FOUND = "not found"             # no distributor match and not created
MANUAL_ROW = "manual"               # too under-specified to import
SKIPPED = "skipped"                 # DNP / non-orderable


def _ip_outcome_rows(plan: list[dict], result: dict, accepted: set[int]) -> list[dict]:
    created_rows = set(result["created_rows"])
    rows = []
    for i, (it, pid) in enumerate(zip(plan, result["part_ids"])):
        status = it.get("status")
        if i in created_rows:
            outcome = CREATED
        elif pid is not None:
            outcome = LINKED
        elif status == "manual":
            outcome = MANUAL_ROW
        elif status == "skip":
            outcome = SKIPPED
        elif status == "unresolved" and i not in accepted:
            outcome = NOT_FOUND
        else:
            outcome = NOT_TICKED
        rows.append({
            "refdes": it.get("refdes"), "qty": it.get("qty"), "orig": it.get("orig"),
            "status": status, "outcome": outcome, "part_id": pid, "part_no": it.get("part_no"),
            "value": it.get("value"), "note": it.get("note"),
            # Unresolved rows that were created anyway are still "not found at a distributor".
            "unfound": status in ("unresolved", "manual"),
        })
    return rows


def _ip_result_page(request: Request, job_id: str, data: dict, *, values: dict | None = None,
                    error: str | None = None, status: int = 200):
    rows = data["rows"]
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_result.html",
        {"job_id": job_id, "source": data["source"], "result": data["counts"],
         "review": data["review"], "rows": rows,
         "stock_shelf": data.get("stock_shelf"),
         "created": [r for r in rows if r["outcome"] == CREATED],
         "not_found": [r for r in rows if r["unfound"]],
         "bom_rows": [r for r in rows if r["part_id"] is not None],
         "values": values or {}, "error": error}, status_code=status)


@router.get("/import-parts/{job_id}/result", response_class=HTMLResponse)
def import_parts_result(request: Request, job_id: str, part_no: str | None = None,
                        desc: str | None = None):
    require_role(request, SETUP_ROLES)
    path = _ip_result_path(request.app.state.jobs_dir, job_id)
    if path is None:
        return RedirectResponse("/setup/import-parts", status_code=303)
    # part_no / desc arrive when bouncing back from an Article Register allocator.
    values = {"part_no": (part_no or "").strip(), "description": (desc or "").strip()}
    return _ip_result_page(request, job_id, json.loads(path.read_text()), values=values)


@router.post("/import-parts/{job_id}/assembly")
async def import_parts_assembly(request: Request, job_id: str):
    """Wrap every imported row that has a part — created or pre-existing — into a new assembly."""
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    path = _ip_result_path(request.app.state.jobs_dir, job_id)
    if path is None:
        return RedirectResponse("/setup/import-parts", status_code=303)
    data = json.loads(path.read_text())
    form = await request.form()
    values = {"part_no": (form.get("part_no") or "").strip(),
              "description": (form.get("description") or "").strip()}
    if not values["part_no"]:
        return _ip_result_page(request, job_id, data, values=values,
                               error="Part number is required.", status=400)
    if catalog_repo.find_part_by_part_no(db, values["part_no"]) is not None:
        return _ip_result_page(request, job_id, data, values=values,
                               error=f"A part or assembly numbered {values['part_no']} already exists.",
                               status=400)
    asm_id = asm_repo.create_assembly(db, {"part_no": values["part_no"],
                                           "description": values["description"] or None})
    for r in data["rows"]:
        if r["part_id"] is not None:
            asm_repo.add_bom_line(db, asm_id, r["part_id"], r.get("qty") or 1, r.get("refdes"))
    path.unlink(missing_ok=True)
    return RedirectResponse(f"/assemblies/{asm_id}", status_code=303)


@router.post("/import-parts/{job_id}/done")
def import_parts_done(request: Request, job_id: str):
    require_role(request, SETUP_ROLES)
    path = _ip_result_path(request.app.state.jobs_dir, job_id)
    if path is not None:
        path.unlink(missing_ok=True)
    return RedirectResponse("/setup/import-parts", status_code=303)

@router.get("/part-cleanup", response_class=HTMLResponse)
def part_cleanup_page(request: Request):
    require_role(request, SETUP_ROLES)
    return _cleanup_page(request)


@router.post("/part-cleanup", response_class=HTMLResponse)
async def part_cleanup_run(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    form = await request.form()
    action = (form.get("action") or "lookup").strip()
    picked = {int(v) for v in form.getlist("pick") if str(v).isdigit()}
    form_values = _cleanup_form_values(form)

    if not picked:
        return _cleanup_page(request, form_values=form_values,
                             error="Tick at least one part first.", status=400)

    if action == "lookup":
        try:
            dk, mouser = build_clients()
        except Exception as exc:  # missing/invalid Digi-Key credentials
            return _cleanup_page(request, form_values=form_values,
                                 error=f"Couldn't reach the distributors: {exc}", status=500)
        by_id = {s["id"]: s for s in repo.find_suspect_parts(db)}
        proposals = {}
        for pid in picked:
            s = by_id.get(pid)
            if s:
                proposals[pid] = await run_in_threadpool(
                    recover, s["part_no"], s["suppliers"], dk, mouser)
        found = sum(1 for p in proposals.values() if p.mpn)
        return _cleanup_page(
            request, proposals=proposals, form_values=form_values,
            summary={"kind": "lookup", "looked": len(proposals), "found": found})

    # action == "apply"
    applied, skipped = [], []
    for pid in picked:
        mpn = form_values.get(pid, {}).get("mpn", "")
        mfr = form_values.get(pid, {}).get("mfr", "")
        if not mpn:
            skipped.append((pid, "no manufacturer P/N entered"))
            continue
        collision = repo.part_no_taken(db, pid, mpn)
        if collision is not None:
            skipped.append((pid, f"'{mpn}' already used by part #{collision}"))
            continue
        repo.set_part_mpn(db, pid, mpn, mfr)
        applied.append((pid, mpn))
    return _cleanup_page(request, summary={"kind": "apply", "applied": applied, "skipped": skipped})
