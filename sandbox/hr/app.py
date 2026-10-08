"""Meridian People (port 8103): the HR system of record.

Employee directory, new employee records, system-access requests and equipment
requests. Same conventions as the other sandbox apps (plan §7.2).
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

APP = "hr"
TEMPLATES = Path(__file__).parent / "templates"
SYSTEMS = ("erp", "mail", "hr", "github")
LEVELS = ("viewer", "editor", "admin")
ITEMS = ("standard_laptop", "developer_laptop", "monitor")
EMAIL = re.compile(r"^[a-z0-9._-]+@[a-z0-9.-]+\.[a-z]{2,}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _reset() -> str:
    from sandbox.seed.seed import reset_app

    return reset_app(APP)


def _state() -> dict:
    with opened(APP) as conn:
        return {"employees": conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]}


app, templates, chaos = create_app(
    APP,
    "Meridian People",
    TEMPLATES,
    write_paths=["/employees", "/employees/*/access-requests", "/employees/*/equipment"],
    form_paths=["/employees/new", "/employees/*"],
    nav=[("/", "Dashboard"), ("/employees", "Employees"), ("/employees/new", "New employee")],
    reset_fn=_reset,
    state_fn=_state,
)

EMPLOYEE_SELECT = "SELECT e.*, m.full_name AS manager_name FROM employees e LEFT JOIN employees m ON m.id = e.manager_id"


def _departments(conn) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM departments ORDER BY name")]


def _employee_json(row) -> dict:
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "work_email": row["work_email"],
        "department": row["department"],
        "title": row["title"],
        "start_date": row["start_date"],
        "manager_id": row["manager_id"],
        "manager_name": row["manager_name"],
        "status": row["status"],
    }


def _search(conn, q: str) -> list:
    if not q:
        return conn.execute(EMPLOYEE_SELECT + " ORDER BY e.id").fetchall()
    like = f"%{q.strip().lower()}%"
    return conn.execute(
        EMPLOYEE_SELECT + " WHERE lower(e.id) LIKE ? OR lower(e.full_name) LIKE ? OR lower(e.work_email) LIKE ? ORDER BY e.id",
        (like, like, like),
    ).fetchall()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    require_user(request)
    with opened(APP) as conn:
        count = conn.execute("SELECT COUNT(*) FROM employees WHERE status = 'active'").fetchone()[0]
        joiners = conn.execute(EMPLOYEE_SELECT + " WHERE e.start_date >= date('now') ORDER BY e.start_date").fetchall()
    return templates.TemplateResponse(request, "dashboard.html", {"count": count, "joiners": joiners})


@app.get("/employees", response_class=HTMLResponse)
def employees(request: Request, q: str = ""):
    require_user(request)
    with opened(APP) as conn:
        rows = _search(conn, q)
    return templates.TemplateResponse(request, "employees.html", {"employees": rows, "q": q})


def _new_context(conn, values: dict, errors: list[str]) -> dict:
    managers = conn.execute("SELECT id, full_name FROM employees WHERE status = 'active' ORDER BY full_name").fetchall()
    return {"values": values, "errors": errors, "departments": _departments(conn), "managers": managers}


@app.get("/employees/new", response_class=HTMLResponse)
def employee_new(request: Request):
    require_user(request)
    with opened(APP) as conn:
        context = _new_context(conn, {}, [])
    return templates.TemplateResponse(request, "employee_new.html", context)


@app.post("/employees")
async def employee_create(request: Request):
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    keys = ("full_name", "work_email", "department", "title", "start_date", "manager_id")
    values = {k: str(form.get(k) or "").strip() for k in keys}
    values["work_email"] = values["work_email"].lower()
    with opened(APP) as conn:
        errors = [f"{label} is required." for key, label in (("full_name", "Full name"), ("work_email", "Work email"), ("department", "Department"), ("title", "Title"), ("start_date", "Start date"), ("manager_id", "Manager")) if not values[key]]
        if values["work_email"] and not EMAIL.match(values["work_email"]):
            errors.append("Work email is not a valid address.")
        if values["start_date"]:
            try:
                if not ISO_DATE.match(values["start_date"]):
                    raise ValueError
                date.fromisoformat(values["start_date"])
            except ValueError:
                errors.append("Start date must be a date in YYYY-MM-DD format.")
        if values["department"] and values["department"] not in _departments(conn):
            errors.append("Choose a department from the list.")
        if values["manager_id"] and conn.execute("SELECT 1 FROM employees WHERE id = ?", (values["manager_id"],)).fetchone() is None:
            errors.append("Choose a manager from the list.")
        existing = conn.execute("SELECT id FROM employees WHERE work_email = ?", (values["work_email"],)).fetchone()
        if existing:
            errors.append(f"An employee with this work email already exists ({existing['id']}).")
        if errors:
            return templates.TemplateResponse(request, "employee_new.html", _new_context(conn, values, errors), status_code=400)
        count = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        employee_id = f"E{count + 1:03d}"
        conn.execute(
            "INSERT INTO employees(id, full_name, work_email, department, title, start_date, manager_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (employee_id, values["full_name"], values["work_email"], values["department"], values["title"], values["start_date"], values["manager_id"]),
        )
        audit(conn, user, "create_employee", {"employee_id": employee_id, **values})
    return RedirectResponse(f"/employees/{employee_id}?created=1", status_code=303)


def _detail_context(conn, employee_id: str, banner: str, errors: list[str]) -> dict:
    employee = conn.execute(EMPLOYEE_SELECT + " WHERE e.id = ?", (employee_id,)).fetchone()
    if employee is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found.")
    access = conn.execute("SELECT * FROM access_requests WHERE employee_id = ? ORDER BY id", (employee_id,)).fetchall()
    equipment = conn.execute("SELECT * FROM equipment_requests WHERE employee_id = ? ORDER BY id", (employee_id,)).fetchall()
    return {"e": employee, "access": access, "equipment": equipment, "banner": banner, "errors": errors, "systems": SYSTEMS, "levels": LEVELS, "items": ITEMS}


@app.get("/employees/{employee_id}", response_class=HTMLResponse)
def employee_detail(request: Request, employee_id: str, created: int = 0, done: str = ""):
    require_user(request)
    banner = f"Employee created — {employee_id}" if created else done
    with opened(APP) as conn:
        context = _detail_context(conn, employee_id, banner, [])
    return templates.TemplateResponse(request, "employee_detail.html", context)


@app.post("/employees/{employee_id}/access-requests")
async def access_request(request: Request, employee_id: str):
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    system = str(form.get("system") or "").strip()
    level = str(form.get("level") or "").strip()
    justification = str(form.get("justification") or "").strip()
    with opened(APP) as conn:
        errors = []
        if system not in SYSTEMS:
            errors.append(f"Choose a system: {', '.join(SYSTEMS)}.")
        if level not in LEVELS:
            errors.append(f"Choose a level: {', '.join(LEVELS)}.")
        if not justification:
            errors.append("A business justification is required.")
        if errors:
            return templates.TemplateResponse(request, "employee_detail.html", _detail_context(conn, employee_id, "", errors), status_code=400)
        _detail_context(conn, employee_id, "", [])  # 404 if the employee does not exist
        cursor = conn.execute(
            "INSERT INTO access_requests(employee_id, system, level, justification, status, requested_by, created_at) VALUES (?, ?, ?, ?, 'Requested', ?, ?)",
            (employee_id, system, level, justification, user, now_iso()),
        )
        audit(conn, user, "request_access", {"employee_id": employee_id, "system": system, "level": level, "request_id": cursor.lastrowid})
    return RedirectResponse(f"/employees/{employee_id}?done=Access request submitted: {system} {level}", status_code=303)


@app.post("/employees/{employee_id}/equipment")
async def equipment_request(request: Request, employee_id: str):
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    item = str(form.get("item") or "").strip()
    notes = str(form.get("notes") or "").strip()
    with opened(APP) as conn:
        if item not in ITEMS:
            return templates.TemplateResponse(request, "employee_detail.html", _detail_context(conn, employee_id, "", [f"Choose an item: {', '.join(ITEMS)}."]), status_code=400)
        _detail_context(conn, employee_id, "", [])
        cursor = conn.execute(
            "INSERT INTO equipment_requests(employee_id, item, notes, requested_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (employee_id, item, notes, user, now_iso()),
        )
        audit(conn, user, "request_equipment", {"employee_id": employee_id, "item": item, "request_id": cursor.lastrowid})
    return RedirectResponse(f"/employees/{employee_id}?done=Equipment requested: {item}", status_code=303)


# --- read-only API ---------------------------------------------------------------------------------


@app.get("/api/employees")
def api_employees(request: Request, q: str = ""):
    require_api_key(request, APP)
    with opened(APP) as conn:
        return [_employee_json(r) for r in _search(conn, q)]


@app.get("/api/employees/{employee_id}")
def api_employee(request: Request, employee_id: str):
    require_api_key(request, APP)
    with opened(APP) as conn:
        row = conn.execute(EMPLOYEE_SELECT + " WHERE e.id = ?", (employee_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"employee {employee_id} not found")
    return _employee_json(row)


@app.get("/api/access-requests")
def api_access_requests(request: Request, employee_id: str = ""):
    require_api_key(request, APP)
    with opened(APP) as conn:
        rows = conn.execute("SELECT * FROM access_requests WHERE (? = '' OR employee_id = ?) ORDER BY id", (employee_id, employee_id)).fetchall()
    return [{"id": r["id"], "employee_id": r["employee_id"], "system": r["system"], "level": r["level"], "justification": r["justification"], "status": r["status"], "requested_by": r["requested_by"], "created_at": r["created_at"]} for r in rows]


@app.get("/api/equipment-requests")
def api_equipment_requests(request: Request, employee_id: str = ""):
    require_api_key(request, APP)
    with opened(APP) as conn:
        rows = conn.execute("SELECT * FROM equipment_requests WHERE (? = '' OR employee_id = ?) ORDER BY id", (employee_id, employee_id)).fetchall()
    return [{"id": r["id"], "employee_id": r["employee_id"], "item": r["item"], "notes": r["notes"], "requested_by": r["requested_by"], "created_at": r["created_at"]} for r in rows]
