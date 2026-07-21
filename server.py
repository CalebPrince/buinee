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

import json
import os
import re
import sys
import time
import webbrowser
from datetime import date
from http.client import responses as HTTP_REASONS
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import providers  # noqa: E402
import voucher  # noqa: E402

# Render (and most PaaS hosts) assign the port via $PORT and expect a bind on
# 0.0.0.0, not 127.0.0.1. Local dev still gets the old localhost-only default.
# Irrelevant under Passenger, which owns the socket itself.
PORT = int(os.environ.get("PORT", 8080))
HOST = os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
ENV_FILE = ROOT / ".env"
FX_FILE = ROOT / "bog-fx-rates.xlsx"

STATIC_PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/register.html": "register.html",
    "/login.html": "login.html",
    "/dashboard.html": "dashboard.html",
    "/admin.html": "admin.html",
    "/admin-companies.html": "admin-companies.html",
    "/admin-plans.html": "admin-plans.html",
    "/admin-login.html": "admin-login.html",
    "/admin-settings.html": "admin-settings.html",
}

# --- public-endpoint limits ------------------------------------------------
MAX_MESSAGE = 600
MAX_TURNS = 12               # per IP, per window
WINDOW_SECONDS = 600
MAX_HISTORY = 8

# --- auth ---------------------------------------------------------------
COOKIE_NAME = "ledgerline_session"
ADMIN_COOKIE_NAME = "ledgerline_admin_session"

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
    for k in list(PROVIDER_KEYS.values()) + ["CLERK_PROVIDER", "CLERK_MODEL"]:
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
    }
    company["user_count"] = db.company_user_count(user["company_id"])
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
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
    return {"id": admin["id"], "name": admin["name"], "email": admin["email"]}


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
workspace for finance departments: prepare a payment voucher, get it approved, \
issue the payment letter - with real roles, a real approval trail and a \
signature recorded in the system rather than printed and scanned.

Who you are talking to: someone who works in finance - an accountant, a senior \
accountant, a finance supervisor - who has landed on the page and is deciding \
whether this is worth their time.

How the product works, so you can answer accurately:
- Three roles. The account assistant prepares vouchers and letters. The senior
  accountant approves and signs. The finance supervisor oversees everything.
- Visibility runs downward only: you see your own work and the work of people
  below you, never above. A junior cannot see their supervisor's documents.
