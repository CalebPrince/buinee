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
}

LEGACY_PAGE_REDIRECTS = {
    "/index.html": "/",
    "/register.html": "/register",
    "/login.html": "/login",
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
}

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
        "PAYSTACK_PUBLIC_KEY", "PAYSTACK_SECRET_KEY", "PAYSTACK_CALLBACK_URL", "PAYSTACK_WEBHOOK_URL",
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


def rate_limited(key: str, max_hits: int = MAX_TURNS, window: int = WINDOW_SECONDS) -> bool:
    now = time.time()
    seen = [t for t in _hits.get(key, []) if now - t < window]
    _hits[key] = seen + [now]
    return len(seen) >= max_hits


def client_ip(handler) -> str:
    return handler.headers.get("X-Forwarded-For", handler.client_address[0]).split(",")[0].strip()


# ------------------------------------------------------------------- auth helpers

def _cookie_header(name: str, token: str, max_age: int) -> str:
    return f"{name}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"


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
            "role": admin.get("role", "owner"), "status": admin.get("status", "active")}


def admin_is_owner(admin: dict | None) -> bool:
    return bool(admin and admin.get("role", "owner") == "owner")


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


SYSTEM = """You are the assistant on Buinee's landing page. Buinee is a \
back-office approval workspace: prepare a voucher, get it approved, issue \
the payment letter - with real roles, a real approval trail and a signature \
recorded in the system rather than printed and scanned. Vouchers are the \
first thing built on it, not the only thing it's for - the roles and \
approval-trail model apply to any back-office document, not just finance.

Who you are talking to: someone on a back-office or finance team - a \
preparer, an approver, a supervisor - who has landed on the page and is \
deciding whether this is worth their time.

How the product works, so you can answer accurately:
- Three roles. The preparer prepares vouchers and letters. The approver
  approves and signs. The supervisor oversees everything.
- Visibility runs downward only: you see your own work and the work of people
  below you, never above. A junior cannot see their supervisor's documents.
- The team chats in the app and shares files there. Any file in the chat
  can be handed to an assistant to read, without leaving the conversation.
- Tax lines are computed, never estimated by a model. Ghana's rates are 5%
  NHIL/GETFL, 15% VAT and 7.5% withholding tax, applied to the vatable portion
  of the invoice - which is often only part of the total.
- Net payable is the invoice total less withholding tax.
- Exchange rates are taken from the Bank of Ghana daily interbank average for
  the invoice date. If none was published that day - a weekend or holiday - the
  most recent prior rate is used and the voucher says so.
- A company registers, then colleagues join it. If the company already exists,
  a new user is placed into it.

Rules for you:
- Be brief. Two or three sentences unless they asked for detail. You are on a
  landing page, not in a meeting.
- Never invent a feature, a price or a date. Buinee is early - if you do not
  know, say so and offer to pass the question on at registration.
- You have no access to anyone's real records, and you cannot read uploaded
  documents here. That is available once their company is registered. Say so
  plainly if asked.
- Never do arithmetic yourself. If figures have been computed for you they will
  appear below - present those and nothing else. If a visitor gives figures and
  no computation appears, ask for the invoice total and the vatable portion.
- If they seem convinced, point them at registering their company. Do not push
  it into every reply."""


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


