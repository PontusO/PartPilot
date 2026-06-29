"""PartPilot application factory: the core platform that hosts feature modules.

The core owns auth/login, navigation, the SQLite database + migrations and the shared
template environment. Features (purchasing today; catalog/inventory next) are registered
here and contribute their own routes, nav entries and migrations without the core
knowing their internals.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import UserStore
from .core import FeatureRegistry
from .core.db import Database
from .core.deps import Forbidden, LoginRequired, current_user
from .core.paths import data_dir as default_data_dir, db_path as default_db_path
from .features.assemblies import feature as assemblies_feature
from .features.catalog import feature as catalog_feature
from .features.contacts import feature as contacts_feature
from .features.customer_orders import feature as customer_orders_feature
from .features.despatch import feature as despatch_feature
from .features.goods_receipts import feature as goods_receipts_feature
from .features.planning import feature as planning_feature
from .features.purchase_orders import feature as purchase_orders_feature
from .features.purchasing import feature as purchasing_feature
from .features.reports import feature as reports_feature
from .features.setup import feature as setup_feature
from .features.setup.scheduler import webshop_sync_loop
from .features.work_orders import feature as work_orders_feature

_CORE_TEMPLATES = Path(__file__).parent / "core" / "templates"

# The features this deployment ships, in nav order. Explicit and greppable on purpose —
# adding functionality is appending a feature here, not editing the core. Categories not yet
# built are placeholders (greyed "soon" nav entries) so the app structure is visible up front.
FEATURES = [
    catalog_feature,                                                         # Parts (order 10)
    assemblies_feature,                                                      # order 20
    purchasing_feature,                                                      # order 50
    contacts_feature,                                                        # order 60
    # Registered after contacts so its FK to contacts(id) resolves; nav order 30 keeps its slot.
    customer_orders_feature,                                                 # order 30
    # Registered after customer_orders so its FK to customer_order_lines resolves; nav order 40.
    work_orders_feature,                                                     # order 40
    planning_feature,                                                        # order 42 (after Work Orders)
    despatch_feature,                                                        # order 52 (after Purchasing; needs CO FKs)
    purchase_orders_feature,                                                 # order 45
    goods_receipts_feature,                                                   # order 46 (views PO-owned GRN tables)
    reports_feature,                                                         # Reports (order 70)
    setup_feature,                                                          # Setup & Tools (order 80)
]


def create_app(
    *,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    secret_key: str | None = None,
) -> FastAPI:
    resolved_data_dir = Path(data_dir) if data_dir else default_data_dir()
    if db_path:
        resolved_db = Path(db_path)
    elif data_dir:
        resolved_db = resolved_data_dir / "partpilot.db"
    else:
        resolved_db = default_db_path()
    jobs_dir = resolved_data_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    store = UserStore(resolved_db)
    _seed_admin(store)

    registry = FeatureRegistry()
    registry.register(*FEATURES)

    database = Database(resolved_db)
    database.apply_migrations(registry)

    secret_key = secret_key or os.getenv("PARTPILOT_SECRET_KEY")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        print(
            "[partpilot] WARNING: no PARTPILOT_SECRET_KEY set — using a random key; "
            "sessions reset on restart. Set PARTPILOT_SECRET_KEY for persistent logins."
        )

    def inject(request: Request) -> dict:
        user = current_user(request)
        return {"current_user": user, "nav": registry.nav_for(user.role if user else None)}

    templates = Jinja2Templates(
        directory=[str(_CORE_TEMPLATES), *[str(d) for d in registry.template_dirs()]],
        context_processors=[inject],
    )

    # Background runners (currently just the webshop auto-sync). Disabled on scratch/test
    # instances via PARTPILOT_DISABLE_SCHEDULER so a dev session never syncs the live shop.
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if os.getenv("PARTPILOT_DISABLE_SCHEDULER") != "1":
            task = asyncio.create_task(webshop_sync_loop(database))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="PartPilot", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax")
    app.state.store = store
    app.state.registry = registry
    app.state.database = database
    app.state.templates = templates
    app.state.jobs_dir = jobs_dir

    # Static assets (vendored FullCalendar for the planning board). The app's only static mount.
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    # ----- platform-wide auth handling -----

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return templates.TemplateResponse(
            request, "error.html", {"message": exc.message}, status_code=403
        )

    # ----- core routes: login + landing -----

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        user = store.verify(username, password)
        if user is None:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid username or password."}, status_code=401,
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/account/password", response_class=HTMLResponse)
    def account_password_form(request: Request):
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, "account_password.html", {"error": None, "saved": False}
        )

    @app.post("/account/password", response_class=HTMLResponse)
    def account_password_submit(
        request: Request,
        current: str = Form(...),
        new_password: str = Form(...),
        confirm: str = Form(...),
    ):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        def page(error=None, saved=False, status=200):
            return templates.TemplateResponse(
                request, "account_password.html", {"error": error, "saved": saved},
                status_code=status,
            )

        if store.verify(user.username, current) is None:
            return page(error="Your current password is incorrect.", status=400)
        if not new_password:
            return page(error="The new password can't be empty.", status=400)
        if new_password != confirm:
            return page(error="The new passwords don't match.", status=400)
        store.set_password(user.id, new_password)
        return page(saved=True)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "home.html", {})

    # ----- feature routes -----

    for feature in registry.features:
        if feature.router is not None:
            app.include_router(feature.router)

    return app


def _seed_admin(store: UserStore) -> None:
    """Create an initial admin on first run so someone can log in."""
    if store.count() > 0:
        return
    username = os.getenv("PARTPILOT_ADMIN_USER", "admin")
    password = os.getenv("PARTPILOT_ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)
    store.create_user(username, password, role="admin")
    if generated:
        print(
            "\n[partpilot] Created initial admin account:\n"
            f"    username: {username}\n"
            f"    password: {password}\n"
            "  Log in and change it. Set PARTPILOT_ADMIN_PASSWORD to choose your own.\n"
        )