- The department chats in the app and shares files there. Any file in the chat
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

    def _body(self, max_len: int = 20000) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_len:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    # ---------------------------------------------------------------- GET

    def _route_get(self):
        path = self.path.split("?")[0]

        if path == "/api/demo/status":
            cfg = load_env()
            return self._json({"available": active_provider(cfg) is not None})

        if path == "/api/me":
            user = current_user(self)
            if not user:
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"user": public_user(user)})

        if path == "/api/company/pending":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            if user["role"] != "finance_supervisor":
                return self._json({"error": "Only a finance supervisor can see this."}, 403)
            return self._json({"pending": db.list_pending(user["company_id"])})

        if path == "/api/company/team":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            return self._json({"team": db.list_team(user["company_id"])})

        if path == "/api/vouchers":
            user = current_user(self)
            if not user or user["status"] != "approved":
                return self._json({"error": "Not signed in."}, 401)
            vouchers = db.list_vouchers(user["company_id"], user["id"], user["role"])
            for v in vouchers:
                v["computed"] = compute_voucher(v)
            return self._json({"vouchers": vouchers})

        if path == "/api/join/search":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            return self._json({"companies": db.find_companies_by_name(q)})

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
            "/api/register": self._handle_register,
            "/api/join": self._handle_join,
            "/api/login": self._handle_login,
            "/api/logout": self._handle_logout,
            "/api/company/approve": self._handle_approve,
            "/api/vouchers/create": self._handle_voucher_create,
            "/api/vouchers/submit": self._handle_voucher_submit,
            "/api/vouchers/review": self._handle_voucher_review,
            "/api/admin/login": self._handle_admin_login,
            "/api/admin/logout": self._handle_admin_logout,
            "/api/admin/change-password": self._handle_admin_change_password,
            "/api/admin/company/delete": self._handle_admin_delete_company,
            "/api/admin/plans/create": self._handle_admin_create_plan,
            "/api/admin/plans/update": self._handle_admin_update_plan,
            "/api/admin/company/set-plan": self._handle_admin_set_company_plan,
        }
        handler = handlers.get(path)
        if not handler:
            return self._json({"error": "not found"}, 404)
        return handler()

    def _handle_demo(self):
        ip = client_ip(self)
        if rate_limited(f"demo:{ip}"):
            return self._json(
                {"error": "That's a fair few questions — give it a few minutes, "
                          "or register and talk to the real thing."}, 429)

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
            return self._json(
                {"error": "The demo agent isn't configured on this server yet."}, 503)

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
                history, build_system(computed), None,
            )
        except providers.ProviderError as exc:
            return self._json({"error": str(exc)}, 502)
        except Exception as exc:
            print(f"  ! demo failure: {exc}")
            return self._json({"error": "Something went wrong on our side."}, 500)

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
            user = db.register_company(
                str(req.get("company_name") or ""),
                str(req.get("name") or ""),
                str(req.get("email") or ""),
                str(req.get("password") or ""),
                str(req.get("role") or ""),
            )
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
        token = db.create_session(user["id"])
        return self._json(
            {"ok": True, "user": public_user(user)},
            extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
        )

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
            user = db.request_to_join(
                company_id,
                str(req.get("name") or ""),
                str(req.get("email") or ""),
                str(req.get("password") or ""),
                str(req.get("role") or ""),
            )
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)

        if user["status"] == "approved":
            # Only reachable by claiming Finance Supervisor on a company that
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
        return self._json(
            {"ok": True, "user": public_user(user)},
            extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, token, db.SESSION_TTL_SECONDS))],
        )

    def _handle_logout(self):
        db.destroy_session(session_token(self))
        return self._json({"ok": True}, extra_headers=[("Set-Cookie", _cookie_header(COOKIE_NAME, "", 0))])

    def _handle_approve(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] != "finance_supervisor":
            return self._json({"error": "Only a finance supervisor can approve requests."}, 403)
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
        v["computed"] = compute_voucher(v)
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
        v["computed"] = compute_voucher(v)
        return self._json({"ok": True, "voucher": v})

    def _handle_voucher_review(self):
        user = current_user(self)
        if not user or user["status"] != "approved":
            return self._json({"error": "Not signed in."}, 401)
        if user["role"] not in ("senior_accountant", "finance_supervisor"):
            return self._json(
                {"error": "Only a senior accountant or finance supervisor can review vouchers."}, 403)
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
        v["computed"] = compute_voucher(v)
        return self._json({"ok": True, "voucher": v})

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
        except db.AuthError as exc:
            return self._json({"error": str(exc)}, 400)
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
            plan = db.create_plan(
                str(req.get("name") or ""),
                float(req.get("price") or 0),
                str(req.get("currency") or "GHS"),
                int(req.get("user_limit") or 0),
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
            plan = db.update_plan(
                plan_id,
                name=req.get("name"),
                price=(float(req["price"]) if req.get("price") is not None else None),
                currency=req.get("currency"),
                user_limit=(int(req["user_limit"]) if req.get("user_limit") is not None else None),
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
    """One-time platform-admin creation for hosts with no shell access (e.g.
    Render's free tier). No-op the moment any admin exists, so it's safe to
    leave the env vars set indefinitely - this never runs a second time and
    is never reachable over HTTP, only at process startup."""
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
        # Same response as GET, minus the body - needed because Render's (and
        # most platforms') health checks probe with HEAD, and stdlib's
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
