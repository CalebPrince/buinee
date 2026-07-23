"""
Buinee — landing page server with a public demo agent.

Two ways to run this, same routing logic underneath either way:

    python server.py         ->  http://127.0.0.1:8080   (local dev)
    passenger_wsgi.py        ->  cPanel's Python Selector / Passenger

RouteHandlerMixin holds every route and never touches a socket directly -
it just fills in self._status / self._resp_headers / self._resp_body.
Two thin transports read those afterward:
  - Handler(RouteHandlerMixin, BaseHTTPRequestHandler) - a real socket
    server for local dev, via ThreadingHTTPServer.
  - application(environ, start_response) - a WSGI callable for Passenger,
    via the WSGIRequest adapter below, which fakes just enough of
    BaseHTTPRequestHandler's interface (self.path, self.headers, self.rfile,
    self.client_address) for the mixin to run unmodified.

The agent on the landing page is deliberately narrow. It is an unauthenticated
endpoint on a public page, which means it is somebody else's free LLM if it is
not fenced in, so:

  * rate limited per IP
  * short messages, short replies
  * scoped by prompt to the product and to demonstrating a voucher
  * no file uploads (that is behind registration)

The arithmetic is never done by the model. When a visitor supplies figures the
server computes the voucher with voucher.py and hands the model the finished
numbers to present. Same principle as the product itself: the model reads and
explains, code computes.
"""

from __future__ import annotations

import email.utils
import hashlib
import hmac
import base64
import binascii
import html
import io
import json
import os
import re
import secrets
import struct
import sys
import time
import traceback
import webbrowser
import zipfile
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.client import responses as HTTP_REASONS
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import mailbox  # noqa: E402
import providers  # noqa: E402
import secretstore  # noqa: E402
import tools  # noqa: E402
import voucher  # noqa: E402

# Only the local dev transport reads these - production runs under Passenger,
# which owns the socket itself. Kept because a host that hands you a port via
# $PORT also expects a bind on 0.0.0.0 rather than 127.0.0.1; local dev keeps
# the localhost-only default.
PORT = int(os.environ.get("PORT", 8080))
HOST = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
ENV_FILE = ROOT / ".env"
FX_FILE = ROOT / "bog-fx-rates.xlsx"

OFFICE_MEDIA_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def report_application_error(source: str, error, user: dict | None = None,
                             context: str = "") -> str:
    """Store private diagnostics and return a safe support reference."""
    reference = secrets.token_hex(4).upper()
    identity = ""
    if user:
        identity = f"user_id={user.get('id')} company_id={user.get('company_id')}"
    trace = traceback.format_exc()
    if trace.strip() == "NoneType: None":
        trace = ""
    # Deliberately exclude request bodies, mailbox contents, credentials and keys.
    details = "\n".join(part for part in (
        f"Reference: {reference}", identity, context, trace
    ) if part)
    technical_message = str(error).strip() or type(error).__name__
    try:
        db.record_application_error(source, f"[{reference}] {technical_message}", details)
    except Exception as logging_error:
        print(f"  ! could not record application error {reference}: {logging_error}")
    return reference


def ada_unavailable(reference: str, action: str = "help with that") -> str:
    return (f"Ada couldn't {action} right now. Please try again shortly. "
            f"Reference: {reference}.")


def _xml_visible_text(blob: bytes) -> str:
    """Extract visible OOXML text without accepting markup as instructions."""
    source = blob.decode("utf-8", "replace")
    values = [html.unescape(value).strip() for value in re.findall(r">([^<]+)<", source)]
    return " ".join(value for value in values if value)


def extract_office_text(raw: bytes, office_kind: str) -> str:
    if office_kind == "xlsx":
        try:
            import openpyxl
            book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            lines = []
            for sheet in book.worksheets:
                lines.append(f"## Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    values = [str(value) for value in row if value is not None and str(value).strip()]
                    if values:
                        lines.append(" | ".join(values))
                    if sum(len(line) for line in lines) > 100000:
                        break
            book.close()
            return "\n".join(lines)[:100000].strip()
        except ImportError as exc:
            raise ValueError("Excel reading is not installed on this server.") from exc
        except Exception as exc:
            raise ValueError("That Excel workbook could not be read.") from exc

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            prefix = "word/" if office_kind == "docx" else "ppt/slides/"
            suffix = ".xml"
            members = [item for item in archive.infolist()
                       if item.filename.startswith(prefix) and item.filename.endswith(suffix)]
            members.sort(key=lambda item: item.filename)
            chunks = []
            total_unpacked = 0
            for item in members:
                total_unpacked += item.file_size
                if total_unpacked > 20 * 1024 * 1024:
                    raise ValueError("That Office document expands beyond the safe reading limit.")
                text = _xml_visible_text(archive.read(item))
                if text:
                    chunks.append(text)
            result = "\n\n".join(chunks)[:100000].strip()
            if not result:
                raise ValueError("That Office document contains no readable text.")
            return result
    except zipfile.BadZipFile as exc:
        raise ValueError("That Office document is damaged or uses an older binary format.") from exc


def extract_rtf_text(value: str) -> str:
    value = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda match: bytes.fromhex(match.group(1)).decode("cp1252", "replace"),
        value,
    )
    value = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", value)
    value = value.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    value = value.replace("{", "").replace("}", "")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())[:100000]


def normalize_document_upload(req: dict) -> dict:
    name = Path(str(req.get("name") or "document")).name[:160]
    media_type = str(req.get("media_type") or "").lower()
    text_content = str(req.get("text") or "")
    data_base64 = str(req.get("data") or "")
    text_types = ("text/plain", "text/markdown", "text/csv", "text/html",
                  "application/json", "application/xml", "text/xml",
                  "application/rtf", "text/rtf")
    if media_type in text_types:
        kind, data_base64 = "text", ""
        size_bytes = len(text_content.encode("utf-8"))
        if media_type in ("application/rtf", "text/rtf"):
            text_content = extract_rtf_text(text_content)
        text_content = text_content[:100000]
    elif media_type in OFFICE_MEDIA_TYPES:
        raw = base64.b64decode(data_base64, validate=True)
        size_bytes = len(raw)
        kind = "text"
        text_content = extract_office_text(raw, OFFICE_MEDIA_TYPES[media_type])
    elif media_type == "application/pdf" or media_type.startswith("image/"):
        kind = "pdf" if media_type == "application/pdf" else "image"
        if media_type not in ("application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"):
            raise ValueError("That image type is not supported.")
        raw = base64.b64decode(data_base64, validate=True)
        size_bytes = len(raw)
        text_content = ""
    else:
        raise ValueError("Use DOCX, XLSX, PPTX, PDF, RTF, text/data files, or a supported image.")
    if size_bytes <= 0 or size_bytes > 5 * 1024 * 1024:
        raise ValueError("Each document must be between 1 byte and 5 MB.")
    return {"name": name, "kind": kind, "media_type": media_type,
            "text_content": text_content, "data_base64": data_base64,
            "size_bytes": size_bytes}

STATIC_PAGES = {
    "/": "index.html",
    "/register": "register.html",
    "/login": "login.html",
    "/contact": "contact.html",
    "/privacy": "legal.html",
    "/terms": "legal.html",
    "/cookies": "legal.html",
    "/refunds": "legal.html",
    "/security": "legal.html",
    "/dashboard": "dashboard.html",
    "/admin": "admin.html",
    "/admin/companies": "admin-companies.html",
    "/admin/plans": "admin-plans.html",
    "/admin/pipeline": "admin-pipeline.html",
    "/admin/payments": "admin-payments.html",
    "/admin/team": "admin-team.html",
    "/admin/activity": "admin-activity.html",
    "/admin/errors": "admin-errors.html",
    "/admin/reports": "admin-reports.html",
    "/admin/inbox": "admin-inbox.html",
    "/admin/invoices": "admin-invoices.html",
    "/admin/login": "admin-login.html",
    "/admin/settings": "admin-settings.html",
    "/admin/ada": "admin-ada.html",
    "/admin/site-settings": "admin-site-settings.html",
    "/admin/site-contents": "admin-site-contents.html",
}

LEGACY_PAGE_REDIRECTS = {
    "/index.html": "/",
    "/register.html": "/register",
    "/login.html": "/login",
    "/contact.html": "/contact",
    "/privacy.html": "/privacy",
    "/terms.html": "/terms",
    "/cookies.html": "/cookies",
    "/refunds.html": "/refunds",
    "/security.html": "/security",
    "/dashboard.html": "/dashboard",
    "/admin.html": "/admin",
    "/admin-companies.html": "/admin/companies",
    "/admin-plans.html": "/admin/plans",
    "/admin-pipeline.html": "/admin/pipeline",
    "/admin-payments.html": "/admin/payments",
    "/admin-team.html": "/admin/team",
    "/admin-activity.html": "/admin/activity",
    "/admin-errors.html": "/admin/errors",
    "/admin-reports.html": "/admin/reports",
    "/admin-inbox.html": "/admin/inbox",
    "/admin-invoices.html": "/admin/invoices",
    "/admin-login.html": "/admin/login",
    "/admin-settings.html": "/admin/settings",
    "/admin-ada.html": "/admin/ada",
    "/admin-site-settings.html": "/admin/site-settings",
    "/admin-site-contents.html": "/admin/site-contents",
}

# --- site contents CMS ------------------------------------------------------
# Every visible string on the public pages that a business owner would
# reasonably want to reword, grouped by page. `type` controls how a saved
# value is turned back into HTML by render_site_content():
#   text      - single line, HTML-escaped only
#   paragraph - HTML-escaped, blank lines become paragraph breaks
#   bullets   - one item per line, each wrapped as its own <li> with the
#               same checkmark icon the page already uses
CMS_CHECK_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  'stroke-width="2.4"><path d="M20 6 9 17l-5-5"/></svg>')

SITE_CONTENT_SCHEMA = {
    "index": [
        {"key": "hero_eyebrow", "label": "Hero eyebrow", "type": "text",
         "default": "One workspace for the work behind your business"},
        {"key": "hero_headline", "label": "Hero headline", "type": "paragraph",
         "default": "Your team already has a way of working.\nIt just lives in too many places."},
        {"key": "hero_lede", "label": "Hero subtext", "type": "paragraph",
         "default": "Buinee brings messages, documents, tasks, customer follow-ups, approvals and "
                     "everyday decisions into one place. Give each person the right view, keep a clear "
                     "history of what happened, and let AI help with the work without taking control away."},
        {"key": "hero_cta_primary", "label": "Hero primary button", "type": "text", "default": "Create your workspace"},
        {"key": "hero_cta_secondary", "label": "Hero secondary button", "type": "text", "default": "Meet the agents"},
        {"key": "hero_note", "label": "Hero note", "type": "text",
         "default": "Your team joins by name — no per-seat setup, no IT project."},

        {"key": "how_eyebrow", "label": "\"How it works\" eyebrow", "type": "text", "default": "How work moves"},
        {"key": "how_headline", "label": "\"How it works\" headline", "type": "text",
         "default": "One request. The right people. A clear outcome."},
        {"key": "how_subtext", "label": "\"How it works\" subtext", "type": "paragraph",
         "default": "Whether it starts as an email, file, customer question or internal task, the work "
                     "stays connected from arrival to completion."},
        {"key": "how_step1_title", "label": "Step 1 title", "type": "text", "default": "Work comes in"},
        {"key": "how_step1_text", "label": "Step 1 text", "type": "paragraph",
         "default": "Bring in an email, document, request or customer conversation. Buinee keeps the "
                     "source and context together."},
        {"key": "how_step2_title", "label": "Step 2 title", "type": "text", "default": "Someone takes ownership"},
        {"key": "how_step2_text", "label": "Step 2 text", "type": "paragraph",
         "default": "Assign it, discuss it, add instructions and complete the next step without losing "
                     "decisions across separate tools."},
        {"key": "how_step3_title", "label": "Step 3 title", "type": "text", "default": "The outcome is recorded"},
        {"key": "how_step3_text", "label": "Step 3 text", "type": "paragraph",
         "default": "Send the reply, approve the document, close the task or issue the final file with a "
                     "history everyone can trust."},

        {"key": "roles_eyebrow", "label": "Roles eyebrow", "type": "text", "default": "Roles & visibility"},
        {"key": "roles_headline", "label": "Roles headline", "type": "text",
         "default": "Give everyone the access their work requires."},
        {"key": "roles_subtext", "label": "Roles subtext", "type": "paragraph",
         "default": "Buinee supports different responsibilities without forcing every business into the "
                     "same job titles. People see their own work, shared team work, or the full workspace "
                     "according to their role."},
        {"key": "role1_tier", "label": "Role 1 tier label", "type": "text", "default": "Tier 3"},
        {"key": "role1_title", "label": "Role 1 title", "type": "text", "default": "Team member"},
        {"key": "role1_tagline", "label": "Role 1 tagline", "type": "text", "default": "Handles day-to-day work."},
        {"key": "role1_bullets", "label": "Role 1 bullets (one per line)", "type": "bullets",
         "default": "Manages assigned work\nWorks with messages and files\nRequests review when needed"},
        {"key": "role1_sees", "label": "Role 1 \"sees\" line", "type": "text", "default": "Sees their own work only."},
        {"key": "role2_tier", "label": "Role 2 tier label", "type": "text", "default": "Tier 2"},
        {"key": "role2_title", "label": "Role 2 title", "type": "text", "default": "Team lead"},
        {"key": "role2_tagline", "label": "Role 2 tagline", "type": "text", "default": "Coordinates people and decisions."},
        {"key": "role2_bullets", "label": "Role 2 bullets (one per line)", "type": "bullets",
         "default": "Reviews team work\nApproves or returns requests\nKeeps work moving"},
        {"key": "role2_sees", "label": "Role 2 \"sees\" line", "type": "text",
         "default": "Sees their own work and their team's."},
        {"key": "role3_tier", "label": "Role 3 tier label", "type": "text", "default": "Tier 1"},
        {"key": "role3_title", "label": "Role 3 title", "type": "text", "default": "Supervisor"},
        {"key": "role3_tagline", "label": "Role 3 tagline", "type": "text", "default": "Runs the workspace."},
        {"key": "role3_bullets", "label": "Role 3 bullets (one per line)", "type": "bullets",
         "default": "Oversees teams and workflows\nSets access and business rules\nAdds and removes people"},
        {"key": "role3_sees", "label": "Role 3 \"sees\" line", "type": "text", "default": "Sees the whole workspace."},

        {"key": "workspace_eyebrow", "label": "Workspace eyebrow", "type": "text", "default": "The workspace"},
        {"key": "workspace_headline", "label": "Workspace headline", "type": "text",
         "default": "Where your team already talks — with the work in the room."},
        {"key": "workspace_feat1_title", "label": "Workspace feature 1 title", "type": "text",
         "default": "Send a file to a colleague. Or to an agent."},
        {"key": "workspace_feat1_text", "label": "Workspace feature 1 text", "type": "paragraph",
         "default": "The chat is where the team already works things out. Buinee puts the documents in "
                     "the same place — so a file goes to a person or straight to an assistant that reads "
                     "it, without leaving the conversation."},
        {"key": "workspace_feat1_bullets", "label": "Workspace feature 1 bullets (one per line)", "type": "bullets",
         "default": "Team and one-to-one chat\nShare PDFs, Word, Excel, images and more\n"
                     "Hand any file to an assistant in one tap"},
        {"key": "workspace_feat2_title", "label": "Workspace feature 2 title", "type": "text",
         "default": "A signature that means something."},
        {"key": "workspace_feat2_text", "label": "Workspace feature 2 text", "type": "paragraph",
         "default": "Sign inside the system and the PDF prints ready. But the signature on the page is "
                     "only the visible part — underneath it sits a record of who approved what, when, and "
                     "what changed between drafts. That record is the thing an auditor actually asks for."},
        {"key": "workspace_feat2_bullets", "label": "Workspace feature 2 bullets (one per line)", "type": "bullets",
         "default": "No printing to sign, then scanning back\nEvery approval and return time-stamped\n"
                     "Nothing editable after it is approved"},

        {"key": "agents_eyebrow", "label": "Agents eyebrow", "type": "text", "default": "Inside your dashboard"},
        {"key": "agents_headline", "label": "Agents headline", "type": "text",
         "default": "Every person gets an assistant that understands their work."},
        {"key": "agents_subtext", "label": "Agents subtext", "type": "paragraph",
         "default": "Once your company is in, each person's dashboard comes with an AI assistant — one "
                     "that works with the actual context: mailbox conversations, documents, customer "
                     "records, team instructions and outstanding tasks. Not a generic chatbot bolted to "
                     "the corner of the screen."},
        {"key": "agents_card1_tag", "label": "Agents card 1 tag", "type": "text", "default": "Your inbox"},
        {"key": "agents_card1_title", "label": "Agents card 1 title", "type": "text",
         "default": "It reads the mail before you do"},
        {"key": "agents_card1_text", "label": "Agents card 1 text", "type": "paragraph",
         "default": "Connect your work mailbox and it goes through overnight — sorting what came in, "
                     "drafting the replies that are obvious, and putting the things that genuinely need "
                     "you at the top."},
        {"key": "agents_card1_bullets", "label": "Agents card 1 bullets (one per line)", "type": "bullets",
         "default": "Sorts requests, updates, questions and noise\nDrafts replies in your own tone\n"
                     "Flags risks, changes and unanswered messages"},
        {"key": "agents_card1_sees", "label": "Agents card 1 \"sees\" line", "type": "text",
         "default": "Nothing is ever sent without you pressing send."},
        {"key": "agents_card2_tag", "label": "Agents card 2 tag", "type": "text", "default": "Your documents"},
        {"key": "agents_card2_title", "label": "Agents card 2 title", "type": "text", "default": "Hand it a file and ask"},
        {"key": "agents_card2_text", "label": "Agents card 2 text", "type": "paragraph",
         "default": "Give it a report, spreadsheet, contract, proposal, invoice or internal guide. It "
                     "reads the document itself, finds the useful details, and helps you act on what is "
                     "missing or important."},
        {"key": "agents_card2_bullets", "label": "Agents card 2 bullets (one per line)", "type": "bullets",
         "default": "PDFs, scans, Word and Excel\nSummarises, compares and extracts details\n"
                     "Explains what needs attention and why"},
        {"key": "agents_card2_sees", "label": "Agents card 2 \"sees\" line", "type": "text",
         "default": "Works from your rules — your thresholds, your terms, your templates."},
        {"key": "agents_card3_title", "label": "Agents card 3 title", "type": "text",
         "default": "You tell it how your business works. Once."},
        {"key": "agents_card3_text", "label": "Agents card 3 text", "type": "paragraph",
         "default": "Every business has rules a new hire spends months learning — how customers are "
                     "handled, what needs approval, when work is escalated, and which templates to use. "
                     "Write them down once and the assistant works to them instead of generic practice."},
        {"key": "agents_card3_bullets", "label": "Agents card 3 bullets (one per line)", "type": "bullets",
         "default": "Your approval, service and escalation rules\nYour documents and response templates\n"
                     "Your vocabulary, customers, teams and processes"},

        {"key": "integrations_eyebrow", "label": "Integrations eyebrow", "type": "text",
         "default": "Connect what you already use"},
        {"key": "integrations_headline", "label": "Integrations headline", "type": "text",
         "default": "Your team's tools, right where the work happens."},
        {"key": "integrations_subtext", "label": "Integrations subtext", "type": "paragraph",
         "default": "Once someone's in, they connect Slack, Drive, Trello and the rest from their own "
                     "workstation in a couple of clicks. Buinee reads alongside them — nothing here "
                     "replaces the tools your team already relies on."},

        {"key": "trust_eyebrow", "label": "Trust eyebrow", "type": "text", "default": "Built for dependable work"},
        {"key": "trust_headline", "label": "Trust headline", "type": "text",
         "default": "AI can assist. Your rules and records remain in control."},
        {"key": "trust_subtext", "label": "Trust subtext", "type": "paragraph",
         "default": "Business software earns its place by being predictable. Buinee separates suggestions "
                     "from facts, keeps actions reviewable, and records important changes."},
        {"key": "trust_card1_title", "label": "Trust card 1 title", "type": "text", "default": "Facts stay verifiable"},
        {"key": "trust_card1_text", "label": "Trust card 1 text", "type": "paragraph",
         "default": "Stored records, calculated figures and completed actions remain distinct from AI "
                     "suggestions, so people can see what is known and what is proposed."},
        {"key": "trust_card2_title", "label": "Trust card 2 title", "type": "text",
         "default": "People approve important actions"},
        {"key": "trust_card2_text", "label": "Trust card 2 text", "type": "paragraph",
         "default": "Drafts, summaries and recommendations stay ready for review. Sending, approving and "
                     "changing records remains a deliberate human action."},
        {"key": "trust_card3_title", "label": "Trust card 3 title", "type": "text",
         "default": "Your company's data is yours"},
        {"key": "trust_card3_text", "label": "Trust card 3 text", "type": "paragraph",
         "default": "Every record belongs to one company and is scoped to it. No document, figure or "
                     "conversation is visible outside the organisation that created it."},

        {"key": "pricing_eyebrow", "label": "Pricing eyebrow", "type": "text", "default": "Pricing"},
        {"key": "pricing_headline", "label": "Pricing headline", "type": "text",
         "default": "Working alone, or setting this up for a team?"},
        {"key": "pricing_subtext", "label": "Pricing subtext", "type": "paragraph",
         "default": "Both get the same connected workspace for messages, documents, tasks and business "
                     "records. What changes is how many people it covers and which advanced features are "
                     "included."},
        {"key": "pricing_aud_individual", "label": "Pricing toggle: individual", "type": "text", "default": "Just me"},
        {"key": "pricing_aud_team", "label": "Pricing toggle: team", "type": "text", "default": "My team"},

        {"key": "faq_eyebrow", "label": "FAQ eyebrow", "type": "text", "default": "Questions"},
        {"key": "faq_headline", "label": "FAQ headline", "type": "text", "default": "Things people usually ask first."},
        {"key": "faq_subtext", "label": "FAQ subtext", "type": "paragraph",
         "default": "Can't find what you need here? Ask Ada in the corner, or get in touch directly."},
        {"key": "faq_items", "label": "FAQ questions and answers", "type": "qa_list",
         "default": "Q: What exactly is Buinee?\n"
                     "A: A single workspace for the work behind your business - team chat, documents, "
                     "tasks, approvals, customer follow-ups and an AI assistant, in one place instead of "
                     "scattered across separate tools.\n"
                     "Q: Do I need a team to use it, or can I work alone?\n"
                     "A: Either. The solo plans give one person the same workspace without any of the "
                     "team-management features; team plans add roles, approvals and visibility across "
                     "everyone you invite.\n"
                     "Q: How does everyone else join my company's workspace?\n"
                     "A: They register with your company's name and their own details, then a Supervisor "
                     "approves the request. Nobody gets in on the strength of the company name alone.\n"
                     "Q: What can the AI assistant actually do?\n"
                     "A: It answers questions grounded in your own workspace data, drafts replies, reads "
                     "documents you hand it, and flags what needs attention - but it never sends, approves "
                     "or changes a record without a person choosing to.\n"
                     "Q: Is my company's data visible to other companies on Buinee?\n"
                     "A: No. Every record is scoped to the company that created it - no document, figure or "
                     "conversation is visible outside your own workspace.\n"
                     "Q: Can I change plans later?\n"
                     "A: Yes - moving between solo and team plans, or up and down tiers, doesn't require "
                     "setting anything up twice."},

        {"key": "cta_headline", "label": "Closing CTA headline", "type": "text",
         "default": "Start with the work your team handles every day."},
        {"key": "cta_subtext", "label": "Closing CTA subtext", "type": "paragraph",
         "default": "Create a workspace, invite the right people, and bring your next request, "
                     "conversation or document into one clear process."},
        {"key": "cta_primary", "label": "Closing CTA primary button", "type": "text", "default": "Choose a plan"},
        {"key": "cta_secondary", "label": "Closing CTA secondary button", "type": "text",
         "default": "Join a company already here"},
        {"key": "cta_fine", "label": "Closing CTA fine print", "type": "text",
         "default": "If your company is already registered, you'll be placed in it automatically."},

        {"key": "footer_tagline", "label": "Footer tagline", "type": "text",
         "default": "Buinee — one workspace for the work behind your business."},
    ],
    "register": [
        {"key": "brand_headline", "label": "Brand panel headline", "type": "text",
         "default": "Bring your whole team into one place."},
        {"key": "brand_subtext", "label": "Brand panel subtext", "type": "paragraph",
         "default": "Register your company once. Everyone else joins by name and gets the role — and "
                     "the visibility — that fits them."},
        {"key": "brand_foot", "label": "Brand panel footnote", "type": "paragraph",
         "default": "Every join request needs a supervisor's approval — a company name alone never "
                     "grants access."},
        {"key": "gate_title", "label": "\"Choose a plan\" gate title", "type": "text", "default": "Choose a plan first"},
        {"key": "gate_text", "label": "\"Choose a plan\" gate text", "type": "paragraph",
         "default": "Registering a company starts with picking the tier it runs on — how many people it "
                     "covers, and whether the AI assistant is included."},
        {"key": "gate_cta", "label": "\"Choose a plan\" gate button", "type": "text", "default": "See the plans"},
        {"key": "team_h1", "label": "Form heading (team plan)", "type": "text", "default": "Set up Buinee"},
        {"key": "team_subtitle", "label": "Form subtitle (team plan)", "type": "paragraph",
         "default": "Register your company, or join one that's already here."},
        {"key": "solo_h1", "label": "Form heading (solo plan)", "type": "text", "default": "Set up your workspace"},
        {"key": "solo_subtitle", "label": "Form subtitle (solo plan)", "type": "paragraph",
         "default": "Yours alone — nobody else can see it or join it."},
        {"key": "tab_register_team", "label": "Register tab label (team)", "type": "text",
         "default": "Register your company"},
        {"key": "tab_register_solo", "label": "Register tab label (solo)", "type": "text", "default": "Work on your own"},
        {"key": "tab_join", "label": "Join tab label", "type": "text", "default": "Join a company"},
        {"key": "company_caption_team", "label": "Company field label (team)", "type": "text", "default": "Company name"},
        {"key": "company_caption_solo", "label": "Company field label (solo)", "type": "text",
         "default": "Workspace name (optional)"},
        {"key": "reg_note_team", "label": "Register form note (team)", "type": "paragraph",
         "default": "Not the boss? Pick your real role — you get full access to it right away. Whoever "
                     "should hold Supervisor can join afterward and claim it themselves, as long as nobody "
                     "holds it yet."},
        {"key": "reg_note_solo", "label": "Register form note (solo)", "type": "paragraph",
         "default": "You prepare and approve your own vouchers. If you later need colleagues in here, "
                     "ask us to move you onto a team plan — nothing gets set up twice."},
        {"key": "reg_submit_team", "label": "Register submit button (team)", "type": "text", "default": "Register company"},
        {"key": "reg_submit_solo", "label": "Register submit button (solo)", "type": "text",
         "default": "Create my workspace"},
        {"key": "join_note", "label": "Join form note", "type": "paragraph",
         "default": "A supervisor at that company approves every join request before it's active — "
                     "company name alone doesn't grant access. The one exception: claiming Supervisor when "
                     "the company doesn't have one yet gets you in immediately, since there'd be no one to "
                     "approve it."},
        {"key": "join_submit", "label": "Join submit button", "type": "text", "default": "Request to join"},
    ],
    "login": [
        {"key": "brand_headline", "label": "Brand panel headline", "type": "text",
         "default": "Everyone who touches a payment, in one place."},
        {"key": "brand_subtext", "label": "Brand panel subtext", "type": "paragraph",
         "default": "The people who prepare, approve and sign — with the roles and the trail to prove "
                     "who did what, and when."},
        {"key": "brand_foot", "label": "Brand panel footnote", "type": "paragraph",
         "default": "Visibility runs downward only — a supervisor sees everything, an assistant sees "
                     "their own work."},
        {"key": "card_eyebrow", "label": "Card eyebrow", "type": "text", "default": "Welcome back"},
        {"key": "card_h1", "label": "Card heading", "type": "text", "default": "Sign in"},
    ],
    "legal": [
        {"key": "privacy_title", "label": "Privacy Policy — title", "type": "text", "default": "Privacy Policy"},
        {"key": "privacy_intro", "label": "Privacy Policy — intro", "type": "paragraph", "default": ""},
        {"key": "privacy_body", "label": "Privacy Policy — full text (HTML)", "type": "html", "default": ""},
        {"key": "terms_title", "label": "Terms of Use — title", "type": "text", "default": "Terms of Use"},
        {"key": "terms_intro", "label": "Terms of Use — intro", "type": "paragraph", "default": ""},
        {"key": "terms_body", "label": "Terms of Use — full text (HTML)", "type": "html", "default": ""},
        {"key": "cookies_title", "label": "Cookie Policy — title", "type": "text", "default": "Cookie Policy"},
        {"key": "cookies_intro", "label": "Cookie Policy — intro", "type": "paragraph", "default": ""},
        {"key": "cookies_body", "label": "Cookie Policy — full text (HTML)", "type": "html", "default": ""},
        {"key": "refunds_title", "label": "Refund Policy — title", "type": "text", "default": "Refund Policy"},
        {"key": "refunds_intro", "label": "Refund Policy — intro", "type": "paragraph", "default": ""},
        {"key": "refunds_body", "label": "Refund Policy — full text (HTML)", "type": "html", "default": ""},
        {"key": "security_title", "label": "Security — title", "type": "text", "default": "Security"},
        {"key": "security_intro", "label": "Security — intro", "type": "paragraph", "default": ""},
        {"key": "security_body", "label": "Security — full text (HTML)", "type": "html", "default": ""},
    ],
    "contact": [
        {"key": "contact_eyebrow", "label": "Contact page eyebrow", "type": "text", "default": "Get in touch"},
        {"key": "contact_headline", "label": "Contact page headline", "type": "text",
         "default": "We're here when you need us."},
        {"key": "contact_intro", "label": "Contact page intro", "type": "paragraph",
         "default": "Questions about pricing, onboarding, or something not working right? Reach us any of "
                     "these ways."},
        {"key": "contact_email", "label": "Contact email", "type": "text", "default": "hello@buinee.app"},
        {"key": "contact_phone", "label": "Contact phone number", "type": "text", "default": "+233 20 000 0000"},
        {"key": "contact_whatsapp", "label": "WhatsApp number (digits only, with country code)", "type": "text",
         "default": "233200000000"},
        {"key": "contact_address", "label": "Physical address (leave blank to hide)", "type": "text", "default": ""},
        {"key": "social_twitter", "label": "X / Twitter URL (leave blank to hide)", "type": "text", "default": ""},
        {"key": "social_linkedin", "label": "LinkedIn URL (leave blank to hide)", "type": "text", "default": ""},
        {"key": "social_facebook", "label": "Facebook URL (leave blank to hide)", "type": "text", "default": ""},
        {"key": "social_instagram", "label": "Instagram URL (leave blank to hide)", "type": "text", "default": ""},
    ],
}

