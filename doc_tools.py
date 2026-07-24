"""LLM-callable document-generation tools Ada can invoke via Gemini function
calling (same tools/tool_runner wiring as ada_tools.py and solar_tools.py -
see providers._chat_google).

Generic across every company and vertical - the solar company, the
accounting client, an airline, all get the same three tools. What differs
is the content Ada supplies as arguments, never the tool itself.

generate_invoice is the one exception to "the model composes the content
freely": line-item math (subtotal, tax, total) is computed here, never
trusted to the model, for the same reason voucher.py and solar_calc.py
compute rather than ask an LLM to get arithmetic right by chance. Reports
and presentations carry no money math, so Ada supplies their content
directly - doc_gen.py only lays it out.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import db
import doc_gen

DOC_TOOLS = [
    {
        "name": "generate_invoice",
        "description": (
            "Generate a PDF invoice to bill a customer. Give the line items (description, "
            "quantity, unit price) and this computes the subtotal, tax, and total - never state "
            "a total yourself, call this instead. Returns a link the user can download."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "customer_address": {"type": "string", "description": "Optional, multi-line"},
                "line_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["description", "quantity", "unit_price"],
                    },
                },
                "apply_vat": {"type": "boolean", "description": "Ghana VAT at 15%. Defaults to true."},
                "apply_nhil": {"type": "boolean", "description": "Ghana NHIL/GETFund at 5% combined. Defaults to true."},
                "due_in_days": {"type": "integer", "description": "Payment terms in days from today. Defaults to 14."},
                "notes": {"type": "string", "description": "Optional footer note, e.g. payment/bank details."},
            },
            "required": ["customer_name", "line_items"],
        },
    },
    {
        "name": "generate_pdf_report",
        "description": (
            "Generate a formatted PDF document that is not a bill - a proposal, summary, or report. "
            "Give a title and a list of sections, each with a heading, paragraphs, and/or a table. "
            "Returns a link the user can download."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "recipient": {"type": "string", "description": "Optional - who this is prepared for"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "paragraphs": {"type": "array", "items": {"type": "string"}},
                            "table": {
                                "type": "object",
                                "properties": {
                                    "headers": {"type": "array", "items": {"type": "string"}},
                                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                                },
                            },
                        },
                    },
                },
                "footer": {"type": "string", "description": "Optional closing note"},
            },
            "required": ["title", "sections"],
        },
    },
    {
        "name": "generate_presentation",
        "description": (
            "Generate a PPTX slide deck. Give a title and a list of slides, each with a heading "
            "and either bullet points or a simple table. Returns a link the user can download."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "slides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                            "table": {
                                "type": "object",
                                "properties": {
                                    "headers": {"type": "array", "items": {"type": "string"}},
                                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                                },
                            },
                        },
                    },
                },
            },
            "required": ["title", "slides"],
        },
    },
]


def _download_url(doc_id: int) -> str:
    return f"/api/documents?id={doc_id}"


def _run_generate_invoice(tool_input: dict, *, company_id: int, user_id: int) -> dict:
    if not doc_gen.pdf_available():
        return {"error": "PDF generation isn't available on this server yet."}
    line_items = tool_input.get("line_items") or []
    if not isinstance(line_items, list) or not line_items:
        return {"error": "At least one line item is required."}

    rows, subtotal = [], 0.0
    for item in line_items:
        try:
            qty = float(item.get("quantity") or 0)
            price = float(item.get("unit_price") or 0)
        except (TypeError, ValueError, AttributeError):
            return {"error": "Each line item needs a numeric quantity and unit_price."}
        amount = round(qty * price, 2)
        subtotal += amount
        rows.append([str(item.get("description") or "")[:200], f"{qty:g}", f"{price:,.2f}", f"{amount:,.2f}"])
    subtotal = round(subtotal, 2)

    apply_vat = tool_input.get("apply_vat", True)
    apply_nhil = tool_input.get("apply_nhil", True)
    nhil = round(subtotal * 0.05, 2) if apply_nhil else 0.0
    vat = round(subtotal * 0.15, 2) if apply_vat else 0.0
    total = round(subtotal + nhil + vat, 2)

    company = db.get_company(company_id)
    now = datetime.now(timezone.utc)
    try:
        due_days = max(0, int(tool_input.get("due_in_days") or 14))
    except (TypeError, ValueError):
        due_days = 14
    due_date = now + timedelta(days=due_days)
    invoice_number = f"INV-{int(time.time())}"

    totals_rows = [["Subtotal", f"{subtotal:,.2f}"]]
    if apply_nhil:
        totals_rows.append(["NHIL / GETFund (5%)", f"{nhil:,.2f}"])
    if apply_vat:
        totals_rows.append(["VAT (15%)", f"{vat:,.2f}"])
    totals_rows.append(["Total Payable", f"{total:,.2f}"])

    recipient_lines = [f"Bill To: {tool_input['customer_name']}"]
    if tool_input.get("customer_address"):
        recipient_lines += str(tool_input["customer_address"]).splitlines()

    spec = {
        "company_name": company["name"] if company else "",
        "doc_label": "INVOICE",
        "reference": invoice_number,
        "meta_lines": [
            f"Date: {now.strftime('%d %B %Y')}",
            f"Due: {due_date.strftime('%d %B %Y')}",
        ],
        "recipient_lines": recipient_lines,
        "sections": [
            {"heading": "Line Items",
             "table": {"headers": ["Description", "Qty", "Unit Price", "Amount"], "rows": rows}},
            {"table": {"headers": ["", ""], "rows": totals_rows, "emphasize_last_row": True}},
        ],
        "footer": str(tool_input.get("notes") or "")[:500],
    }
    pdf_bytes = doc_gen.build_pdf(spec)
    doc_id = db.save_generated_document(
        company_id, user_id, kind="invoice", filename=f"{invoice_number}.pdf",
        media_type="application/pdf", data=pdf_bytes,
    )
    return {
        "ok": True, "invoice_number": invoice_number,
        "subtotal": subtotal, "nhil_getfund": nhil, "vat": vat, "total": total,
        "download_url": _download_url(doc_id),
    }


def _run_generate_pdf_report(tool_input: dict, *, company_id: int, user_id: int) -> dict:
    if not doc_gen.pdf_available():
        return {"error": "PDF generation isn't available on this server yet."}
    title = str(tool_input.get("title") or "").strip()
    sections = tool_input.get("sections") or []
    if not title or not isinstance(sections, list) or not sections:
        return {"error": "A title and at least one section are required."}

    company = db.get_company(company_id)
    now = datetime.now(timezone.utc)
    recipient_lines = [f"Prepared for: {tool_input['recipient']}"] if tool_input.get("recipient") else []

    spec = {
        "company_name": company["name"] if company else "",
        "doc_label": "REPORT",
        "meta_lines": [f"Date: {now.strftime('%d %B %Y')}"],
        "recipient_lines": recipient_lines,
        "title": title,
        "sections": sections,
        "footer": str(tool_input.get("footer") or ""),
    }
    pdf_bytes = doc_gen.build_pdf(spec)
    doc_id = db.save_generated_document(
        company_id, user_id, kind="report", filename=f"{title[:60]}.pdf",
        media_type="application/pdf", data=pdf_bytes,
    )
    return {"ok": True, "title": title, "download_url": _download_url(doc_id)}


def _run_generate_presentation(tool_input: dict, *, company_id: int, user_id: int) -> dict:
    if not doc_gen.pptx_available():
        return {"error": "Presentation generation isn't available on this server yet."}
    title = str(tool_input.get("title") or "").strip()
    slides = tool_input.get("slides") or []
    if not title or not isinstance(slides, list) or not slides:
        return {"error": "A title and at least one slide are required."}

    company = db.get_company(company_id)
    spec = {
        "title": title,
        "subtitle": str(tool_input.get("subtitle") or ""),
        "company_name": company["name"] if company else "",
        "slides": slides,
        "footer": company["name"] if company else "",
    }
    pptx_bytes = doc_gen.build_pptx(spec)
    doc_id = db.save_generated_document(
        company_id, user_id, kind="presentation", filename=f"{title[:60]}.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        data=pptx_bytes,
    )
    return {"ok": True, "title": title, "download_url": _download_url(doc_id)}


_RUNNERS = {
    "generate_invoice": _run_generate_invoice,
    "generate_pdf_report": _run_generate_pdf_report,
    "generate_presentation": _run_generate_presentation,
}


def run_doc_tool(name: str, tool_input: dict, *, company_id: int, user_id: int) -> dict:
    runner = _RUNNERS.get(name)
    if not runner:
        return {"error": f"Unknown tool: {name}"}
    try:
        return runner(tool_input, company_id=company_id, user_id=user_id)
    except Exception as exc:
        return {"error": f"Could not generate that document: {exc}"}
