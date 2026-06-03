"""FastAPI application for the DigiSearch web front-end.

First slice: log in, upload a BOM, run the quoting pipeline, see the resolved table,
and download the Excel report + distributor cart CSVs. Auth is session-based and the
quote action is gated by role. The heavy, network-bound resolve runs in a threadpool
so it never blocks the event loop.
"""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from ..config import PROJECT_ROOT, Settings
from ..models import Status
from .auth import QUOTE_ROLES, User, UserStore
from .service import run_quote

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}

# Status -> CSS badge class, for colouring the results table.
STATUS_CLASS = {
    Status.RESOLVED: "ok",
    Status.IN_STOCK: "stock",
    Status.REVIEW: "warn",
    Status.NOT_FOUND: "bad",
    Status.ERROR: "bad",
    Status.DNP: "muted",
    Status.NON_ORDERABLE: "muted",
}


def _default_data_dir() -> Path:
    env = os.getenv("PARTPILOT_DATA_DIR")
    return Path(env) if env else PROJECT_ROOT / "data"


def create_app(
    *,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    secret_key: str | None = None,
) -> FastAPI:
    data_dir = Path(data_dir) if data_dir else _default_data_dir()
    db_path = Path(db_path) if db_path else Path(os.getenv("PARTPILOT_DB", data_dir / "partpilot.db"))
    jobs_dir = data_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    store = UserStore(db_path)
    _seed_admin(store)

    secret_key = secret_key or os.getenv("PARTPILOT_SECRET_KEY")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        print(
            "[partpilot] WARNING: no PARTPILOT_SECRET_KEY set — using a random key; "
            "sessions reset on restart. Set PARTPILOT_SECRET_KEY for persistent logins."
        )

    app = FastAPI(title="PartPilot")
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax")
    app.state.store = store
    app.state.jobs_dir = jobs_dir

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    def current_user(request: Request) -> User | None:
        uid = request.session.get("user_id")
        return store.get(uid) if uid else None

    # ----- auth -----

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
                request,
                "login.html",
                {"error": "Invalid username or password."},
                status_code=401,
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ----- quoting -----

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        settings = Settings.load(None)
        return templates.TemplateResponse(
            request,
            "upload.html",
            {
                "user": user,
                "can_quote": user.role in QUOTE_ROLES,
                "default_build_qty": settings.build_qty,
                "default_currency": settings.currency or "",
                "stock_available": bool(settings.minimrp_path),
            },
        )

    @app.post("/quote", response_class=HTMLResponse)
    async def quote(
        request: Request,
        file: UploadFile = File(...),
        build_qty: int = Form(...),
        check_stock: bool = Form(False),
    ):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if user.role not in QUOTE_ROLES:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"user": user, "message": "Your role is not permitted to run quotes."},
                status_code=403,
            )

        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"user": user,
                 "message": f"Unsupported file type '{suffix}'. Upload a CSV or Excel BOM."},
                status_code=400,
            )

        job_id = uuid.uuid4().hex
        job_dir = jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        bom_path = job_dir / (Path(file.filename).name)
        bom_path.write_bytes(await file.read())

        try:
            result = await run_in_threadpool(
                run_quote, bom_path, job_dir,
                build_qty=build_qty, check_stock=check_stock,
            )
        except Exception as exc:  # surface engine/credential errors to the user
            return templates.TemplateResponse(
                request,
                "error.html",
                {"user": user, "message": f"Quote failed: {exc}"},
                status_code=500,
            )

        downloads = [("Report (Excel)", result.report_path.name)]
        downloads += [(f"{label} cart", path.name) for label, path in result.cart_paths.items()]

        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "user": user,
                "job_id": job_id,
                "result": result,
                "downloads": downloads,
                "status_class": STATUS_CLASS,
                "source_name": Path(file.filename).name,
            },
        )

    @app.get("/download/{job_id}/{name}")
    def download(request: Request, job_id: str, name: str):
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        target = (jobs_dir / job_id / name).resolve()
        if not target.is_relative_to(jobs_dir.resolve()) or not target.is_file():
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(target, filename=name)

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
