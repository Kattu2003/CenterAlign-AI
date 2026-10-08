"""Test configuration.

Every test run uses throwaway directories for the sandbox data, shared drive,
runtime database and runs, so tests never touch a developer's real state.
These variables are set at import time, before any settings are read.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

REPO_COMPANY = Path(__file__).resolve().parent.parent / "company"
_TMP = Path(tempfile.mkdtemp(prefix="karya-tests-"))
os.environ["SANDBOX_DATA_DIR"] = str(_TMP / "sandbox_data")
os.environ["SANDBOX_SHARED_DRIVE"] = str(_TMP / "shared_drive")
os.environ["KARYA_DB_PATH"] = str(_TMP / "karya.db")
os.environ["KARYA_RUNS_DIR"] = str(_TMP / "runs")
os.environ.setdefault("KARYA_HEADLESS", "1")
os.environ["KARYA_COMPANY_DIR"] = str(REPO_COMPANY)
# Sandbox credentials: the .env.example values, so tests never depend on a developer's .env.
for _name, _value in {
    "SANDBOX_ADMIN_TOKEN": "change-me-admin",
    "ERP_AGENT_USER": "karya.agent",
    "ERP_AGENT_PASSWORD": "change-me-erp",
    "ERP_API_KEY": "change-me-erp-api",
    "MAIL_AGENT_USER": "karya.agent",
    "MAIL_AGENT_PASSWORD": "change-me-mail",
    "MAIL_API_KEY": "change-me-mail-api",
    "HR_AGENT_USER": "karya.agent",
    "HR_AGENT_PASSWORD": "change-me-hr",
    "HR_API_KEY": "change-me-hr-api",
}.items():
    os.environ[_name] = _value


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    return _TMP


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token in page"
    return match.group(1)


# --- live sandbox for browser integration tests ------------------------------------------------

import socket
import threading
import time

import yaml

LIVE_APPS = {"erp": "sandbox.erp.app", "mail": "sandbox.mail.app", "hr": "sandbox.hr.app"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def live_sandbox(tmp_root):
    """Sandbox apps served by uvicorn in background threads on free ports, plus a
    temporary company dir whose connectors point at them. Yields {app: base_url}."""
    import importlib

    import uvicorn

    from karya.config import reset_settings
    from sandbox.seed.seed import reset_all

    reset_all()
    urls, servers = {}, []
    for name, module in LIVE_APPS.items():
        if importlib.util.find_spec(module) is None:
            continue
        app = importlib.import_module(module).app
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            assert time.monotonic() < deadline, f"{name} did not start"
            time.sleep(0.05)
        urls[name] = f"http://127.0.0.1:{port}"
        servers.append((server, thread))

    company_dir = tmp_root / "company"
    if company_dir.exists():
        shutil.rmtree(company_dir)
    shutil.copytree(Path(__file__).resolve().parent.parent / "company", company_dir)
    for path in (company_dir / "connectors").glob("*.yaml"):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        if spec["id"] in urls:
            spec["base_url"] = urls[spec["id"]]
        if spec.get("kind") == "filesystem":
            spec["root"] = os.environ["SANDBOX_SHARED_DRIVE"]
        path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    os.environ["KARYA_COMPANY_DIR"] = str(company_dir)
    reset_settings()
    yield urls
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def fresh_sandbox(live_sandbox, tmp_path, monkeypatch):
    """Re-seed the sandbox data, clear chaos, and give the test its own runtime DB and runs dir
    (so a task left queued by one test can never be claimed by another)."""
    import importlib

    from karya.config import reset_settings
    from sandbox.seed.seed import reset_all

    monkeypatch.setenv("KARYA_DB_PATH", str(tmp_path / "karya.db"))
    monkeypatch.setenv("KARYA_RUNS_DIR", str(tmp_path / "runs"))
    reset_settings()
    base = reset_all()
    for module in LIVE_APPS.values():
        if importlib.util.find_spec(module) is not None:
            importlib.import_module(module).chaos.reset()
    return base
