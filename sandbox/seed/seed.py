"""Seed the sandbox company: databases, invoice PDFs and shared drive files.

`reset_all()` is what `karya sandbox reset` runs. It recreates the sandbox
state from `data.yaml` relative to D = today (Asia/Kolkata). It never touches
the Karya runtime database or company memory.
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml

from sandbox.common import settings
from sandbox.common.auth import hash_password
from sandbox.common.dates import IST, resolve, today_ist
from sandbox.common.money import to_paise
from sandbox.seed.generate_pdfs import generate_all, pdf_filename, render_offer_letter

DATA_FILE = Path(__file__).with_name("data.yaml")

ERP_SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS vendors;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS audit;
CREATE TABLE users (username TEXT PRIMARY KEY, full_name TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE vendors (
    id TEXT PRIMARY KEY, legal_name TEXT NOT NULL, gstin TEXT, category TEXT,
    status TEXT NOT NULL DEFAULT 'active', bank_account TEXT, ifsc TEXT, phone TEXT, created_at TEXT
);
-- Deliberately NO unique constraint on (vendor_id, invoice_number): a legacy system that
-- allows duplicates, so the AI employee and the gateway must prevent them.
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY, erp_ref TEXT, vendor_id TEXT NOT NULL, invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL, due_date TEXT NOT NULL, amount_paise INTEGER NOT NULL,
    po_number TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'Posted', entered_by TEXT, entered_at TEXT
);
CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, details TEXT);
"""

MAIL_SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS attachments;
DROP TABLE IF EXISTS audit;
CREATE TABLE users (username TEXT PRIMARY KEY, full_name TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL, from_name TEXT NOT NULL, from_email TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, labels TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL, content_type TEXT NOT NULL
);
CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, details TEXT);
"""


HR_SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS departments;
DROP TABLE IF EXISTS employees;
DROP TABLE IF EXISTS access_requests;
DROP TABLE IF EXISTS equipment_requests;
DROP TABLE IF EXISTS audit;
CREATE TABLE users (username TEXT PRIMARY KEY, full_name TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE departments (name TEXT PRIMARY KEY);
CREATE TABLE employees (
    id TEXT PRIMARY KEY, full_name TEXT NOT NULL, work_email TEXT NOT NULL, department TEXT, title TEXT,
    start_date TEXT, manager_id TEXT, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY, employee_id TEXT NOT NULL, system TEXT NOT NULL, level TEXT NOT NULL,
    justification TEXT, status TEXT NOT NULL DEFAULT 'Requested', requested_by TEXT, created_at TEXT
);
CREATE TABLE equipment_requests (
    id INTEGER PRIMARY KEY, employee_id TEXT NOT NULL, item TEXT NOT NULL, notes TEXT, requested_by TEXT, created_at TEXT
);
CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT, action TEXT NOT NULL, details TEXT);
"""


def load_data() -> dict[str, Any]:
    return yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))


def _connect(app: str) -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path(app), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _stamp(token: str, base: date, at: str = "10:00") -> str:
    """'D-3' + '09:41' -> ISO timestamp in IST."""
    hours, minutes = (int(x) for x in at.split(":"))
    return datetime.combine(resolve(token, base), time(hours, minutes), tzinfo=IST).isoformat()


def _seed_users(conn: sqlite3.Connection, app: str, data: dict) -> None:
    agent_user, agent_password = settings.agent_credentials(app)
    conn.execute(
        "INSERT INTO users(username, full_name, password_hash) VALUES (?, ?, ?)",
        (agent_user, "Karya (AI employee service account)", hash_password(agent_password)),
    )
    for user in data["users"]:
        conn.execute(
            "INSERT INTO users(username, full_name, password_hash) VALUES (?, ?, ?)",
            (user["username"], user["full_name"], hash_password(str(user["password"]))),
        )


def seed_erp(data: dict, base: date) -> None:
    conn = _connect("erp")
    try:
        conn.executescript(ERP_SCHEMA)
        _seed_users(conn, "erp", data)
        for v in data["vendors"]:
            conn.execute(
                "INSERT INTO vendors(id, legal_name, gstin, category, status, bank_account, ifsc, phone, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (v["id"], v["legal_name"], v["gstin"], v["category"], str(v["bank_account"]), v["ifsc"], v["phone"], _stamp("D-400", base)),
            )
        for inv in data["erp_invoices"]:
            cursor = conn.execute(
                "INSERT INTO invoices(vendor_id, invoice_number, invoice_date, due_date, amount_paise, po_number, notes, status, entered_by, entered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'Posted', 'arjun.rao', ?)",
                (
                    inv["vendor_id"],
                    inv["invoice_number"],
                    resolve(inv["invoice_date"], base).isoformat(),
                    resolve(inv["due_date"], base).isoformat(),
                    to_paise(inv["amount"]),
                    inv.get("po_number"),
                    inv.get("notes"),
                    _stamp(inv["entered_at"], base, "15:30"),
                ),
            )
            conn.execute("UPDATE invoices SET erp_ref = ? WHERE id = ?", (f"ERP-INV-{100 + cursor.lastrowid:06d}", cursor.lastrowid))
        conn.commit()
    finally:
        conn.close()