CMS_PAGE_ROUTES = {"/": "index", "/register": "register", "/login": "login", "/contact": "contact"}

_CMS_TOKEN_RE = re.compile(r"\{\{cms:(?:([a-z0-9_]+)\.)?([a-z0-9_]+)\}\}")
_CMS_JSON_TOKEN_RE = re.compile(r"\{\{cms_json:([a-z0-9_]+)\}\}")


def site_content_effective(page: str) -> dict:
    """Default copy for `page`, overridden by whatever an admin has saved."""
    fields = SITE_CONTENT_SCHEMA.get(page, [])
    overrides = db.get_site_content_overrides(page)
    return {f["key"]: overrides.get(f["key"], f["default"]) for f in fields}


def _cms_field_type(page: str, key: str) -> str:
    for f in SITE_CONTENT_SCHEMA.get(page, []):
        if f["key"] == key:
            return f["type"]
    return "text"


def _render_cms_value(field_type: str, value: str) -> str:
    if field_type == "bullets":
        lines = [ln.strip() for ln in value.split("\n") if ln.strip()]
        return "".join(f"<li>{CMS_CHECK_SVG}{html.escape(ln)}</li>" for ln in lines)
    if field_type == "qa_list":
        items = []
        question, answer = None, []
        for raw_line in value.split("\n"):
            line = raw_line.strip()
            if line.lower().startswith("q:"):
                if question is not None:
                    items.append((question, " ".join(answer).strip()))
                question, answer = line[2:].strip(), []
            elif line.lower().startswith("a:"):
                answer.append(line[2:].strip())
            elif line:
                answer.append(line)
        if question is not None:
            items.append((question, " ".join(answer).strip()))
        return "".join(
            f'<details class="faq-item"><summary>{html.escape(q)}'
            f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            f'<path d="m6 9 6 6 6-6"/></svg></summary>'
            f'<div class="faq-a">{html.escape(a)}</div></details>'
            for q, a in items if q
        )
    if field_type == "paragraph":
        return html.escape(value).replace("\n", "<br>")
    return html.escape(value)


def render_site_content(page: str, raw_html: bytes) -> bytes:
    """Replace {{cms:key}} / {{cms:page.key}} / {{cms_json:page}} tokens in a
    static page's HTML with admin-edited copy (falling back to the schema
    default), escaping every value so saved content can never inject markup
    into the public site. A token may name a different page than the one
    being rendered (e.g. index.html's footer pulling from the "contact"
    page) so the same saved value shows up everywhere it's used."""
    if page not in SITE_CONTENT_SCHEMA:
        return raw_html
    values_cache = {page: site_content_effective(page)}
    text = raw_html.decode("utf-8")

    def repl_token(m: re.Match) -> str:
        ref_page, key = m.group(1) or page, m.group(2)
        if ref_page not in values_cache:
            values_cache[ref_page] = site_content_effective(ref_page) if ref_page in SITE_CONTENT_SCHEMA else {}
        return _render_cms_value(_cms_field_type(ref_page, key), values_cache[ref_page].get(key, ""))

    def repl_json(m: re.Match) -> str:
        return json.dumps(values_cache[page]) if m.group(1) == page else m.group(0)

    text = _CMS_TOKEN_RE.sub(repl_token, text)
    text = _CMS_JSON_TOKEN_RE.sub(repl_json, text)
    return text.encode("utf-8")


# --- public-endpoint limits ------------------------------------------------
MAX_MESSAGE = 600
MAX_TURNS = 12               # per IP, per window
WINDOW_SECONDS = 600
MAX_HISTORY = 8

# --- auth ---------------------------------------------------------------
COOKIE_NAME = "ledgerline_session"
ADMIN_COOKIE_NAME = "ledgerline_admin_session"
TERMS_VERSION = "2026-07-22"

PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_hits: dict[str, list[float]] = {}
_fx: voucher.FxRates | None = None


def load_env() -> dict:
    cfg: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(PROVIDER_KEYS.values()) + [
        "CLERK_PROVIDER", "CLERK_MODEL",
        "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_REDIRECT_URI",
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
        "BUINEE_SECRET_KEY",
        "BUINEE_COOKIE_SECURE", "BUINEE_TRUST_PROXY",
        "PAYSTACK_PUBLIC_KEY", "PAYSTACK_SECRET_KEY", "PAYSTACK_CALLBACK_URL", "PAYSTACK_WEBHOOK_URL",
        # Two per connectable tool plus the shared callback. Generated rather
        # than listed: this deployment configures itself through Passenger
        # environment variables, so a key missing from here is a credential
        # that silently does nothing in production while working locally
        # from .env - which is the worst way to find out.
        tools.REDIRECT_KEY,
        *(k for t in tools.TOOL_IDS
          for k in (tools.client_id_key(t), tools.client_secret_key(t))),
    ]:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def active_provider(cfg: dict) -> str | None:
    want = cfg.get("CLERK_PROVIDER", "").strip().lower()
    if want in PROVIDER_KEYS and cfg.get(PROVIDER_KEYS[want], "").strip():
        return want
    for p, key in PROVIDER_KEYS.items():
        if cfg.get(key, "").strip():
            return p
    return None


def configured_providers(cfg: dict) -> list[str]:
    """Every provider with a key set on this deployment, not just the one
    active_provider() would pick - what a company's model picker is allowed
    to choose from. Fixed order matches PROVIDER_KEYS."""
    return [p for p, key in PROVIDER_KEYS.items() if cfg.get(key, "").strip()]


def resolve_provider_model(cfg: dict, company: dict) -> tuple[str | None, str]:
    """A company's chat provider/model preference, falling back to the
    server default the moment their choice isn't actually configured here -
    e.g. an admin removed that provider's key after the company picked it.
    Never lets a company's stale preference produce a hard failure."""
    configured = configured_providers(cfg)
    provider = company.get("model_provider")
    if provider not in configured:
        provider = active_provider(cfg)
    if not provider:
        return None, ""
    model = (
        (company.get("model_model") or "").strip()
        or cfg.get("CLERK_MODEL", "").strip()
        or providers.DEFAULT_MODELS[provider]
    )
    return provider, model


def resolve_admin_provider_model(cfg: dict) -> tuple[str | None, str]:
    """The Command Center's own chat provider/model preference - same
    fallback behavior as resolve_provider_model, but reading the platform-
    wide setting instead of one company's."""
    settings = db.get_platform_settings()
    configured = configured_providers(cfg)
    provider = settings.get("ai_provider")
    if provider not in configured:
        provider = active_provider(cfg)
    if not provider:
        return None, ""
    model = (
        (settings.get("ai_model") or "").strip()
        or cfg.get("CLERK_MODEL", "").strip()
        or providers.DEFAULT_MODELS[provider]
    )
    return provider, model


def rate_limited(key: str, max_hits: int = MAX_TURNS, window: int = WINDOW_SECONDS) -> bool:
    now = time.time()
    seen = [t for t in _hits.get(key, []) if now - t < window]
    _hits[key] = seen + [now]
    return len(seen) >= max_hits


def client_ip(handler) -> str:
    direct = handler.client_address[0]
    if load_env().get("BUINEE_TRUST_PROXY", "0") == "1":
        forwarded = handler.headers.get("X-Forwarded-For") or ""
        return forwarded.split(",")[0].strip() or direct
    return direct


# ------------------------------------------------------------------- auth helpers

def _cookie_header(name: str, token: str, max_age: int) -> str:
    secure = "; Secure" if load_env().get("BUINEE_COOKIE_SECURE", "1") != "0" else ""
    return f"{name}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"


def _cookie(handler, name: str) -> str | None:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    morsel = jar.get(name)
    return morsel.value if morsel else None


def session_token(handler) -> str | None:
    return _cookie(handler, COOKIE_NAME)


def live_mailbox(user_id: int, cfg: dict, connection_id: int | None = None) -> tuple[dict, dict]:
    """A user's connection plus usable, current credentials.

    Everything that touches a mailbox goes through here rather than trusting
    what was stored at connect time: OAuth access tokens last about an hour,
    so this refreshes when they're close to expiring and re-encrypts the
    result. A refresh the provider rejects means the grant is gone for good
    (revoked, password changed, admin removed the app), so the connection is
    dropped - the UI should say "not connected", not fail forever.
    """
    conn = db.get_mailbox_connection(user_id, connection_id)
    if not conn:
        raise mailbox.MailboxError("No mailbox is connected.")
    try:
        creds = secretstore.decrypt(cfg, conn["credentials_enc"])
    except secretstore.SecretsUnavailable as exc:
        raise mailbox.MailboxError(str(exc)) from exc

    try:
        fresh = mailbox.refresh(cfg, conn["provider"], creds)
    except mailbox.MailboxError:
        db.delete_mailbox_connection(user_id, conn["id"])
        raise mailbox.MailboxError(
            "The mailbox connection has expired or been revoked. "
            "Connect it again to carry on."
        )
    if fresh:
        db.update_mailbox_credentials(user_id, secretstore.encrypt(cfg, fresh), conn["id"])
        creds = fresh
    return conn, creds


def public_mailbox(user_id: int, cfg: dict) -> dict:
    """What the dashboard is told about someone's mailbox. Never credentials."""
    connections = db.list_mailbox_connections(user_id)
    user = db.get_user(user_id)
    limit = db.plan_for_company(user["company_id"])["mailbox_limit"] if user else 1
    accounts = [{"id": conn["id"], "provider": conn["provider"],
                 "label": mailbox.LABELS.get(conn["provider"], conn["provider"]),
                 "email": conn["account_email"], "name": conn["account_name"],
                 "connected_at": conn["connected_at"]} for conn in connections]
    return {
        # Which providers this deployment can actually offer. An empty list
        # is a different situation from "you haven't connected yet", and the
        # UI says so rather than showing buttons that can't work.
        "providers": [
            {"id": p, "label": mailbox.LABELS[p]} for p in mailbox.available(cfg)
        ],
        # Credentials are refused rather than stored in the clear, so the UI
        # needs to explain why connecting is unavailable.
        "secrets_ready": secretstore.is_ready(cfg),
        "secrets_problem": secretstore.why_unavailable(cfg),
        "connected": bool(accounts), "accounts": accounts,
        "account": accounts[0] if accounts else None,
        "mailbox_limit": limit, "can_connect": len(accounts) < limit,
    }


def _clean_tool_ids(raw) -> list[str]:
    """Only ids the catalog actually knows, in catalog order. A tier granting
    a tool that doesn't exist would show up as a permanently unconnectable
    row on somebody's dashboard, with nothing there to explain it."""
    if not isinstance(raw, list):
        raise ValueError("Bad tool list.")
    wanted = {str(x) for x in raw}
    return [t for t in tools.TOOL_IDS if t in wanted]


def public_tools(user: dict, cfg: dict) -> dict:
    """Every tool in the catalog, each with the one thing that matters about
    it right now. Never credentials.

    Three separate reasons a tool might not be connectable, kept apart
    because only one of them is anything the customer can act on:

      `included`    their plan covers it - if not, that's an upgrade.
      `configured`  this deployment holds credentials for that vendor - if
                    not, nobody can connect it and it isn't their fault.
      `secrets_ready` we can encrypt what comes back - if not, we decline to
                    store a token rather than write one in the clear.
    """
    plan = db.plan_for_company(user["company_id"])
    included = set(plan["tool_ids"])
    connections = db.list_tool_connections(user["id"])
    by_tool: dict[str, list] = {}
    for conn in connections:
        by_tool.setdefault(conn["tool"], []).append({
            "id": conn["id"], "label": conn["account_label"],
            "name": conn["account_name"], "connected_at": conn["connected_at"],
            "last_error": conn["last_error"],
        })
    catalog = []
    for entry in tools.public_catalog(cfg):
        catalog.append(entry | {
            "included": entry["id"] in included,
            "connections": by_tool.get(entry["id"], []),
        })
    return {
        "categories": [{"id": cid, "label": label} for cid, label in tools.CATEGORIES],
        "tools": catalog,
        "plan_name": plan["name"],
        "included_count": len(included),
        "connected_count": len(connections),
        "secrets_ready": secretstore.is_ready(cfg),
        "secrets_problem": secretstore.why_unavailable(cfg),
    }


