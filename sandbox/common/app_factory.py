"""Builds a sandbox FastAPI app with the shared conventions (plan §7.2):

session login, CSRF on every form, chaos middleware, the `/__admin` router,
shared templates and stylesheet, and a read-only API authenticated by
`X-API-Key`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from sandbox.common import settings
from sandbox.common.auth import (
    LoginRequired,
    csrf_ok,
    csrf_token,
    current_user,
    login_redirect_url,
    safe_next,
    verify_password,
)
from sandbox.common.chaos import ChaosMiddleware, ChaosState, drift_for
from sandbox.common.db import audit, audit_chaos, opened
from sandbox.common.money import format_inr

COMMON_DIR = Path(__file__).parent


def make_admin_router(chaos: ChaosState, reset_fn: Callable[[], Any], state_fn: Callable[[], dict]) -> APIRouter:
    """Sandbox control plane. The runtime gateway blocks `/__*` for the agent regardless."""
    router = APIRouter(prefix="/__admin")

    def check(request: Request) -> None:
        if request.headers.get("x-sandbox-admin") != settings.admin_token():
            raise HTTPException(status_code=403, detail="sandbox admin token required")

    @router.post("/reset")
    def reset(request: Request) -> dict:
        check(request)
        result = reset_fn()
        chaos.reset()
        return {"ok": True, "reset": str(result)}

    @router.post("/chaos")
    async def set_chaos(request: Request) -> dict:
        check(request)
        body = await request.json()
        try:
            chaos.set_profile(body.get("profile", "off"), int(body.get("seed", 0)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "chaos": chaos.snapshot()}

    @router.post("/chaos/arm")
    async def arm(request: Request) -> dict:
        check(request)
        body = await request.json()
        try:
            chaos.arm(body["fault"], int(body.get("count", 1)), body.get("path"))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "chaos": chaos.snapshot()}

    @router.post("/chaos/disarm")
    def disarm(request: Request) -> dict:
        check(request)
        chaos.disarm()
        return {"ok": True, "chaos": chaos.snapshot()}

    @router.get("/state")
    def state(request: Request) -> dict:
        check(request)
        return {"chaos": chaos.snapshot(), **state_fn()}

    return router


def create_app(
    name: str,
    title: str,
    templates_dir: Path,
    *,
    write_paths: list[str],
    form_paths: list[str],
    nav: list[tuple[str, str]],
    reset_fn: Callable[[], Any],
    state_fn: Callable[[], dict],
) -> tuple[FastAPI, Jinja2Templates, ChaosState]:
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=[str(templates_dir), str(COMMON_DIR / "templates")])
    templates.env.globals.update(
        app_title=title,
        nav=nav,
        drift=drift_for,
        csrf_token=csrf_token,
        current_user=current_user,
        format_inr=format_inr,
    )

    chaos = ChaosState(app_name=name, write_paths=write_paths, form_paths=form_paths)
    initial_profile = settings.env("SANDBOX_CHAOS_PROFILE", "off")
    chaos.set_profile(initial_profile, int(settings.env("SANDBOX_CHAOS_SEED", "0") or 0))

    # Order matters: the last middleware added is the outermost. Sessions wrap chaos so the
    # chaos middleware can clear the session before the app sees the request.
    app.add_middleware(ChaosMiddleware, state=chaos, audit=audit_chaos(name))
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret(),
        session_cookie=f"{name}_session",  # cookies are per host, not per port: names must differ
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(COMMON_DIR / "static")), name="static")
    app.include_router(make_admin_router(chaos, reset_fn, state_fn))

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse(login_redirect_url(exc.next_path), status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/") -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": None})

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        next_path = safe_next(str(form.get("next") or "/"))
        if not csrf_ok(request, form.get("csrf_token")):
            return templates.TemplateResponse(
                request, "login.html", {"next": next_path, "error": "Your form expired. Please sign in again."}, status_code=400
            )
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        with opened(name) as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
            ok = bool(row and verify_password(password, row["password_hash"]))
            audit(conn, username or None, "login" if ok else "login_failed", {})
        if not ok:
            return templates.TemplateResponse(
                request, "login.html", {"next": next_path, "error": "Invalid username or password."}, status_code=401
            )
        request.session.clear()
        request.session["user"] = username
        return RedirectResponse(next_path, status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        user = current_user(request)
        request.session.clear()
        with opened(name) as conn:
            audit(conn, user, "logout", {})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Exception):
        detail = getattr(exc, "detail", None)
        detail = detail if isinstance(detail, str) and detail != "Not Found" else "That page does not exist."
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=404)
        return templates.TemplateResponse(
            request, "error.html", {"status": 404, "title": "Not found", "message": detail}, status_code=404
        )

    return app, templates, chaos


def require_api_key(request: Request, app_name: str) -> None:
    """Read-only reporting API: `X-API-Key` header or 401."""
    if request.headers.get("x-api-key") != settings.api_key(app_name):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def form_error(templates: Jinja2Templates, request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"status": 400, "title": "Bad request", "message": message}, status_code=400
    )
