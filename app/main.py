from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .routers import (account, admin, analysis, auth, billing, catalog, diagnosis, ingest, learn,
                      mocks, payments, practice, psychometrics, review)
from .seed import seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot init (create_all + seed + mount + ensure_admins) must run exactly once even when many
    # gunicorn workers (and many containers) start at the same moment against one Postgres. Without
    # this, every worker races to CREATE TABLE and all but one crash with DuplicateTable. A Postgres
    # session-level advisory lock serialises the whole block database-wide: the first worker builds
    # the schema/seed; the rest wait, then find it already there (create_all/seed are idempotent).
    from sqlalchemy import text

    from .config import settings
    from .db import SessionLocal, engine

    _BOOT_LOCK_KEY = 727202699  # arbitrary app-wide constant
    is_pg = settings.database_url.startswith("postgres")
    lock_conn = None
    if is_pg:
        lock_conn = engine.connect()
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _BOOT_LOCK_KEY})
        lock_conn.commit()
    try:
        init_db()
        seed_if_empty()
        from .services import billing, mount
        from .services.admin_auth import ensure_admins
        _db = SessionLocal()
        try:
            billing.ensure_catalog(_db)
            mount.mount_gmat_gre_if_empty(_db)
            ensure_admins(_db)  # promote ADMIN_EMAILS accounts that already exist
        finally:
            _db.close()
    finally:
        if lock_conn is not None:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _BOOT_LOCK_KEY})
            lock_conn.commit()
            lock_conn.close()
    yield


app = FastAPI(title=settings.app_name, version="0.17.0 (Phase 16 — student signup, email OTP, Google sign-in, sliding sessions)", lifespan=lifespan)

# Dev-only: allow any origin so a separately-hosted frontend can call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(catalog.router)
app.include_router(practice.router)
app.include_router(learn.router)
app.include_router(analysis.router)
app.include_router(psychometrics.router)
app.include_router(mocks.router)
app.include_router(diagnosis.router)
app.include_router(diagnosis.plan_router)
app.include_router(billing.router)
app.include_router(review.router)
app.include_router(review.honesty_router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(payments.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "phase": 1}


@app.get("/admin", include_in_schema=False)
def admin_portal():
    """The content admin portal (login + syllabus/item management). All data and actions behind it
    require an admin JWT, so opening this page does nothing without admin credentials."""
    path = os.path.join(_static_dir, "admin.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return RedirectResponse(url="/docs")


@app.get("/app", include_in_schema=False)
def connected_app():
    """The Vettalume dashboard, wired to the live backend (real concepts, questions, grading, mastery)."""
    path = os.path.join(_static_dir, "app.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return RedirectResponse(url="/docs")


@app.get("/verify", include_in_schema=False)
def verify_console():
    """Developer API verification console — exercises every endpoint across all phases."""
    path = os.path.join(_static_dir, "verify.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return RedirectResponse(url="/docs")


@app.get("/console", include_in_schema=False)
def console():
    """Live test console — the dashboard's look, driven entirely by the real backend."""
    path = os.path.join(_static_dir, "console.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return RedirectResponse(url="/docs")


@app.get("/chapter", include_in_schema=False)
def chapter_page():
    """Per-chapter analytics dashboard, driven by /analysis/chapter. Use ?exam=&topic= in the URL."""
    path = os.path.join(_static_dir, "chapter.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return RedirectResponse(url="/docs")


@app.get("/play", include_in_schema=False)
def play():
    """Visual Learning playground — a clickable test client for the /learn endpoints."""
    path = os.path.join(_static_dir, "play.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return RedirectResponse(url="/docs")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


# Serve the Vettalume dashboard prototype for reference (its Learning view is wired to real
# endpoints in Phase 1). Available at /dashboard once static/dashboard.html exists.
_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(_static_dir):
    app.mount("/dashboard", StaticFiles(directory=_static_dir, html=True), name="dashboard")
