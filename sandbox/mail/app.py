"""Meridian Mail (port 8102): the accounts-payable shared mailbox.

Vendors email invoices here as PDF attachments. Staff (and the AI employee)
label each message: Processed, Needs review or Duplicate.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from sandbox.common.app_factory import create_app, form_error, require_api_key
from sandbox.common.auth import csrf_ok, require_user
from sandbox.common.db import audit, opened

APP = "mail"
TEMPLATES = Path(__file__).parent / "templates"
LABELS = ("Processed", "Needs review", "Duplicate")


def _reset() -> str:
    from sandbox.seed.seed import reset_app

    return reset_app(APP)


def _state() -> dict:
    with opened(APP) as conn:
        return {"messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]}


app, templates, chaos = create_app(
    APP,
    "Meridian Mail — AP mailbox",
    TEMPLATES,
    write_paths=[],  # commit_then_504 / write_conflict_409 target ERP invoice posting only (plan §7.7)
    form_paths=["/messages/*"],
    nav=[("/inbox", "Inbox")],
    reset_fn=_reset,
    state_fn=_state,
)


def _when(value: str) -> str:
    """'2026-10-05T09:41:00+05:30' -> '2026-10-05 09:41' for display (the API keeps ISO)."""
    return value[:16].replace("T", " ") if value else ""


templates.env.filters["when"] = _when


def _labels(row) -> list[str]:
    return json.loads(row["labels"] or "[]")


def _attachments(conn, message_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, filename, content_type FROM attachments WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()
    return [{"id": r["id"], "filename": r["filename"], "content_type": r["content_type"], "url": f"/attachments/{r['id']}"} for r in rows]


def _search(conn, q: str, label: str, unlabelled: bool) -> list:
    clauses, params = [], []
    if q:
        like = f"%{q.strip().lower()}%"
        clauses.append("(lower(from_name) LIKE ? OR lower(from_email) LIKE ? OR lower(subject) LIKE ? OR lower(body) LIKE ?)")
        params += [like, like, like, like]
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute("SELECT * FROM messages" + where + " ORDER BY received_at DESC, id DESC", params).fetchall()
    if label:
        rows = [r for r in rows if label in _labels(r)]
    if unlabelled:
        rows = [r for r in rows if not _labels(r)]
    return rows


@app.get("/")
def home(request: Request):
    require_user(request)
    return RedirectResponse("/inbox", status_code=303)


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, q: str = "", label: str = ""):
    require_user(request)
    unlabelled = label == "__none__"
    with opened(APP) as conn:
        rows = _search(conn, q, "" if unlabelled else label, unlabelled)
        with_pdf = {r["message_id"] for r in conn.execute("SELECT DISTINCT message_id FROM attachments")}
    items = [
        {
            "id": r["id"],
            "from_name": r["from_name"],
            "from_email": r["from_email"],
            "subject": r["subject"],
            "received_at": r["received_at"],
            "labels": _labels(r),
            "has_pdf": r["id"] in with_pdf,
        }
        for r in rows
    ]
    return templates.TemplateResponse(request, "inbox.html", {"items": items, "q": q, "label": label, "labels": LABELS})


@app.get("/messages/{message_id}", response_class=HTMLResponse)
def message(request: Request, message_id: int, labelled: str = ""):
    require_user(request)
    with opened(APP) as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Message {message_id} not found.")
        attachments = _attachments(conn, message_id)
    return templates.TemplateResponse(
        request,
        "message.html",
        {"m": row, "labels_now": _labels(row), "attachments": attachments, "labels": LABELS, "labelled": labelled, "error": None},
    )


@app.get("/attachments/{attachment_id}")
def attachment(request: Request, attachment_id: int):
    require_user(request)
    with opened(APP) as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None or not Path(row["path"]).exists():
        raise HTTPException(status_code=404, detail="Attachment not found.")
    # FileResponse with a filename sets Content-Disposition: attachment.
    return FileResponse(row["path"], media_type=row["content_type"], filename=row["filename"])


@app.post("/messages/{message_id}/labels")
async def add_label(request: Request, message_id: int):
    user = require_user(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        return form_error(templates, request, "Invalid or missing CSRF token.")
    label = str(form.get("label") or "").strip()
    with opened(APP) as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Message {message_id} not found.")
        if label not in LABELS:
            return templates.TemplateResponse(
                request,
                "message.html",
                {
                    "m": row,
                    "labels_now": _labels(row),
                    "attachments": _attachments(conn, message_id),
                    "labels": LABELS,
                    "labelled": "",
                    "error": f"Choose a label: {', '.join(LABELS)}.",
                },
                status_code=400,
            )
        current = _labels(row)
        if label not in current:
            current.append(label)
            conn.execute("UPDATE messages SET labels = ? WHERE id = ?", (json.dumps(current), message_id))
        audit(conn, user, "label_message", {"message_id": message_id, "label": label})
    return RedirectResponse(f"/messages/{message_id}?labelled={label}", status_code=303)


# --- read-only API -------------------------------------------------------------------------------


def _message_json(conn, row, with_body: bool) -> dict:
    data = {
        "id": row["id"],
        "from_name": row["from_name"],
        "from_email": row["from_email"],
        "subject": row["subject"],
        "received_at": row["received_at"],
        "labels": _labels(row),
        "attachments": _attachments(conn, row["id"]),
    }
    if with_body:
        data["body"] = row["body"]
    return data


@app.get("/api/messages")
def api_messages(request: Request, q: str = "", label: str = "", unlabelled: int = 0):
    require_api_key(request, APP)
    with opened(APP) as conn:
        rows = _search(conn, q, label, bool(unlabelled))
        return [_message_json(conn, r, with_body=False) for r in rows]


@app.get("/api/messages/{message_id}")
def api_message(request: Request, message_id: int):
    require_api_key(request, APP)
    with opened(APP) as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"message {message_id} not found")
        return _message_json(conn, row, with_body=True)
