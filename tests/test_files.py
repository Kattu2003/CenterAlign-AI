"""File sandbox (plan §12): no traversal, no absolute paths, no symlink escape,
writes only where allowed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from karya import db
from karya.company import load_company
from karya.config import get_settings
from karya.gateway.gateway import Gateway
from karya.gateway.secrets import Secrets
from karya.llm.base import ToolCall
from karya.tools.base import ToolSession
from karya.tools.files import FileAccessError, resolve
from tests.conftest import REPO_COMPANY


@pytest.fixture()
def session(tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    (drive / "finance").mkdir(parents=True)
    (drive / "outbox").mkdir()
    (drive / "finance" / "vendor_list.csv").write_text("vendor_id,legal_name\nV001,Acme Industries Pvt Ltd\n")
    company = load_company(REPO_COMPANY)
    company.connectors["shared_drive"].root = str(drive)
    conn = db.connect(tmp_path / "f.db")
    gateway = Gateway("T-1", company, conn, Secrets(company.connectors))
    s = ToolSession("T-1", company, conn, None, gateway, run_dir=tmp_path / "run")
    yield s
    conn.close()


def call(session, name, **args):
    return session.execute(ToolCall("c", name, {"why": "test", **args}), step=1)


@pytest.mark.parametrize("bad", ["../etc/passwd", "shared_drive/../../secret", "/etc/passwd", "C:/Windows/win.ini", "~/x", "nowhere/x.txt", ""])
def test_rejected_paths(session, bad):
    with pytest.raises(FileAccessError):
        resolve(session, bad)


def test_symlink_escape_rejected(session, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = Path(session.company.connectors["shared_drive"].root) / "finance" / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(FileAccessError, match="escapes"):
        resolve(session, "shared_drive/finance/link.txt")


def test_list_and_read(session):
    roots = call(session, "files_list", path="")
    assert "shared_drive/" in roots.content and "downloads/" in roots.content
    listing = call(session, "files_list", path="shared_drive/finance")
    assert "vendor_list.csv" in listing.content
    read = call(session, "files_read", path="shared_drive/finance/vendor_list.csv")
    assert read.ok and "Acme Industries" in read.content and read.untrusted


def test_writes_only_where_allowed(session):
    ok = call(session, "files_write", path="workspace/notes.md", content="hello")
    assert ok.ok and (session.run_dir / "workspace" / "notes.md").read_text() == "hello"
    assert call(session, "files_write", path="shared_drive/outbox/summary.csv", content="a,b").ok
    denied = call(session, "files_write", path="shared_drive/finance/vendor_list.csv", content="tampered")
    assert not denied.ok and denied.policy_events[0].type == "denied"
    assert "tampered" not in (Path(session.company.connectors["shared_drive"].root) / "finance" / "vendor_list.csv").read_text()
    assert not call(session, "files_write", path="downloads/../../evil.txt", content="x").ok


def test_unknown_tool_and_bad_arguments(session):
    assert not call(session, "rm_rf", path="/").ok
    missing_why = session.execute(ToolCall("c", "files_list", {"path": ""}), step=1)
    assert not missing_why.ok and "why" in missing_why.error


def test_browser_window_setting_is_parsed_strictly():
    from karya.tools.browser import parse_window

    assert parse_window("640,0,1280,1040") == (640, 0, 1280, 1040)
    assert parse_window(" 512, 0, 1024, 860 ") == (512, 0, 1024, 860)
    for bad in ("", "640,0,1280", "a,b,c,d", "0,0,0,900"):
        assert parse_window(bad) is None