def paystack_config(cfg: dict) -> dict:
    return {
        "public_key": cfg.get("PAYSTACK_PUBLIC_KEY", ""),
        "secret_key": cfg.get("PAYSTACK_SECRET_KEY", ""),
        "callback_url": cfg.get("PAYSTACK_CALLBACK_URL", "https://buinee.app/api/paystack/callback"),
        "webhook_url": cfg.get("PAYSTACK_WEBHOOK_URL", "https://buinee.app/api/paystack/webhook"),
    }


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

    # ---------------------------------------------------------------- GET

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

        if path == "/api/plans":
            # Public and unauthenticated on purpose - pricing is marketing
            # copy, and the landing page's pricing section and register.html
            # both need the real, current tiers rather than hardcoded copies.
            return self._json({"plans": db.list_plans()})

        if path == "/api/admin/me":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"admin": public_admin(admin)})

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
                    "database_path": str(db.DB_FILE),
                },
            })

        if path == "/api/admin/plans":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"plans": db.list_plans()})

        if path == "/api/admin/pipeline":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"opportunities": db.list_crm_opportunities(),
                               "companies": db.list_company_choices()})

        if path == "/api/admin/payments":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
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
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            query = parse_qs(urlparse(self.path).query)
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            return self._json(db.list_admin_activity(
                page=page, entity_type=query.get("entity_type", [""])[0],
                action=query.get("action", [""])[0]))

        if path == "/api/admin/errors":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            query = parse_qs(urlparse(self.path).query)
            try: limit = int(query.get("limit", ["300"])[0])
            except ValueError: limit = 300
            return self._json({"errors": db.list_application_errors(limit,
                query.get("severity", [""])[0], query.get("query", [""])[0])})

        if path == "/api/admin/reports":
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
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
            admin = current_admin(self)
            if not admin:
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"items": db.list_admin_inbox()})

        if path == "/api/admin/invoices":
            admin = current_admin(self)
            if not admin: return self._json({"error": "Not signed in."}, 401)
            return self._json({"invoices": db.list_admin_invoices(), "companies": db.list_company_choices()})

        if path in STATIC_PAGES:
            f = ROOT / STATIC_PAGES[path]
            if not f.exists():
                return self._json({"error": f"{STATIC_PAGES[path]} missing"}, 404)
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        return self._json({"error": "not found"}, 404)

    # --------------------------------------------------------------- POST

    def _route_post(self):
        path = self.path.split("?")[0]
        handlers = {
            "/api/demo": self._handle_demo,
            "/api/mailbox/connect-imap": self._handle_mailbox_connect_imap,
            "/api/mailbox/disconnect": self._handle_mailbox_disconnect,
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
            "/api/admin/company/delete": self._handle_admin_delete_company,
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

        cfg = load_env()
        provider = active_provider(cfg)
        if not provider:
            reference = report_application_error(
                "ada.demo.configuration", "No AI provider is configured for the public demo")
            return self._json(
                {"error": ada_unavailable(reference)}, 503)

        history = []
        for t in (req.get("history") or [])[-MAX_HISTORY:]:
            role = "assistant" if t.get("role") == "assistant" else "user"
            text = str(t.get("content") or "").strip()[:1500]
            if text:
                history.append({"role": role, "content": text})

        computed = maybe_compute(message)
        model = cfg.get("CLERK_MODEL", "").strip() or providers.DEFAULT_MODELS[provider]

        try:
            reply = providers.chat(
                provider, model, cfg.get(PROVIDER_KEYS[provider], ""),
                message, "(no inbox — this is the public demo)",
                history, system=build_system(computed),
            )
        except providers.ProviderError as exc:
            reference = report_application_error("ada.demo.provider", exc)
            return self._json({"error": ada_unavailable(reference)}, 503)
        except Exception as exc:
            print(f"  ! demo failure: {exc}")
            reference = report_application_error("ada.demo.server", exc)
            return self._json({"error": ada_unavailable(reference)}, 500)

        return self._json({"reply": reply, "computed": bool(computed)})

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
        if rate_limited(f"auth:{client_ip(self)}", max_hits=20, window=600):
            return self._json({"error": "Too many attempts — try again shortly."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            user = db.authenticate(str(req.get("email") or ""), str(req.get("password") or ""))
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 401)
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
                       + (mail_context or "No recent readable messages were returned by the connected mailboxes."))

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
        if rate_limited(f"admin-auth:{client_ip(self)}", max_hits=20, window=600):
            return self._json({"error": "Too many attempts — try again shortly."}, 429)
        try:
            req = self._body()
        except Exception:
            return self._json({"error": "Bad request."}, 400)
        try:
            admin = db.authenticate_admin(str(req.get("email") or ""), str(req.get("password") or ""))
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 401)
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
        return self._json({"ok": True})

    def _handle_admin_delete_company(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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

    def _handle_admin_errors_clear(self):
        admin = current_admin(self)
        if not admin_is_owner(admin):
            return self._json({"error": "Only an owner can clear error logs."}, 403)
        db.clear_application_errors()
        db.record_admin_activity(admin, "cleared", "error_log", details="All stored application errors removed")
        return self._json({"ok": True})

    def _handle_admin_inbox_state(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body()
            db.update_admin_inbox_state(int(req.get("item_id")), str(req.get("state") or ""))
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Could not update inbox item."}, 400)
        return self._json({"ok": True})

    def _handle_admin_invoice_create(self):
        admin=current_admin(self)
        if not admin: return self._json({"error":"Not signed in."},401)
        try: invoice=db.save_admin_invoice(self._body(max_len=20000))
        except (db.AuthError,TypeError,ValueError) as exc: return self._json({"error":str(exc)},400)
        db.record_admin_activity(admin,"created","invoice",invoice["id"],invoice["invoice_number"],invoice["customer_name"])
        return self._json({"ok":True,"invoice":invoice})

    def _handle_admin_invoice_status(self):
        admin=current_admin(self)
        if not admin: return self._json({"error":"Not signed in."},401)
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
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=12000)
            company_id = int(req.get("company_id"))
            account = db.update_crm_account(company_id, req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad account profile."}, 400)
        return self._json({"ok": True, "account": account})

    def _handle_admin_save_crm_contact(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=6000)
            contact = db.save_crm_contact(int(req.get("company_id")), req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad contact."}, 400)
        return self._json({"ok": True, "contact": contact})

    def _handle_admin_delete_crm_contact(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=12000)
            interaction = db.save_crm_interaction(
                int(req.get("company_id")), req, admin["name"]
            )
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad interaction."}, 400)
        return self._json({"ok": True, "interaction": interaction})

    def _handle_admin_delete_crm_interaction(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=8000)
            task = db.save_crm_task(int(req.get("company_id")), req, admin["name"])
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad follow-up task."}, 400)
        return self._json({"ok": True, "task": task})

    def _handle_admin_delete_crm_task(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            req = self._body(max_len=6000)
            subscription = db.save_crm_subscription(int(req.get("company_id")), req)
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad subscription record."}, 400)
        return self._json({"ok": True, "subscription": subscription})

    def _handle_admin_save_opportunity(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            opportunity = db.save_crm_opportunity(self._body(max_len=8000))
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad opportunity."}, 400)
        return self._json({"ok": True, "opportunity": opportunity})

    def _handle_admin_delete_opportunity(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
        try:
            opportunity_id = int(self._body().get("opportunity_id"))
        except (TypeError, ValueError):
            return self._json({"error": "Bad opportunity."}, 400)
        if not db.delete_crm_opportunity(opportunity_id):
            return self._json({"error": "Opportunity not found."}, 404)
        return self._json({"ok": True})

    def _handle_admin_create_plan(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad request."}, 400)
        return self._json({"ok": True, "plan": plan})

    def _handle_admin_update_plan(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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
        except (db.AuthError, TypeError, ValueError) as exc:
            return self._json({"error": str(exc) or "Bad request."}, 400)
        return self._json({"ok": True, "plan": plan})

    def _handle_admin_set_company_plan(self):
        admin = current_admin(self)
        if not admin:
            return self._json({"error": "Not signed in."}, 401)
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
        self._route_get()
        self._emit()

    def do_POST(self):
        self._route_post()
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
        req._route_get()
    elif method == "HEAD":
        req._head_only = True
        try:
            req._route_get()
        finally:
            req._head_only = False
    elif method == "POST":
        req._route_post()
    else:
        req._json({"error": "method not allowed"}, 405)

    reason = HTTP_REASONS.get(req._status, "")
    start_response(f"{req._status} {reason}", req._resp_headers)
    return [req._resp_body]


if __name__ == "__main__":
    raise SystemExit(main())
