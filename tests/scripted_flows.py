"""Reusable ScriptedLLM scripts for integration tests and the red-team scenario.

Each script mimics what a real model would do, step by step, using element refs
from the latest snapshot (find_ref), so the whole stack — browser, gateway,
intents, approvals, verifier, report — is exercised without a real LLM.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from karya.llm.scripted import ScriptedLLM, find_ref, last_user_text


def goal_spec(objective: str, criteria: list[tuple[str, str, str]], batch: bool = False) -> dict[str, Any]:
    return {
        "goal_spec": {
            "objective": objective,
            "deliverable": "Confirmation with the ERP reference and evidence.",
            "entities": [],
            "success_criteria": [{"id": cid, "description": desc, "verification": ver} for cid, desc, ver in criteria],
            "constraints": ["AP-001: totals above INR 50,000 need Finance Manager approval", "ap-01: confirm vendor and check duplicates before posting"],
            "assumptions": ["'Latest' = most recent invoice date"],
            "blocking_questions": [],
            "risk_notes": [],
            "batch": batch,
        },
        "plan": [{"description": d} for d in ("Find the invoice email", "Download and extract the PDF", "Check vendor and duplicates", "Post in the ERP", "Label the email", "Report")],
    }


HAPPY_GOAL = goal_spec(
    "Enter the most recent Acme Industries invoice from the AP mailbox into Meridian ERP and confirm it.",
    [
        ("c1", "The latest Acme Industries invoice in the AP mailbox is identified and its fields extracted from the PDF.", "mail: message list + downloaded PDF"),
        ("c2", "Meridian ERP has exactly one invoice for Acme Industries Pvt Ltd with that invoice number, and its amount, invoice date and due date equal the PDF values.", "erp api find_invoice"),
        ("c3", "The source email is labelled Processed.", "mail api get_message"),
    ],
)

NO_INJECTION = {"contains_instructions": False, "excerpt": "", "reason": "ordinary invoice"}


def ref(messages: Any, kind: str, label: str | None = None, name: str | None = None) -> str:
    return find_ref(last_user_text(messages), kind, label=label, name=name)


def step(name: str, why: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": {"why": why, **arguments}}


def invoice_extraction(number: str, vendor: str, invoice_date: str, due_date: str, subtotal: str, tax: str, total: str, po: str | None = None) -> dict[str, Any]:
    return {
        "vendor_legal_name": vendor, "vendor_gstin": None, "invoice_number": number, "invoice_date": invoice_date,
        "due_date": due_date, "po_number": po, "subtotal": subtotal, "tax_total": tax, "total": total, "suspicious_text": [],
    }


def fill_invoice_form(erp: str, vendor_label: str, number: str, inv_date: date, due: date, amount: str, po: str, notes: str) -> list[Any]:
    return [
        step("browser_login", "Log in to the ERP to post the invoice.", connector_id="erp"),
        step("browser_open", "Open the new-invoice form.", url=f"{erp}/invoices/new"),
        lambda m: step("browser_select", "Choose the vendor.", ref=ref(m, "select", name="vendor_id"), option_label=vendor_label),
        lambda m: step("browser_fill", "Enter the invoice number.", ref=ref(m, "textbox", name="invoice_number"), value=number),
        lambda m: step("browser_fill", "Enter the invoice date.", ref=ref(m, "date", name="invoice_date"), value=inv_date.isoformat()),
        lambda m: step("browser_fill", "Enter the due date.", ref=ref(m, "date", name="due_date"), value=due.isoformat()),
        lambda m: step("browser_fill", "Enter the total payable incl. GST.", ref=ref(m, "number", name="amount"), value=amount),
        lambda m: step("browser_fill", "Enter the PO number.", ref=ref(m, "textbox", name="po_number"), value=po),
        lambda m: step("browser_fill", "Note the source email.", ref=ref(m, "textarea", name="notes"), value=notes),
        lambda m: step("browser_click", "Submit the invoice form.", ref=ref(m, "button", "")),
    ]


def submit_button(messages: Any) -> str:
    """The form's submit button is the last button in the snapshot (its text may drift)."""
    import re

    text = last_user_text(messages)
    text = text[text.rfind("URL: "):]
    buttons = re.findall(r"\[(e\d+)\] button", text)
    return buttons[-1]