def tool_credentials(user: dict, connection: dict, cfg: dict) -> dict:
    """Decrypt a tool's credentials, renewing the access token first if it is
    close to expiring. A renewal is written back straight away - a token
    refreshed but not stored would be fetched again on the next request, and
    vendors that rotate refresh tokens would then be handed a spent one.

    Every read from a connected tool has to come through here, because this
    is where the plan is re-checked. A downgrade leaves existing connections
    in place rather than deleting somebody's data behind their back, and the
    dashboard tells them it has stopped being read - this is what makes that
    true rather than merely true-for-now.
    """
    if connection["tool"] not in db.plan_for_company(user["company_id"])["tool_ids"]:
        raise tools.ToolError(
            f"{tools.LABELS.get(connection['tool'], 'That tool')} isn't part of your "
            "current plan, so Buinee has stopped reading from it.")
    creds = secretstore.decrypt(cfg, connection["credentials_enc"])
    fresh = tools.refresh(cfg, connection["tool"], creds)
    if fresh:
        db.update_tool_credentials(user["id"], connection["id"], secretstore.encrypt(cfg, fresh))
        creds = fresh
    return creds


def current_user(handler) -> dict | None:
    return db.get_user_by_session(session_token(handler))


def public_user(user: dict) -> dict:
    company = db.get_company(user["company_id"])
    plan = db.plan_for_company(user["company_id"])
    company["plan"] = {
        "id": plan["id"],
        "name": plan["name"],
        "price": plan["price"],
        "currency": plan["currency"],
        "user_limit": plan["user_limit"],
        "audience": plan["audience"],
        "chat_enabled": plan["chat_enabled"],
        "chat_limit": plan["chat_monthly_limit"],
        "mailbox_limit": plan["mailbox_limit"],
        "team_chat_enabled": plan["team_chat_enabled"],
        "tool_ids": plan["tool_ids"],
    }
    company["user_count"] = db.company_user_count(user["company_id"])
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "onboarding_complete": bool(user.get("onboarding_complete", 1)),
        "status": user["status"],
        "company": company,
    }


# -------------------------------------------------- platform admin auth helpers
#
# A separate identity and a separate cookie from the company auth above -
# see db.py's "platform admin" section for why.

def admin_session_token(handler) -> str | None:
    return _cookie(handler, ADMIN_COOKIE_NAME)


def current_admin(handler) -> dict | None:
    return db.get_admin_by_session(admin_session_token(handler))


def public_admin(admin: dict) -> dict:
    return {"id": admin["id"], "name": admin["name"], "email": admin["email"],
            "role": admin.get("role", "owner"), "status": admin.get("status", "active"),
            "mfa_enabled": bool(admin.get("mfa_secret_enc"))}


def admin_is_owner(admin: dict | None) -> bool:
    return bool(admin and admin.get("role", "owner") == "owner")


