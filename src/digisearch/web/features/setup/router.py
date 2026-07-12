"""Setup & Tools — an admin hub of maintenance tools (first one: the miniMRP import).

The landing page renders ``TOOLS`` as a list, so adding a tool later is: append an entry
here and add its route. All routes are admin-only.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from digisearch.config import Settings

from digisearch.fortnox import FortnoxError
from digisearch.fortnox.client import authorize_url, exchange_code
from digisearch.woocommerce import WooClient, WooError

from ...auth import ROLES
from ...core.deps import require_role
from ..assemblies.importer import import_boms
from ..catalog import woo_sync
from ..catalog.importer import import_from_minimrp
from ..contacts.importer import import_contacts
from . import repo
from .part_cleanup import build_clients, recover

SETUP_ROLES = frozenset({"admin"})

router = APIRouter(prefix="/setup")

# Tools shown on the Setup & Tools page — append here to add more later.
TOOLS = [
    {"label": "Import from miniMRP", "url": "/setup/import", "icon": "📥",
     "description": "Import or re-sync parts, suppliers, stock and the assembly BOM structure "
                    "from the miniMRP database."},
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
    repo.save_pricing(request.app.state.database, {"default_markup": form.get("default_markup")})
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