def happy_path_steps(urls: dict[str, str], base: date) -> list[Any]:
    erp, mail = urls["erp"], urls["mail"]
    inv_date, due = base - timedelta(days=3), base + timedelta(days=27)
    form = fill_invoice_form(erp, "Acme Industries Pvt Ltd", "AI/2026/0917", inv_date, due, "42380.00", "4471", "Source: AP mailbox — Invoice AI/2026/0917 for PO 4471")
    form[-1] = lambda m: step("browser_click", "Submit the invoice form.", ref=submit_button(m))
    return [
        step("browser_login", "Log in to the AP mailbox to find the invoice.", connector_id="mail"),
        step("browser_open", "Open the latest Acme Industries invoice email.", url=f"{mail}/messages/2"),
        lambda m: step("browser_click", "Download the invoice PDF.", ref=ref(m, "link", "AI-2026-0917.pdf")),
        step("files_extract", "Extract the invoice fields from the PDF.", path="downloads/AI-2026-0917.pdf", schema="invoice"),
        step("api_get", "Confirm the vendor exists in the vendor master (ap-01 step 4).", connector_id="erp", endpoint="search_vendors", params={"q": "Acme Industries"}),
        step("api_get", "Check the invoice is not already posted (ap-01 step 5).", connector_id="erp", endpoint="find_invoice", params={"vendor_id": "V001", "invoice_number": "AI/2026/0917"}),
        *form,
        step("browser_open", "Go back to the email to label it.", url=f"{mail}/messages/2"),
        lambda m: step("browser_select", "Choose the Processed label.", ref=ref(m, "select", name="label"), option_label="Processed"),
        lambda m: step("browser_click", "Apply the label.", ref=submit_button(m)),
        step(
            "finish",
            "All criteria are met.",
            status="completed",
            summary="Posted Acme Industries invoice AI/2026/0917 (INR 42,380.00) as ERP-INV-000104 and labelled the email Processed.",
            claims=[
                {"criterion_id": "c1", "statement": "AI/2026/0917 is the latest Acme Industries invoice; fields extracted.", "evidence": ["downloads/AI-2026-0917.pdf"]},
                {"criterion_id": "c2", "statement": "Exactly one ERP record ERP-INV-000104 with matching values.", "evidence": ["erp:ERP-INV-000104"]},
                {"criterion_id": "c3", "statement": "Email labelled Processed.", "evidence": ["mail:/messages/2"]},
            ],
        ),
    ]


def happy_llm(urls: dict[str, str], base: date, extra_forced: dict[str, Any] | None = None) -> ScriptedLLM:
    forced = {
        "submit_goal_spec": HAPPY_GOAL,
        "submit_invoice": invoice_extraction(
            "AI/2026/0917", "Acme Industries Pvt Ltd", (base - timedelta(days=3)).strftime("%d/%m/%Y"),
            (base + timedelta(days=27)).strftime("%d/%m/%Y"), "Rs. 35,915.25", "Rs. 6,464.75", "Rs. 42,380.00", "4471",
        ),
        "submit_injection_check": NO_INJECTION,
    }
    forced.update(extra_forced or {})
    return ScriptedLLM(happy_path_steps(urls, base), forced=forced)


VERTEX_GOAL = goal_spec(
    "Post the Vertex Semiconductors invoice VS-55120 from the AP mailbox in Meridian ERP.",
    [
        ("c1", "Meridian ERP has exactly one invoice VS-55120 for Vertex Semiconductors Ltd (V003) for INR 184500.00 with the PDF's dates.", "erp api find_invoice"),
        ("c2", "The source email is labelled Processed.", "mail api get_message"),
    ],
)


def vertex_form_steps(urls: dict[str, str], base: date) -> list[Any]:
    inv_date, due = base - timedelta(days=4), base + timedelta(days=41)
    form = fill_invoice_form(urls["erp"], "Vertex Semiconductors Ltd", "VS-55120", inv_date, due, "184500.00", "4502", "Source: AP mailbox — Invoice VS-55120")
    form[-1] = lambda m: step("browser_click", "Submit the invoice form.", ref=submit_button(m))
    return form


def vertex_llm(urls: dict[str, str], base: date, after_hold: list[Any] | None = None) -> ScriptedLLM:
    """Posts VS-55120 (held by AP-001). `after_hold` continues the script once approved."""
    return ScriptedLLM(vertex_form_steps(urls, base) + (after_hold or []), forced={"submit_goal_spec": VERTEX_GOAL, "submit_injection_check": NO_INJECTION})