def _totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _totp_code(secret: str, counter: int) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret: str, supplied: str) -> bool:
    code = re.sub(r"\D", "", supplied or "")
    if len(code) != 6:
        return False
    counter = int(time.time() // 30)
    return any(secrets.compare_digest(_totp_code(secret, counter + drift), code)
               for drift in (-1, 0, 1))


def _mfa_secret(cfg: dict, encrypted: str) -> str:
    return str(secretstore.decrypt(cfg, encrypted).get("totp_secret") or "")


def verify_admin_mfa(admin: dict, supplied: str, consume_recovery: bool = True) -> bool:
    encrypted = admin.get("mfa_secret_enc") or ""
    if not encrypted:
        return True
    try:
        if verify_totp(_mfa_secret(load_env(), encrypted), supplied):
            return True
    except secretstore.SecretsUnavailable:
        pass
    normalized = re.sub(r"[^A-Z0-9]", "", (supplied or "").upper())
    if len(normalized) < 8 or not consume_recovery:
        return False
    recovery_hash = hashlib.sha256(normalized.encode()).hexdigest()
    return db.consume_admin_recovery_code(admin["id"], recovery_hash)


def fx() -> voucher.FxRates | None:
    global _fx
    if _fx is None and FX_FILE.exists():
        try:
            _fx = voucher.FxRates.from_workbook(FX_FILE)
        except Exception as exc:
            print(f"  ! could not load FX rates: {exc}")
    return _fx


def compute_voucher(v: dict) -> dict:
    """Derive a stored voucher's figures for display. db.py only ever stores
    what a preparer typed - the tax/net numbers are always computed fresh
    here, same principle as the landing page's demo (voucher.py, never the
    model, does the arithmetic)."""
    lines = [
        voucher.LineItem(l["description"], l["amount"], l.get("supplier_type", ""), l.get("cost_centre", ""))
        for l in v["lines"]
    ]
    inp = voucher.VoucherInput(
        supplier_name=v["supplier_name"],
        supplier_address=v["supplier_address"],
        supplier_tel=v["supplier_tel"],
        supplier_email=v["supplier_email"],
        invoice_number=v["invoice_number"],
        invoice_date=date.fromisoformat(v["invoice_date"]),
        received_date=date.fromisoformat(v["received_date"]),
        credit_terms_days=v["credit_terms_days"],
        lines=lines,
        vatable_amount=v["vatable_amount"],
        apply_nhil=v["apply_nhil"],
        apply_vat=v["apply_vat"],
        vrpo=v["vrpo"],
        vrpo_deduction=v["vrpo_deduction"],
        non_taxable=v["non_taxable"],
        overpayment=v["overpayment"],
    )
    try:
        result = voucher.compute(inp, fx())
    except ValueError:
        result = voucher.compute(inp, None)
    result["review_notes"] = voucher.review(result)
    result.pop("lines", None)
    for key in ("invoice_date", "received_date", "due_date", "exchange_rate_date"):
        if hasattr(result.get(key), "isoformat"):
            result[key] = result[key].isoformat()
    return result


def _user_name(user_id: int | None) -> str | None:
    if not user_id:
        return None
    u = db.get_user(user_id)
    return u["name"] if u else None


def enrich_voucher(v: dict) -> dict:
    """Attach computed figures and the preparer's/approver's real names -
    every place a voucher gets serialised for the API should go through
    this, so the client never has to resolve a bare user id itself."""
    v["computed"] = compute_voucher(v)
    v["created_by_name"] = _user_name(v["created_by"])
    v["approved_by_name"] = _user_name(v.get("approved_by"))
    return v


def build_voucher_digest(vouchers: list[dict]) -> str:
    """A plain-text summary of the vouchers a signed-in user can see, for
    grounding the authenticated chat - same role as the demo's `computed`
    figures, just scoped to a real account instead of one hypothetical."""
    if not vouchers:
        return "No vouchers exist yet in what this person can see."
    lines = [f"{len(vouchers)} voucher(s) visible to this person:"]
    for v in vouchers:
        c = v["computed"]
        line = (
            f"- #{v['id']} {v['supplier_name']} (invoice {v['invoice_number']}), "
            f"status: {v['status']}, net payable: {c['currency']} {c['net_payable']:,.2f}"
        )
        if c.get("review_notes"):
            line += f" — FLAGGED: {'; '.join(c['review_notes'])}"
        if v["status"] == "rejected" and v.get("rejection_reason"):
            line += f" — rejection reason: {v['rejection_reason']}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------- admin chat digest
#
# Each section here mirrors the exact db call an existing role-gated GET
# endpoint already uses (see _admin_role_request allow-lists on
# /api/admin/{plans,pipeline,payments,activity,errors,inbox,invoices}) - a
# Command Center role only gets a section in Ada's digest if that role can
# already fetch the same data directly today.

def _admin_companies_section(companies: list[dict]) -> str:
    if not companies:
        return "## Companies\nNo companies exist on the platform yet."
    lines = [f"## Companies\n{len(companies)} compan(y/ies) on the platform:"]
    for c in companies[:40]:
        crm, plan, sub = c["crm"], c["plan"], c["subscription"]
        line = (f"- {c['name']} — plan: {plan['name']}, lifecycle: {crm['lifecycle_status']}, "
                f"team: {c['approved_count']} approved / {c['pending_count']} pending, "
                f"subscription: {sub['subscription_status']} ({sub['payment_status']})")
        if crm.get("relationship_owner"):
            line += f", owner: {crm['relationship_owner']}"
        if c.get("needs_team_plan"):
            line += " — FLAGGED: needs a team plan"
        lines.append(line)
    if len(companies) > 40:
        lines.append(f"...and {len(companies) - 40} more not listed here.")
    return "\n".join(lines)


def _admin_plans_section() -> str:
    plans = db.list_plans()
    if not plans:
        return "## Plans\nNo plans are configured."
    lines = ["## Plans"]
    for p in plans:
        lines.append(f"- {p['name']} ({p.get('audience', 'individual')}): "
                      f"{p['currency']} {p['price']:,.2f}, up to {p['user_limit']} user(s)"
                      + (" [default]" if p.get("is_default") else ""))
    return "\n".join(lines)


def _admin_payments_section() -> str:
    payments = db.list_payments()
    if not payments:
        return "## Payments\nNo payments recorded yet."
    lines = [f"## Payments\n{len(payments)} payment(s) recorded, most recent first:"]
    for pay in payments[:20]:
        lines.append(f"- {pay['company_name']}: {pay['currency']} {pay['amount_subunit'] / 100:,.2f}, "
                      f"status: {pay['status']}, plan: {pay.get('plan_name', '')}")
    if len(payments) > 20:
        lines.append(f"...and {len(payments) - 20} more not listed here.")
    return "\n".join(lines)


def _admin_invoices_section() -> str:
    invoices = db.list_admin_invoices()
    if not invoices:
        return "## Invoices\nNo invoices recorded yet."
    lines = [f"## Invoices\n{len(invoices)} invoice(s), most recent first:"]
    for inv in invoices[:20]:
        lines.append(f"- {inv.get('invoice_number', '?')} for {inv.get('company_name', '?')}: "
                      f"{inv.get('customer_name', '')}, total {inv.get('total', 0):,.2f}, "
                      f"status: {inv.get('status', '')}")
    if len(invoices) > 20:
        lines.append(f"...and {len(invoices) - 20} more not listed here.")
    return "\n".join(lines)


def _admin_pipeline_section() -> str:
    opportunities = db.list_crm_opportunities()
    if not opportunities:
        return "## Sales pipeline\nNo sales opportunities recorded yet."
    lines = [f"## Sales pipeline\n{len(opportunities)} opportunity(ies):"]
    for o in opportunities[:25]:
        lines.append(f"- {o['name']} ({o['company_name']}): stage {o['stage']}, "
                      f"{o['currency']} {o['value']:,.2f}, probability {o['probability']}%")
    if len(opportunities) > 25:
        lines.append(f"...and {len(opportunities) - 25} more not listed here.")
    return "\n".join(lines)


def _admin_inbox_section() -> str:
    items = db.list_admin_inbox()
    unread = [i for i in items if i["state"] == "unread"]
    lines = [f"## Support inbox\n{len(items)} item(s) total, {len(unread)} unread. "
             "Most recent unread first:"]
    for i in unread[:20]:
        lines.append(f"- {i['company_name']} ({i['source']}, {i['direction']}): "
                      f"{i['subject'] or '(no subject)'}")
    if not items:
        lines = ["## Support inbox\nNo inbox items yet."]
    return "\n".join(lines)


def _admin_activity_section() -> str:
    log = db.list_admin_activity(per_page=15)
    rows = log["rows"]
    if not rows:
        return "## Recent Command Center activity\nNo activity recorded yet."
    lines = ["## Recent Command Center activity\nMost recent first:"]
    for r in rows:
        lines.append(f"- {r['admin_name']} {r['action']} {r['entity_type']}"
                      + (f" {r['entity_label']}" if r.get("entity_label") else ""))
    return "\n".join(lines)


def _admin_errors_section() -> str:
    errors = db.list_application_errors(limit=20)
    if not errors:
        return "## Recent application errors\nNone recorded recently - the system looks healthy."
    lines = [f"## Recent application errors\n{len(errors)} most recent:"]
    for e in errors:
        lines.append(f"- [{e['source']}] {e['message']}")
    return "\n".join(lines)


ADMIN_DIGEST_SECTIONS = {
    "owner": (_admin_plans_section, _admin_pipeline_section, _admin_payments_section,
              _admin_invoices_section, _admin_activity_section, _admin_errors_section),
    "operations": (_admin_inbox_section, _admin_activity_section),
    "sales": (_admin_pipeline_section,),
    "support": (_admin_inbox_section, _admin_activity_section, _admin_errors_section),
    "billing": (_admin_plans_section, _admin_payments_section, _admin_invoices_section),
}


def build_admin_digest(admin: dict) -> str:
    """Plain-text platform summary for the admin chat, scoped to what this
    admin's Command Center role can already see through other endpoints -
    see ADMIN_DIGEST_SECTIONS and the _admin_role_request allow-lists this
    mirrors."""
    parts = [
        "## Platform totals\n" + json.dumps(db.platform_stats()),
        _admin_companies_section(db.list_companies_with_stats()),
    ]
    for section in ADMIN_DIGEST_SECTIONS.get(admin.get("role", "owner"), ()):
        parts.append(section())
    return "\n\n".join(parts)


def admin_alerts(admin: dict, cfg: dict) -> dict:
    """Periodic, role-scoped 'is anything not working well' signals for the
    Command Center's banner - deliberately cheap (platform_alert_counts is
    COUNT(*)-only) since the client polls this every few minutes. Every role
    sees the base signals (identical to what the Overview page's one-time
    attention widget already shows everyone); errors and payments are only
    included for the roles that can already see the Error log / Payments
    pages, matching the same _admin_role_request allow-lists used there."""
    # Runs on every poll regardless of who's looking, since a compromised or
    # tampered back-office account needs locking fast - not only when an
    # owner's tab happens to be the one open. The finding itself is only
    # ever surfaced to owners, below, since they're the only role that can
    # act on it from the Back-office Team page.
    db.check_role_integrity()
    counts = db.platform_alert_counts()
    role = admin.get("role", "owner")
    alerts = []
    if counts["pending_access"]:
        alerts.append({"key": "pending_access", "n": counts["pending_access"],
                       "title": "Pending access requests", "copy": "Across all companies",
                       "href": "/admin/companies"})
    if counts["missing_supervisor"]:
        alerts.append({"key": "missing_supervisor", "n": counts["missing_supervisor"],
                       "title": "No supervisor assigned", "copy": "Companies without an approver",
                       "href": "/admin/companies"})
    if counts["needs_team_plan"]:
        alerts.append({"key": "needs_team_plan", "n": counts["needs_team_plan"],
                       "title": "Move to a Team tier", "copy": "Multi-user companies still on Individual",
                       "href": "/admin/companies"})
    if not active_provider(cfg):
        alerts.append({"key": "ai_not_configured", "n": 1,
                       "title": "No AI provider configured", "copy": "Ada and the demo agent are both offline",
                       "href": "/admin/settings"})
    if fx() is None:
        alerts.append({"key": "fx_missing", "n": 1,
                       "title": "Bank of Ghana FX rates not loaded", "copy": "Multi-currency vouchers may be affected",
                       "href": "/admin"})
    if role in ("owner", "support") and counts["recent_errors"]:
        alerts.append({"key": "recent_errors", "n": counts["recent_errors"],
                       "title": "Application errors in the last hour", "copy": "Check the error log",
                       "href": "/admin/errors"})
    if role in ("owner", "billing") and counts["failed_payments"]:
        alerts.append({"key": "failed_payments", "n": counts["failed_payments"],
                       "title": "Failed payments in the last 24 hours", "copy": "Check Payments",
                       "href": "/admin/payments"})
    if role == "owner" and counts["ada_pending"]:
        alerts.append({"key": "ada_pending", "n": counts["ada_pending"],
                       "title": "Ada-registered signups awaiting review", "copy": "Created via the landing chat",
                       "href": "/admin/companies"})
    if role == "owner" and counts["security_lockouts"]:
        alerts.append({"key": "security_lockouts", "n": counts["security_lockouts"],
                       "title": "Ada locked a back-office account", "copy": "Role/status didn't match the audit trail",
                       "href": "/admin/team"})
    return {"alerts": alerts, "total": sum(a["n"] for a in alerts)}


# Definitions stay in code while user choices/results stay in the database.
# Adding a recipe is one registry entry plus a runner branch; existing rows and
# clients continue to work because recipe_key is deliberately open-ended.
AUTOMATION_RECIPES = {
    "morning_triage": {
        "name": "Morning triage & brief", "schedule": "06:30 GMT · weekdays",
        "description": "Read overnight mail, categorize it, draft replies, and list what needs attention.",
    },
    "invoice_crosscheck": {
        "name": "Invoice cross-check", "schedule": "Every 15 minutes",
        "description": "Compare incoming invoices with visible vouchers and flag differences.",
    },
}


def next_automation_run(recipe_key: str, now: float | None = None) -> float:
    current = datetime.fromtimestamp(now or time.time(), timezone.utc)
    if recipe_key == "invoice_crosscheck":
        return (current + timedelta(minutes=15)).timestamp()
    candidate = current.replace(hour=6, minute=30, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def public_automations(user_id: int) -> list[dict]:
    states = db.automation_states(user_id)
    return [
        {"key": key, **recipe, **states.get(key, {"enabled": 0, "next_run_at": None})}
        for key, recipe in AUTOMATION_RECIPES.items()
    ]


def run_automation(user_id: int, recipe_key: str) -> dict:
    if recipe_key not in AUTOMATION_RECIPES:
        raise ValueError("Unknown automation.")
    user = db.get_user(user_id)
    if not user or user["status"] != "approved":
        raise ValueError("This user can no longer run automations.")
    gate = db.can_use_chat(user["company_id"])
    if not gate["allowed"]:
        raise ValueError(f"AI automations aren't included in the {gate['plan']} plan.")
    cfg = load_env()
    pub = public_user(user)
    provider, model = resolve_provider_model(cfg, pub["company"])
    if not provider:
        raise ValueError("Ada isn't configured on this server yet.")
    messages = []
    for saved in db.list_mailbox_connections(user_id):
        connection, creds = live_mailbox(user_id, cfg, saved["id"])
        messages.extend(mailbox.list_recent(cfg, connection, creds, include_body=True))
    readable = [m for m in messages if (m.get("body") or "").strip()]
    if not readable:
        return {"headline": "No readable new mail", "items": [], "message_count": 0}

    emails = []
    for message in readable:
        from_name, from_email = email.utils.parseaddr(message.get("from") or "")
        emails.append({
            "id": str(message.get("id") or ""), "from_name": from_name or from_email,
            "from_email": from_email, "received": message.get("received") or "",
            "subject": message.get("subject") or "(no subject)",
            "attachments": [item.get("name") for item in message.get("attachments", [])],
            "body": message.get("body") or "", "unread": bool(message.get("unread")),
        })

    items = providers.triage(
        provider, model, cfg.get(PROVIDER_KEYS[provider], ""), emails,
        briefing=effective_briefing(pub),
    )
    db.increment_chat_usage(user["company_id"])
    if recipe_key == "morning_triage":
        important = [item for item in items if item.get("priority") != "low" or item.get("needs_reply")]
        return {"headline": f"{len(important)} message(s) need attention", "items": important, "message_count": len(items)}

    invoices = [item for item in items if item.get("category") == "invoice"]
    visible = db.list_vouchers(user["company_id"], user["id"], user["role"])
    for voucher_row in visible:
        enrich_voucher(voucher_row)
    digest = build_voucher_digest(visible)
    if not invoices:
        return {"headline": "No invoices found in recent mail", "items": [], "message_count": len(items)}
    if not db.can_use_chat(user["company_id"])["allowed"]:
        raise ValueError("The invoice scan used the last AI message available on this plan; cross-checking needs one more.")
    invoice_ids = {item.get("id") for item in invoices}
    docs = [{"kind": "text", "source": "attached", "name": mail["subject"], "text": mail["body"]}
            for mail in emails if mail["id"] in invoice_ids][:3]
    comparison = providers.chat(
        provider, model, cfg.get(PROVIDER_KEYS[provider], ""),
        "Cross-check these incoming invoices against the voucher digest. List exact matches, missing vouchers, and any conflicting supplier, reference, or amount. Do not invent a match.",
        digest, [], system=build_chat_system(pub),
        briefing=effective_briefing(pub, include_library_text=False),
        docs=(docs + user_reference_docs(user["id"]))[:10],
    )
    db.increment_chat_usage(user["company_id"])
    return {"headline": f"{len(invoices)} invoice(s) checked", "summary": comparison, "items": invoices}


def build_chat_system(user: dict) -> str:
    label = {
        "account_assistant": "a Preparer, who prepares vouchers",
        "senior_accountant": "an Approver, who approves and signs vouchers",
        "finance_supervisor": "the Supervisor, who oversees the whole workspace",
    }.get(user["role"], user["role"])
    return (
        providers.CHAT_SYSTEM
        + f"\n\nThey are {user['name']}, {label} at {user['company']['name']}. "
        "Everything in the digest below is already scoped to what their role "
        "can see in the product - do not tell them a voucher exists that "
        "isn't listed, and do not assume they can see more than this."
    )


ADMIN_ROLE_LABELS = {
    "owner": "the Owner, with full Command Center access",
    "operations": "Operations, handling companies and service follow-ups",
    "sales": "Sales, running the CRM and pipeline",
    "support": "Support, resolving customer issues",
    "billing": "Billing, handling payments, subscriptions and plans",
}


def build_admin_chat_system(admin: dict) -> str:
    label = ADMIN_ROLE_LABELS.get(admin.get("role", "owner"), admin.get("role", "owner"))
    return (
        providers.ADMIN_CHAT_SYSTEM
        + f"\n\nThey are {admin['name']}, {label}. Everything in the digest "
        "below is already scoped to what their Command Center role can see "
        "in the product - do not tell them about data that isn't listed, and "
        "do not assume they can see more than this."
    )


def effective_briefing(user: dict, include_library_text: bool = True) -> str:
    parts = []
    company_text = (user.get("company") or {}).get("briefing", "").strip()
    personal_text = db.get_user_instructions(user["id"]).strip()
    if company_text:
        parts.append("## Company instructions\n" + company_text)
    if personal_text:
        parts.append("## Personal instructions for this user\n" + personal_text)
    if include_library_text:
        docs = db.list_reference_documents(user["id"], include_content=True)
        text_docs = [f"### {d['name']}\n{d['text_content']}" for d in docs if d["kind"] == "text" and d["text_content"]]
        if text_docs:
            parts.append("## Personal reference documents\n" + "\n\n".join(text_docs)[:30000])
    return "\n\n".join(parts)


def user_reference_docs(user_id: int) -> list[dict]:
    out = []
    for doc in db.list_reference_documents(user_id, include_content=True):
        if doc["kind"] == "text":
            out.append({"kind": "text", "source": "library", "name": doc["name"], "text": doc["text_content"]})
        elif doc["data_base64"]:
            out.append({"kind": doc["kind"], "source": "library", "name": doc["name"],
                        "media_type": doc["media_type"], "data": doc["data_base64"]})
    return out


# ----------------------------------------------------------- the demo itself

_AMOUNT = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")

# For flagging a landing-chat conversation for the Command Center inbox -
# best-effort, not validation. A false positive just means a harmless
# unread flag; a false negative means a lead sits unflagged until someone
# reads the inbox anyway, so these stay deliberately permissive.
_DEMO_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DEMO_PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{6,}\d")
_DEMO_NAME_RE = re.compile(
    r"(?i:my name(?:'s| is)|i'?m|i am|this is|name'?s)\s+"
    r"([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,2})"
)
_DEMO_COMPLAINT_WORDS = (
    "complain", "complaint", "unhappy", "frustrat", "terrible", "awful",
    "worst", "refund", "cancel", "not working", "broken", "disappointed",
    "angry", "scam", "unacceptable", "poor service", "bad experience",
)

_ADA_REGISTER_RE = re.compile(r"\[\[ADA_REGISTER\]\](.*?)\[\[/ADA_REGISTER\]\]", re.S)


def extract_ada_register(reply: str) -> tuple[str, dict | None]:
    """Split Ada's marker (if any) out of her reply, and turn it into a
    validated registration draft the frontend can act on. The marker is
    never shown to the visitor - present or malformed, it's always stripped
    before the reply leaves this function, so a bad marker degrades to
    "the offer just doesn't appear" rather than a broken-looking reply."""
    m = _ADA_REGISTER_RE.search(reply)
    if not m:
        return reply, None
    cleaned = (reply[:m.start()] + reply[m.end():]).strip()
    try:
        raw = json.loads(m.group(1))
        name = str(raw.get("name") or "").strip()[:120]
        email = str(raw.get("email") or "").strip().lower()[:180]
        company_name = str(raw.get("company_name") or "").strip()[:120]
        plan_id = int(raw.get("plan_id"))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return cleaned, None
    if len(name) < 2 or "@" not in email:
        return cleaned, None
    plan = db.get_plan(plan_id)
    if not plan:
        return cleaned, None
    if plan["audience"] != "individual" and len(company_name) < 2:
        return cleaned, None
    return cleaned, {
        "name": name, "email": email, "company_name": company_name,
        "plan_id": plan_id, "plan_name": plan["name"], "audience": plan["audience"],
        "price": plan["price"], "currency": plan["currency"],
    }


def maybe_compute(message: str) -> dict | None:
    """If the visitor gave an invoice total and a vatable portion, compute it.

    Deliberately conservative: two plausible money figures, largest treated as
    the invoice total. Guessing wrong is better than the model inventing a
    number, because the reply shows its working and can be corrected.
    """
    nums = [float(m.replace(",", "")) for m in _AMOUNT.findall(message)]
    money = [n for n in nums if n >= 100]
    if len(money) < 2:
        return None
    total, vatable = max(money), min(money)
    if vatable > total:
        return None
    try:
        return voucher.compute(
            voucher.VoucherInput(
                supplier_name="(demo supplier)",
                invoice_number="(demo)",
                invoice_date=date.today(),
                received_date=date.today(),
                credit_terms_days=30,
                vatable_amount=vatable,
                lines=[voucher.LineItem("Demonstration line", total)],
            ),
            fx(),
        )
    except Exception:
        return None


SYSTEM = """You are Ada, greeting visitors on Buinee's public website. Your \
job here is different from Ada inside a customer's dashboard: you are not \
grounded in anyone's real data, you are helping a stranger work out what \
Buinee is, whether it fits their business, and how to get started.

What Buinee is, so you can answer accurately:
- One workspace that brings a team's messages, documents, tasks, approvals,
  customer work and AI assistance into one place, instead of scattered across
  chat apps, inboxes and spreadsheets.
- Once a company registers, every person gets a role-appropriate view and a
  clear history of what happened - not everyone sees everything.
- Once registered, each person's dashboard comes with an AI assistant grounded
  in their actual work: it can read their connected mailbox overnight, draft
  replies in their tone, read a document they hand it, and follow written-down
  company rules (approval thresholds, templates, escalation paths). It never
  sends or approves anything on its own - a person always does that.
- Vouchers - prepare a payment request, get it approved, with a real approval
  trail and signature recorded in the system - are the first concrete workflow
  built on this model, not the only thing the product does.
- Every company's records, documents and conversations are isolated to that
  company. Nothing is visible across organisations.
- Registration: someone creates the company's workspace, picks a plan (solo
  or team), and colleagues then join by name afterward - no per-seat setup or
  IT project. If their company already registered, they join that one instead
  of creating a new one.
- Current pricing, if asked, is in the context below - present it exactly,
  never estimate or round it.

A small bonus you can actually demonstrate live: Ghana's payment-voucher tax
computation (5% NHIL/GETFL, 15% VAT, 7.5% withholding tax, applied to the
vatable portion of an invoice - which is often only part of the total. Net
payable is the invoice total less withholding tax). If a visitor gives two
plausible money figures, a computation may appear below - present exactly
those figures and mention this is the same engine the product uses, never
recompute it yourself. Offer this only if they're curious about the vouchers
workflow specifically, not as your main pitch.

Getting their details - do this early, not as an afterthought:
As soon as it's clear you're talking to someone genuinely interested in
Buinee for their business (not just idly asking what it is), get their name
and a way to reach them - phone number, email, or both - before going deep
into a long back-and-forth. Ask naturally, in one line, framed as "so
someone can follow up if we get cut off" - not as a cold form. Still answer
whatever they just asked first; don't dodge a direct question to demand
contact details before responding to it. If they decline or dodge, don't
push twice - answer their question anyway.

Registering them yourself - you can actually do this, not just describe it:
Once someone has clearly decided to sign up (not just asking questions - they
said something like "let's do it" or "sign me up" or picked a plan), you can
create their workspace right in this chat, instead of sending them to the
registration page. To do that you need, gathered naturally over the
conversation rather than as a form:
- Their name.
- Their email.
- Which plan (match it to one of the plan_id values in the pricing list
  above - never invent an id, never guess one if you're unsure which plan
  they mean, just ask).
- A company name - but only if the plan is a team plan. Solo/individual
  plans don't need one; if they haven't given one, leave it blank rather
  than asking for it.
Do not ask for or accept a password here under any circumstances, even if
they offer one - passwords are never handled in chat. Once you have
everything above and they've confirmed they want to proceed, tell them
their workspace is being set up and that a secure box will appear right
here in the chat for them to set a password and finish - then, as the very
last thing in your reply, on its own, emit exactly this and nothing else
around it:
[[ADA_REGISTER]]{"name":"...","email":"...","company_name":"...","plan_id":0}[[/ADA_REGISTER]]
with the real values filled in (company_name as "" for a solo plan). This
marker is never shown to the visitor and you should never mention it,
describe it, or explain that you're "generating a code" - it's invisible
plumbing, not something to narrate. Emit it once per signup, not on every
later reply. If registration fails for a reason you're told about (for
example a company by that name already exists), explain it in plain terms
and suggest registering directly at /register instead.

Rules for you:
- Be brief. Two or three sentences unless they asked for detail. You are on a
  landing page, not in a meeting.
- Never invent a feature, a price or a launch date. Buinee is early - if you
  do not know, say so plainly rather than guessing.
- You have no access to anyone's real account, mailbox or documents here -
  that only exists once their company is registered and they're signed in.
  If someone describes an account problem, direct them to register or sign in
  rather than trying to solve it.
- If they seem convinced but haven't said they're ready to sign up right now,
  point them at registering their company (either you doing it here, or the
  registration page). Do not push it into every reply."""


def build_system(computed: dict | None) -> str:
    if not computed:
        return SYSTEM
    v = computed
    lines = [
        SYSTEM,
        "\n\n## Figures computed for this visitor - present these exactly",
        f"Invoice total: {v['total_invoice']:,.2f}",
        f"Vatable portion: {v['vatable_amount']:,.2f}",
        f"NHIL/GETFL at 5%: {v['nhil_getfl']:,.2f}",
        f"VAT at 15%: {v['vat']:,.2f}",
        f"Withholding tax at 7.5%: {v['wht']:,.2f}",
        f"Net amount payable: {v['net_payable']:,.2f}",
    ]
    if v.get("exchange_rate"):
        lines.append(
            f"Bank of Ghana rate used: {v['exchange_rate']:.4f} "
            f"(published {v['exchange_rate_date']}), so the net payable is "
            f"{v['fcy_net_payable']:,.2f} in USD."
        )
    lines.append(
        "Say plainly that these were calculated, not estimated, and that in the "
        "product the figures are read off the invoice rather than typed."
    )
    return "\n".join(lines)


def landing_plans_digest() -> str:
    """Current pricing, for the public demo to answer from instead of
    guessing - see /api/plans, which the landing page's own pricing section
    renders from the same list."""
    plans = db.list_plans()
    if not plans:
        return "## Current pricing\nNo pricing plans are configured yet."
    lines = ["## Current pricing"]
    for p in plans:
        seats = "just one person" if p["audience"] == "individual" else f"up to {p['user_limit']} people"
        price = f"{p['currency']} {p['price']:,.2f}/month" if p["price"] else "free"
        if not p["chat_enabled"]:
            chat = "no AI assistant"
        elif p["chat_monthly_limit"] is None:
            chat = "unlimited AI assistant messages"
        else:
            chat = f"{p['chat_monthly_limit']} AI assistant messages/month"
        lines.append(
            f"- {p['name']} (plan_id: {p['id']}, {p['audience']}): {price}, {seats}, "
            f"{p['mailbox_limit']} connected mailbox(es) per user, {chat}"
            + (", team chat included" if p.get("team_chat_enabled") else "")
            + _tools_digest(p)
        )
    return "\n".join(lines)


def _tools_digest(plan: dict) -> str:
    """What a tier can connect to, for Ada to answer from. Named rather than
    counted: a visitor asking "does it work with Slack?" wants to hear Slack,
    not "eight integrations"."""
    labels = [tools.LABELS[t] for t in plan["tool_ids"] if t in tools.LABELS]
    if not labels:
        return ", no tool connections"
    return ", connects to " + ", ".join(labels)


LIVECHAT_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def is_livechat_online(settings: dict, now: float | None = None) -> bool:
    """Whether the landing page's Ask Ada widget should show as online.

    Times in the schedule are plain HH:MM with no timezone conversion -
    Ghana is UTC+0 year-round (no DST), so treating 'now' as UTC already
    matches Ghana local time without pulling in a timezone library."""
    mode = settings.get("livechat_mode", "schedule")
    if mode == "always_on":
        return True
    if mode == "always_off":
        return False
    try:
        schedule = json.loads(settings.get("livechat_schedule_json") or "{}")
    except (TypeError, ValueError):
        return True
    if not schedule:
        return True  # never configured - don't silently take the widget offline
    dt = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
    entry = schedule.get(LIVECHAT_DAYS[dt.weekday()])
    if not entry or not entry.get("enabled"):
        return False
    try:
        start_h, start_m = (int(x) for x in str(entry.get("start", "00:00")).split(":")[:2])
        end_h, end_m = (int(x) for x in str(entry.get("end", "23:59")).split(":")[:2])
    except ValueError:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    return start_h * 60 + start_m <= now_minutes < end_h * 60 + end_m


def paystack_config(cfg: dict) -> dict:
    return {
        "public_key": cfg.get("PAYSTACK_PUBLIC_KEY", ""),
        "secret_key": cfg.get("PAYSTACK_SECRET_KEY", ""),
        "callback_url": cfg.get("PAYSTACK_CALLBACK_URL", "https://buinee.app/api/paystack/callback"),
        "webhook_url": cfg.get("PAYSTACK_WEBHOOK_URL", "https://buinee.app/api/paystack/webhook"),
    }


def poll_connected_mailboxes() -> dict:
    """Header-only polling used by the scheduled runner for arrival alerts."""
    cfg = load_env()
    checked = arrivals = failed = 0
    for saved in db.list_all_mailbox_connections():
        try:
            connection, creds = live_mailbox(saved["user_id"], cfg, saved["id"])
            messages = mailbox.list_recent(cfg, connection, creds, limit=25)
            arrivals += db.record_mailbox_poll(connection, messages)
            checked += 1
        except Exception as exc:
            failed += 1
            db.record_mailbox_poll_error(saved["id"], saved["user_id"], str(exc))
            print(f"mailbox poll failed connection={saved['id']}: {exc}")
    return {"checked": checked, "arrivals": arrivals, "failed": failed}


def _mail_received_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return parsed.timestamp() if parsed else 0
        except (ValueError, TypeError, OverflowError):
            return 0


def recent_mail_context(user_id: int, cfg: dict, limit: int = 8) -> str:
    """Recent connected-mail content supplied only for mailbox-related chat."""
    messages = []
    for saved in db.list_mailbox_connections(user_id):
        try:
            connection, creds = live_mailbox(user_id, cfg, saved["id"])
            for item in mailbox.list_recent(cfg, connection, creds, limit=5, include_body=True):
                item = dict(item)
                item["mailbox_email"] = connection["account_email"]
                messages.append(item)
        except mailbox.MailboxError:
            continue
    messages.sort(key=lambda item: _mail_received_timestamp(item.get("received") or ""), reverse=True)
    lines = []
    for index, item in enumerate(messages[:limit], 1):
        attachments = ", ".join(file.get("name") or "attachment" for file in item.get("attachments", [])) or "none"
        lines.append(
            f"### {index}. {'LATEST EMAIL' if index == 1 else 'Recent email'}\n"
            f"Inbox: {item.get('mailbox_email') or ''}\nFrom: {item.get('from') or ''}\n"
            f"Received: {item.get('received') or ''}\nSubject: {item.get('subject') or '(no subject)'}\n"
            f"Attachments: {attachments}\nBody:\n{(item.get('body') or '(no readable body)')[:12000]}"
        )
    return "\n\n".join(lines)


# Read at most this many connections per message. Each is a round trip to
# somebody else's API, and a chat that takes eight seconds to answer is worse
# than one that saw three tools instead of five.
MAX_CONTEXT_CONNECTIONS = 3


def recent_tools_context(user: dict, cfg: dict, tool_ids: list[str], limit: int = 12) -> str:
    """Recent items from this person's connected tools, supplied only for
    chat that is plausibly about them.

    Everything here is text other people wrote - Slack messages, Notion
    pages, Trello cards - arriving from outside the product. It's fenced and
    labelled as data below, and one broken tool is skipped rather than
    allowed to take the whole answer down with it.
    """
    # Entitlement is settled here rather than left to tool_credentials to
    # refuse: a paused tool isn't a broken connection, and recording it as
    # one would put a red error on a card that already explains itself.
    wanted = set(tool_ids) & set(db.plan_for_company(user["company_id"])["tool_ids"])
    items = []
    for saved in db.list_tool_connections(user["id"]):
        if saved["tool"] not in wanted:
            continue
        if len({i["_tool"] for i in items}) >= MAX_CONTEXT_CONNECTIONS:
            break
        try:
            creds = tool_credentials(user, saved, cfg)
            for item in tools.list_recent(cfg, saved["tool"], creds, limit=5):
                items.append(dict(item) | {"_tool": saved["tool"],
                                           "_account": saved["account_label"]})
        except (tools.ToolError, secretstore.SecretsUnavailable) as exc:
            # Recorded so the Connections card can explain itself later; the
            # chat carries on without this tool rather than failing.
            db.record_tool_error(saved["id"], str(exc))
            continue
    if not items:
        return ""
    items.sort(key=lambda i: i.get("when") or "", reverse=True)

    lines = []
    for item in items[:limit]:
        label = tools.LABELS.get(item["_tool"], item["_tool"])
        lines.append(
            f"- [{label} · {item.get('subtitle') or ''}] {item.get('title') or ''}"
            f"{(' (' + item['when'] + ')') if item.get('when') else ''}"
        )
    return (
        "## Recent items from connected tools\n"
        + providers.UNTRUSTED_CONTENT_NOTE
        + " Do not claim anything is in these tools beyond what is listed.\n\n"
        + "\n".join(lines)
    )


def paystack_api(method: str, path: str, cfg: dict, payload: dict | None = None) -> dict:
    ps = paystack_config(cfg)
    if not ps["secret_key"]:
        raise ValueError("Paystack is not configured.")
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request("https://api.paystack.co" + path, data=body, method=method,
        headers={"Authorization": "Bearer " + ps["secret_key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError("Paystack could not be reached. Try again.") from exc
    if not result.get("status"):
        raise ValueError(str(result.get("message") or "Paystack rejected the request."))
    return result.get("data") or {}


def initialize_plan_payment(user: dict, plan: dict, cfg: dict) -> dict:
    amount = int(round(float(plan["price"]) * 100))
    if amount <= 0:
        raise ValueError("This plan has no payment due.")
    reference = "buinee-" + secrets.token_hex(12)
    ps = paystack_config(cfg)
    db.create_payment_intent(user["company_id"], user["id"], plan["id"], amount,
                             plan["currency"].upper(), user["email"], reference)
    result = paystack_api("POST", "/transaction/initialize", cfg, {
        "email": user["email"], "amount": str(amount),
        "currency": plan["currency"].upper(), "reference": reference,
        "callback_url": ps["callback_url"],
        "metadata": json.dumps({"company_id": user["company_id"],
                                "plan_id": plan["id"], "registration": True}),
    })
    return {"authorization_url": result.get("authorization_url"), "reference": reference}


# --------------------------------------------------------------- route logic
#
# Every route lives here, transport-agnostic. A caller just needs to provide
# self.path, self.headers (an object with .get(name)), self.rfile (readable),
# and self.client_address (a tuple, [0] used). _send() never touches a socket
# - it only sets attributes; each transport reads them afterward.

class RouteHandlerMixin:
    def _send(self, code, body: bytes, ctype: str, extra_headers=None):
        self._status = code
        self._resp_headers = [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
            ("Content-Security-Policy",
             "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
             "form-action 'self'; script-src 'self' 'unsafe-inline'; "
             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "font-src 'self'; connect-src 'self'"),
            *list(extra_headers or []),
        ]
        self._resp_body = b"" if getattr(self, "_head_only", False) else body

    def _json(self, obj, code=200, extra_headers=None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra_headers)

    def _redirect(self, location: str, status: int = 302):
        """Browser-facing redirect - used by the OAuth round trip, which is a
        navigation rather than a fetch, so it can't answer in JSON."""
        self._send(status, b"", "text/plain; charset=utf-8", [("Location", location)])

    def _body(self, max_len: int = 20000) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_len:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _raw_body(self, max_len: int = 100000) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_len:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def _post_is_same_origin(self) -> bool:
        """Reject browser cross-site mutations while preserving API clients."""
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if fetch_site == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin:
            origin_url = urlparse(origin)
            expected_host = (self.headers.get("Host") or "").lower()
            return origin_url.scheme in ("https", "http") and origin_url.netloc.lower() == expected_host
        # Modern browsers send Origin on POST. Requests without it are allowed
        # only when they carry no authenticated Buinee cookie (CLI/API login,
        # registration and health tooling).
        raw_cookie = self.headers.get("Cookie") or ""
        return COOKIE_NAME not in raw_cookie and ADMIN_COOKIE_NAME not in raw_cookie

    # ---------------------------------------------------------------- GET

    def _route_safely(self, fn) -> None:
        """Run a route handler, turning any exception that escapes it into a
        JSON 500 instead of a dead connection. Without this, an unhandled
        exception anywhere in a handler (e.g. a missed foreign-key cleanup)
        propagates all the way out of do_GET/do_POST/application with no
        response ever written - the browser sees that as the request simply
        failing, indistinguishable from the server being unreachable."""
        try:
            fn()
        except Exception as exc:
            reference = report_application_error(
                "server.unhandled", exc, context=f"{getattr(self, 'command', '')} {self.path}".strip())
            print(f"  ! unhandled exception: {exc}")
            try:
                self._json({"error": f"Something went wrong on our end. Reference: {reference}."}, 500)
            except Exception:
                pass

    def _route_get(self):
        path = self.path.split("?")[0]

        if path in LEGACY_PAGE_REDIRECTS:
            query = urlparse(self.path).query
            location = LEGACY_PAGE_REDIRECTS[path] + (f"?{query}" if query else "")
            return self._redirect(location, 301)

        if path == "/api/demo/status":
            cfg = load_env()
            return self._json({"available": active_provider(cfg) is not None})

        if path == "/api/me":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"user": public_user(user)})

        if path == "/api/company/pending":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            if user["role"] != "finance_supervisor":
                return self._json({"error": "Only a supervisor can see this."}, 403)
            return self._json({"pending": db.list_pending(user["company_id"])})

        if path == "/api/company/team":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"team": db.list_team(user["company_id"])})

        if path == "/api/company/profile":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            if user["role"] != "finance_supervisor":
                return self._json({"error": "Only a supervisor can manage the company profile."}, 403)
            return self._json({"profile": db.get_company_profile(user["company_id"])})

        if path == "/api/notifications":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            db.touch_presence(user["id"])
            return self._json({"notifications": db.notification_summary(user)})

        if path == "/api/follow-ups":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"tasks": db.list_user_crm_tasks(user)})

        if path == "/api/daily-briefing":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            briefing = db.daily_briefing(user)
            morning = next((item for item in public_automations(user["id"])
                            if item["key"] == "morning_triage"), {})
            latest = morning.get("latest_run") or {}
            briefing["mail"] = latest.get("result") if latest.get("status") == "complete" else None
            briefing["role"] = user["role"]
            return self._json({"briefing": briefing})

        if path == "/api/payments":
            user = current_user(self)
            if not user or user["status"] != "approved" or user["role"] != "finance_supervisor":
                return self._json({"error": "Only a supervisor can manage billing."}, 403)
            return self._json({"payments": db.list_payments(user["company_id"], 20),
                               "configured": bool(paystack_config(load_env())["secret_key"])})

        if path == "/api/paystack/callback":
            reference = parse_qs(urlparse(self.path).query).get("reference", [""])[0]
            payment = db.get_payment(reference)
            verified = False
            if payment:
                try:
                    recorded = db.record_paystack_payment(
                        paystack_api("GET", "/transaction/verify/" + quote(reference), load_env())
                    )
                    verified = bool(recorded and recorded.get("status") == "success")
                except ValueError:
                    pass
            return self._redirect("/dashboard?payment=success" if verified else "/login?payment=failed")

        if path == "/api/team-chat":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            if not db.plan_for_company(user["company_id"])["team_chat_enabled"]:
                return self._json({"error": "Team chat requires a team plan."}, 403)
            try:
                query = parse_qs(urlparse(self.path).query)
                after = int(query.get("after", ["0"])[0])
                recipient_raw = query.get("recipient", ["group"])[0]
                recipient_id = None if recipient_raw == "group" else int(recipient_raw)
            except ValueError:
                return self._json({"error": "Bad conversation."}, 400)
            return self._json({
                "messages": db.list_team_messages(
                    user["company_id"], user["id"], recipient_id, after
                )
            })

        if path == "/api/team-chat/file":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            if not db.plan_for_company(user["company_id"])["team_chat_enabled"]:
                return self._json({"error": "Team chat requires a team plan."}, 403)
            try:
                file_id = int(parse_qs(urlparse(self.path).query).get("id", [""])[0])
            except ValueError:
                return self._json({"error": "Bad file id."}, 400)
            file = db.get_team_file(user["company_id"], user["id"], file_id)
            if not file:
                return self._json({"error": "File not found."}, 404)
            try:
                body = (base64.b64decode(file["data_base64"], validate=True)
                        if file["data_base64"] else file["text_content"].encode("utf-8"))
            except binascii.Error:
                return self._json({"error": "Stored file is damaged."}, 500)
            disposition = "attachment; filename*=UTF-8''" + quote(file["name"])
            return self._send(200, body, file["media_type"] or "application/octet-stream",
                              [("Content-Disposition", disposition)])

        if path == "/api/company/model-options":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            cfg = load_env()
            company = db.get_company(user["company_id"])
            provider, model = resolve_provider_model(cfg, company)
            return self._json({
                "configured": configured_providers(cfg),
                "current": {"provider": provider, "model": model},
                "saved": {"provider": company["model_provider"], "model": company["model_model"] or ""},
            })

        if path == "/api/user/instructions":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"briefing": db.get_user_instructions(user["id"])})

        if path == "/api/user/reference-documents":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"documents": db.list_reference_documents(user["id"])})

        if path == "/api/company/chat-usage":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json(db.can_use_chat(user["company_id"]))

        if path == "/api/vouchers":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            vouchers = db.list_vouchers(user["company_id"], user["id"], user["role"])
            for v in vouchers:
                enrich_voucher(v)
            return self._json({"vouchers": vouchers})

        if path == "/api/activity":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({
                "events": db.list_activity(user["company_id"], user["id"], user["role"]),
            })

        if path == "/api/join/search":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            return self._json({"companies": db.find_companies_by_name(q)})

        if path == "/api/mailbox/status":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json(public_mailbox(user["id"], load_env()))

        if path == "/api/mailbox/messages":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            try:
                include_body = parse_qs(urlparse(self.path).query).get("body", ["0"])[0] == "1"
                msgs = []
                for saved in db.list_mailbox_connections(user["id"]):
                    conn, creds = live_mailbox(user["id"], load_env(), saved["id"])
                    for msg in mailbox.list_recent(load_env(), conn, creds, include_body=include_body):
                        msg["connection_id"] = conn["id"]
                        msg["mailbox_email"] = conn["account_email"]
                        msg["id"] = f"{conn['id']}:{msg['id']}"
                        msgs.append(msg)
                msgs.sort(key=lambda item: item.get("received") or "", reverse=True)
                msgs = msgs[:25]
            except mailbox.MailboxError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"messages": msgs})

        if path == "/api/mailbox/attachment":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            query = parse_qs(urlparse(self.path).query)
            message_id = str(query.get("message_id", [""])[0])
            part = str(query.get("part", [""])[0])
            try:
                connection_id_text, provider_message_id = message_id.split(":", 1)
                connection_id = int(connection_id_text)
            except (ValueError, TypeError):
                return self._json({"error": "Bad attachment reference."}, 400)
            if not provider_message_id or not part or len(provider_message_id) > 1000 or len(part) > 2000:
                return self._json({"error": "Bad attachment reference."}, 400)
            try:
                connection, creds = live_mailbox(user["id"], load_env(), connection_id)
                attachment = mailbox.get_attachment(connection, creds, provider_message_id, part)
            except mailbox.MailboxError as exc:
                return self._json({"error": str(exc)}, 400)
            disposition = "attachment; filename*=UTF-8''" + quote(attachment["name"])
            return self._send(
                200, attachment["data"], attachment["media_type"],
                [("Content-Disposition", disposition),
                 ("Content-Security-Policy", "default-src 'none'; sandbox")],
            )

        if path == "/api/automations":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"automations": public_automations(user["id"])})

        if path == "/api/mailbox/connect":
            # A browser navigation, not a fetch - it ends at the provider's
            # sign-in page, so failures answer in redirects rather than JSON
            # the user would never see.
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._redirect("/login")
            if len(db.list_mailbox_connections(user["id"])) >= db.plan_for_company(user["company_id"])["mailbox_limit"]:
                return self._redirect("/dashboard?mailbox=limit")
            cfg = load_env()
            provider = parse_qs(urlparse(self.path).query).get("provider", [""])[0]
            if provider not in mailbox.OAUTH or not mailbox.is_configured(cfg, provider):
                return self._redirect("/dashboard?mailbox=unconfigured")
            if not secretstore.is_ready(cfg):
                return self._redirect("/dashboard?mailbox=nokey")
            state = db.new_oauth_state(user["id"], provider)
            return self._redirect(
                mailbox.authorize_url(cfg, provider, state, login_hint=user["email"])
            )

        if path == "/api/mailbox/callback":
            return self._handle_mailbox_callback()

        if path == "/api/tools/status":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json(public_tools(user, load_env()))

        if path == "/api/tools/recent":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            try:
                connection_id = int(parse_qs(urlparse(self.path).query).get("connection_id", [""])[0])
            except (TypeError, ValueError):
                return self._json({"error": "Choose a connection."}, 400)
            connection = db.get_tool_connection(user["id"], connection_id)
            if not connection:
                return self._json({"error": "That connection is not yours."}, 404)
            # Answered here as well as inside tool_credentials, which guards
            # every read path: this is a billing answer, not an upstream
            # failure, and the two shouldn't share a status code.
            if connection["tool"] not in db.plan_for_company(user["company_id"])["tool_ids"]:
                return self._json({"error": f"{tools.LABELS.get(connection['tool'], 'That tool')} "
                                            "isn't part of your current plan."}, 402)
            cfg = load_env()
            try:
                creds = tool_credentials(user, connection, cfg)
                items = tools.list_recent(cfg, connection["tool"], creds)
            except tools.ToolError as exc:
                # Remembered against the connection so the card can explain
                # itself later without the person having to re-trigger it.
                db.record_tool_error(connection["id"], str(exc))
                return self._json({"error": str(exc)}, 502)
            except secretstore.SecretsUnavailable as exc:
                return self._json({"error": str(exc)}, 400)
            db.record_tool_error(connection["id"], "")
            return self._json({"items": items, "tool": connection["tool"]})

        if path == "/api/tools/connect":
            # A browser navigation like the mailbox one, so failures answer in
            # redirects rather than JSON nobody would ever see.
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._redirect("/login")
            cfg = load_env()
            tool_id = parse_qs(urlparse(self.path).query).get("tool", [""])[0]
            entry = tools.BY_ID.get(tool_id)
            if not entry or entry["auth"] != "oauth2":
                return self._redirect("/dashboard?tool=unknown")
            if tool_id not in db.plan_for_company(user["company_id"])["tool_ids"]:
                return self._redirect("/dashboard?tool=notincluded")
            if not tools.is_configured(cfg, tool_id):
                return self._redirect("/dashboard?tool=unconfigured")
            if not secretstore.is_ready(cfg):
                return self._redirect("/dashboard?tool=nokey")
            # Namespaced so the shared oauth_states table can't confuse a tool
            # callback with a mailbox one - they land on different endpoints,
            # but the state is what decides whose token endpoint gets talked to.
            state = db.new_oauth_state(user["id"], f"tool:{tool_id}")
            return self._redirect(tools.authorize_url(cfg, tool_id, state))

        if path == "/api/tools/callback":
            return self._handle_tool_callback()

        if path == "/api/plans":
            # Public and unauthenticated on purpose - pricing is marketing
            # copy, and the landing page's pricing section and register.html
            # both need the real, current tiers rather than hardcoded copies.
            return self._json({"plans": db.list_plans()})

        if path == "/api/livechat/status":
            # Public and unauthenticated - the landing widget polls this to
            # show an online/offline badge before anyone signs in.
            return self._json({"online": is_livechat_online(db.get_platform_settings())})

        if path == "/api/site-content":
            # Public and unauthenticated - legal.html fetches this to merge
            # admin-edited copy into its client-rendered legal documents.
            page = parse_qs(urlparse(self.path).query).get("page", [""])[0]
            if page not in SITE_CONTENT_SCHEMA:
                return self._json({"error": "Unknown page."}, 404)
            return self._json({"values": site_content_effective(page)})

        if path == "/api/admin/me":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"admin": public_admin(admin)})

        if path == "/api/admin/site-content":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            page = parse_qs(urlparse(self.path).query).get("page", [""])[0]
            if page not in SITE_CONTENT_SCHEMA:
                return self._json({"error": "Unknown page."}, 404)
            values = site_content_effective(page)
            fields = [{**f, "value": values[f["key"]]} for f in SITE_CONTENT_SCHEMA[page]]
            return self._json({
                "pages": list(SITE_CONTENT_SCHEMA.keys()),
                "page": page,
                "fields": fields,
            })

        if path == "/api/admin/ai-settings":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            cfg = load_env()
            settings = db.get_platform_settings()
            provider, model = resolve_admin_provider_model(cfg)
            return self._json({
                "configured": configured_providers(cfg),
                "current": {"provider": provider, "model": model},
                "saved": {"provider": settings["ai_provider"], "model": settings["ai_model"] or ""},
                "briefing": settings["ai_briefing"] or "",
            })

        if path == "/api/admin/site-settings":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            settings = db.get_platform_settings()
            return self._json({
                "livechat_mode": settings["livechat_mode"],
                "livechat_schedule": json.loads(settings["livechat_schedule_json"] or "{}"),
                "livechat_online": is_livechat_online(settings),
            })

        if path == "/api/admin/overview":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            cfg = load_env()
            return self._json({
                "platform": db.platform_stats(),
                "companies": db.list_companies_with_stats(),
                "system": {
                    "demo_agent_configured": active_provider(cfg) is not None,
                    "fx_rates_loaded": fx() is not None,
                    "database_ready": db.DB_FILE.exists(),
                },
            })

        if path == "/api/admin/alerts":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            return self._json(admin_alerts(admin, load_env()))

        if path == "/api/admin/plans":
            admin = self._admin_role_request("owner", "billing")
            if not admin:
                return
            return self._json({"plans": db.list_plans()})

        if path == "/api/admin/pipeline":
            admin = self._admin_role_request("owner", "sales")
            if not admin:
                return
            return self._json({"opportunities": db.list_crm_opportunities(),
                               "companies": db.list_company_choices()})

        if path == "/api/admin/payments":
            admin = self._admin_role_request("owner", "billing")
            if not admin:
                return
            ps = paystack_config(load_env())
            return self._json({"payments": db.list_payments(), "configuration": {
                "public_key": ps["public_key"], "public_key_configured": bool(ps["public_key"]),
                "secret_key_configured": bool(ps["secret_key"]), "webhook_url": ps["webhook_url"],
                "callback_url": ps["callback_url"]}})

        if path == "/api/admin/team":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            if not admin_is_owner(admin):
                return self._json({"error": "Only a Command Center owner can manage the back-office team."}, 403)
            return self._json({"members": db.list_platform_admins(),
                               "roles": list(db.PLATFORM_ADMIN_ROLES)})

        if path == "/api/admin/activity":
            admin = self._admin_role_request("owner", "operations", "support")
            if not admin:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            return self._json(db.list_admin_activity(
                page=page, entity_type=query.get("entity_type", [""])[0],
                action=query.get("action", [""])[0]))

        if path == "/api/admin/errors":
            admin = self._admin_role_request("owner", "support")
            if not admin:
                return
            query = parse_qs(urlparse(self.path).query)
            try: limit = int(query.get("limit", ["300"])[0])
            except ValueError: limit = 300
            return self._json({"errors": db.list_application_errors(limit,
                query.get("severity", [""])[0], query.get("query", [""])[0])})

        if path == "/api/admin/reports":
            admin = self._admin_role_request("owner", "sales", "billing")
            if not admin:
                return
            companies, payments, opportunities = (db.list_companies_with_stats(),
                                                   db.list_payments(), db.list_crm_opportunities())
            revenue, forecast, plans, lifecycle = {}, {}, {}, {}
            for payment in payments:
                if payment["status"] == "success":
                    key = payment["currency"]
                    revenue[key] = revenue.get(key, 0) + payment["amount_subunit"]
            for opportunity in opportunities:
                if opportunity["stage"] not in ("lost",):
                    key = opportunity["currency"]
                    forecast[key] = forecast.get(key, 0) + round(float(opportunity["value"]) * 100)
            for company in companies:
                plans[company["plan"]["name"]] = plans.get(company["plan"]["name"], 0) + 1
                status = company["crm"]["lifecycle_status"]
                lifecycle[status] = lifecycle.get(status, 0) + 1
            return self._json({"companies": len(companies), "members": sum(c["approved_count"] for c in companies),
                               "revenue": revenue, "forecast": forecast, "plans": plans,
                               "lifecycle": lifecycle, "payments": payments[:10],
                               "opportunities": opportunities[:10]})

        if path == "/api/admin/inbox":
            admin = self._admin_role_request("owner", "operations", "support")
            if not admin:
                return
            return self._json({"items": db.list_admin_inbox()})

        if path == "/api/admin/invoices":
            admin = self._admin_role_request("owner", "billing")
            if not admin: return
            return self._json({"invoices": db.list_admin_invoices(), "companies": db.list_company_choices()})

        if path == "/api/admin/tools-catalog":
            admin = self._admin_role_request("owner", "billing")
            if not admin: return
            cfg = load_env()
            # `configured` and `connections` are here so the Plans page can
            # warn before a tier is sold on a tool nobody can actually
            # connect, and show which vendors are worth registering next.
            return self._json({"tools": [
                entry | {"note": tools.BY_ID[entry["id"]].get("note", ""),
                         "connections": db.count_tool_connections(entry["id"])}
                for entry in tools.public_catalog(cfg)
            ], "categories": [{"id": c, "label": l} for c, l in tools.CATEGORIES]})

        if path == "/api/admin/ada-signups":
            admin = self._admin_role_request("owner")
            if not admin: return
            return self._json({"signups": db.list_ada_pending_signups()})

        if path in STATIC_PAGES:
            f = ROOT / STATIC_PAGES[path]
            if not f.exists():
                return self._json({"error": f"{STATIC_PAGES[path]} missing"}, 404)
            raw = f.read_bytes()
            cms_page = CMS_PAGE_ROUTES.get(path)
            if cms_page:
                raw = render_site_content(cms_page, raw)
            return self._send(200, raw, "text/html; charset=utf-8")

        return self._json({"error": "not found"}, 404)

    # --------------------------------------------------------------- POST

    def _route_post(self):
        path = self.path.split("?")[0]
        if path != "/api/paystack/webhook" and not self._post_is_same_origin():
            return self._json({"error": "This request did not come from Buinee."}, 403)
        handlers = {
            "/api/demo": self._handle_demo,
            "/api/demo/register": self._handle_demo_register,
            "/api/demo/contact": self._handle_demo_contact,
            "/api/mailbox/connect-imap": self._handle_mailbox_connect_imap,
            "/api/mailbox/disconnect": self._handle_mailbox_disconnect,
            "/api/tools/connect-key": self._handle_tool_connect_key,
            "/api/tools/disconnect": self._handle_tool_disconnect,
            "/api/mailbox/notifications/seen": self._handle_mailbox_notifications_seen,
            "/api/mailbox/triage": self._handle_mailbox_triage,
            "/api/automations/update": self._handle_automation_update,
            "/api/automations/run": self._handle_automation_run,
            "/api/register": self._handle_register,
            "/api/join": self._handle_join,
            "/api/login": self._handle_login,
            "/api/logout": self._handle_logout,
            "/api/company/approve": self._handle_approve,
            "/api/company/set-model": self._handle_set_company_model,
            "/api/company/briefing": self._handle_set_company_briefing,
            "/api/company/profile": self._handle_set_company_profile,
            "/api/user/instructions": self._handle_set_user_instructions,
            "/api/user/onboarding/complete": self._handle_complete_onboarding,
            "/api/user/reference-documents/upload": self._handle_reference_upload,
            "/api/user/reference-documents/delete": self._handle_reference_delete,
            "/api/team-chat/send": self._handle_team_message,
            "/api/team-chat/clear": self._handle_clear_team_conversation,
            "/api/team-chat/seen": self._handle_team_messages_seen,
            "/api/team-chat/add-to-library": self._handle_team_file_to_library,
            "/api/follow-ups/status": self._handle_follow_up_status,
            "/api/follow-ups/from-chat": self._handle_follow_up_from_chat,
            "/api/payments/initialize": self._handle_payment_initialize,
            "/api/paystack/webhook": self._handle_paystack_webhook,
            "/api/vouchers/create": self._handle_voucher_create,
            "/api/vouchers/submit": self._handle_voucher_submit,
            "/api/vouchers/review": self._handle_voucher_review,
            "/api/chat": self._handle_chat,
            "/api/admin/login": self._handle_admin_login,
            "/api/admin/logout": self._handle_admin_logout,
            "/api/admin/change-password": self._handle_admin_change_password,
            "/api/admin/chat": self._handle_admin_chat,
            "/api/admin/ai-settings/model": self._handle_admin_set_ai_model,
            "/api/admin/ai-settings/briefing": self._handle_admin_set_ai_briefing,
            "/api/admin/site-settings/livechat": self._handle_admin_set_livechat_settings,
            "/api/admin/mfa/setup": self._handle_admin_mfa_setup,
            "/api/admin/mfa/enable": self._handle_admin_mfa_enable,
            "/api/admin/mfa/disable": self._handle_admin_mfa_disable,
            "/api/admin/company/delete": self._handle_admin_delete_company,
            "/api/admin/payment/delete": self._handle_admin_delete_payment,
            "/api/admin/company/crm": self._handle_admin_update_crm_account,
            "/api/admin/company/contact/save": self._handle_admin_save_crm_contact,
            "/api/admin/company/contact/delete": self._handle_admin_delete_crm_contact,
            "/api/admin/company/interaction/save": self._handle_admin_save_crm_interaction,
            "/api/admin/company/interaction/delete": self._handle_admin_delete_crm_interaction,
            "/api/admin/company/task/save": self._handle_admin_save_crm_task,
            "/api/admin/company/task/delete": self._handle_admin_delete_crm_task,
            "/api/admin/company/subscription": self._handle_admin_save_crm_subscription,
            "/api/admin/opportunity/save": self._handle_admin_save_opportunity,
            "/api/admin/opportunity/delete": self._handle_admin_delete_opportunity,
            "/api/admin/plans/create": self._handle_admin_create_plan,
            "/api/admin/plans/update": self._handle_admin_update_plan,
            "/api/admin/company/set-plan": self._handle_admin_set_company_plan,
            "/api/admin/team/create": self._handle_admin_team_create,
            "/api/admin/team/update": self._handle_admin_team_update,
            "/api/admin/team/reset-password": self._handle_admin_team_reset_password,
            "/api/admin/errors/clear": self._handle_admin_errors_clear,
            "/api/admin/inbox/state": self._handle_admin_inbox_state,
            "/api/admin/inbox/delete": self._handle_admin_inbox_delete,
            "/api/admin/site-content/save": self._handle_admin_site_content_save,
            "/api/admin/ada-signups/review": self._handle_admin_ada_signup_review,
            "/api/admin/invoices/create": self._handle_admin_invoice_create,
            "/api/admin/invoices/status": self._handle_admin_invoice_status,
        }
        handler = handlers.get(path)
        if not handler:
            return self._json({"error": "not found"}, 404)
        return handler()

    def _handle_demo(self):
        ip = client_ip(self)
        if rate_limited(f"demo:{ip}"):
            return self._json(
                {"error": "Ada is busy right now. Please wait a few minutes and try again."}, 429)

        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)

        message = str(req.get("message") or "").strip()[:MAX_MESSAGE]
        if not message:
            return self._json({"error": "Say something first."}, 400)

        session_id = str(req.get("session_id") or "").strip()[:64]
        history = []
        for t in (req.get("history") or [])[-MAX_HISTORY:]:
            role = "assistant" if t.get("role") == "assistant" else "user"
            text = str(t.get("content") or "").strip()[:1500]
            if text:
                history.append({"role": role, "content": text})

        def record(reply_text: str | None) -> None:
            # Best-effort: a visitor who leaves contact details or a
            # complaint shouldn't be lost just because the AI provider was
            # briefly down, so this records the exchange on every exit path,
            # not only a successful reply - and never breaks the actual
            # response to the visitor if it fails.
            if not session_id:
                return
            try:
                lines = [f"{'Ada' if t['role'] == 'assistant' else 'Visitor'}: {t['content']}" for t in history]
                lines.append(f"Visitor: {message}")
                if reply_text:
                    lines.append(f"Ada: {reply_text}")
                name = _DEMO_NAME_RE.search(message)
                email = _DEMO_EMAIL_RE.search(message)
                phone = _DEMO_PHONE_RE.search(message)
                complaint = any(word in message.lower() for word in _DEMO_COMPLAINT_WORDS)
                db.save_landing_chat_session(
                    session_id, "\n".join(lines),
                    contact_name=name.group(1) if name else "",
                    contact_email=email.group(0) if email else "",
                    contact_phone=phone.group(0) if phone else "",
                    should_flag=bool(email or phone or complaint),
                )
            except Exception as exc:
                print(f"  ! could not save landing chat session: {exc}")

        cfg = load_env()
        provider, model = resolve_admin_provider_model(cfg)
        if not provider:
            reference = report_application_error(
                "ada.demo.configuration", "No AI provider is configured for the public demo")
            record(None)
            return self._json(
                {"error": ada_unavailable(reference)}, 503)

        computed = maybe_compute(message)

        try:
            reply = providers.chat(
                provider, model, cfg.get(PROVIDER_KEYS[provider], ""),
                message, landing_plans_digest(),
                history, system=build_system(computed),
            )
        except providers.ProviderError as exc:
            reference = report_application_error("ada.demo.provider", exc)
            record(None)
            return self._json({"error": ada_unavailable(reference)}, 503)
        except Exception as exc:
            print(f"  ! demo failure: {exc}")
            reference = report_application_error("ada.demo.server", exc)
            record(None)
            return self._json({"error": ada_unavailable(reference)}, 500)

        reply, register_draft = extract_ada_register(reply)
        record(reply)
        resp = {"reply": reply, "computed": bool(computed)}
        if register_draft:
            resp["register_draft"] = register_draft
        return self._json(resp)

    def _handle_demo_register(self):
        """Ada registering a company on a visitor's behalf, mid-chat. Shares
        the same rate limiter as the real registration form (this is just a
        second door into the same room) and the same db.register_company/
        initialize_plan_payment logic - the only real difference is that a
        free-plan signup here starts life as 'ada_pending' rather than
        'approved', since nobody has verified there's a real business behind
        it the way a normal free registration implicitly gets by not needing
        any review at all. A paid plan is unaffected: the Paystack gate
        already does that job."""
        if rate_limited(f"auth:{client_ip(self)}", max_hits=20, window=600):
            return self._json({"error": "Too many attempts — try again shortly."}, 429)
        if rate_limited(f"demo:{client_ip(self)}"):
            return self._json({"error": "Too many attempts — try again shortly."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)

        if req.get("terms_accepted") is not True:
            return self._json({"error": "You must agree to the Terms of Use and Privacy Policy."}, 400)
        try:
            plan_id = int(req.get("plan_id"))
        except (TypeError, ValueError):
            plan_id = None
        plan = db.get_plan(plan_id) if plan_id is not None else None
        if not plan:
            return self._json({"error": "Choose a valid pricing plan."}, 400)
        payment_required = float(plan["price"]) > 0

        try:
            user = db.register_company(
                str(req.get("company_name") or ""),
                str(req.get("name") or ""),
                str(req.get("email") or ""),
                str(req.get("password") or ""),
                "finance_supervisor",
                plan_id=plan_id,
                allow_duplicate_name=bool(req.get("allow_duplicate_name")),
                initial_status="payment_pending" if payment_required else "ada_pending",
            )
        except db.DuplicateCompanyError as exc:
            return self._json({"error": str(exc), "duplicate_name": True, "company": exc.company}, 409)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)

        db.record_terms_acceptance(user["id"], TERMS_VERSION)
        db.record_admin_activity(
            {"id": None, "name": "Ada", "email": ""}, "registered", "company", user["company"]["id"],
            user["company"]["name"], details=f"Registered via landing chat, plan={plan['name']}")

        if not payment_required:
            return self._json({
                "ok": True, "payment_required": False,
                "message": "Your workspace has been created and is pending a quick review by our team "
                           "before it goes live - we'll email you once it's approved.",
            })

        token = db.create_session(user["id"])
        try:
            payment = initialize_plan_payment(user, plan, load_env())
        except ValueError as exc:
            return self._json(
                {"error": f"Your account was created, but payment could not start: {exc}"}, 503,
                [("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
            )
        return self._json(
            {"ok": True, "payment_required": True, "authorization_url": payment.get("authorization_url") or ""},
            extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
        )

    def _handle_demo_contact(self):
        """A visitor leaving contact details while the live chat is offline.

        No AI call happens here - the frontend already skips /api/demo while
        offline, so this only ever records what someone typed plus who they
        are, the same way _handle_demo's record() does, and flags it in the
        Command Center inbox so a person follows up once back online."""
        if rate_limited(f"demo:{client_ip(self)}"):
            return self._json({"error": "Please wait a moment and try again."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)

        name = str(req.get("name") or "").strip()[:120]
        email = str(req.get("email") or "").strip()[:200]
        phone = str(req.get("phone") or "").strip()[:40]
        if not name or not (email or phone):
            return self._json(
                {"error": "Add your name and an email or phone number so we can reach you."}, 400)

        session_id = str(req.get("session_id") or "").strip()[:64]
        if not session_id:
            return self._json({"error": "Bad request."}, 400)

        history = []
        for t in (req.get("history") or [])[-MAX_HISTORY:]:
            role = "assistant" if t.get("role") == "assistant" else "user"
            text = str(t.get("content") or "").strip()[:1500]
            if text:
                history.append({"role": role, "content": text})
        lines = [f"{'Ada' if t['role'] == 'assistant' else 'Visitor'}: {t['content']}" for t in history]
        left = f"[Left contact details while offline: {name}"
        if email:
            left += f", {email}"
        if phone:
            left += f", {phone}"
        lines.append(left + "]")

        try:
            db.save_landing_chat_session(
                session_id, "\n".join(lines),
                contact_name=name, contact_email=email, contact_phone=phone,
                should_flag=True,
            )
        except Exception as exc:
            print(f"  ! could not save offline contact: {exc}")
            return self._json({"error": "Could not save your details. Please try again."}, 500)

        return self._json({"ok": True})

    # ---------------------------------------------------------------- auth

    def _handle_register(self):
        if rate_limited(f"auth:{client_ip(self)}", max_hits=20, window=600):
            return self._json({"error": "Too many attempts — try again shortly."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            if req.get("terms_accepted") is not True:
                return self._json({"error": "You must agree to the Terms of Use and Privacy Policy."}, 400)
            plan_id = int(req["plan_id"]) if req.get("plan_id") is not None else None
        except (TypeError, ValueError):
            plan_id = None
        plan = db.get_plan(plan_id) if plan_id is not None else None
        if not plan:
            return self._json({"error": "Choose a valid pricing plan."}, 400)
        payment_required = float(plan["price"]) > 0
        try:
            user = db.register_company(
                str(req.get("company_name") or ""),
                str(req.get("name") or ""),
                str(req.get("email") or ""),
                str(req.get("password") or ""),
                str(req.get("role") or ""),
                plan_id=plan_id,
                allow_duplicate_name=bool(req.get("allow_duplicate_name")),
                initial_status="payment_pending" if payment_required else "approved",
            )
        except db.DuplicateCompanyError as exc:
            # Not a failure so much as a question - the form asks whether this
            # is the same company (join it) or a different one with the same
            # name (register anyway), and sends the answer back.
            return self._json({
                "error": str(exc),
                "duplicate_name": True,
                "company": exc.company,
            }, 409)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        db.record_terms_acceptance(user["id"], TERMS_VERSION)
        token = db.create_session(user["id"])
        payment = None
        if payment_required:
            try:
                payment = initialize_plan_payment(user, plan, load_env())
            except ValueError as exc:
                return self._json(
                    {"error": f"Your account was created, but payment could not start: {exc}"},
                    503,
                    [("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
                )
        return self._json(
            {"ok": True, "user": public_user(user), "payment_required": payment_required,
             "authorization_url": payment.get("authorization_url") if payment else ""},
            extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
        )

    def _handle_mailbox_callback(self):
        """Where Microsoft or Google sends the browser back after consent.

        Everything here ends in a redirect to the dashboard carrying a short
        `mailbox=` code, because the person is looking at a browser tab, not
        at a JSON response. Nothing sensitive goes in that query string.
        """
        params = parse_qs(urlparse(self.path).query)
        cfg = load_env()

        # The user can decline, or the provider can refuse (admin consent
        # required, app not approved in that tenant, unverified app). Either
        # way it arrives as ?error= rather than a code.
        if params.get("error"):
            return self._redirect("/dashboard?mailbox=denied")

        # State is checked before anything else is trusted: it proves this
        # callback belongs to a connect this browser started, it carries the
        # provider so that isn't taken from the query string, and it's spent
        # on read so a replay finds nothing.
        spent = db.consume_oauth_state(params.get("state", [""])[0])
        if not spent:
            return self._redirect("/dashboard?mailbox=badstate")
        user_id, provider = spent

        # The state says which user asked, but the cookie says who is
        # actually driving this browser. If they disagree, someone is being
        # walked through a callback that isn't theirs - refuse rather than
        # attach a mailbox to whichever account happens to be signed in.
        user = current_user(self)
        if not user or user["id"] != user_id or user["status"] != "approved":
            return self._redirect("/dashboard?mailbox=badstate")

        code = params.get("code", [""])[0]
        if not code:
            return self._redirect("/dashboard?mailbox=denied")

        try:
            connection = mailbox.exchange_code(cfg, provider, code)
            enc = secretstore.encrypt(cfg, connection["credentials"])
        except mailbox.MailboxError:
            return self._redirect("/dashboard?mailbox=failed")
        except secretstore.SecretsUnavailable:
            # Credentials are never written in the clear - better to lose the
            # connection attempt than to store a token unprotected.
            return self._redirect("/dashboard?mailbox=nokey")

        db.save_mailbox_connection(user["id"], user["company_id"], connection, enc)
        # Not written to the activity log: that table hangs off a voucher
        # (voucher_events.voucher_id is NOT NULL), and connecting a mailbox
        # isn't an event about one.
        return self._redirect("/dashboard?mailbox=connected")

    def _handle_mailbox_connect_imap(self):
        """IMAP has no consent screen, so it's a form post rather than a
        redirect. The details are proven by logging in before anything is
        stored - credentials that don't work never reach the database."""
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if len(db.list_mailbox_connections(user["id"])) >= db.plan_for_company(user["company_id"])["mailbox_limit"]:
            return self._json({"error": "Your plan's mailbox limit has been reached."}, 402)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        cfg = load_env()
        if not secretstore.is_ready(cfg):
            return self._json({"error": secretstore.why_unavailable(cfg)}, 400)
        try:
            port = int(req.get("port") or 993)
        except (TypeError, ValueError):
            port = 993
        try:
            connection = mailbox.connect_imap(
                str(req.get("host") or ""),
                port,
                str(req.get("email") or ""),
                str(req.get("password") or ""),
            )
            enc = secretstore.encrypt(cfg, connection["credentials"])
        except mailbox.MailboxError as exc:
            return self._json({"error": str(exc)}, 400)
        except secretstore.SecretsUnavailable as exc:
            return self._json({"error": str(exc)}, 400)
        db.save_mailbox_connection(user["id"], user["company_id"], connection, enc)
        return self._json({"ok": True, "mailbox": public_mailbox(user["id"], cfg)})

    def _handle_mailbox_disconnect(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            connection_id = int(self._body().get("connection_id"))
        except Exception:
            return self._json({"error": "Choose a mailbox to disconnect."}, 400)
        db.delete_mailbox_connection(user["id"], connection_id)
        return self._json({"ok": True, "mailbox": public_mailbox(user["id"], load_env())})

    def _handle_tool_callback(self):
        """Where a tool vendor sends the browser back after consent.

        Deliberately the same shape as _handle_mailbox_callback, including
        the check that the browser driving the callback belongs to the user
        the state was minted for - otherwise somebody can be walked through
        a callback that isn't theirs and end up with a stranger's account
        attached to their workspace.
        """
        params = parse_qs(urlparse(self.path).query)
        cfg = load_env()

        if params.get("error"):
            return self._redirect("/dashboard?tool=denied")

        spent = db.consume_oauth_state(params.get("state", [""])[0])
        if not spent:
            return self._redirect("/dashboard?tool=badstate")
        user_id, marker = spent
        if not marker.startswith("tool:"):
            # A mailbox state arriving here means the two flows have been
            # crossed somewhere; refuse rather than guess which was meant.
            return self._redirect("/dashboard?tool=badstate")
        tool_id = marker[len("tool:"):]

        user = current_user(self)
        if not user or user["id"] != user_id or user["status"] != "approved":
            return self._redirect("/dashboard?tool=badstate")

        # Re-checked rather than trusted from when the redirect was issued:
        # a plan can be downgraded, or a tool withdrawn from a tier, while
        # somebody is still sitting on the vendor's consent screen.
        if tool_id not in db.plan_for_company(user["company_id"])["tool_ids"]:
            return self._redirect("/dashboard?tool=notincluded")

        code = params.get("code", [""])[0]
        if not code:
            return self._redirect("/dashboard?tool=denied")

        try:
            connection = tools.exchange_code(cfg, tool_id, code)
            enc = secretstore.encrypt(cfg, connection["credentials"])
        except tools.ToolError:
            return self._redirect("/dashboard?tool=failed")
        except secretstore.SecretsUnavailable:
            return self._redirect("/dashboard?tool=nokey")

        db.save_tool_connection(user["id"], user["company_id"], connection, enc)
        return self._redirect("/dashboard?tool=connected")

    def _handle_tool_connect_key(self):
        """The api_key tools - Trello today. No consent screen, so it's a form
        post, and the credential is stored only if encryption is available."""
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        tool_id = str(req.get("tool") or "")
        entry = tools.BY_ID.get(tool_id)
        if not entry or entry["auth"] != "api_key":
            return self._json({"error": "That tool is not connected with a key."}, 400)
        if tool_id not in db.plan_for_company(user["company_id"])["tool_ids"]:
            return self._json({"error": f"{entry['label']} isn't part of your plan."}, 402)
        cfg = load_env()
        if not secretstore.is_ready(cfg):
            return self._json({"error": secretstore.why_unavailable(cfg)}, 400)
        try:
            connection = tools.connect_api_key(
                tool_id, str(req.get("key") or ""), str(req.get("token") or ""),
                str(req.get("label") or ""))
            enc = secretstore.encrypt(cfg, connection["credentials"])
        except tools.ToolError as exc:
            return self._json({"error": str(exc)}, 400)
        except secretstore.SecretsUnavailable as exc:
            return self._json({"error": str(exc)}, 400)
        db.save_tool_connection(user["id"], user["company_id"], connection, enc)
        return self._json({"ok": True, "tools": public_tools(user, cfg)})

    def _handle_tool_disconnect(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            connection_id = int(self._body().get("connection_id"))
        except Exception:
            return self._json({"error": "Choose a connection to remove."}, 400)
        db.delete_tool_connection(user["id"], connection_id)
        return self._json({"ok": True, "tools": public_tools(user, load_env())})

    def _handle_mailbox_notifications_seen(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        db.mark_mailbox_arrivals_seen(user["id"])
        return self._json({"ok": True})

    def _handle_automation_update(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        key = str(req.get("key") or "")
        if key not in AUTOMATION_RECIPES:
            return self._json({"error": "Unknown automation."}, 400)
        enabled = bool(req.get("enabled"))
        if enabled:
            if not db.get_mailbox_connection(user["id"]):
                return self._json({"error": "Connect a mailbox first."}, 400)
            gate = db.can_use_chat(user["company_id"])
            if not gate["allowed"]:
                return self._json({"error": f"AI automations aren't included in the {gate['plan']} plan."}, 402)
        db.set_automation(user["id"], key, enabled, next_automation_run(key) if enabled else None)
        return self._json({"ok": True, "automations": public_automations(user["id"])})

    def _handle_automation_run(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        key = str(req.get("key") or "")
        if key not in AUTOMATION_RECIPES:
            return self._json({"error": "Unknown automation."}, 400)
        run_id = db.start_automation_run(user["id"], user["company_id"], key, next_automation_run(key))
        try:
            result = run_automation(user["id"], key)
            db.finish_automation_run(run_id, result=result)
        except providers.ProviderError as exc:
            reference = report_application_error(
                "ada.automation.provider", exc, user, f"automation={key}")
            db.finish_automation_run(run_id, error=f"Reference: {reference}")
            return self._json({"error": ada_unavailable(reference, "run that automation")}, 503)
        except ValueError as exc:
            if "isn't configured" in str(exc):
                reference = report_application_error(
                    "ada.automation.configuration", exc, user, f"automation={key}")
                db.finish_automation_run(run_id, error=f"Reference: {reference}")
                return self._json(
                    {"error": ada_unavailable(reference, "run that automation")}, 503)
            db.finish_automation_run(run_id, error=str(exc))
            return self._json({"error": str(exc)}, 400)
        except mailbox.MailboxError as exc:
            db.finish_automation_run(run_id, error=str(exc))
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            print(f"  ! automation failure ({key}): {exc}")
            reference = report_application_error(
                "ada.automation.server", exc, user, f"automation={key}")
            db.finish_automation_run(run_id, error=f"Reference: {reference}")
            return self._json({"error": ada_unavailable(reference, "run that automation")}, 500)
        return self._json({"ok": True, "result": result, "automations": public_automations(user["id"])})

    def _handle_mailbox_triage(self):
        """Run Ada's structured review on one message already visible in
        this user's connected inbox. Nothing is cached or written back to the
        mailbox; the result exists only in the current dashboard session."""
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if rate_limited(f"mail-triage:{user['id']}"):
            return self._json(
                {"error": "Ada is busy right now. Please wait a few minutes and try again."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        message_id = str(req.get("message_id") or "").strip()
        if not message_id:
            return self._json({"error": "Choose an email first."}, 400)
        try:
            connection_id_text, provider_message_id = message_id.split(":", 1)
            connection_id = int(connection_id_text)
        except (ValueError, TypeError):
            return self._json({"error": "That email reference is invalid."}, 400)

        gate = db.can_use_chat(user["company_id"])
        if not gate["allowed"]:
            reason = (f"AI summaries aren't included in your company's {gate['plan']} plan."
                      if gate["reason"] == "not_included" else
                      f"Your company has used all {gate['limit']} AI messages included "
                      f"in the {gate['plan']} plan this month.")
            return self._json({"error": reason, "reason": gate["reason"]}, 402)

        cfg = load_env()
        pub = public_user(user)
        provider, model = resolve_provider_model(cfg, pub["company"])
        if not provider:
            reference = report_application_error(
                "ada.summary.configuration", "No AI provider is configured", user)
            return self._json({"error": ada_unavailable(reference, "summarize that email")}, 503)

        try:
            connection, creds = live_mailbox(user["id"], cfg, connection_id)
            messages = mailbox.list_recent(cfg, connection, creds, include_body=True)
        except mailbox.MailboxError as exc:
            return self._json({"error": str(exc)}, 400)
        message = next((m for m in messages if str(m.get("id")) == provider_message_id), None)
        if not message:
            return self._json({"error": "That email is no longer in the recent inbox list."}, 404)
        attachments = message.get("attachments", [])
        if not (message.get("body") or "").strip() and not attachments:
            return self._json({"error": "That email has no readable text body to summarize."}, 400)

        docs = []
        for meta in attachments[:3]:
            if not meta.get("downloadable") or int(meta.get("size") or 0) > 5 * 1024 * 1024:
                continue
            try:
                attached = mailbox.get_attachment(
                    connection, creds, provider_message_id, str(meta.get("part"))
                )
                media_type = attached["media_type"]
                upload = {"name": attached["name"], "media_type": media_type}
                if media_type.startswith("text/") or media_type in ("application/json", "application/xml"):
                    upload["text"] = attached["data"].decode("utf-8", "replace")
                else:
                    upload["data"] = base64.b64encode(attached["data"]).decode("ascii")
                normalized = normalize_document_upload(upload)
                if normalized["kind"] == "text":
                    docs.append({"kind": "text", "source": "attached", "name": normalized["name"],
                                 "text": normalized["text_content"]})
                else:
                    docs.append({"kind": normalized["kind"], "source": "attached",
                                 "name": normalized["name"], "media_type": normalized["media_type"],
                                 "data": normalized["data_base64"]})
            except (mailbox.MailboxError, ValueError, UnicodeError):
                continue

        from_name, from_email = email.utils.parseaddr(message.get("from") or "")
        email_input = {
            "id": message_id,
            "from_name": from_name or from_email or "Unknown sender",
            "from_email": from_email,
            "received": message.get("received") or "",
            "subject": message.get("subject") or "(no subject)",
            "attachments": [item.get("name") for item in message.get("attachments", [])],
            "body": message.get("body") or "(No message body; review the attached documents.)",
            "unread": bool(message.get("unread")),
        }
        try:
            triage_fn = providers.triage_with_docs if docs else providers.triage
            items = triage_fn(provider, model, cfg.get(PROVIDER_KEYS[provider], ""),
                              [email_input], briefing=effective_briefing(pub),
                              **({"docs": docs} if docs else {}))
        except providers.ProviderError as exc:
            reference = report_application_error("ada.summary.provider", exc, user)
            return self._json({"error": ada_unavailable(reference, "summarize that email")}, 503)
        except Exception as exc:
            print(f"  ! mailbox triage failure: {exc}")
            reference = report_application_error("ada.summary.server", exc, user)
            return self._json({"error": ada_unavailable(reference, "summarize that email")}, 500)
        if not items:
            reference = report_application_error(
                "ada.summary.empty", "AI provider returned no summary", user)
            return self._json({"error": ada_unavailable(reference, "summarize that email")}, 503)

        used = db.increment_chat_usage(user["company_id"])
        return self._json({
            "item": items[0],
            "usage": {"used": used, "limit": gate["limit"]},
        })

    def _handle_join(self):
        if rate_limited(f"auth:{client_ip(self)}", max_hits=20, window=600):
            return self._json({"error": "Too many attempts — try again shortly."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            company_id = int(req.get("company_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Pick a company from the list first."}, 400)
        try:
            if req.get("terms_accepted") is not True:
                return self._json({"error": "You must agree to the Terms of Use and Privacy Policy."}, 400)
            user = db.request_to_join(
                company_id,
                str(req.get("name") or ""),
                str(req.get("email") or ""),
                str(req.get("password") or ""),
                str(req.get("role") or ""),
            )
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)

        db.record_terms_acceptance(user["id"], TERMS_VERSION)
        if user["status"] == "approved":
            # Only reachable by claiming Supervisor on a company that
            # doesn't have one yet — same bootstrap case as registering.
            token = db.create_session(user["id"])
            return self._json(
                {"ok": True, "pending": False, "user": public_user(user)},
                extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
            )
        return self._json({"ok": True, "pending": True, "company": db.get_company(company_id)})

    def _handle_login(self):
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        email_address = str(req.get("email") or "").strip().lower()
        if db.login_rate_limited("user", client_ip(self), email_address):
            return self._json({"error": "Too many sign-in attempts. Try again in 15 minutes."}, 429)
        try:
            user = db.authenticate(email_address, str(req.get("password") or ""))
        except db.AuthError as exc:
            db.record_login_failure("user", client_ip(self), email_address)
            if str(exc).startswith("Your account is"):
                return self._json({"error": str(exc)}, 401)
            return self._json({"error": "Email or password is incorrect."}, 401)
        db.clear_login_failures("user", email_address)
        token = db.create_session(user["id"])
        if user["status"] == "payment_pending":
            try:
                payment = initialize_plan_payment(
                    user, db.plan_for_company(user["company_id"]), load_env()
                )
            except ValueError as exc:
                return self._json(
                    {"error": f"Payment could not start: {exc}"}, 503,
                    [("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
                )
            return self._json(
                {"ok": True, "payment_required": True,
                 "authorization_url": payment["authorization_url"]},
                extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
            )
        return self._json(
            {"ok": True, "user": public_user(user)},
            extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
        )

    def _handle_complete_onboarding(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        db.complete_user_onboarding(user["id"])
        return self._json({"ok": True})

    def _handle_logout(self):
        user = current_user(self)
        if user:
            db.clear_presence(user["id"])
        db.destroy_session(session_token(self))
        return self._json({"ok": True}, extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, "", 0))])

    def _handle_approve(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] != "finance_supervisor":
            return self._json({"error": "Only a supervisor can approve requests."}, 403)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            target_id = int(req.get("user_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad request."}, 400)
        action = str(req.get("action") or "")
        try:
            if action == "approve":
                db.approve_user(user["company_id"], target_id)
            elif action == "reject":
                db.reject_user(user["company_id"], target_id)
            else:
                return self._json({"error": "action must be approve or reject."}, 400)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True})

    def _handle_set_company_model(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] != "finance_supervisor":
            return self._json({"error": "Only a supervisor can change this."}, 403)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        provider = req.get("provider")
        cfg = load_env()
        if provider and provider not in configured_providers(cfg):
            return self._json({"error": "That provider isn't configured on this server."}, 400)
        try:
            company = db.set_company_model(user["company_id"], provider, str(req.get("model") or ""))
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "company": company})

    def _handle_set_company_briefing(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] != "finance_supervisor":
            return self._json({"error": "Only a supervisor can change this."}, 403)
        try:
            req = self._body(max_len=8000)
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            company = db.set_company_briefing(user["company_id"], str(req.get("briefing") or ""))
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "company": company})

    def _handle_set_company_profile(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] != "finance_supervisor":
            return self._json({"error": "Only a supervisor can manage the company profile."}, 403)
        try:
            req = self._body(max_len=8000)
            profile = db.update_company_profile(user["company_id"], req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad company profile."}, 400)
        return self._json({"ok": True, "profile": profile})

    def _handle_set_user_instructions(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=16000)
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        db.set_user_instructions(user["id"], str(req.get("briefing") or ""))
        return self._json({"ok": True, "briefing": db.get_user_instructions(user["id"])})

    def _handle_reference_upload(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=8 * 1024 * 1024)
            doc = normalize_document_upload(req)
            document = db.add_reference_document(
                user["id"], doc["name"], doc["kind"], doc["media_type"],
                doc["text_content"], doc["data_base64"], doc["size_bytes"],
            )
        except (ValueError, binascii.Error, db.AuthError) as exc:
            return self._json({"error": str(exc) or "Could not upload that document."}, 400)
        except Exception:
            return self._json({"error": "Bad upload."}, 400)
        return self._json({"ok": True, "document": document})

    def _handle_reference_delete(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            document_id = int(self._body().get("document_id"))
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        if not db.delete_reference_document(user["id"], document_id):
            return self._json({"error": "Document not found."}, 404)
        return self._json({"ok": True})

    def _handle_follow_up_status(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
            task = db.set_user_crm_task_status(
                user, int(req.get("task_id")), str(req.get("status") or "")
            )
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad follow-up task."}, 400)
        return self._json({"ok": True, "task": task})

    def _handle_follow_up_from_chat(self):
        user = current_user(self)
        if not user or user["status"] != "approved" or user["role"] != "finance_supervisor":
            return self._json({"error": "Only a supervisor can assign conversation follow-ups."}, 403)
        try:
            req = self._body(max_len=8000)
            message_id = int(req.get("message_id"))
            assigned_user_id = int(req.get("assigned_user_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Choose a message and team member."}, 400)
        message = db.get_team_message(user["company_id"], message_id, user["id"])
        if not message:
            return self._json({"error": "That conversation message is not available to you."}, 404)
        title = str(req.get("title") or message.get("body") or "Conversation follow-up").strip()[:180]
        try:
            task = db.save_crm_task(user["company_id"], {
                "assigned_user_id": assigned_user_id, "title": title,
                "details": f"From team chat: {message.get('body') or '(file shared)'}",
                "due_date": str(req.get("due_date") or ""),
                "priority": str(req.get("priority") or "normal"), "status": "open",
            }, user["name"])
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "task": task})

    def _handle_payment_initialize(self):
        user = current_user(self)
        if not user or user["status"] != "approved" or user["role"] != "finance_supervisor":
            return self._json({"error": "Only a supervisor can manage billing."}, 403)
        current_plan = db.plan_for_company(user["company_id"])
        try:
            req = self._body()
            requested_id = int(req.get("plan_id")) if req.get("plan_id") is not None else None
        except (ValueError, TypeError):
            return self._json({"error": "Choose a valid plan."}, 400)
        plan = db.get_plan(requested_id) if requested_id is not None else current_plan
        if not plan:
            return self._json({"error": "Choose a valid plan."}, 400)
        if requested_id is not None and float(plan["price"]) <= float(current_plan["price"]):
            return self._json({"error": "Choose a plan above your current plan."}, 400)
        try:
            payment = initialize_plan_payment(user, plan, load_env())
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json(payment)

    def _handle_paystack_webhook(self):
        cfg = load_env()
        secret = paystack_config(cfg)["secret_key"]
        try:
            raw = self._raw_body()
        except ValueError:
            return self._json({"error": "Bad webhook."}, 400)
        supplied = self.headers.get("X-Paystack-Signature") or ""
        expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest() if secret else ""
        if not secret or not hmac.compare_digest(supplied, expected):
            return self._json({"error": "Invalid signature."}, 401)
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "Bad webhook."}, 400)
        if event.get("event") == "charge.success":
            db.record_paystack_payment(event.get("data") or {})
        return self._json({"ok": True})

    def _handle_team_message(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if not db.plan_for_company(user["company_id"])["team_chat_enabled"]:
            return self._json({"error": "Team chat requires a team plan."}, 403)
        if rate_limited(f"team-chat:{user['id']}", max_hits=30, window=60):
            return self._json({"error": "Too many messages—wait a moment and try again."}, 429)
        try:
            req = self._body(max_len=18 * 1024 * 1024)
            body = str(req.get("message") or "").strip()[:4000]
            recipient_raw = req.get("recipient_id")
            recipient_id = int(recipient_raw) if recipient_raw not in (None, "", "group") else None
            raw_files = (req.get("files") or [])[:3]
            files = [normalize_document_upload(file) for file in raw_files]
            if sum(file["size_bytes"] for file in files) > 10 * 1024 * 1024:
                raise ValueError("Files in one message are limited to 10 MB total.")
            if not body and not files:
                raise ValueError("Write a message or attach a file.")
            message = db.create_team_message(
                user["company_id"], user["id"], body, files, recipient_id
            )
        except (ValueError, binascii.Error, db.AuthError) as exc:
            return self._json({"error": str(exc) or "Could not send that message."}, 400)
        except Exception as exc:
            print(f"  ! team message failure: {exc}")
            return self._json({"error": "Could not send that message."}, 500)
        return self._json({"ok": True, "message": message})

    def _handle_clear_team_conversation(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if not db.plan_for_company(user["company_id"])["team_chat_enabled"]:
            return self._json({"error": "Team chat requires a team plan."}, 403)
        try:
            recipient_raw = self._body().get("recipient_id")
            recipient_id = int(recipient_raw) if recipient_raw not in (None, "", "group") else None
            db.clear_team_conversation(user["company_id"], user["id"], recipient_id)
        except (TypeError, ValueError):
            return self._json({"error": "Bad conversation."}, 400)
        return self._json({"ok": True})

    def _handle_team_messages_seen(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            recipient_raw = self._body().get("recipient_id")
            recipient_id = int(recipient_raw) if recipient_raw not in (None, "", "group") else None
        except (TypeError, ValueError):
            return self._json({"error": "Bad conversation."}, 400)
        db.mark_team_messages_seen(user, recipient_id)
        return self._json({"ok": True})

    def _handle_team_file_to_library(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if not db.plan_for_company(user["company_id"])["team_chat_enabled"]:
            return self._json({"error": "Team chat requires a team plan."}, 403)
        try:
            file_id = int(self._body().get("file_id"))
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        file = db.get_team_file(user["company_id"], user["id"], file_id)
        if not file:
            return self._json({"error": "File not found."}, 404)
        try:
            document = db.add_reference_document(
                user["id"], file["name"], file["kind"], file["media_type"],
                file["text_content"], file["data_base64"], file["size_bytes"],
            )
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "document": document})

    # ------------------------------------------------------------ vouchers

    def _handle_voucher_create(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            v = db.create_voucher(
                user["company_id"], user["id"],
                supplier_name=str(req.get("supplier_name") or ""),
                supplier_address=str(req.get("supplier_address") or ""),
                supplier_tel=str(req.get("supplier_tel") or ""),
                supplier_email=str(req.get("supplier_email") or ""),
                invoice_number=str(req.get("invoice_number") or ""),
                invoice_date=str(req.get("invoice_date") or ""),
                received_date=str(req.get("received_date") or ""),
                credit_terms_days=int(req.get("credit_terms_days") or 0),
                lines=req.get("lines") or [],
                vatable_amount=float(req.get("vatable_amount") or 0),
                apply_nhil=bool(req.get("apply_nhil", True)),
                apply_vat=bool(req.get("apply_vat", True)),
                vrpo=bool(req.get("vrpo", False)),
                vrpo_deduction=float(req.get("vrpo_deduction") or 0),
                non_taxable=float(req.get("non_taxable") or 0),
                overpayment=float(req.get("overpayment") or 0),
            )
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad request."}, 400)
        enrich_voucher(v)
        return self._json({"ok": True, "voucher": v})

    def _handle_voucher_submit(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
            voucher_id = int(req.get("voucher_id"))
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            v = db.submit_voucher(user["company_id"], user["id"], voucher_id)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        enrich_voucher(v)
        return self._json({"ok": True, "voucher": v})

    def _handle_voucher_review(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] not in ("senior_accountant", "finance_supervisor"):
            return self._json(
                {"error": "Only an approver or supervisor can review vouchers."}, 403)
        try:
            req = self._body()
            voucher_id = int(req.get("voucher_id"))
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        action = str(req.get("action") or "")
        try:
            if action == "approve":
                v = db.approve_voucher(user["company_id"], user["id"], voucher_id)
            elif action == "reject":
                v = db.reject_voucher(
                    user["company_id"], user["id"], voucher_id, str(req.get("reason") or ""))
            else:
                return self._json({"error": "action must be approve or reject."}, 400)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        enrich_voucher(v)
        return self._json({"ok": True, "voucher": v})

    def _handle_chat(self):
        """The signed-in equivalent of the landing page's demo agent. Grounded
        in this person's real, role-scoped vouchers instead of a hypothetical
        one - see build_voucher_digest/build_chat_system."""
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if rate_limited(f"chat:{user['id']}"):
            return self._json(
                {"error": "Ada is busy right now. Please wait a few minutes and try again."}, 429)
        try:
            req = self._body(max_len=60000)  # higher than the default - a pasted document is bigger than a chat message
        except Exception:
            return self._json({"error": "Bad request."}, 400)

        message = str(req.get("message") or "").strip()[:MAX_MESSAGE]
        if not message:
            return self._json({"error": "Say something first."}, 400)

        gate = db.can_use_chat(user["company_id"])
        if not gate["allowed"]:
            if gate["reason"] == "not_included":
                return self._json({
                    "error": f"Chat isn't included in your company's {gate['plan']} plan.",
                    "reason": "not_included",
                }, 402)
            return self._json({
                "error": f"Your company has used all {gate['limit']} Chat messages included "
                         f"in the {gate['plan']} plan this month.",
                "reason": "quota_exceeded", "used": gate["used"], "limit": gate["limit"],
            }, 402)

        cfg = load_env()
        pub = public_user(user)
        provider, model = resolve_provider_model(cfg, pub["company"])
        if not provider:
            reference = report_application_error(
                "ada.chat.configuration", "No AI provider is configured", user)
            return self._json({"error": ada_unavailable(reference)}, 503)

        history = []
        for t in (req.get("history") or [])[-MAX_HISTORY:]:
            role = "assistant" if t.get("role") == "assistant" else "user"
            text = str(t.get("content") or "").strip()[:1500]
            if text:
                history.append({"role": role, "content": text})

        # Tagged 'text'/'attached' unconditionally here, never taken from the
        # request - there's no template/reference-library feature to draw
        # from, so nothing a client sends should ever be labelled trusted
        # 'library' content (see providers.split_docs).
        docs = []
        for d in (req.get("docs") or [])[:3]:
            text = str(d.get("text") or "").strip()[:20000]
            if text:
                docs.append({
                    "kind": "text", "source": "attached",
                    "name": str(d.get("name") or "attachment").strip()[:120],
                    "text": text,
                })

        vouchers = db.list_vouchers(user["company_id"], user["id"], user["role"])
        for v in vouchers:
            enrich_voucher(v)
        digest = build_voucher_digest(vouchers)
        if re.search(r"\b(email|emails|mailbox|inbox|sender|thread|reply|replies)\b", message, re.I):
            mail_context = recent_mail_context(user["id"], cfg)
            digest += ("\n\n## Recent connected mailbox messages\n"
                       + providers.UNTRUSTED_CONTENT_NOTE + "\n\n"
                       + (mail_context or "No recent readable messages were returned by the connected mailboxes."))

        # Same shape as the mailbox gate above, but narrowed to the tools this
        # person actually has connected - see tools.relevant_to.
        connected_ids = {c["tool"] for c in db.list_tool_connections(user["id"])}
        relevant = tools.relevant_to(message, connected_ids) if connected_ids else []
        if relevant:
            tools_context = recent_tools_context(user, cfg, relevant)
            if tools_context:
                digest += "\n\n" + tools_context

        try:
            reply = providers.chat(
                provider, model, cfg.get(PROVIDER_KEYS[provider], ""),
                message, digest, history, system=build_chat_system(pub),
                briefing=effective_briefing(pub, include_library_text=False),
                docs=(user_reference_docs(user["id"]) + docs) or None,
            )
        except providers.ProviderError as exc:
            reference = report_application_error("ada.chat.provider", exc, user)
            return self._json({"error": ada_unavailable(reference)}, 503)
        except Exception as exc:
            print(f"  ! chat failure: {exc}")
            reference = report_application_error("ada.chat.server", exc, user)
            return self._json({"error": ada_unavailable(reference)}, 500)

        used = db.increment_chat_usage(user["company_id"])
        return self._json({"reply": reply, "usage": {"used": used, "limit": gate["limit"]}})

    # ------------------------------------------------------- platform admin

    def _handle_admin_login(self):
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        email_address = str(req.get("email") or "").strip().lower()
        if db.login_rate_limited("admin", client_ip(self), email_address, max_hits=6):
            return self._json({"error": "Too many sign-in attempts. Try again in 15 minutes."}, 429)
        try:
            admin = db.authenticate_admin(email_address, str(req.get("password") or ""))
        except db.AuthError:
            db.record_login_failure("admin", client_ip(self), email_address)
            return self._json({"error": "Email or password is incorrect."}, 401)
        if admin.get("mfa_secret_enc"):
            code = str(req.get("mfa_code") or "")
            if not code:
                return self._json({"error": "Enter the code from your authenticator app.",
                                   "mfa_required": True}, 401)
            if not verify_admin_mfa(admin, code):
                db.record_login_failure("admin", client_ip(self), email_address)
                return self._json({"error": "That verification code is not valid.",
                                   "mfa_required": True}, 401)
        db.clear_login_failures("admin", email_address)
        token = db.create_admin_session(admin["id"])
        db.record_admin_activity(admin, "signed_in", "admin", admin["id"], admin["name"])
        return self._json(
            {"ok": True, "admin": public_admin(admin)},
            extra_headers=[("Set-Cookie", _cookie_header(ADMIN_COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
        )

    def _handle_admin_logout(self):
        db.destroy_admin_session(admin_session_token(self))
        return self._json(
            {"ok": True}, extra_headers=[("Set-Cookie", _cookie_header(ADMIN_COOKIE_NAME, "", 0))]
        )

    def _handle_admin_change_password(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            db.change_admin_password(
                admin["id"],
                str(req.get("current_password") or ""),
                str(req.get("new_password") or ""),
            )
            db.record_admin_activity(admin, "password_changed", "admin", admin["id"], admin["name"])
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True},
                          extra_headers=[("Set-Cookie", _cookie_header(ADMIN_COOKIE_NAME, "", 0))])

    def _handle_admin_chat(self):
        """Ask Ada, for the Command Center. Grounded in a platform digest
        scoped to this admin's role - see build_admin_digest/
        build_admin_chat_system. Every role can chat; no plan/quota concept
        applies here, unlike the company-facing chat - rate limiting is the
        only cost control."""
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        if rate_limited(f"admin_chat:{admin['id']}"):
            return self._json(
                {"error": "Ada is busy right now. Please wait a few minutes and try again."}, 429)
        try:
            req = self._body(max_len=60000)
        except Exception:
            return self._json({"error": "Bad request."}, 400)

        message = str(req.get("message") or "").strip()[:MAX_MESSAGE]
        if not message:
            return self._json({"error": "Say something first."}, 400)

        cfg = load_env()
        provider, model = resolve_admin_provider_model(cfg)
        if not provider:
            reference = report_application_error(
                "ada.admin_chat.configuration", "No AI provider is configured",
                context=f"admin_id={admin['id']} role={admin.get('role')}")
            return self._json({"error": ada_unavailable(reference)}, 503)

        history = []
        for t in (req.get("history") or [])[-MAX_HISTORY:]:
            role = "assistant" if t.get("role") == "assistant" else "user"
            text = str(t.get("content") or "").strip()[:1500]
            if text:
                history.append({"role": role, "content": text})

        docs = []
        for d in (req.get("docs") or [])[:3]:
            text = str(d.get("text") or "").strip()[:20000]
            if text:
                docs.append({
                    "kind": "text", "source": "attached",
                    "name": str(d.get("name") or "attachment").strip()[:120],
                    "text": text,
                })

        try:
            reply = providers.chat(
                provider, model, cfg.get(PROVIDER_KEYS[provider], ""),
                message, build_admin_digest(admin), history,
                system=build_admin_chat_system(admin),
                briefing=db.get_platform_settings().get("ai_briefing") or "",
                docs=docs or None,
            )
        except providers.ProviderError as exc:
            reference = report_application_error(
                "ada.admin_chat.provider", exc, context=f"admin_id={admin['id']}")
            return self._json({"error": ada_unavailable(reference)}, 503)
        except Exception as exc:
            print(f"  ! admin chat failure: {exc}")
            reference = report_application_error(
                "ada.admin_chat.server", exc, context=f"admin_id={admin['id']}")
            return self._json({"error": ada_unavailable(reference)}, 500)

        return self._json({"reply": reply})

    def _handle_admin_set_ai_model(self):
        admin = self._admin_role_request("owner")
        if not admin:
            return
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        provider = req.get("provider")
        cfg = load_env()
        if provider and provider not in configured_providers(cfg):
            return self._json({"error": "That provider isn't configured on this server."}, 400)
        try:
            settings = db.set_platform_ai_model(provider, str(req.get("model") or ""))
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        db.record_admin_activity(admin, "ai_model_changed", "platform_settings", 1,
                                 settings["ai_provider"] or "server default")
        return self._json({"ok": True, "settings": settings})

    def _handle_admin_set_ai_briefing(self):
        admin = self._admin_role_request("owner")
        if not admin:
            return
        try:
            req = self._body(max_len=8000)
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        settings = db.set_platform_ai_briefing(str(req.get("briefing") or ""))
        db.record_admin_activity(admin, "ai_briefing_changed", "platform_settings", 1, "")
        return self._json({"ok": True, "settings": settings})

    def _handle_admin_set_livechat_settings(self):
        admin = self._admin_role_request("owner")
        if not admin:
            return
        try:
            req = self._body(max_len=4000)
            mode = str(req.get("mode") or "schedule")
            schedule = req.get("schedule") or {}
            if not isinstance(schedule, dict):
                raise ValueError
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            settings = db.set_livechat_settings(mode, schedule)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        db.record_admin_activity(admin, "livechat_settings_changed", "platform_settings", 1, mode)
        return self._json({
            "ok": True,
            "livechat_mode": settings["livechat_mode"],
            "livechat_schedule": json.loads(settings["livechat_schedule_json"] or "{}"),
            "livechat_online": is_livechat_online(settings),
        })

    def _handle_admin_site_content_save(self):
        admin = self._admin_role_request("owner")
        if not admin:
            return
        try:
            req = self._body(max_len=200000)
            page = str(req.get("page") or "")
            values = req.get("values") or {}
            if page not in SITE_CONTENT_SCHEMA or not isinstance(values, dict):
                raise ValueError
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        allowed_keys = {f["key"] for f in SITE_CONTENT_SCHEMA[page]}
        unknown = set(values.keys()) - allowed_keys
        if unknown:
            return self._json({"error": f"Unknown field(s): {', '.join(sorted(unknown))}"}, 400)
        try:
            db.set_site_content(page, values)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        db.record_admin_activity(admin, "edited", "site_content", details=f"page={page}, {len(values)} field(s)")
        updated = site_content_effective(page)
        fields = [{**f, "value": updated[f["key"]]} for f in SITE_CONTENT_SCHEMA[page]]
        return self._json({"ok": True, "fields": fields})

    def _handle_admin_delete_company(self):
        admin = self._admin_role_request("owner")
        if not admin:
            return
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            company_id = int(req.get("company_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad request."}, 400)
        try:
            db.delete_company(company_id)
            db.record_admin_activity(admin, "deleted", "company", company_id,
                                     details="Company and related records permanently removed")
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True})

    def _handle_admin_ada_signup_review(self):
        admin = self._admin_role_request("owner")
        if not admin:
            return
        try:
            req = self._body()
            company_id = int(req.get("company_id"))
            action = str(req.get("action") or "")
        except (TypeError, ValueError):
            return self._json({"error": "Bad request."}, 400)
        if action not in ("approve", "reject"):
            return self._json({"error": "Bad request."}, 400)
        try:
            if action == "approve":
                db.approve_ada_signup(company_id)
                db.record_admin_activity(admin, "approved", "company", company_id,
                                         details="Ada-registered signup approved")
            else:
                db.reject_ada_signup(company_id)
                db.record_admin_activity(admin, "rejected", "company", company_id,
                                         details="Ada-registered signup rejected and removed")
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True})

    def _handle_admin_delete_payment(self):
        admin = self._admin_role_request("owner", "billing")
        if not admin:
            return
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            payment_id = int(req.get("payment_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad request."}, 400)
        try:
            db.delete_payment(payment_id)
            db.record_admin_activity(admin, "deleted", "payment", payment_id,
                                     details="Payment record permanently removed")
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True})

    def _handle_admin_mfa_setup(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
            db.authenticate_admin(admin["email"], str(req.get("current_password") or ""))
            cfg = load_env()
            if not secretstore.is_ready(cfg):
                raise db.AuthError("Secure credential storage is not configured.")
            secret = _totp_secret()
            db.set_admin_mfa_pending(
                admin["id"], secretstore.encrypt(cfg, {"totp_secret": secret})
            )
        except (db.AuthError, secretstore.SecretsUnavailable) as exc:
            return self._json({"error": str(exc)}, 400)
        label = quote(f"Buinee:{admin['email']}")
        issuer = quote("Buinee Command Center")
        return self._json({"secret": secret,
                           "otpauth_uri": f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"})

    def _handle_admin_mfa_enable(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            code = str(self._body().get("code") or "")
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        pending = admin.get("mfa_pending_enc") or ""
        try:
            secret = _mfa_secret(load_env(), pending)
        except secretstore.SecretsUnavailable:
            secret = ""
        if not secret or not verify_totp(secret, code):
            return self._json({"error": "That authenticator code is not valid."}, 400)
        recovery_codes = [secrets.token_hex(8).upper() for _ in range(8)]
        recovery_hashes = [hashlib.sha256(code.encode()).hexdigest() for code in recovery_codes]
        db.enable_admin_mfa(admin["id"], pending, recovery_hashes)
        db.record_admin_activity(admin, "mfa_enabled", "admin", admin["id"], admin["name"])
        return self._json({"ok": True, "recovery_codes": recovery_codes},
                          extra_headers=[("Set-Cookie", _cookie_header(ADMIN_COOKIE_NAME, "", 0))])

    def _handle_admin_mfa_disable(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
            db.authenticate_admin(admin["email"], str(req.get("current_password") or ""))
        except db.AuthError:
            return self._json({"error": "Current password is incorrect."}, 400)
        if not verify_admin_mfa(admin, str(req.get("code") or "")):
            return self._json({"error": "That verification code is not valid."}, 400)
        db.disable_admin_mfa(admin["id"])
        db.record_admin_activity(admin, "mfa_disabled", "admin", admin["id"], admin["name"])
        return self._json({"ok": True},
                          extra_headers=[("Set-Cookie", _cookie_header(ADMIN_COOKIE_NAME, "", 0))])

    def _handle_admin_errors_clear(self):
        admin = current_admin(self)
        if not admin_is_owner(admin):
            return self._json({"error": "Only an owner can clear error logs."}, 403)
        db.clear_application_errors()
        db.record_admin_activity(admin, "cleared", "error_log", details="All stored application errors removed")
        return self._json({"ok": True})

    def _handle_admin_inbox_state(self):
        admin = self._admin_role_request("owner", "operations", "support")
        if not admin:
            return
        try:
            req = self._body()
            db.update_admin_inbox_state(
                int(req.get("item_id")), str(req.get("state") or ""),
                item_type=str(req.get("item_type") or "interaction"),
            )
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Could not update inbox item."}, 400)
        return self._json({"ok": True})

    def _handle_admin_inbox_delete(self):
        admin = self._admin_role_request("owner", "operations", "support")
        if not admin:
            return
        try:
            req = self._body()
            db.delete_admin_inbox_item(
                int(req.get("item_id")), item_type=str(req.get("item_type") or "interaction"),
            )
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Could not delete inbox item."}, 400)
        db.record_admin_activity(admin, "deleted", "inbox_item", details=f"item_type={req.get('item_type')}")
        return self._json({"ok": True})

    def _handle_admin_invoice_create(self):
        admin=self._admin_role_request("owner", "billing")
        if not admin: return
        try: invoice=db.save_admin_invoice(self._body(max_len=20000))
        except (db.AuthError,TypeError,ValueError) as exc: return self._json({"error":str(exc)},400)
        db.record_admin_activity(admin,"created","invoice",invoice["id"],invoice["invoice_number"],invoice["customer_name"])
        return self._json({"ok":True,"invoice":invoice})

    def _handle_admin_invoice_status(self):
        admin=self._admin_role_request("owner", "billing")
        if not admin: return
        try:
            req=self._body(); invoice=db.update_admin_invoice_status(int(req.get("invoice_id")),str(req.get("status") or ""))
        except (db.AuthError,TypeError,ValueError) as exc: return self._json({"error":str(exc)},400)
        db.record_admin_activity(admin,"status_changed","invoice",invoice["id"],invoice["invoice_number"],"Status: "+invoice["status"])
        return self._json({"ok":True,"invoice":invoice})

    def _owner_request(self):
        admin = current_admin(self)
        if not admin:
            self._json({"error": "Not signed in."}, 401)
            return None
        if not admin_is_owner(admin):
            self._json({"error": "Only a Command Center owner can manage the back-office team."}, 403)
            return None
        return admin

    def _admin_role_request(self, *allowed):
        admin = current_admin(self)
        if not admin:
            self._json({"error": "Not signed in."}, 401)
            return None
        if admin.get("role", "owner") not in allowed:
            self._json({"error": "Your Command Center role cannot make this change."}, 403)
            return None
        return admin

    def _handle_admin_team_create(self):
        admin = self._owner_request()
        if not admin:
            return
        try:
            req = self._body()
            member = db.create_platform_admin(str(req.get("name") or ""),
                                               str(req.get("email") or ""),
                                               str(req.get("password") or ""),
                                               str(req.get("role") or ""))
            db.record_admin_activity(admin, "created", "admin", member["id"], member["name"],
                                     "Role: " + member["role"])
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Could not add team member."}, 400)
        return self._json({"ok": True, "member": public_admin(member)})

    def _handle_admin_team_update(self):
        admin = self._owner_request()
        if not admin:
            return
        try:
            req = self._body()
            member = db.update_platform_admin(int(req.get("member_id")),
                                              str(req.get("role") or ""),
                                              str(req.get("status") or ""), admin["id"])
            db.record_admin_activity(admin, "updated", "admin", member["id"], member["name"],
                                     "Role: " + member["role"] + "; status: " + member["status"])
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Could not update team member."}, 400)
        return self._json({"ok": True, "member": public_admin(member)})

    def _handle_admin_team_reset_password(self):
        admin = self._owner_request()
        if not admin:
            return
        try:
            req = self._body()
            db.reset_platform_admin_password(int(req.get("member_id")),
                                             str(req.get("password") or ""))
            db.record_admin_activity(admin, "password_reset", "admin", req.get("member_id"),
                                     details="Existing sessions were signed out")
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Could not reset password."}, 400)
        return self._json({"ok": True})

    def _handle_admin_update_crm_account(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body(max_len=12000)
            company_id = int(req.get("company_id"))
            account = db.update_crm_account(company_id, req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad account profile."}, 400)
        return self._json({"ok": True, "account": account})

    def _handle_admin_save_crm_contact(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body(max_len=6000)
            contact = db.save_crm_contact(int(req.get("company_id")), req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad contact."}, 400)
        return self._json({"ok": True, "contact": contact})

    def _handle_admin_delete_crm_contact(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body()
            company_id = int(req.get("company_id"))
            contact_id = int(req.get("contact_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad contact."}, 400)
        if not db.delete_crm_contact(company_id, contact_id):
            return self._json({"error": "Contact not found."}, 404)
        return self._json({"ok": True})

    def _handle_admin_save_crm_interaction(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body(max_len=12000)
            interaction = db.save_crm_interaction(
                int(req.get("company_id")), req, admin["name"]
            )
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad interaction."}, 400)
        return self._json({"ok": True, "interaction": interaction})

    def _handle_admin_delete_crm_interaction(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body()
            company_id = int(req.get("company_id"))
            interaction_id = int(req.get("interaction_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad interaction."}, 400)
        if not db.delete_crm_interaction(company_id, interaction_id):
            return self._json({"error": "Interaction not found."}, 404)
        return self._json({"ok": True})

    def _handle_admin_save_crm_task(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body(max_len=8000)
            task = db.save_crm_task(int(req.get("company_id")), req, admin["name"])
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad follow-up task."}, 400)
        return self._json({"ok": True, "task": task})

    def _handle_admin_delete_crm_task(self):
        admin = self._admin_role_request("owner", "operations", "sales")
        if not admin:
            return
        try:
            req = self._body()
            company_id = int(req.get("company_id"))
            task_id = int(req.get("task_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad follow-up task."}, 400)
        if not db.delete_crm_task(company_id, task_id):
            return self._json({"error": "Follow-up task not found."}, 404)
        return self._json({"ok": True})

    def _handle_admin_save_crm_subscription(self):
        admin = self._admin_role_request("owner", "billing")
        if not admin:
            return
        try:
            req = self._body(max_len=6000)
            subscription = db.save_crm_subscription(int(req.get("company_id")), req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad subscription record."}, 400)
        return self._json({"ok": True, "subscription": subscription})

    def _handle_admin_save_opportunity(self):
        admin = self._admin_role_request("owner", "sales")
        if not admin:
            return
        try:
            opportunity = db.save_crm_opportunity(self._body(max_len=8000))
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad opportunity."}, 400)
        return self._json({"ok": True, "opportunity": opportunity})

    def _handle_admin_delete_opportunity(self):
        admin = self._admin_role_request("owner", "sales")
        if not admin:
            return
        try:
            opportunity_id = int(self._body().get("opportunity_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad opportunity."}, 400)
        if not db.delete_crm_opportunity(opportunity_id):
            return self._json({"error": "Opportunity not found."}, 404)
        return self._json({"ok": True})

    def _handle_admin_create_plan(self):
        admin = self._admin_role_request("owner", "billing")
        if not admin:
            return
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            raw_limit = req.get("chat_monthly_limit")
            chat_limit = int(raw_limit) if raw_limit is not None and str(raw_limit).strip() != "" else None
            plan = db.create_plan(
                str(req.get("name") or ""),
                float(req.get("price") or 0),
                str(req.get("currency") or "GHS"),
                int(req.get("user_limit") or 0),
                chat_enabled=bool(req.get("chat_enabled")),
                chat_monthly_limit=chat_limit,
                audience=str(req.get("audience") or "team"),
                mailbox_limit=int(req.get("mailbox_limit") or 1),
            )
            if "tool_ids" in req:
                db.set_plan_tools(plan["id"], _clean_tool_ids(req["tool_ids"]))
                plan = db.get_plan(plan["id"])
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad request."}, 400)
        return self._json({"ok": True, "plan": plan})

    def _handle_admin_update_plan(self):
        admin = self._admin_role_request("owner", "billing")
        if not admin:
            return
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            plan_id = int(req.get("plan_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad request."}, 400)
        try:
            if "chat_monthly_limit" in req:
                raw_limit = req["chat_monthly_limit"]
                chat_limit = int(raw_limit) if raw_limit is not None and str(raw_limit).strip() != "" else None
            else:
                chat_limit = "unset"
            plan = db.update_plan(
                plan_id,
                name=req.get("name"),
                price=(float(req["price"]) if req.get("price") is not None else None),
                currency=req.get("currency"),
                user_limit=(int(req["user_limit"]) if req.get("user_limit") is not None else None),
                chat_enabled=(bool(req["chat_enabled"]) if "chat_enabled" in req else None),
                chat_monthly_limit=chat_limit,
                mailbox_limit=(int(req["mailbox_limit"]) if req.get("mailbox_limit") is not None else None),
            )
            # Absent means "not editing the tool list", empty list means "this
            # tier includes nothing" - so the two can't be collapsed.
            if "tool_ids" in req:
                db.set_plan_tools(plan_id, _clean_tool_ids(req["tool_ids"]))
                plan = db.get_plan(plan_id)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad request."}, 400)
        return self._json({"ok": True, "plan": plan})

    def _handle_admin_set_company_plan(self):
        admin = self._admin_role_request("owner", "billing")
        if not admin:
            return
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            company_id = int(req.get("company_id"))
            plan_id = int(req.get("plan_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad request."}, 400)
        try:
            company = db.set_company_plan(company_id, plan_id)
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "company": company})


def maybe_bootstrap_admin() -> None:
    """One-time platform-admin creation for hosts with no shell access (a
    shared-hosting plan without SSH, say). No-op the moment any admin
    exists, so it's safe to leave the env vars set indefinitely - this never
    runs a second time and is never reachable over HTTP, only at process
    startup."""
    if db.count_platform_admins() > 0:
        return
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    if not email or not password:
        return
    name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "").strip() or "Admin"
    try:
        db.create_platform_admin(name, email, password)
        print(f"  bootstrapped platform admin: {email}")
        print("  (you can remove BOOTSTRAP_ADMIN_* env vars now - this won't run again)")
    except db.AuthError as exc:
        print(f"  ! could not bootstrap platform admin: {exc}")


# ------------------------------------------------------- transport: sockets
#
# Local dev only: python server.py starts a real ThreadingHTTPServer. Not
# used under Passenger, which calls application() directly instead.

class Handler(RouteHandlerMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _emit(self):
        self.send_response(self._status)
        for k, v in self._resp_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(self._resp_body)

    def do_GET(self):
        self._route_safely(self._route_get)
        self._emit()

    def do_POST(self):
        self._route_safely(self._route_post)
        self._emit()

    def do_HEAD(self):
        # Same response as GET, minus the body - needed because uptime and
        # health checks commonly probe with HEAD, and stdlib's
        # BaseHTTPRequestHandler 501s on any method without a handler.
        self._head_only = True
        try:
            self._route_get()
        finally:
            self._head_only = False
        self._emit()


def main() -> int:
    db.init_db()
    maybe_bootstrap_admin()
    cfg = load_env()
    provider = active_provider(cfg)
    print("\n  Buinee — landing page")
    print(f"  {'-' * 40}")
    print(f"  demo agent : {provider or 'NOT CONFIGURED (add a key to .env)'}")
    print(f"  FX rates   : {'loaded' if fx() else 'not found'}")
    print(f"  database   : {db.DB_FILE}")
    url = f"http://{HOST}:{PORT}"
    print(f"\n  Serving {url}   (Ctrl+C to stop)\n")
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"  Could not bind {HOST}:{PORT} — {exc}")
        return 1
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    return 0


# ---------------------------------------------------------- transport: WSGI
#
# Used by passenger_wsgi.py under cPanel's Python Selector. Not used by
# local dev (python server.py uses the socket transport above instead).

class _WSGIHeaders:
    """Just enough of BaseHTTPRequestHandler's self.headers (a .get(name)
    interface) for the route logic above, backed by a WSGI environ."""

    def __init__(self, environ: dict):
        self._environ = environ

    def get(self, name: str, default=None):
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self._environ.get(key, default)
        return self._environ.get("HTTP_" + key, default)


class WSGIRequest(RouteHandlerMixin):
    def __init__(self, environ: dict):
        query = environ.get("QUERY_STRING", "")
        self.path = environ.get("PATH_INFO", "/") + (f"?{query}" if query else "")
        self.rfile = environ["wsgi.input"]
        self.headers = _WSGIHeaders(environ)
        self.client_address = (environ.get("REMOTE_ADDR", ""),)


def application(environ, start_response):
    req = WSGIRequest(environ)
    method = environ.get("REQUEST_METHOD", "GET").upper()

    if method == "GET":
        req._route_safely(req._route_get)
    elif method == "HEAD":
        req._head_only = True
        try:
            req._route_safely(req._route_get)
        finally:
            req._head_only = False
    elif method == "POST":
        req._route_safely(req._route_post)
    else:
        req._json({"error": "method not allowed"}, 405)

    reason = HTTP_REASONS.get(req._status, "")
    start_response(f"{req._status} {reason}", req._resp_headers)
    return [req._resp_body]


if __name__ == "__main__":
    raise SystemExit(main())
