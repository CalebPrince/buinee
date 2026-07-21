"""
Buinee's storage: companies, the people in them, and their sessions.

SQLite, stdlib only - consistent with the rest of this project (no ORM, no
extra dependency). One file, gitignored, created on first run.

Auth model:
  - Registering a company creates it. The registrant states their *actual*
    role - Supervisor is not assumed just because they're the one setting
    the account up. Whatever role they pick, they're approved immediately,
    since there's nobody else at a brand-new company who could approve them.
  - Joining an existing company (by name) creates a *pending* user, unless
    they're claiming Supervisor and the company doesn't have one yet - in
    that one case they're approved immediately too, for the same bootstrap
    reason. See has_approved_supervisor(). Once a company has an approved
    supervisor, that door closes: nobody else can walk in and claim the
    role, only the existing supervisor can grant it.
  - Every other join is pending until a Supervisor at that specific company
    approves it. This is deliberate: company name alone is public knowledge,
    so it is not sufficient to grant access on its own. See approve_user().
  - Passwords are salted and stretched with PBKDF2 (stdlib hashlib, no
    bcrypt dependency). Sessions are random tokens in their own table so
    they can be revoked without touching the password.
  - The platform owner (Command Center) is a completely separate identity -
    platform_admins/admin_sessions, nothing to do with companies or users.
    No HTTP route creates a platform_admins row; see create_platform_admin.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).parent
DB_FILE = ROOT / "storage" / "ledgerline.db"

ROLES = ("account_assistant", "senior_accountant", "finance_supervisor")
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

PBKDF2_ITERATIONS = 260_000


class AuthError(ValueError):
    """A user-facing auth problem (bad password, name taken, etc)."""


def _connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _cursor():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _cursor() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id    INTEGER NOT NULL REFERENCES companies(id),
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                role          TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );

            -- Platform owner identity. Deliberately unconnected to companies/
            -- users - this is not a company role, it's whoever runs Buinee
            -- itself. There is no HTTP route that creates a row here; see
            -- db.create_platform_admin and the README.
            CREATE TABLE IF NOT EXISTS platform_admins (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                created_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token      TEXT PRIMARY KEY,
                admin_id   INTEGER NOT NULL REFERENCES platform_admins(id),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );

            -- Pricing tiers. Editable from the Command Center (Plans), not
            -- hardcoded - see list_plans/create_plan/update_plan. Exactly one
            -- row has is_default=1; that's what a newly registered company
            -- gets, and there's always at least the one seeded below.
            CREATE TABLE IF NOT EXISTS plans (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                price      REAL NOT NULL DEFAULT 0,
                currency   TEXT NOT NULL DEFAULT 'GHS',
                user_limit INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            -- Payment vouchers. Figures (vatable_amount, deductions, etc.) are
            -- inputs a preparer typed or an AI read off an invoice - never
            -- something this table computes itself. voucher.py derives the
            -- actual tax/net figures from these on read, so changing a tax
            -- rate later re-derives every existing voucher instead of leaving
            -- stale numbers behind. See list_vouchers for who can see what.
            CREATE TABLE IF NOT EXISTS vouchers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id        INTEGER NOT NULL REFERENCES companies(id),
                created_by        INTEGER NOT NULL REFERENCES users(id),
                status            TEXT NOT NULL DEFAULT 'draft',

                supplier_name     TEXT NOT NULL,
                supplier_address  TEXT NOT NULL DEFAULT '',
                supplier_tel      TEXT NOT NULL DEFAULT '',
                supplier_email    TEXT NOT NULL DEFAULT '',
                invoice_number    TEXT NOT NULL,
                invoice_date      TEXT NOT NULL,
                received_date     TEXT NOT NULL,
                credit_terms_days INTEGER NOT NULL DEFAULT 0,

                lines_json        TEXT NOT NULL,
                vatable_amount    REAL NOT NULL DEFAULT 0,
                apply_nhil        INTEGER NOT NULL DEFAULT 1,
                apply_vat         INTEGER NOT NULL DEFAULT 1,
                vrpo              INTEGER NOT NULL DEFAULT 0,
                vrpo_deduction    REAL NOT NULL DEFAULT 0,
                non_taxable       REAL NOT NULL DEFAULT 0,
                overpayment       REAL NOT NULL DEFAULT 0,

                rejection_reason  TEXT,
                submitted_at      REAL,
                approved_by       INTEGER REFERENCES users(id),
                approved_at       REAL,
                created_at        REAL NOT NULL
            );

            -- Append-only, never edited or deleted - the real approval trail.
            -- vouchers.approved_by/approved_at only ever reflect the CURRENT
            -- state (and are cleared on rejection, since a rejected voucher
            -- isn't approved by anyone), so on their own they lose who
            -- rejected a voucher and when. This table is what list_activity
            -- reads from instead.
            CREATE TABLE IF NOT EXISTS voucher_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id  INTEGER NOT NULL REFERENCES vouchers(id),
                company_id  INTEGER NOT NULL REFERENCES companies(id),
                actor_id    INTEGER NOT NULL REFERENCES users(id),
                event       TEXT NOT NULL,
                note        TEXT,
                created_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions(admin_id);
            CREATE INDEX IF NOT EXISTS idx_vouchers_company ON vouchers(company_id);
            CREATE INDEX IF NOT EXISTS idx_voucher_events_company ON voucher_events(company_id);
            """
        )
        _seed_default_plans(conn)
        _migrate_company_plan_id(conn)
        _migrate_company_ai_settings(conn)