def seed_mail(data: dict, base: date, pdf_dir: Path) -> None:
    conn = _connect("mail")
    try:
        conn.executescript(MAIL_SCHEMA)
        _seed_users(conn, "mail", data)
        for msg in data["messages"]:  # seeded in key order: M1 -> id 1
            token, _, at = str(msg["received"]).partition(" ")
            cursor = conn.execute(
                "INSERT INTO messages(key, from_name, from_email, subject, body, received_at, labels) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg["key"], msg["from_name"], msg["from_email"], msg["subject"], msg["body"].strip(), _stamp(token, base, at or "10:00"), json.dumps(msg.get("labels") or [])),
            )
            if msg.get("attachment"):
                filename = pdf_filename(msg["attachment"])
                conn.execute(
                    "INSERT INTO attachments(message_id, filename, path, content_type) VALUES (?, ?, ?, 'application/pdf')",
                    (cursor.lastrowid, filename, str(pdf_dir / filename)),
                )
        conn.commit()
    finally:
        conn.close()


def work_email(full_name: str) -> str:
    parts = full_name.lower().split()
    return f"{parts[0]}.{parts[-1]}@meridian.example"


def seed_hr(data: dict, base: date) -> None:
    hr = data["hr"]
    conn = _connect("hr")
    try:
        conn.executescript(HR_SCHEMA)
        _seed_users(conn, "hr", {"users": data["users"] + hr["users"]})
        conn.executemany("INSERT INTO departments(name) VALUES (?)", [(d,) for d in hr["departments"]])
        for e in hr["employees"]:
            conn.execute(
                "INSERT INTO employees(id, full_name, work_email, department, title, start_date, manager_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                (e["id"], e["full_name"], work_email(e["full_name"]), e["department"], e["title"], resolve(e["start"], base).isoformat(), e.get("manager")),
            )
        conn.commit()
    finally:
        conn.close()


def build_shared_drive(data: dict, base: date) -> Path:
    """Recreate the generated shared-drive content (finance reference files, HR offer letters, empty outbox)."""
    root = settings.shared_drive_dir()
    for name in ("finance", "outbox", "hr"):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    with (root / "finance" / "vendor_list.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["vendor_id", "legal_name", "gstin", "category", "status"])
        for v in data["vendors"]:
            writer.writerow([v["id"], v["legal_name"], v["gstin"], v["category"], "active"])
    (root / "outbox" / "README.txt").write_text(
        "Files placed here are picked up by the finance team each evening.\n", encoding="utf-8"
    )
    for offer in data.get("hr", {}).get("offer_letters", []):
        render_offer_letter(offer, data["buyer"], base, root / "hr" / "offers")
    return root


def write_meta(base: date) -> None:
    meta = {"D": base.isoformat(), "reset_at": datetime.now(IST).isoformat(timespec="seconds")}
    (settings.data_dir() / "seed_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_meta() -> dict:
    path = settings.data_dir() / "seed_meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def reset_app(app: str) -> str:
    """Re-seed one app (used by POST /__admin/reset). Uses the D of the last full reset."""
    data = load_data()
    base = date.fromisoformat(read_meta().get("D", today_ist().isoformat()))
    if app == "erp":
        seed_erp(data, base)
    elif app == "mail":
        generate_all(data, base, settings.pdf_dir())
        seed_mail(data, base, settings.pdf_dir())
    elif app == "hr":
        seed_hr(data, base)
    else:
        raise ValueError(f"unknown sandbox app {app!r}")
    return base.isoformat()


def reset_all() -> date:
    """Full reset: PDFs, every app database, shared drive, seed metadata."""
    data = load_data()
    base = today_ist()
    generate_all(data, base, settings.pdf_dir())
    seed_erp(data, base)
    seed_mail(data, base, settings.pdf_dir())
    seed_hr(data, base)
    build_shared_drive(data, base)
    write_meta(base)
    return base


if __name__ == "__main__":
    print(f"Sandbox reset; D = {reset_all().isoformat()}")
