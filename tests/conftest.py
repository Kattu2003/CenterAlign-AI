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

_TMP = Path(tempfile.mkdtemp(prefix="karya-tests-"))
os.environ["SANDBOX_DATA_DIR"] = str(_TMP / "sandbox_data")
os.environ["SANDBOX_SHARED_DRIVE"] = str(_TMP / "shared_drive")
os.environ["KARYA_DB_PATH"] = str(_TMP / "karya.db")
os.environ["KARYA_RUNS_DIR"] = str(_TMP / "runs")
os.environ.setdefault("KARYA_HEADLESS", "1")
os.environ["KARYA_COMPANY_DIR"] = str(Path(__file__).resolve().parent.parent / "company")
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
