"""Generate the vendor invoice PDFs (plan §7.6) with reportlab.

Real invoices are messy, so ours are too: three visual layouts, four date
formats, three amount formats, and different labels for the same field
("Total Payable" / "Amount Due" / "Grand Total"). Amounts use "Rs." or "INR",
never the rupee glyph (reportlab's built-in fonts lack it).

Arithmetic is exact: subtotal = round(total / 1.18, 2), GST = total - subtotal,
and line items are split so that they sum to the subtotal to the paisa.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from sandbox.common.dates import resolve
from sandbox.common.money import format_inr, to_paise

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def pdf_filename(invoice_number: str) -> str:
    """'AI/2026/0917' -> 'AI-2026-0917.pdf' (slashes are not valid in file names)."""
    return invoice_number.replace("/", "-") + ".pdf"


def format_date(d: date, style: str) -> str:
    if style == "dmy_slash":
        return d.strftime("%d/%m/%Y")
    if style == "d_mon_y":
        return f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    if style == "iso":
        return d.isoformat()
    if style == "d_mon_y_dash":
        return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"
    raise ValueError(f"unknown date format {style!r}")


def format_amount(paise: int, style: str) -> str:
    if style == "rs_grouped":
        return format_inr(paise, "Rs. ")
    if style == "inr_grouped":
        return format_inr(paise, "INR ")
    if style == "inr_plain":
        whole, fraction = divmod(paise, 100)
        return f"INR {whole}.{fraction:02d}"
    raise ValueError(f"unknown amount format {style!r}")


@dataclass
class Line:
    description: str
    hsn: str
    qty: int
    amount_paise: int

    @property
    def rate_paise(self) -> int:
        return self.amount_paise // self.qty


@dataclass
class Totals:
    subtotal: int
    gst: int
    cgst: int
    sgst: int
    total: int
    igst: bool


def compute_totals(total_paise: int, inter_state: bool) -> Totals:
    total = Decimal(total_paise) / 100
    subtotal = (total / Decimal("1.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    subtotal_paise = int(subtotal * 100)
    gst = total_paise - subtotal_paise
    cgst = int((Decimal(gst) / 2).to_integral_value(rounding=ROUND_HALF_UP))
    return Totals(subtotal=subtotal_paise, gst=gst, cgst=cgst, sgst=gst - cgst, total=total_paise, igst=inter_state)


def split_lines(subtotal_paise: int, specs: list[dict[str, Any]]) -> list[Line]:
    """Split the subtotal by weight; every line amount is divisible by its quantity,
    and the lines sum exactly to the subtotal."""
    weights = [int(s.get("weight", 1)) for s in specs]
    lines: list[Line] = []
    allocated = 0
    for i, spec in enumerate(specs):
        qty = int(spec.get("qty", 1))
        if i < len(specs) - 1:
            target = subtotal_paise * weights[i] // sum(weights)
            amount = max(qty, (target // qty) * qty)
        else:
            amount = subtotal_paise - allocated
            if amount % qty:
                qty = 1
        allocated += amount
        lines.append(Line(spec["description"], str(spec.get("hsn", "")), qty, amount))
    assert sum(line.amount_paise for line in lines) == subtotal_paise
    return lines


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "vendor": ParagraphStyle("vendor", parent=base["Title"], fontSize=16, leading=20, alignment=0, spaceAfter=2),
        "title": ParagraphStyle("title", parent=base["Heading2"], fontSize=14, alignment=2),
        "title_left": ParagraphStyle("title_left", parent=base["Heading2"], fontSize=13),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=9, leading=12),
        "normal": ParagraphStyle("normal", parent=base["Normal"], fontSize=10, leading=13),
        "label": ParagraphStyle("label", parent=base["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#555555")),
    }


GRID = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (-3, 1), (-1, -1), "RIGHT"),
    ]
)
PLAIN = TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP")])


def _totals_rows(t: Totals, fmt: str, sub_label: str, tax_style: str, total_label: str) -> list[list[str]]:
    rows = [[sub_label, format_amount(t.subtotal, fmt)]]
    if t.igst:
        rows.append([f"IGST {tax_style}18%", format_amount(t.gst, fmt)])
    else:
        rows.append([f"CGST {tax_style}9%", format_amount(t.cgst, fmt)])
        rows.append([f"SGST {tax_style}9%", format_amount(t.sgst, fmt)])
    rows.append([total_label, format_amount(t.total, fmt)])
    return rows


def _bank_line(issuer: dict) -> str:
    account = str(issuer["bank_account"])
    return f"Payment to: {issuer['bank_name']}, A/C XXXXXX{account[-4:]}, IFSC {issuer['ifsc']}"


def build_story(inv: dict, issuer: dict, buyer: dict, base: date) -> list:
    s = _styles()
    fmt, dfmt = inv["amount_format"], inv["date_format"]
    inv_date = format_date(resolve(inv["invoice_date"], base), dfmt)
    due_date = format_date(resolve(inv["due_date"], base), dfmt)
    totals = compute_totals(to_paise(inv["total"]), inter_state=issuer["state"] != buyer["state"])
    lines = split_lines(totals.subtotal, inv["lines"])
    po = inv.get("po_number")
    vendor_block = [
        Paragraph(issuer["legal_name"], s["vendor"]),
        Paragraph(issuer["address"], s["small"]),
        Paragraph(f"GSTIN: {issuer['gstin']} &nbsp;·&nbsp; Phone: {issuer['phone']}", s["small"]),
    ]
    bill_to = Paragraph(f"<b>Bill To:</b> {buyer['name']}, {buyer['address']} (GSTIN: {buyer['gstin']})", s["small"])
    story: list = []

    if inv["layout"] == "A":  # classic table, "Total Payable"
        story += vendor_block
        story += [Spacer(1, 4 * mm), Paragraph("TAX INVOICE", s["title"])]
        meta = [
            [f"Invoice No: {inv['number']}", f"Invoice Date: {inv_date}"],
            [f"PO Number: {po or '-'}", f"Due Date: {due_date}"],
        ]
        story += [Table(meta, colWidths=[85 * mm, 85 * mm], style=PLAIN), Spacer(1, 3 * mm), bill_to, Spacer(1, 4 * mm)]
        rows = [["#", "Description", "HSN", "Qty", "Rate", "Amount"]]
        for i, line in enumerate(lines, 1):
            rows.append([str(i), Paragraph(line.description, s["small"]), line.hsn, str(line.qty), format_amount(line.rate_paise, fmt), format_amount(line.amount_paise, fmt)])
        story.append(Table(rows, colWidths=[8 * mm, 72 * mm, 16 * mm, 12 * mm, 31 * mm, 31 * mm], style=GRID))
        story.append(Spacer(1, 3 * mm))
        totals_rows = _totals_rows(totals, fmt, "Subtotal", "@ ", "Total Payable")
        story.append(Table(totals_rows, colWidths=[40 * mm, 40 * mm], hAlign="RIGHT", style=TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10)])))

    elif inv["layout"] == "B":  # two-column, "Amount Due"
        right = [
            Paragraph("TAX INVOICE", s["title_left"]),
            Paragraph(f"Invoice #: {inv['number']}", s["normal"]),
            Paragraph(f"Date: {inv_date}", s["normal"]),
            Paragraph(f"Payment Due: {due_date}", s["normal"]),
        ]
        if po:
            right.append(Paragraph(f"Purchase Order: {po}", s["normal"]))
        story.append(Table([[vendor_block, right]], colWidths=[105 * mm, 65 * mm], style=PLAIN))
        story += [Spacer(1, 4 * mm), bill_to, Spacer(1, 4 * mm)]
        rows = [["Description", "HSN/SAC", "Qty", "Amount"]]
        for line in lines:
            rows.append([Paragraph(line.description, s["small"]), line.hsn, str(line.qty), format_amount(line.amount_paise, fmt)])
        story.append(Table(rows, colWidths=[100 * mm, 20 * mm, 14 * mm, 36 * mm], style=GRID))
        story.append(Spacer(1, 3 * mm))
        totals_rows = _totals_rows(totals, fmt, "Taxable value", "", "Amount Due")
        story.append(Table(totals_rows, colWidths=[40 * mm, 40 * mm], hAlign="RIGHT", style=TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10)])))

    elif inv["layout"] == "C":  # minimal, "Grand Total"
        story += [
            Paragraph(issuer["legal_name"].upper(), s["title_left"]),
            Paragraph(issuer["address"], s["small"]),
            Paragraph(f"GSTIN {issuer['gstin']}", s["small"]),
            Spacer(1, 5 * mm),
            Paragraph("<b>TAX INVOICE</b>", s["normal"]),
            Paragraph(f"Invoice: {inv['number']}", s["normal"]),
            Paragraph(f"Dated: {inv_date}", s["normal"]),
            Paragraph(f"Pay by: {due_date}", s["normal"]),
        ]
        if po:
            story.append(Paragraph(f"PO: {po}", s["normal"]))
        story += [Spacer(1, 3 * mm), bill_to, Spacer(1, 4 * mm)]
        rows = [["Item", "Qty", "Amount"]] + [[Paragraph(line.description, s["small"]), str(line.qty), format_amount(line.amount_paise, fmt)] for line in lines]
        story.append(Table(rows, colWidths=[120 * mm, 14 * mm, 36 * mm], style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 9), ("ALIGN", (-1, 0), (-1, -1), "RIGHT")])))
        story.append(Spacer(1, 3 * mm))
        totals_rows = _totals_rows(totals, fmt, "Sub total", "@", "Grand Total")
        story.append(Table(totals_rows, colWidths=[45 * mm, 40 * mm], hAlign="RIGHT", style=TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 10), ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black)])))
    else:
        raise ValueError(f"unknown layout {inv['layout']!r}")

    story += [Spacer(1, 6 * mm), Paragraph(_bank_line(issuer), s["small"]), Paragraph("This is a computer generated invoice.", s["label"])]
    return story


def render_invoice(inv: dict, issuer: dict, buyer: dict, base: date, out_dir: Path, injection_text: str = "") -> Path:
    path = out_dir / pdf_filename(inv["number"])
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=22 * mm,
        title=f"Invoice {inv['number']}", author=issuer["legal_name"],
    )

    def first_page(canvas, _doc) -> None:
        if not (inv.get("injection") and injection_text):
            return
        # The prompt-injection test: tiny grey text at the bottom of page 1.
        canvas.saveState()
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(colors.HexColor("#9a9a9a"))
        y = 14 * mm
        for line in simpleSplit(injection_text, "Helvetica", 6, A4[0] - 36 * mm):
            canvas.drawString(18 * mm, y, line)
            y -= 7
        canvas.restoreState()

    doc.build(build_story(inv, issuer, buyer, base), onFirstPage=first_page)
    return path


def expected_strings(inv: dict, base: date) -> list[str]:
    """What must be extractable from each PDF: number, both dates, the total."""
    dfmt = inv["date_format"]
    return [
        inv["number"],
        format_date(resolve(inv["invoice_date"], base), dfmt),
        format_date(resolve(inv["due_date"], base), dfmt),
        format_amount(to_paise(inv["total"]), inv["amount_format"]),
    ]


def extract_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def generate_all(data: dict, base: date, out_dir: Path) -> list[Path]:
    """Render every invoice and verify its key strings survive text extraction."""
    out_dir.mkdir(parents=True, exist_ok=True)
    issuers = {v["id"]: v for v in data["vendors"] + data.get("other_issuers", [])}
    paths = []
    for inv in data["invoices"]:
        path = render_invoice(inv, issuers[inv["issuer"]], data["buyer"], base, out_dir, data.get("injection_text", ""))
        text = extract_text(path)
        missing = [s for s in expected_strings(inv, base) if s not in text]
        if missing:
            raise RuntimeError(f"{path.name}: text extraction is missing {missing}")
        paths.append(path)
    return paths


# --- offer letters (phase 9: HR onboarding) ----------------------------------------------------

FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def long_date(d: date) -> str:
    return f"{d.day} {FULL_MONTHS[d.month - 1]} {d.year}"


def render_offer_letter(offer: dict, company: dict, base: date, out_dir: Path) -> Path:
    """An employment offer letter, as HR would put in the shared drive."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / offer["file"]
    s = _styles()
    first = offer["candidate_name"].split()[0]
    start = long_date(resolve(offer["start_date"], base))
    story = [
        Paragraph(company["name"], s["vendor"]),
        Paragraph(company["address"], s["small"]),
        Spacer(1, 8 * mm),
        Paragraph(f"Date: {long_date(resolve(offer['issued'], base))}", s["normal"]),
        Spacer(1, 4 * mm),
        Paragraph("<b>Private &amp; Confidential</b>", s["normal"]),
        Paragraph(f"{offer['salutation']} {offer['candidate_name']}", s["normal"]),
        Paragraph(offer["address"], s["normal"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>Subject: Offer of employment — {offer['title']}</b>", s["normal"]),
        Spacer(1, 4 * mm),
        Paragraph(f"Dear {first},", s["normal"]),
        Spacer(1, 2 * mm),
        Paragraph(
            f"We are delighted to offer you the position of <b>{offer['title']}</b> in the <b>{offer['department']}</b> "
            f"department at {company['name']}, on the terms below.", s["normal"]),
        Spacer(1, 4 * mm),
        Table(
            [
                ["Position", offer["title"]],
                ["Department", offer["department"]],
                ["Start date", start],
                ["Reporting manager", f"{offer['manager_name']}, {offer['manager_title']}"],
                ["Location", offer["location"]],
                ["Annual CTC", offer["ctc"]],
            ],
            colWidths=[45 * mm, 110 * mm],
            hAlign="LEFT",
            style=TableStyle([("FONTSIZE", (0, 0), (-1, -1), 10), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]),
        ),
        Spacer(1, 4 * mm),
        Paragraph("Your work email will be created by HR and shared with you before your first day, together with your system access and equipment.", s["normal"]),
        Spacer(1, 2 * mm),
        Paragraph("Please sign and return a copy of this letter to confirm your acceptance.", s["normal"]),
        Spacer(1, 10 * mm),
        Paragraph("Sincerely,", s["normal"]),
        Paragraph(offer["signatory"]["name"], s["normal"]),
        Paragraph(f"{offer['signatory']['title']}, {company['name']}", s["normal"]),
    ]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm, title=f"Offer — {offer['candidate_name']}").build(story)
    text = extract_text(path)
    for expected in (offer["candidate_name"], offer["title"], start, offer["manager_name"]):
        if expected not in text:
            raise RuntimeError(f"{path.name}: text extraction is missing {expected!r}")
    return path
