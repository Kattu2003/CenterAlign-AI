"""Sandbox settings, read from the environment (and `.env`) at call time.

Functions rather than constants so tests can point the sandbox at a temporary
data directory by setting environment variables. The sandbox does not import
the `karya` runtime: it is the environment the AI employee works in.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)


def env(name: str, default: str = "") -> str:
    """Read an env var; values starting with '#' count as empty (python-dotenv quirk)."""
    value = os.environ.get(name, default).strip()
    return default if value.startswith("#") else value


def _path(name: str, default: str) -> Path:
    path = Path(env(name, default)).expanduser()
    return path if path.is_absolute() else ROOT / path


def data_dir() -> Path:
    """Sandbox databases, generated PDFs, logs and pid file (gitignored)."""
    path = _path("SANDBOX_DATA_DIR", "sandbox/data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def shared_drive_dir() -> Path:
    """The company shared drive the agent may read (and write to `outbox/`)."""
    path = _path("SANDBOX_SHARED_DRIVE", "sandbox/shared_drive")
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(app: str) -> Path:
    return data_dir() / f"{app}.db"


def pdf_dir() -> Path:
    path = data_dir() / "pdfs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def admin_token() -> str:
    return env("SANDBOX_ADMIN_TOKEN", "change-me-admin")


def agent_credentials(app: str) -> tuple[str, str]:
    """Service-account credentials for the AI employee, e.g. ERP_AGENT_USER / ERP_AGENT_PASSWORD."""
    prefix = app.upper()
    return env(f"{prefix}_AGENT_USER", "karya.agent"), env(f"{prefix}_AGENT_PASSWORD", f"change-me-{app}")


def api_key(app: str) -> str:
    return env(f"{app.upper()}_API_KEY", f"change-me-{app}-api")


def session_secret() -> str:
    return env("SANDBOX_SESSION_SECRET", "meridian-sandbox-session-" + admin_token())
