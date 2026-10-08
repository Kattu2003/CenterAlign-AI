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
