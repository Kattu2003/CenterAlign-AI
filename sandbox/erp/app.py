"""Meridian ERP (port 8101): the finance system of record.

A deliberately "legacy" app: it has NO uniqueness constraint on
(vendor_id, invoice_number), so it happily accepts duplicate invoices. Preventing
duplicates is the AI employee's job (and the gateway's backstop).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sandbox.common.app_factory import create_app, form_error, require_api_key
from sandbox.common.auth import csrf_ok, require_user
from sandbox.common.db import audit, now_iso, opened
from sandbox.common.money import paise_to_str, to_paise

APP = "erp"
TEMPLATES = Path(__file__).parent / "templates"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def erp_ref(invoice_id: int) -> str:
    """ERP refs are ERP-INV-{100 + id:06d}: seeded rows are 000101-000103."""
    return f"ERP-INV-{100 + invoice_id:06d}"


def _reset() -> str:
    from sandbox.seed.seed import reset_app

    return reset_app(APP)


def _state() -> dict:
    with opened(APP) as conn:
        return {
            "vendors": conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0],
            "invoices": conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
        }


app, templates, chaos = create_app(
    APP,
    "Meridian ERP",
    TEMPLATES,
    write_paths=["/invoices"],
    form_paths=["/invoices/new", "/vendors/new", "/vendors/*"],
    nav=[("/", "Dashboard"), ("/vendors", "Vendors"), ("/invoices", "Invoices"), ("/invoices/new", "New invoice")],
    reset_fn=_reset,
    state_fn=_state,
)

INVOICE_SELECT = """
SELECT i.*, v.legal_name AS vendor_name
FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id
"""


def _invoice_filters(vendor_id: str, invoice_number: str, q: str) -> tuple[str, list]:
    clauses, params = [], []
    if vendor_id:
        clauses.append("i.vendor_id = ?")
        params.append(vendor_id.strip())
    if invoice_number:
        clauses.append("lower(trim(i.invoice_number)) = lower(trim(?))")
        params.append(invoice_number)
    if q:
        like = f"%{q.strip().lower()}%"
        clauses.append(
            "(lower(i.invoice_number) LIKE ? OR lower(i.erp_ref) LIKE ? OR lower(v.legal_name) LIKE ? OR lower(coalesce(i.notes,'')) LIKE ?)"
        )
        params += [like, like, like, like]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _invoice_json(row) -> dict:
    return {
        "id": row["id"],
        "erp_ref": row["erp_ref"],
        "vendor_id": row["vendor_id"],
        "vendor_name": row["vendor_name"],
        "invoice_number": row["invoice_number"],
        "invoice_date": row["invoice_date"],
        "due_date": row["due_date"],
        "amount": paise_to_str(row["amount_paise"]),
        "po_number": row["po_number"],
        "notes": row["notes"],
        "status": row["status"],
        "entered_by": row["entered_by"],
        "entered_at": row["entered_at"],
    }


def _vendor_json(row) -> dict:
    account = row["bank_account"] or ""
    return {
        "id": row["id"],
        "legal_name": row["legal_name"],
        "gstin": row["gstin"],
        "category": row["category"],
        "status": row["status"],
        "phone": row["phone"],
        "bank_account_last4": account[-4:] if account else None,
        "ifsc": row["ifsc"],
    }


def mask_account(account: str | None) -> str:
    return ("XXXXXX" + account[-4:]) if account else "—"


# --- pages -------------------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    require_user(request)
    with opened(APP) as conn:
        counts = {
            "vendors": conn.execute("SELECT COUNT(*) FROM vendors WHERE status='active'").fetchone()[0],
            "invoices": conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
        }
    return templates.TemplateResponse(request, "dashboard.html", {"counts": counts})


@app.get("/vendors", response_class=HTMLResponse)
def vendors(request: Request, q: str = ""):
    require_user(request)
    with opened(APP) as conn:
        if q:
            like = f"%{q.strip().lower()}%"
            rows = conn.execute(
                "SELECT * FROM vendors WHERE lower(id) LIKE ? OR lower(legal_name) LIKE ? OR lower(coalesce(gstin,'')) LIKE ? ORDER BY id",
                (like, like, like),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM vendors ORDER BY id").fetchall()
    return templates.TemplateResponse(request, "vendors.html", {"vendors": rows, "q": q})


@app.get("/vendors/new", response_class=HTMLResponse)
def vendor_new(request: Request):
    require_user(request)
    return templates.TemplateResponse(request, "vendor_new.html", {"values": {}, "errors": []})


@app.post("/vendors")
async def vendor_create(request: Request):
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    fields = ("legal_name", "gstin", "category", "phone", "bank_account", "ifsc")
    values = {k: str(form.get(k) or "").strip() for k in fields}
    errors = []
    if not values["legal_name"]:
        errors.append("Legal name is required.")
    if values["ifsc"] and not IFSC.match(values["ifsc"]):
        errors.append("IFSC must look like HDFC0001234.")
    if errors:
        return templates.TemplateResponse(request, "vendor_new.html", {"values": values, "errors": errors}, status_code=400)
    with opened(APP) as conn:
        count = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
        vendor_id = f"V{count + 1:03d}"
        conn.execute(
            "INSERT INTO vendors(id, legal_name, gstin, category, status, bank_account, ifsc, phone, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (vendor_id, values["legal_name"], values["gstin"], values["category"], values["bank_account"], values["ifsc"], values["phone"], now_iso()),
        )
        audit(conn, user, "create_vendor", {"vendor_id": vendor_id, **values})
    return RedirectResponse(f"/vendors/{vendor_id}?created=1", status_code=303)


@app.get("/vendors/{vendor_id}", response_class=HTMLResponse)
def vendor_detail(request: Request, vendor_id: str, updated: int = 0, created: int = 0):
    require_user(request)
    with opened(APP) as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if vendor is None:
        raise HTTPException(status_code=404, detail=f"Vendor {vendor_id} not found.")
    return templates.TemplateResponse(
        request,
        "vendor_detail.html",
        {"vendor": vendor, "masked": mask_account(vendor["bank_account"]), "updated": updated, "created": created, "errors": []},
    )


@app.post("/vendors/{vendor_id}/bank")
async def vendor_bank(request: Request, vendor_id: str):
    """Exists so policy AP-002 ("never change bank details") is demonstrable."""
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    account = re.sub(r"\s", "", str(form.get("account_number") or ""))
    ifsc = str(form.get("ifsc") or "").strip().upper()
    reason = str(form.get("reason") or "").strip()
    with opened(APP) as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if vendor is None:
            raise HTTPException(status_code=404, detail=f"Vendor {vendor_id} not found.")
        errors = []
        if not re.fullmatch(r"\d{9,18}", account):
            errors.append("Account number must be 9-18 digits.")
        if not IFSC.match(ifsc):
            errors.append("IFSC must look like HDFC0001234.")
        if not reason:
            errors.append("A reason is required.")
        if errors:
            return templates.TemplateResponse(
                request,
                "vendor_detail.html",
                {"vendor": vendor, "masked": mask_account(vendor["bank_account"]), "updated": 0, "created": 0, "errors": errors},
                status_code=400,
            )
        conn.execute("UPDATE vendors SET bank_account = ?, ifsc = ? WHERE id = ?", (account, ifsc, vendor_id))
        audit(conn, user, "update_vendor_bank", {"vendor_id": vendor_id, "account_last4": account[-4:], "ifsc": ifsc, "reason": reason})
    return RedirectResponse(f"/vendors/{vendor_id}?updated=1", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices(request: Request, vendor_id: str = "", invoice_number: str = "", q: str = ""):
    require_user(request)
    where, params = _invoice_filters(vendor_id, invoice_number, q)
    with opened(APP) as conn:
        rows = conn.execute(INVOICE_SELECT + where + " ORDER BY i.id DESC", params).fetchall()
        vendor_rows = conn.execute("SELECT id, legal_name FROM vendors ORDER BY legal_name").fetchall()
    return templates.TemplateResponse(
        request,
        "invoices.html",
        {"invoices": rows, "vendors": vendor_rows, "vendor_id": vendor_id, "invoice_number": invoice_number, "q": q},
    )


def _new_invoice_context(conn, values: dict, errors: list[str]) -> dict:
    vendors_rows = conn.execute("SELECT id, legal_name FROM vendors WHERE status = 'active' ORDER BY legal_name").fetchall()
    recent = conn.execute(INVOICE_SELECT + " ORDER BY i.id DESC LIMIT 5").fetchall()
    return {"vendors": vendors_rows, "recent": recent, "values": values, "errors": errors}


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request):
    require_user(request)
    with opened(APP) as conn:
        context = _new_invoice_context(conn, {}, [])
    return templates.TemplateResponse(request, "invoice_new.html", context)


def _validate_invoice(conn, values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for key, label in (("vendor_id", "Vendor"), ("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"), ("due_date", "Due date"), ("amount", "Amount")):
        if not values.get(key):
            errors.append(f"{label} is required.")
    if errors:
        return errors
    vendor = conn.execute("SELECT status FROM vendors WHERE id = ?", (values["vendor_id"],)).fetchone()
    if vendor is None:
        errors.append(f"Vendor {values['vendor_id']} does not exist.")
    elif vendor["status"] != "active":
        errors.append(f"Vendor {values['vendor_id']} is not active.")
    dates: dict[str, date] = {}
    for key, label in (("invoice_date", "Invoice date"), ("due_date", "Due date")):
        raw = values[key]
        try:
            if not ISO_DATE.match(raw):
                raise ValueError
            dates[key] = date.fromisoformat(raw)
        except ValueError:
            errors.append(f"{label} must be a date in YYYY-MM-DD format.")
    if len(dates) == 2 and dates["due_date"] < dates["invoice_date"]:
        errors.append("Due date cannot be before the invoice date.")
    try:
        if to_paise(values["amount"]) <= 0:
            errors.append("Amount must be greater than zero.")
    except ValueError:
        errors.append("Amount must be a number with at most two decimals (e.g. 42380.00).")
    if len(values["invoice_number"]) > 50:
        errors.append("Invoice number is too long.")
    return errors


@app.post("/invoices")
async def invoice_create(request: Request):
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    keys = ("vendor_id", "invoice_number", "invoice_date", "due_date", "amount", "po_number", "notes")
    values = {k: str(form.get(k) or "").strip() for k in keys}
    with opened(APP) as conn:
        errors = _validate_invoice(conn, values)
        if errors:
            return templates.TemplateResponse(
                request, "invoice_new.html", _new_invoice_context(conn, values, errors), status_code=400
            )
        cursor = conn.execute(
            "INSERT INTO invoices(vendor_id, invoice_number, invoice_date, due_date, amount_paise, po_number, notes, status, entered_by, entered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'Posted', ?, ?)",
            (
                values["vendor_id"],
                values["invoice_number"],
                values["invoice_date"],
                values["due_date"],
                to_paise(values["amount"]),
                values["po_number"] or None,
                values["notes"] or None,
                user,
                now_iso(),
            ),
        )
        invoice_id = cursor.lastrowid
        ref = erp_ref(invoice_id)
        conn.execute("UPDATE invoices SET erp_ref = ? WHERE id = ?", (ref, invoice_id))
        audit(conn, user, "create_invoice", {"erp_ref": ref, **values})
    return RedirectResponse(f"/invoices/{invoice_id}?posted=1", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int, posted: int = 0):
    require_user(request)
    with opened(APP) as conn:
        row = conn.execute(INVOICE_SELECT + " WHERE i.id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found.")
    return templates.TemplateResponse(request, "invoice_detail.html", {"inv": row, "posted": posted})


# --- read-only API -------------------------------------------------------------------------------


@app.get("/api/vendors")
def api_vendors(request: Request, q: str = ""):
    require_api_key(request, APP)
    with opened(APP) as conn:
        like = f"%{q.strip().lower()}%"
        rows = conn.execute(
            "SELECT * FROM vendors WHERE lower(id) LIKE ? OR lower(legal_name) LIKE ? OR lower(coalesce(gstin,'')) LIKE ? ORDER BY id",
            (like, like, like),
        ).fetchall()
    return [_vendor_json(r) for r in rows]


@app.get("/api/vendors/{vendor_id}")
def api_vendor(request: Request, vendor_id: str):
    require_api_key(request, APP)
    with opened(APP) as conn:
        row = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"vendor {vendor_id} not found")
    return _vendor_json(row)


@app.get("/api/invoices")
def api_invoices(request: Request, vendor_id: str = "", invoice_number: str = "", q: str = ""):
    require_api_key(request, APP)
    where, params = _invoice_filters(vendor_id, invoice_number, q)
    with opened(APP) as conn:
        rows = conn.execute(INVOICE_SELECT + where + " ORDER BY i.id DESC", params).fetchall()
    return [_invoice_json(r) for r in rows]


@app.get("/api/invoices/{invoice_id}")
def api_invoice(request: Request, invoice_id: str):
    """Accepts the numeric id or the ERP reference (ERP-INV-000104)."""
    require_api_key(request, APP)
    with opened(APP) as conn:
        if invoice_id.isdigit():
            row = conn.execute(INVOICE_SELECT + " WHERE i.id = ?", (int(invoice_id),)).fetchone()
        else:
            row = conn.execute(INVOICE_SELECT + " WHERE upper(i.erp_ref) = upper(?)", (invoice_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found")
    return _invoice_json(row)
