"""BuineePay sandbox: short-lived "pay from your own phone" sessions.

The merchant sandbox (buineepay-sandbox.html) keeps everything in the
visitor's browser. The one thing a browser cannot do alone is hear about a
payment made on a *different* device, so this module holds just enough
shared state for that hop:

  desktop  -> create a session (business name, optional amount, a note)
  phone    -> read the session, then "pay" it with test details
  desktop  -> poll the session for payments and add them to its dashboard

Nothing here is real money, nothing is tied to an account, and nothing is
kept for long:

- Its own SQLite file (storage/sandbox.db), never the main ledgerline.db.
- Sessions expire after SESSION_TTL seconds and are purged on every create.
- A hard cap on live sessions and on payments per session, so an anonymous
  caller can't grow the file without bound.
- Session ids are 128-bit random tokens; knowing one is the only "access",
  which is acceptable because a session only ever holds test data.
- Every field is validated and length-capped here, not trusted from the page.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "storage" / "sandbox.db"

SESSION_TTL = 30 * 60          # seconds a QR code stays payable
MAX_LIVE_SESSIONS = 2000       # across all visitors
MAX_PAYMENTS_PER_SESSION = 20
MIN_AMOUNT = 100               # pesewas (GHS 1.00)
MAX_AMOUNT = 5_000_000         # pesewas (GHS 50,000.00)

METHODS = {"mtn", "telecel", "at", "card"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Test numbers decide the outcome, the same rules the in-browser checkout uses.
FAILURES = {
    "0001": ("declined", "The customer declined the prompt on their phone."),
    "0002": ("insufficient", "The customer's wallet or card did not have enough funds."),
}


class SandboxError(ValueError):
    """A request the caller can fix; the message is safe to show."""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, data TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS payments ("
        " session_id TEXT NOT NULL, seq INTEGER NOT NULL, created REAL NOT NULL, data TEXT NOT NULL,"
        " PRIMARY KEY (session_id, seq))")
    return conn


def _clean_text(value, limit: int) -> str:
    text = _CONTROL.sub("", str(value or "")).strip()
    return text[:limit]


def _amount(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        pesewas = int(value)
    except (TypeError, ValueError):
        raise SandboxError("Amount must be a whole number of pesewas.")
    if not MIN_AMOUNT <= pesewas <= MAX_AMOUNT:
        raise SandboxError("Amount must be between GHS 1.00 and GHS 50,000.00.")
    return pesewas


def _purge(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM payments WHERE session_id IN (SELECT id FROM sessions WHERE expires < ?)", (now,))
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))


def create_session(req: dict) -> dict:
    if not isinstance(req, dict):
        raise SandboxError("Bad request.")
    data = {
        "business": _clean_text(req.get("business"), 60) or "BuineePay merchant",
        "amount": _amount(req.get("amount")),
        "note": _clean_text(req.get("note"), 80),
    }
    now = time.time()
    with _connect() as conn:
        _purge(conn, now)
        live = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if live >= MAX_LIVE_SESSIONS:
            raise SandboxError("The sandbox is busy right now. Try again in a few minutes.")
        sid = secrets.token_urlsafe(16)
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, now, now + SESSION_TTL, json.dumps(data)))
    return {"id": sid, "expires": int(now + SESSION_TTL), **data}


def _load(conn: sqlite3.Connection, sid: str) -> dict:
    if not ID_RE.match(sid or ""):
        raise SandboxError("That payment code isn't valid.")
    row = conn.execute("SELECT data, expires FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not row or row[1] < time.time():
        raise SandboxError("This payment code has expired. Ask for a new QR code.")
    return {**json.loads(row[0]), "expires": int(row[1])}


def get_session(sid: str) -> dict:
    with _connect() as conn:
        return _load(conn, sid)


def pay(req: dict) -> dict:
    if not isinstance(req, dict):
        raise SandboxError("Bad request.")
    sid = str(req.get("id") or "")
    name = _clean_text(req.get("name"), 60)
    method = str(req.get("method") or "")
    digits = re.sub(r"\D", "", str(req.get("number") or ""))
    if not name:
        raise SandboxError("Enter your name.")
    if method not in METHODS:
        raise SandboxError("Choose how to pay.")
    if not 9 <= len(digits) <= 19:
        raise SandboxError("Enter a test wallet or card number.")

    with _connect() as conn:
        # Take the write lock before counting, so two payments landing at the
        # same moment can't both claim the same sequence number.
        conn.execute("BEGIN IMMEDIATE")
        session = _load(conn, sid)
        amount = session["amount"] if session["amount"] is not None else _amount(req.get("amount"))
        if amount is None:
            raise SandboxError("Enter an amount.")
        count = conn.execute("SELECT COUNT(*) FROM payments WHERE session_id = ?", (sid,)).fetchone()[0]
        if count >= MAX_PAYMENTS_PER_SESSION:
            raise SandboxError("This QR code has taken its limit of test payments. Ask for a new one.")

        outcome, reason = FAILURES.get(digits[-4:], ("succeeded", None))
        if method == "card":
            brand = "Mastercard" if digits.startswith("5") else "Visa"
            contact = f"{brand} **** {digits[-4:]}"
        else:
            contact = f"{digits[:3]} *** {digits[-4:]}"
        payment = {
            "seq": count + 1,
            "amount": amount,
            "method": method,
            "customer": name,
            "contact": contact,
            "outcome": outcome,
            "failure": reason,
            "at": int(time.time() * 1000),
        }
        conn.execute("INSERT INTO payments VALUES (?,?,?,?)", (sid, count + 1, time.time(), json.dumps(payment)))
    return payment


def events(sid: str, after: int) -> dict:
    with _connect() as conn:
        session = _load(conn, sid)
        rows = conn.execute(
            "SELECT data FROM payments WHERE session_id = ? AND seq > ? ORDER BY seq",
            (sid, max(0, int(after))),
        ).fetchall()
    return {"expires": session["expires"], "payments": [json.loads(r[0]) for r in rows]}