def _seed_default_plans(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] > 0:
        return
    now = time.time()
    # Demo pricing - deliberately placeholder numbers, meant to be edited from
    # the Command Center's Plans page before any real billing happens.
    for name, price, user_limit, sort_order, is_default in (
        ("Free", 0, 3, 0, 1),
        ("Starter", 50, 10, 1, 0),
        ("Growth", 150, 30, 2, 0),
    ):
        conn.execute(
            """INSERT INTO plans (name, price, currency, user_limit, sort_order, is_default, created_at)
               VALUES (?, ?, 'GHS', ?, ?, ?, ?)""",
            (name, price, user_limit, sort_order, is_default, now),
        )


def _migrate_company_plan_id(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(companies)").fetchall()]
    if "plan_id" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN plan_id INTEGER REFERENCES plans(id)")
    default_id = conn.execute("SELECT id FROM plans WHERE is_default = 1 LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE companies SET plan_id = ? WHERE plan_id IS NULL", (default_id,))


# Known chat providers - mirrors providers.py's PROVIDER_KEYS. Duplicated
# rather than imported so db.py stays storage-only with no dependency on
# providers.py (same reasoning as ROLES being its own source of truth here).
AI_PROVIDERS = ("anthropic", "google", "openrouter")


def _migrate_company_ai_settings(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(companies)").fetchall()]
    if "model_provider" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN model_provider TEXT")
    if "model_model" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN model_model TEXT")
    if "briefing" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN briefing TEXT NOT NULL DEFAULT ''")


# ------------------------------------------------------------------ passwords

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def _new_salt() -> bytes:
    return os.urandom(16)


# -------------------------------------------------------------------- lookups

def find_companies_by_name(query: str, limit: int = 8) -> list[dict]:
    """Loose name search for the 'join a company' picker. Name only -
    nothing else about the company is exposed to someone who isn't in it yet.
    """
    query = query.strip()
    if not query:
        return []
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT id, name FROM companies WHERE name LIKE ? ORDER BY name LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_company(company_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT id, name, plan_id, model_provider, model_model, briefing "
            "FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
    return dict(row) if row else None


def set_company_model(company_id: int, provider: str | None, model: str) -> dict:
    """A company's chat provider preference. `provider` must be one of the
    known providers or None to clear the preference (falls back to the
    server's default). Whether that provider actually has a key configured
    on this deployment is checked at request time in server.py, not here -
    db.py has no knowledge of which keys are set."""
    if not get_company(company_id):
        raise AuthError("No such company.")
    provider = (provider or "").strip().lower() or None
    if provider is not None and provider not in AI_PROVIDERS:
        raise AuthError("Not a known AI provider.")
    with _cursor() as conn:
        conn.execute(
            "UPDATE companies SET model_provider = ?, model_model = ? WHERE id = ?",
            (provider, model.strip(), company_id),
        )
    return get_company(company_id)


def set_company_briefing(company_id: int, briefing: str) -> dict:
    """Custom instructions folded into every chat conversation at this
    company - see providers.with_briefing. Supervisor only."""
    if not get_company(company_id):
        raise AuthError("No such company.")
    with _cursor() as conn:
        conn.execute(
            "UPDATE companies SET briefing = ? WHERE id = ?",
            (briefing.strip()[:4000], company_id),
        )
    return get_company(company_id)


def set_company_plan(company_id: int, plan_id: int) -> dict:
    """Move a company onto a different existing plan. Command Center only -
    a company has no way to change its own plan. Doesn't touch its users:
    if the new plan's limit is below the current headcount, nobody is
    removed, they just can't approve anyone new until they're back under it
    (or upgrade again) - same rule can_add_user already enforces everywhere
    else."""
    if not get_company(company_id):
        raise AuthError("No such company.")
    if not get_plan(plan_id):
        raise AuthError("No such plan.")
    with _cursor() as conn:
        conn.execute("UPDATE companies SET plan_id = ? WHERE id = ?", (plan_id, company_id))
    return get_company(company_id)


def get_user_by_email(email: str) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
    return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------- registration

def has_approved_supervisor(company_id: int) -> bool:
    """Whether this company already has someone holding Supervisor.

    Gates the one bootstrap exception: claiming the role is only open while
    nobody holds it yet. Once true, that door closes for everyone else.
    """
    with _cursor() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE company_id = ? AND role = 'finance_supervisor' "
            "AND status = 'approved' LIMIT 1",
            (company_id,),
        ).fetchone()
    return row is not None


def register_company(company_name: str, name: str, email: str, password: str, role: str) -> dict:
    """Create a company and its first user, in whatever role they actually hold.

    Not assumed to be Supervisor just because they're the one setting
    the account up - a junior person can register the company on the boss's
    behalf and get only their own limited access. Whatever role is chosen,
    this account is approved immediately: there's nobody else at a brand-new
    company who could approve it.
    """
    company_name = company_name.strip()
    name = name.strip()
    email = email.strip().lower()

    if len(company_name) < 2:
        raise AuthError("Company name is too short.")
    if role not in ROLES:
        raise AuthError("Not a valid role.")
    if len(name) < 2:
        raise AuthError("Your name is too short.")
    if "@" not in email:
        raise AuthError("That doesn't look like an email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if get_user_by_email(email):
        raise AuthError("An account with that email already exists.")

    salt = _new_salt()
    now = time.time()
    with _cursor() as conn:
        default_plan_id = conn.execute(
            "SELECT id FROM plans WHERE is_default = 1 LIMIT 1"
        ).fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO companies (name, plan_id, created_at) VALUES (?, ?, ?)",
            (company_name, default_plan_id, now),
        )
        company_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO users
               (company_id, name, email, password_hash, salt, role, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'approved', ?)""",
            (company_id, name, email, _hash_password(password, salt), salt.hex(), role, now),
        )
        user_id = cur.lastrowid

    return get_user(user_id) | {"company": {"id": company_id, "name": company_name}}


# ------------------------------------------------------------------ plans

def list_plans() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute("SELECT * FROM plans ORDER BY sort_order").fetchall()
    return [dict(r) for r in rows]


def get_plan(plan_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return dict(row) if row else None


def get_default_plan() -> dict:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM plans WHERE is_default = 1 LIMIT 1").fetchone()
    return dict(row)


def create_plan(name: str, price: float, currency: str, user_limit: int) -> dict:
    name = name.strip()
    currency = currency.strip().upper() or "GHS"
    if len(name) < 2:
        raise AuthError("Plan name is too short.")
    if user_limit < 1:
        raise AuthError("A plan needs to allow at least 1 user.")
    if price < 0:
        raise AuthError("Price can't be negative.")
    with _cursor() as conn:
        next_sort = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM plans").fetchone()["n"]
        cur = conn.execute(
            """INSERT INTO plans (name, price, currency, user_limit, sort_order, is_default, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (name, price, currency, user_limit, next_sort, time.time()),
        )
        plan_id = cur.lastrowid
    return get_plan(plan_id)


def update_plan(plan_id: int, name: str | None = None, price: float | None = None,
                 currency: str | None = None, user_limit: int | None = None) -> dict:
    plan = get_plan(plan_id)
    if not plan:
        raise AuthError("No such plan.")
    if user_limit is not None and user_limit < 1:
        raise AuthError("A plan needs to allow at least 1 user.")
    if price is not None and price < 0:
        raise AuthError("Price can't be negative.")
    new_name = name.strip() if name is not None and name.strip() else plan["name"]
    new_price = plan["price"] if price is None else price
    new_currency = (currency.strip().upper() if currency and currency.strip() else plan["currency"])
    new_limit = plan["user_limit"] if user_limit is None else user_limit
    with _cursor() as conn:
        conn.execute(
            "UPDATE plans SET name = ?, price = ?, currency = ?, user_limit = ? WHERE id = ?",
            (new_name, new_price, new_currency, new_limit, plan_id),
        )
    return get_plan(plan_id)


def company_user_count(company_id: int) -> int:
    with _cursor() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE company_id = ? AND status = 'approved'",
            (company_id,),
        ).fetchone()["n"]


def plan_for_company(company_id: int) -> dict:
    with _cursor() as conn:
        row = conn.execute(
            """SELECT p.* FROM plans p JOIN companies c ON c.plan_id = p.id
               WHERE c.id = ?""",
            (company_id,),
        ).fetchone()
    return dict(row) if row else get_default_plan()


def can_add_user(company_id: int) -> bool:
    plan = plan_for_company(company_id)
    return company_user_count(company_id) < plan["user_limit"]


def request_to_join(company_id: int, name: str, email: str, password: str, role: str) -> dict:
    """Join an existing company. Pending until approved - except claiming
    Supervisor when the company doesn't have one yet, which is approved
    immediately for the same bootstrap reason registration is."""
    name = name.strip()
    email = email.strip().lower()

    if role not in ROLES:
        raise AuthError("Not a valid role.")
    if len(name) < 2:
        raise AuthError("Your name is too short.")
    if "@" not in email:
        raise AuthError("That doesn't look like an email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if not get_company(company_id):
        raise AuthError("That company no longer exists.")
    if get_user_by_email(email):
        raise AuthError("An account with that email already exists.")

    if role == "finance_supervisor":
        if has_approved_supervisor(company_id):
            raise AuthError(
                "This company already has a Supervisor - ask them to add "
                "you instead of joining as one yourself."
            )
        if not can_add_user(company_id):
            plan = plan_for_company(company_id)
            raise AuthError(
                f"This company is on the {plan['name']} plan ({plan['user_limit']} "
                "users) and is already at that limit. Its plan needs to be "
                "upgraded before anyone new can join."
            )
    status = "approved" if role == "finance_supervisor" else "pending"

    salt = _new_salt()
    now = time.time()
    with _cursor() as conn:
        cur = conn.execute(
            """INSERT INTO users
               (company_id, name, email, password_hash, salt, role, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, name, email, _hash_password(password, salt), salt.hex(), role, status, now),
        )
        user_id = cur.lastrowid
    return get_user(user_id)


# --------------------------------------------------------------------- login

def authenticate(email: str, password: str) -> dict:
    user = get_user_by_email(email)
    if not user:
        raise AuthError("No account with that email.")
    salt = bytes.fromhex(user["salt"])
    if _hash_password(password, salt) != user["password_hash"]:
        raise AuthError("Wrong password.")
    if user["status"] == "pending":
        raise AuthError(
            "Your account is waiting for a supervisor at your company to approve it."
        )
    return user


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL_SECONDS),
        )
    return token


def get_user_by_session(token: str | None) -> dict | None:
    if not token:
        return None
    with _cursor() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if not row or row["expires_at"] < time.time():
            return None
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (row["user_id"],)
        ).fetchone()
    return dict(user) if user else None


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _cursor() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ------------------------------------------------------------- approvals (per-company)

def list_pending(company_id: int) -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            """SELECT id, name, email, role, created_at FROM users
               WHERE company_id = ? AND status = 'pending' ORDER BY created_at""",
            (company_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_team(company_id: int) -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            """SELECT id, name, email, role, status, created_at FROM users
               WHERE company_id = ? AND status = 'approved' ORDER BY created_at""",
            (company_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def approve_user(company_id: int, user_id: int) -> None:
    """Approve a pending user - scoped to company_id so a supervisor can only
    approve their own people, never reach into another company's queue."""
    with _cursor() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE id = ? AND company_id = ? AND status = 'pending'",
            (user_id, company_id),
        ).fetchone()
        if not row:
            raise AuthError("No pending request with that id for your company.")
        if not can_add_user(company_id):
            plan = plan_for_company(company_id)
            raise AuthError(
                f"Your company is on the {plan['name']} plan ({plan['user_limit']} "
                "users) and is already at that limit. Upgrade the plan before "
                "approving anyone new."
            )
        conn.execute("UPDATE users SET status = 'approved' WHERE id = ?", (user_id,))


def reject_user(company_id: int, user_id: int) -> None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE id = ? AND company_id = ? AND status = 'pending'",
            (user_id, company_id),
        ).fetchone()
        if not row:
            raise AuthError("No pending request with that id for your company.")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# -------------------------------------------------------- platform admin (cross-company)
#
# A completely separate identity from companies/users - its own table, own
# sessions, own login page (admin-login.html). Not a company role, and there
# is deliberately no HTTP route that creates one: the only way a row lands in
# platform_admins is either a script run directly against the database, or
# server.maybe_bootstrap_admin() at process startup on hosts with no shell
# access (e.g. Render's free tier) - see README's Command Center section.

def count_platform_admins() -> int:
    with _cursor() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM platform_admins").fetchone()["n"]


def _get_admin(admin_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM platform_admins WHERE id = ?", (admin_id,)
        ).fetchone()
    return dict(row) if row else None


def _get_admin_by_email(email: str) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM platform_admins WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
    return dict(row) if row else None


def create_platform_admin(name: str, email: str, password: str) -> dict:
    """Not exposed over HTTP anywhere - called directly, once, by whoever
    operates this deployment. See the setup script this was created with."""
    name = name.strip()
    email = email.strip().lower()
    if len(name) < 2:
        raise AuthError("Name is too short.")
    if "@" not in email:
        raise AuthError("That doesn't look like an email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if _get_admin_by_email(email):
        raise AuthError("A platform admin with that email already exists.")

    salt = _new_salt()
    now = time.time()
    with _cursor() as conn:
        cur = conn.execute(
            """INSERT INTO platform_admins (name, email, password_hash, salt, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, email, _hash_password(password, salt), salt.hex(), now),
        )
        admin_id = cur.lastrowid
    return _get_admin(admin_id)


def authenticate_admin(email: str, password: str) -> dict:
    admin = _get_admin_by_email(email)
    if not admin:
        raise AuthError("No platform admin with that email.")
    salt = bytes.fromhex(admin["salt"])
    if _hash_password(password, salt) != admin["password_hash"]:
        raise AuthError("Wrong password.")
    return admin


def create_admin_session(admin_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, admin_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, admin_id, now, now + SESSION_TTL_SECONDS),
        )
    return token


def get_admin_by_session(token: str | None) -> dict | None:
    if not token:
        return None
    with _cursor() as conn:
        row = conn.execute(
            "SELECT admin_id, expires_at FROM admin_sessions WHERE token = ?", (token,)
        ).fetchone()
        if not row or row["expires_at"] < time.time():
            return None
        admin = conn.execute(
            "SELECT * FROM platform_admins WHERE id = ?", (row["admin_id"],)
        ).fetchone()
    return dict(admin) if admin else None


def destroy_admin_session(token: str | None) -> None:
    if not token:
        return
    with _cursor() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))


def change_admin_password(admin_id: int, current_password: str, new_password: str) -> None:
    admin = _get_admin(admin_id)
    if not admin:
        raise AuthError("No such admin.")
    salt = bytes.fromhex(admin["salt"])
    if _hash_password(current_password, salt) != admin["password_hash"]:
        raise AuthError("Current password is wrong.")
    if len(new_password) < 8:
        raise AuthError("New password must be at least 8 characters.")
    new_salt = _new_salt()
    with _cursor() as conn:
        conn.execute(
            "UPDATE platform_admins SET password_hash = ?, salt = ? WHERE id = ?",
            (_hash_password(new_password, new_salt), new_salt.hex(), admin_id),
        )


def delete_company(company_id: int) -> None:
    """Permanently remove a company and everyone in it. Irreversible - no
    undo, no soft-delete. Command Center only; a company's own Supervisor
    has no way to do this to their own company."""
    if not get_company(company_id):
        raise AuthError("No such company.")
    with _cursor() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE company_id = ?)",
            (company_id,),
        )
        conn.execute("DELETE FROM users WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))


def list_companies_with_stats() -> list[dict]:
    """Every company, with its supervisor and full team/pending lists inline -
    the command center shows all of this up front, no per-company click."""
    with _cursor() as conn:
        companies = conn.execute(
            """SELECT c.id, c.name, c.created_at, p.id AS plan_id, p.name AS plan_name,
                      p.user_limit AS plan_user_limit
               FROM companies c JOIN plans p ON c.plan_id = p.id
               ORDER BY c.created_at""",
        ).fetchall()
        out = []
        for c in companies:
            team = conn.execute(
                """SELECT id, name, email, role, created_at FROM users
                   WHERE company_id = ? AND status = 'approved' ORDER BY created_at""",
                (c["id"],),
            ).fetchall()
            pending = conn.execute(
                """SELECT id, name, email, role, created_at FROM users
                   WHERE company_id = ? AND status = 'pending' ORDER BY created_at""",
                (c["id"],),
            ).fetchall()
            supervisor = next((u for u in team if u["role"] == "finance_supervisor"), None)
            out.append({
                "id": c["id"],
                "name": c["name"],
                "created_at": c["created_at"],
                "approved_count": len(team),
                "pending_count": len(pending),
                "supervisor": {"name": supervisor["name"], "email": supervisor["email"]} if supervisor else None,
                "team": [dict(u) for u in team],
                "pending": [dict(u) for u in pending],
                "plan": {"id": c["plan_id"], "name": c["plan_name"], "user_limit": c["plan_user_limit"]},
            })
    return out


def _voucher_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["lines"] = json.loads(d.pop("lines_json"))
    d["apply_nhil"] = bool(d["apply_nhil"])
    d["apply_vat"] = bool(d["apply_vat"])
    d["vrpo"] = bool(d["vrpo"])
    return d


def _log_event(conn: sqlite3.Connection, voucher_id: int, company_id: int,
                actor_id: int, event: str, note: str | None, when: float) -> None:
    conn.execute(
        "INSERT INTO voucher_events (voucher_id, company_id, actor_id, event, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (voucher_id, company_id, actor_id, event, note, when),
    )


def create_voucher(
    company_id: int, created_by: int, *, supplier_name: str, invoice_number: str,
    invoice_date: str, received_date: str, credit_terms_days: int, lines: list[dict],
    vatable_amount: float = 0.0, apply_nhil: bool = True, apply_vat: bool = True,
    vrpo: bool = False, vrpo_deduction: float = 0.0, non_taxable: float = 0.0,
    overpayment: float = 0.0, supplier_address: str = "", supplier_tel: str = "",
    supplier_email: str = "",
) -> dict:
    """A voucher a preparer is still working on - always starts as a draft,
    never visible to a reviewer until submit_voucher moves it along."""
    supplier_name = supplier_name.strip()
    invoice_number = invoice_number.strip()
    if len(supplier_name) < 2:
        raise AuthError("Supplier name is too short.")
    if not invoice_number:
        raise AuthError("Invoice number is required.")
    if not invoice_date or not received_date:
        raise AuthError("Invoice date and received date are required.")

    clean_lines = []
    for line in lines or []:
        desc = str(line.get("description") or "").strip()
        try:
            amount = float(line.get("amount"))
        except (TypeError, ValueError):
            amount = None
        if not desc or amount is None or amount <= 0:
            continue
        clean_lines.append({
            "description": desc,
            "amount": round(amount, 2),
            "supplier_type": str(line.get("supplier_type") or "").strip(),
            "cost_centre": str(line.get("cost_centre") or "").strip(),
        })
    if not clean_lines:
        raise AuthError("Add at least one line item with a description and a positive amount.")

    now = time.time()
    with _cursor() as conn:
        cur = conn.execute(
            """INSERT INTO vouchers
               (company_id, created_by, status, supplier_name, supplier_address,
                supplier_tel, supplier_email, invoice_number, invoice_date,
                received_date, credit_terms_days, lines_json, vatable_amount,
                apply_nhil, apply_vat, vrpo, vrpo_deduction, non_taxable,
                overpayment, created_at)
               VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, created_by, supplier_name, supplier_address.strip(),
             supplier_tel.strip(), supplier_email.strip(), invoice_number,
             invoice_date, received_date, int(credit_terms_days),
             json.dumps(clean_lines), round(vatable_amount, 2),
             int(bool(apply_nhil)), int(bool(apply_vat)), int(bool(vrpo)),
             round(vrpo_deduction, 2), round(non_taxable, 2),
             round(overpayment, 2), now),
        )
        voucher_id = cur.lastrowid
        _log_event(conn, voucher_id, company_id, created_by, "prepared", None, now)
    return get_voucher(voucher_id)


def get_voucher(voucher_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
    return _voucher_row(row) if row else None


def list_vouchers(company_id: int, viewer_id: int, viewer_role: str) -> list[dict]:
    """Same downward-only visibility as everywhere else in this app: a
    preparer sees only their own vouchers, an approver sees their own plus
    every preparer's, and a supervisor sees the company's entire voucher
    book."""
    with _cursor() as conn:
        if viewer_role == "finance_supervisor":
            rows = conn.execute(
                "SELECT * FROM vouchers WHERE company_id = ? ORDER BY created_at DESC",
                (company_id,),
            ).fetchall()
        elif viewer_role == "senior_accountant":
            rows = conn.execute(
                """SELECT v.* FROM vouchers v JOIN users u ON u.id = v.created_by
                   WHERE v.company_id = ? AND (v.created_by = ? OR u.role = 'account_assistant')
                   ORDER BY v.created_at DESC""",
                (company_id, viewer_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vouchers WHERE company_id = ? AND created_by = ? ORDER BY created_at DESC",
                (company_id, viewer_id),
            ).fetchall()
    return [_voucher_row(r) for r in rows]


def list_activity(company_id: int, viewer_id: int, viewer_role: str, limit: int = 200) -> list[dict]:
    """The real approval trail, scoped by the same downward-only visibility
    rule as list_vouchers - joins voucher_events to vouchers so a viewer
    never sees an event for a voucher they couldn't see in the Vouchers view
    either, and to users for the actor's name."""
    if viewer_role == "finance_supervisor":
        scope_sql, scope_args = "v.company_id = ?", (company_id,)
    elif viewer_role == "senior_accountant":
        scope_sql = ("v.company_id = ? AND (v.created_by = ? OR "
                      "EXISTS (SELECT 1 FROM users cu WHERE cu.id = v.created_by AND cu.role = 'account_assistant'))")
        scope_args = (company_id, viewer_id)
    else:
        scope_sql, scope_args = "v.company_id = ? AND v.created_by = ?", (company_id, viewer_id)

    with _cursor() as conn:
        rows = conn.execute(
            f"""SELECT e.id, e.event, e.note, e.created_at,
                       v.id AS voucher_id, v.supplier_name, v.invoice_number,
                       u.name AS actor_name
                FROM voucher_events e
                JOIN vouchers v ON v.id = e.voucher_id
                JOIN users u ON u.id = e.actor_id
                WHERE {scope_sql}
                ORDER BY e.created_at DESC
                LIMIT ?""",
            (*scope_args, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def submit_voucher(company_id: int, user_id: int, voucher_id: int) -> dict:
    """Only the preparer can submit their own voucher, and only from draft or
    rejected - approved vouchers are done, and nobody submits someone else's
    work out from under them."""
    v = get_voucher(voucher_id)
    if not v or v["company_id"] != company_id:
        raise AuthError("No such voucher.")
    if v["created_by"] != user_id:
        raise AuthError("You can only submit a voucher you prepared.")
    if v["status"] not in ("draft", "rejected"):
        raise AuthError("Only a draft or a rejected voucher can be submitted.")
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            "UPDATE vouchers SET status = 'submitted', submitted_at = ?, rejection_reason = NULL "
            "WHERE id = ?",
            (now, voucher_id),
        )
        _log_event(conn, voucher_id, company_id, user_id, "submitted", None, now)
    return get_voucher(voucher_id)


def approve_voucher(company_id: int, approver_id: int, voucher_id: int) -> dict:
    """Segregation of duties: the person who prepared a voucher can never be
    the one who signs off on it, even if their role would otherwise allow
    approving - see the same rule in reject_voucher."""
    v = get_voucher(voucher_id)
    if not v or v["company_id"] != company_id:
        raise AuthError("No such voucher.")
    if v["status"] != "submitted":
        raise AuthError("Only a submitted voucher can be approved.")
    if v["created_by"] == approver_id:
        raise AuthError("You can't approve a voucher you prepared yourself.")
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            "UPDATE vouchers SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (approver_id, now, voucher_id),
        )
        _log_event(conn, voucher_id, company_id, approver_id, "approved", None, now)
    return get_voucher(voucher_id)


def reject_voucher(company_id: int, approver_id: int, voucher_id: int, reason: str) -> dict:
    v = get_voucher(voucher_id)
    if not v or v["company_id"] != company_id:
        raise AuthError("No such voucher.")
    if v["status"] != "submitted":
        raise AuthError("Only a submitted voucher can be rejected.")
    if v["created_by"] == approver_id:
        raise AuthError("You can't reject a voucher you prepared yourself.")
    reason = reason.strip()
    if not reason:
        raise AuthError("Give a reason so the preparer knows what to fix.")
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            "UPDATE vouchers SET status = 'rejected', rejection_reason = ?, "
            "approved_by = NULL, approved_at = NULL WHERE id = ?",
            (reason, voucher_id),
        )
        _log_event(conn, voucher_id, company_id, approver_id, "rejected", reason, now)
    return get_voucher(voucher_id)


def platform_stats() -> dict:
    """Totals across every company, for the command center's overview tiles."""
    with _cursor() as conn:
        companies = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
        by_status = conn.execute(
            "SELECT status, COUNT(*) AS n FROM users GROUP BY status"
        ).fetchall()
        by_role = conn.execute(
            "SELECT role, COUNT(*) AS n FROM users WHERE status = 'approved' GROUP BY role"
        ).fetchall()
    return {
        "companies": companies,
        "by_status": {r["status"]: r["n"] for r in by_status},
        "by_role": {r["role"]: r["n"] for r in by_role},
    }
