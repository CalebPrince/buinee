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

# Who a plan is sold to. A company registering for its members buys a 'team'
# plan; one person working alone buys an 'individual' one. The only mechanical
# difference is seats (an individual plan is a 1-seat plan, which can_add_user
# already enforces) plus two consequences that fall out of being alone: the
# workspace stays out of the public join search (see find_companies_by_name)
# and its owner holds Supervisor, since there's nobody else to approve their
# work.
PLAN_AUDIENCES = ("individual", "team")
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

PBKDF2_ITERATIONS = 260_000


class AuthError(ValueError):
    """A user-facing auth problem (bad password, name taken, etc)."""


class DuplicateCompanyError(AuthError):
    """Registration hit a workspace that already goes by that name.

    Not an outright rejection - two genuinely different businesses can share
    a name, and only the person typing it knows which case this is. Carries
    the existing company so the caller can offer the choice: ask to join that
    one, or say it's a different company and carry on. An AuthError subclass
    so callers that only care about "registration failed" still catch it.
    """

    def __init__(self, company: dict):
        self.company = company
        super().__init__(
            f"A workspace called \"{company['name']}\" is already here."
        )


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
                onboarding_complete INTEGER NOT NULL DEFAULT 1,
                terms_accepted_at REAL,
                terms_version TEXT NOT NULL DEFAULT '',
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
            -- itself. The first owner is bootstrapped at deployment; that
            -- owner can then add the rest of the back-office team.
            CREATE TABLE IF NOT EXISTS platform_admins (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                created_at    REAL NOT NULL,
                role          TEXT NOT NULL DEFAULT 'owner',
                status        TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token      TEXT PRIMARY KEY,
                admin_id   INTEGER NOT NULL REFERENCES platform_admins(id),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_activity_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id     INTEGER REFERENCES platform_admins(id),
                admin_name   TEXT NOT NULL DEFAULT 'System',
                admin_email  TEXT NOT NULL DEFAULT '',
                action       TEXT NOT NULL,
                entity_type  TEXT NOT NULL,
                entity_id    TEXT NOT NULL DEFAULT '',
                entity_label TEXT NOT NULL DEFAULT '',
                details      TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS application_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL DEFAULT 'application',
                severity TEXT NOT NULL DEFAULT 'error', message TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_inbox_states (
                item_type TEXT NOT NULL, item_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'unread', updated_at REAL NOT NULL,
                PRIMARY KEY (item_type,item_id)
            );
            CREATE TABLE IF NOT EXISTS admin_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL UNIQUE,
                company_id INTEGER NOT NULL REFERENCES companies(id), customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT 'GHS',
                issue_date TEXT NOT NULL, due_date TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',
                notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL REFERENCES admin_invoices(id),
                description TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 1, unit_amount REAL NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS crm_accounts (
                company_id           INTEGER PRIMARY KEY REFERENCES companies(id),
                legal_name           TEXT NOT NULL DEFAULT '',
                industry             TEXT NOT NULL DEFAULT '',
                website              TEXT NOT NULL DEFAULT '',
                phone                TEXT NOT NULL DEFAULT '',
                location             TEXT NOT NULL DEFAULT '',
                address              TEXT NOT NULL DEFAULT '',
                registration_number  TEXT NOT NULL DEFAULT '',
                tax_id               TEXT NOT NULL DEFAULT '',
                lifecycle_status     TEXT NOT NULL DEFAULT 'customer',
                relationship_owner   TEXT NOT NULL DEFAULT '',
                primary_contact_name TEXT NOT NULL DEFAULT '',
                primary_contact_email TEXT NOT NULL DEFAULT '',
                summary              TEXT NOT NULL DEFAULT '',
                lifecycle_changed_at REAL NOT NULL DEFAULT 0,
                updated_at           REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_contacts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id  INTEGER NOT NULL REFERENCES companies(id),
                name        TEXT NOT NULL,
                job_title   TEXT NOT NULL DEFAULT '',
                email       TEXT NOT NULL DEFAULT '',
                phone       TEXT NOT NULL DEFAULT '',
                is_primary  INTEGER NOT NULL DEFAULT 0,
                notes       TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_opportunities (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id          INTEGER NOT NULL REFERENCES companies(id),
                name                TEXT NOT NULL,
                stage               TEXT NOT NULL DEFAULT 'prospecting',
                value               REAL NOT NULL DEFAULT 0,
                currency            TEXT NOT NULL DEFAULT 'GHS',
                probability         INTEGER NOT NULL DEFAULT 10,
                expected_close_date TEXT NOT NULL DEFAULT '',
                owner               TEXT NOT NULL DEFAULT '',
                notes               TEXT NOT NULL DEFAULT '',
                created_at          REAL NOT NULL,
                updated_at          REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_interactions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id    INTEGER NOT NULL REFERENCES companies(id),
                contact_id    INTEGER REFERENCES crm_contacts(id),
                interaction_type TEXT NOT NULL DEFAULT 'note',
                direction     TEXT NOT NULL DEFAULT 'internal',
                subject       TEXT NOT NULL DEFAULT '',
                body          TEXT NOT NULL,
                occurred_at   REAL NOT NULL,
                author_name   TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_tasks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id    INTEGER NOT NULL REFERENCES companies(id),
                assigned_user_id INTEGER REFERENCES users(id),
                title         TEXT NOT NULL,
                details       TEXT NOT NULL DEFAULT '',
                owner         TEXT NOT NULL DEFAULT '',
                due_date      TEXT NOT NULL DEFAULT '',
                priority      TEXT NOT NULL DEFAULT 'normal',
                status        TEXT NOT NULL DEFAULT 'open',
                completed_at  REAL,
                created_by    TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_subscriptions (
                company_id        INTEGER PRIMARY KEY REFERENCES companies(id),
                subscription_status TEXT NOT NULL DEFAULT 'active',
                billing_cycle     TEXT NOT NULL DEFAULT 'monthly',
                renewal_date      TEXT NOT NULL DEFAULT '',
                payment_status    TEXT NOT NULL DEFAULT 'not_connected',
                customer_reference TEXT NOT NULL DEFAULT '',
                notes             TEXT NOT NULL DEFAULT '',
                updated_at        REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL UNIQUE,
                company_id INTEGER NOT NULL REFERENCES companies(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                plan_id INTEGER NOT NULL REFERENCES plans(id),
                amount_subunit INTEGER NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'initialized',
                provider_id TEXT NOT NULL DEFAULT '', customer_email TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '', card_type TEXT NOT NULL DEFAULT '',
                card_brand TEXT NOT NULL DEFAULT '', card_last4 TEXT NOT NULL DEFAULT '',
                card_bank TEXT NOT NULL DEFAULT '', card_exp_month TEXT NOT NULL DEFAULT '',
                card_exp_year TEXT NOT NULL DEFAULT '', gateway_response TEXT NOT NULL DEFAULT '',
                paid_at TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
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

            -- Chat messages cost real money on the platform owner's own AI
            -- provider keys (see providers.py / server.py's resolve_provider_
            -- model) - a company doesn't bring its own key, so usage has to
            -- tie back to what its plan actually pays for. One row per
            -- company per calendar month; the month simply not existing yet
            -- is what "0 used this month" means, no reset job needed.
            CREATE TABLE IF NOT EXISTS chat_usage (
                company_id  INTEGER NOT NULL REFERENCES companies(id),
                year_month  TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (company_id, year_month)
            );

            -- A person's own connected mailbox - Microsoft, Google or plain
            -- IMAP. One row per user, not per company: the credential is
            -- that individual's access to their own mail, and nobody else at
            -- the company - supervisor included - is entitled to read it
            -- through them. Deleting the row is what "disconnect" means.
            --
            -- credentials_enc holds whatever that provider needs (a refresh
            -- token, or an IMAP password), encrypted via secretstore. It is
            -- the only column here that is secret: host, port and address
            -- are ordinary settings and stay readable so the UI can show
            -- them without a key. Nothing is ever written unencrypted - with
            -- no key configured, connecting is refused instead.
            CREATE TABLE IF NOT EXISTS mailbox_connections (
                user_id         INTEGER PRIMARY KEY REFERENCES users(id),
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                provider        TEXT NOT NULL DEFAULT 'microsoft',
                account_email   TEXT NOT NULL DEFAULT '',
                account_name    TEXT NOT NULL DEFAULT '',
                imap_host       TEXT NOT NULL DEFAULT '',
                imap_port       INTEGER NOT NULL DEFAULT 0,
                credentials_enc TEXT NOT NULL,
                scopes          TEXT NOT NULL DEFAULT '',
                connected_at    REAL NOT NULL
            );

            -- Short-lived CSRF state for the OAuth round trip. A row is spent
            -- the moment the callback consumes it, so a replayed callback
            -- finds nothing and is rejected.
            CREATE TABLE IF NOT EXISTS oauth_states (
                state      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                provider   TEXT NOT NULL DEFAULT 'microsoft',
                created_at REAL NOT NULL
            );

            -- Automation definitions live in code; these rows only store a
            -- user's choice and execution history. recipe_key is deliberately
            -- not an enum/foreign key so new recipes can be added without a
            -- database migration.
            CREATE TABLE IF NOT EXISTS automation_settings (
                user_id     INTEGER NOT NULL REFERENCES users(id),
                recipe_key  TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 0,
                next_run_at REAL,
                updated_at  REAL NOT NULL,
                PRIMARY KEY (user_id, recipe_key)
            );

            CREATE TABLE IF NOT EXISTS automation_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                company_id  INTEGER NOT NULL REFERENCES companies(id),
                recipe_key  TEXT NOT NULL,
                status      TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                error       TEXT NOT NULL DEFAULT '',
                started_at  REAL NOT NULL,
                finished_at REAL
            );

            CREATE TABLE IF NOT EXISTS user_instructions (
                user_id     INTEGER PRIMARY KEY REFERENCES users(id),
                briefing    TEXT NOT NULL DEFAULT '',
                updated_at  REAL NOT NULL
            );

            -- Private reference library. Text is stored as text; PDFs/images
            -- are base64 so they can be passed natively to supported models.
            -- Every query is scoped by user_id: company role does not grant
            -- access to another person's working documents.
            CREATE TABLE IF NOT EXISTS reference_documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                name        TEXT NOT NULL,
                kind        TEXT NOT NULL,
                media_type  TEXT NOT NULL DEFAULT 'text/plain',
                text_content TEXT NOT NULL DEFAULT '',
                data_base64 TEXT NOT NULL DEFAULT '',
                size_bytes  INTEGER NOT NULL,
                created_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id  INTEGER NOT NULL REFERENCES companies(id),
                sender_id   INTEGER NOT NULL REFERENCES users(id),
                recipient_id INTEGER REFERENCES users(id),
                body        TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_message_files (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id   INTEGER NOT NULL REFERENCES team_messages(id),
                company_id   INTEGER NOT NULL REFERENCES companies(id),
                uploader_id  INTEGER NOT NULL REFERENCES users(id),
                name         TEXT NOT NULL,
                kind         TEXT NOT NULL,
                media_type   TEXT NOT NULL,
                text_content TEXT NOT NULL DEFAULT '',
                data_base64  TEXT NOT NULL DEFAULT '',
                size_bytes   INTEGER NOT NULL,
                created_at   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_conversation_clears (
                user_id       INTEGER NOT NULL REFERENCES users(id),
                conversation  TEXT NOT NULL,
                cleared_through_id INTEGER NOT NULL DEFAULT 0,
                cleared_at    REAL NOT NULL,
                PRIMARY KEY (user_id, conversation)
            );

            CREATE TABLE IF NOT EXISTS user_notification_state (
                user_id    INTEGER NOT NULL REFERENCES users(id),
                state_key  TEXT NOT NULL,
                state_value INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, state_key)
            );

            CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions(admin_id);
            CREATE INDEX IF NOT EXISTS idx_admin_activity_created ON admin_activity_log(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_admin_activity_entity ON admin_activity_log(entity_type, action);
            CREATE INDEX IF NOT EXISTS idx_vouchers_company ON vouchers(company_id);
            CREATE INDEX IF NOT EXISTS idx_voucher_events_company ON voucher_events(company_id);
            CREATE INDEX IF NOT EXISTS idx_automation_due ON automation_settings(enabled, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_automation_runs_user ON automation_runs(user_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_reference_documents_user ON reference_documents(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_team_messages_company ON team_messages(company_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_message_files_message ON team_message_files(message_id);
            CREATE INDEX IF NOT EXISTS idx_crm_contacts_company ON crm_contacts(company_id, is_primary, name);
            CREATE INDEX IF NOT EXISTS idx_crm_opportunities_stage ON crm_opportunities(stage, expected_close_date);
            CREATE INDEX IF NOT EXISTS idx_crm_interactions_company ON crm_interactions(company_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_crm_tasks_company ON crm_tasks(company_id, status, due_date);
            CREATE INDEX IF NOT EXISTS idx_payments_company ON payments(company_id, created_at DESC);
            """
        )
        # Chat gating first: the plans CREATE TABLE above predates those two
        # columns, so on a brand-new database they only exist once this has
        # run - and _seed_default_plans inserts into them. Its backfill is a
        # no-op here because there are no rows to backfill yet.
        _migrate_plan_chat_gating(conn)
        _migrate_plan_audience(conn)
        _migrate_multiple_mailboxes(conn)
        _seed_default_plans(conn)
        _seed_individual_plans(conn)
        _migrate_plan_mailbox_limits(conn)
        _migrate_company_plan_id(conn)
        _migrate_company_ai_settings(conn)
        _migrate_team_message_recipient(conn)
        _migrate_crm_profile_fields(conn)
        _migrate_crm_task_assignee(conn)
        _migrate_user_terms_acceptance(conn)
        _migrate_user_onboarding(conn)
        _migrate_platform_admin_roles(conn)


# Demo pricing - deliberately placeholder numbers, meant to be edited from the
# Command Center's Plans page before any real billing happens.
# chat_monthly_limit is NULL for unlimited.
_TEAM_SEEDS = (
    # name, price, user_limit, sort_order, is_default, chat_enabled, chat_limit
    ("Free", 0, 3, 10, 1, 0, None),
    ("Starter", 50, 10, 11, 0, 1, 200),
    ("Growth", 150, 30, 12, 0, 1, None),
)
_INDIVIDUAL_SEEDS = (
    ("Solo Free", 0, 1, 0, 0, 0, None),
    ("Solo Pro", 25, 1, 1, 0, 1, 200),
)


def _insert_seed_plans(conn: sqlite3.Connection, seeds, audience: str) -> None:
    now = time.time()
    for name, price, user_limit, sort_order, is_default, chat_enabled, chat_limit in seeds:
        conn.execute(
            """INSERT INTO plans (name, price, currency, user_limit, sort_order, is_default,
                                   chat_enabled, chat_monthly_limit, audience, created_at)
               VALUES (?, ?, 'GHS', ?, ?, ?, ?, ?, ?, ?)""",
            (name, price, user_limit, sort_order, is_default, chat_enabled, chat_limit,
             audience, now),
        )


def _seed_default_plans(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] > 0:
        return
    _insert_seed_plans(conn, _INDIVIDUAL_SEEDS, "individual")
    _insert_seed_plans(conn, _TEAM_SEEDS, "team")


def _seed_individual_plans(conn: sqlite3.Connection) -> None:
    """Give databases that predate the individual/team split something to sell
    individuals, without touching the team plans already there.

    Only fires when there isn't a single individual plan yet, so an owner who
    reprices or renames these keeps their edits. Plans can't be deleted (there
    is no delete_plan), so this can't resurrect one that was removed on
    purpose.
    """
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE audience = 'individual'"
    ).fetchone()["n"]
    if n == 0:
        _insert_seed_plans(conn, _INDIVIDUAL_SEEDS, "individual")


def _migrate_plan_chat_gating(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
    if "chat_enabled" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN chat_enabled INTEGER NOT NULL DEFAULT 0")
    if "chat_monthly_limit" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN chat_monthly_limit INTEGER")
    if "chat_enabled" not in cols:
        # Backfill only the exact seeded demo plan names from before this
        # migration existed - anything else (a plan the owner already
        # renamed or added) is left at the safe default (chat off,
        # editable from the Command Center) rather than guessed at.
        conn.execute("UPDATE plans SET chat_enabled = 0 WHERE name = 'Free'")
        conn.execute("UPDATE plans SET chat_enabled = 1, chat_monthly_limit = 200 WHERE name = 'Starter'")
        conn.execute("UPDATE plans SET chat_enabled = 1, chat_monthly_limit = NULL WHERE name = 'Growth'")


def _migrate_plan_audience(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
    if "audience" not in cols:
        # Everything that existed before this split was sold to a company for
        # its members, so 'team' is the honest default for old rows - and for
        # any plan created without saying otherwise.
        conn.execute(
            "ALTER TABLE plans ADD COLUMN audience TEXT NOT NULL DEFAULT 'team'"
        )


def _migrate_plan_mailbox_limits(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
    if "mailbox_limit" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN mailbox_limit INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE plans SET mailbox_limit=3 WHERE name IN ('Starter','Solo Pro')")
        conn.execute("UPDATE plans SET mailbox_limit=10 WHERE name='Growth'")


def _migrate_multiple_mailboxes(conn: sqlite3.Connection) -> None:
    cols = conn.execute("PRAGMA table_info(mailbox_connections)").fetchall()
    if any(row["name"] == "id" for row in cols):
        return
    conn.execute("ALTER TABLE mailbox_connections RENAME TO mailbox_connections_single")
    conn.execute("""CREATE TABLE mailbox_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id), company_id INTEGER NOT NULL REFERENCES companies(id),
        provider TEXT NOT NULL DEFAULT 'microsoft', account_email TEXT NOT NULL DEFAULT '',
        account_name TEXT NOT NULL DEFAULT '', imap_host TEXT NOT NULL DEFAULT '', imap_port INTEGER NOT NULL DEFAULT 0,
        credentials_enc TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '', connected_at REAL NOT NULL,
        UNIQUE(user_id, provider, account_email))""")
    conn.execute("""INSERT INTO mailbox_connections
        (user_id,company_id,provider,account_email,account_name,imap_host,imap_port,credentials_enc,scopes,connected_at)
        SELECT user_id,company_id,provider,account_email,account_name,imap_host,imap_port,credentials_enc,scopes,connected_at
        FROM mailbox_connections_single""")
    conn.execute("DROP TABLE mailbox_connections_single")


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


def _migrate_team_message_recipient(conn: sqlite3.Connection) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(team_messages)").fetchall()]
    if "recipient_id" not in cols:
        conn.execute("ALTER TABLE team_messages ADD COLUMN recipient_id INTEGER REFERENCES users(id)")


def _migrate_crm_profile_fields(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(crm_accounts)").fetchall()}
    for name in ("address", "registration_number", "tax_id"):
        if name not in cols:
            conn.execute(f"ALTER TABLE crm_accounts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    if "lifecycle_changed_at" not in cols:
        conn.execute("ALTER TABLE crm_accounts ADD COLUMN lifecycle_changed_at REAL NOT NULL DEFAULT 0")
        conn.execute("UPDATE crm_accounts SET lifecycle_changed_at = updated_at WHERE lifecycle_changed_at = 0")


def _migrate_crm_task_assignee(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(crm_tasks)").fetchall()}
    if "assigned_user_id" not in cols:
        conn.execute("ALTER TABLE crm_tasks ADD COLUMN assigned_user_id INTEGER REFERENCES users(id)")


def _migrate_user_terms_acceptance(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "terms_accepted_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN terms_accepted_at REAL")
    if "terms_version" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN terms_version TEXT NOT NULL DEFAULT ''")


def _migrate_user_onboarding(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "onboarding_complete" not in cols:
        # Existing accounts have already been using the product; only accounts
        # created after this feature ships should receive the first-login tour.
        conn.execute(
            "ALTER TABLE users ADD COLUMN onboarding_complete INTEGER NOT NULL DEFAULT 1"
        )


def _migrate_platform_admin_roles(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(platform_admins)").fetchall()}
    if "role" not in cols:
        conn.execute("ALTER TABLE platform_admins ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'")
    if "status" not in cols:
        conn.execute("ALTER TABLE platform_admins ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")


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

    Workspaces on an individual plan are listed here too. Someone who started
    out alone and then hired is the exact case this picker has to serve: the
    colleague finds the workspace by name, requests to join, and the request
    sits pending until the workspace is moved onto a team plan. Hiding them
    would only push that colleague into registering a duplicate company under
    the same name.
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


def find_company_by_exact_name(name: str) -> dict | None:
    """The workspace that already answers to this exact name, if any.

    Case- and whitespace-insensitive, because "coastal logistics ltd" and
    "Coastal Logistics Ltd" are the same company to everyone except SQL.
    """
    name = " ".join(name.split())
    if not name:
        return None
    with _cursor() as conn:
        row = conn.execute(
            "SELECT id, name FROM companies WHERE LOWER(TRIM(name)) = LOWER(?) "
            "ORDER BY created_at LIMIT 1",
            (name,),
        ).fetchone()
    return dict(row) if row else None


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


def register_company(company_name: str, name: str, email: str, password: str, role: str,
                      plan_id: int | None = None, allow_duplicate_name: bool = False,
                      initial_status: str = "approved") -> dict:
    """Create a company and its first user, in whatever role they actually hold.

    Not assumed to be Supervisor just because they're the one setting
    the account up - a junior person can register the company on the boss's
    behalf and get only their own limited access. Whatever role is chosen,
    this account is approved immediately: there's nobody else at a brand-new
    company who could approve it. Paid-plan registrations instead begin in
    payment_pending and are activated only by a verified gateway payment.

    `plan_id` is the tier chosen on the landing page's pricing section before
    ever reaching this form - falls back to whichever plan is_default if
    omitted or not a real plan, rather than failing the whole registration
    over a bad/missing plan id.

    On an individual plan there is no company and no colleagues: the workspace
    is named after the person unless they gave it a name of their own, and
    they hold Supervisor whatever the caller asked for, because every other
    role depends on somebody else being there to approve the work.

    Registering a name a workspace already answers to raises
    `DuplicateCompanyError` rather than silently creating a second one - that
    is how a team ends up split across two workspaces with the same name on
    the door. It's a question, not a verdict: `allow_duplicate_name=True` is
    the caller saying the person confirmed it really is a different business.
    """
    company_name = company_name.strip()
    name = name.strip()
    email = email.strip().lower()

    plan = (get_plan(plan_id) if plan_id is not None else None) or get_default_plan()
    solo = plan["audience"] == "individual"

    if len(name) < 2:
        raise AuthError("Your name is too short.")
    if solo:
        company_name = company_name or name
        role = "finance_supervisor"
    if len(company_name) < 2:
        raise AuthError("Company name is too short.")
    if role not in ROLES:
        raise AuthError("Not a valid role.")
    if "@" not in email:
        raise AuthError("That doesn't look like an email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if initial_status not in ("approved", "payment_pending"):
        raise AuthError("Not a valid registration status.")
    if get_user_by_email(email):
        raise AuthError("An account with that email already exists.")
    if not allow_duplicate_name:
        existing = find_company_by_exact_name(company_name)
        if existing:
            raise DuplicateCompanyError(existing)

    salt = _new_salt()
    now = time.time()
    with _cursor() as conn:
        cur = conn.execute(
            "INSERT INTO companies (name, plan_id, created_at) VALUES (?, ?, ?)",
            (company_name, plan["id"], now),
        )
        company_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO users
               (company_id, name, email, password_hash, salt, role, status,
                onboarding_complete, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (company_id, name, email, _hash_password(password, salt), salt.hex(), role,
             initial_status, now),
        )
        user_id = cur.lastrowid

    return get_user(user_id) | {"company": {"id": company_id, "name": company_name}}


# ------------------------------------------------------- connected mailboxes
#
# One mailbox per user, whoever hosts it - see mailbox.py. Everything here is
# scoped by user_id and never by company: a supervisor can see that somebody
# on their team connected a mailbox (it's their plan paying for it), but no
# route hands them another person's credentials or mail.

OAUTH_STATE_TTL_SECONDS = 10 * 60


def new_oauth_state(user_id: int, provider: str) -> str:
    """Mint a single-use state token for an OAuth round trip.

    Carries the provider so the callback doesn't have to trust a query
    parameter to decide whose token endpoint to talk to.
    """
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _cursor() as conn:
        # Opportunistic sweep - these are worthless once expired and there is
        # no scheduled job on this host to tidy them up.
        conn.execute(
            "DELETE FROM oauth_states WHERE created_at < ?",
            (now - OAUTH_STATE_TTL_SECONDS,),
        )
        conn.execute(
            "INSERT INTO oauth_states (state, user_id, provider, created_at) "
            "VALUES (?, ?, ?, ?)",
            (state, user_id, provider, now),
        )
    return state


def consume_oauth_state(state: str) -> tuple[int, str] | None:
    """Spend a state token, returning (user_id, provider).

    Single use by construction: the row is deleted as it's read, so a
    replayed callback finds nothing. Returns None if the state is unknown,
    already spent, or older than the TTL.
    """
    if not state:
        return None
    with _cursor() as conn:
        row = conn.execute(
            "SELECT user_id, provider, created_at FROM oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    if time.time() - row["created_at"] > OAUTH_STATE_TTL_SECONDS:
        return None
    return row["user_id"], row["provider"]


def save_mailbox_connection(user_id: int, company_id: int, connection: dict,
                             credentials_enc: str) -> int:
    """Store (or replace) someone's mailbox connection.

    Takes credentials already encrypted - this layer never sees a key and
    never decides whether encryption happened, so there's no path where a
    plaintext credential reaches the table by accident.
    """
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO mailbox_connections
               (user_id, company_id, provider, account_email, account_name,
                imap_host, imap_port, credentials_enc, scopes, connected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, provider, account_email) DO UPDATE SET
                 account_name = excluded.account_name,
                 imap_host = excluded.imap_host,
                 imap_port = excluded.imap_port,
                 credentials_enc = excluded.credentials_enc,
                 scopes = excluded.scopes,
                 connected_at = excluded.connected_at""",
            (user_id, company_id, connection["provider"], connection["account_email"],
             connection["account_name"], connection.get("imap_host", ""),
             connection.get("imap_port", 0), credentials_enc,
             connection.get("scopes", ""), time.time()),
        )
        row = conn.execute("SELECT id FROM mailbox_connections WHERE user_id=? AND provider=? AND account_email=?",
                           (user_id, connection["provider"], connection["account_email"])).fetchone()
    return row["id"]


def list_mailbox_connections(user_id: int) -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute("SELECT * FROM mailbox_connections WHERE user_id=? ORDER BY connected_at", (user_id,)).fetchall()
    return [dict(row) for row in rows]


def get_mailbox_connection(user_id: int, connection_id: int | None = None) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM mailbox_connections WHERE user_id=? AND (? IS NULL OR id=?) ORDER BY id LIMIT 1",
            (user_id, connection_id, connection_id)
        ).fetchone()
    return dict(row) if row else None


def update_mailbox_credentials(user_id: int, credentials_enc: str, connection_id: int | None = None) -> None:
    """Write back re-encrypted credentials after a token refresh."""
    with _cursor() as conn:
        conn.execute(
            "UPDATE mailbox_connections SET credentials_enc=? WHERE user_id=? AND (? IS NULL OR id=?)",
            (credentials_enc, user_id, connection_id, connection_id),
        )


def delete_mailbox_connection(user_id: int, connection_id: int | None = None) -> None:
    """Forget a mailbox entirely - credentials included.

    This is Buinee's side only. For the OAuth providers the consent still
    exists at Microsoft/Google until the person removes it from their own
    account, which the UI tells them.
    """
    with _cursor() as conn:
        conn.execute("DELETE FROM mailbox_connections WHERE user_id=? AND (? IS NULL OR id=?)",
                     (user_id, connection_id, connection_id))


# ------------------------------------------------------------------ plans

def _plan_with_entitlements(row: sqlite3.Row) -> dict:
    plan = dict(row)
    plan["team_chat_enabled"] = plan["audience"] == "team"
    return plan

def list_plans() -> list[dict]:
    """Individual tiers first, then team tiers, each in its own sort_order.

    Grouping here rather than only in the UI keeps the two consistent: a
    database that predates the split has its solo plans sharing sort_order
    numbers with the team plans they were added alongside, so ordering on
    sort_order alone would interleave them.
    """
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM plans "
            "ORDER BY CASE audience WHEN 'individual' THEN 0 ELSE 1 END, sort_order"
        ).fetchall()
    return [_plan_with_entitlements(r) for r in rows]


def get_plan(plan_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return _plan_with_entitlements(row) if row else None


def get_default_plan() -> dict:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM plans WHERE is_default = 1 LIMIT 1").fetchone()
    return _plan_with_entitlements(row)


def create_plan(name: str, price: float, currency: str, user_limit: int,
                 chat_enabled: bool = False, chat_monthly_limit: int | None = None,
                 audience: str = "team", mailbox_limit: int = 1) -> dict:
    name = name.strip()
    currency = currency.strip().upper() or "GHS"
    if len(name) < 2:
        raise AuthError("Plan name is too short.")
    if audience not in PLAN_AUDIENCES:
        raise AuthError("A plan is sold either to an individual or to a team.")
    if audience == "individual" and user_limit != 1:
        raise AuthError("An individual plan covers exactly 1 person.")
    if user_limit < 1:
        raise AuthError("A plan needs to allow at least 1 user.")
    if mailbox_limit < 1:
        raise AuthError("A plan needs to allow at least one mailbox.")
    if price < 0:
        raise AuthError("Price can't be negative.")
    if chat_monthly_limit is not None and chat_monthly_limit < 0:
        raise AuthError("Chat message limit can't be negative.")
    with _cursor() as conn:
        next_sort = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM plans").fetchone()["n"]
        cur = conn.execute(
            """INSERT INTO plans (name, price, currency, user_limit, sort_order, is_default,
                                   chat_enabled, chat_monthly_limit, audience, mailbox_limit, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
            (name, price, currency, user_limit, next_sort,
             int(bool(chat_enabled)), chat_monthly_limit, audience, mailbox_limit, time.time()),
        )
        plan_id = cur.lastrowid
    return get_plan(plan_id)


def update_plan(plan_id: int, name: str | None = None, price: float | None = None,
                 currency: str | None = None, user_limit: int | None = None,
                 chat_enabled: bool | None = None, chat_monthly_limit: int | float | None = "unset",
                 mailbox_limit: int | None = None) -> dict:
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
    new_mailbox_limit = plan["mailbox_limit"] if mailbox_limit is None else mailbox_limit
    if new_mailbox_limit < 1:
        raise AuthError("A plan needs to allow at least one mailbox.")
    # Audience is fixed at creation. Flipping a team plan to individual would
    # strand every company already on it above the seat cap, and widening an
    # individual plan past one seat would quietly turn someone's personal
    # workspace into a joinable company - so the seat count is what's held.
    if plan["audience"] == "individual" and new_limit != 1:
        raise AuthError("An individual plan covers exactly 1 person.")
    new_chat_enabled = plan["chat_enabled"] if chat_enabled is None else int(bool(chat_enabled))
    # chat_monthly_limit needs three states (leave alone / set a number /
    # explicitly clear to unlimited), so "unset" - not None - is the
    # sentinel for "caller didn't pass this".
    if chat_monthly_limit == "unset":
        new_chat_limit = plan["chat_monthly_limit"]
    else:
        if chat_monthly_limit is not None and chat_monthly_limit < 0:
            raise AuthError("Chat message limit can't be negative.")
        new_chat_limit = chat_monthly_limit
    with _cursor() as conn:
        conn.execute(
            "UPDATE plans SET name = ?, price = ?, currency = ?, user_limit = ?, "
            "chat_enabled = ?, chat_monthly_limit = ?, mailbox_limit=? WHERE id = ?",
            (new_name, new_price, new_currency, new_limit, new_chat_enabled, new_chat_limit, new_mailbox_limit, plan_id),
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
    return _plan_with_entitlements(row) if row else get_default_plan()


def can_add_user(company_id: int) -> bool:
    plan = plan_for_company(company_id)
    return company_user_count(company_id) < plan["user_limit"]


def _at_limit_message(company_id: int, joining: bool) -> str:
    """Why nobody else can be added, said the way it's actually true.

    A team at its cap and a solo workspace are the same check but not the
    same situation: the first needs a bigger plan, the second needs a
    different kind of plan, and neither can do it themselves - moving a
    company between plans is Command Center-only.
    """
    plan = plan_for_company(company_id)
    if plan["audience"] == "individual":
        whose = "That workspace is" if joining else "Your workspace is"
        return (
            f"{whose} on the {plan['name']} plan, which covers just one "
            "person. It has to move onto a team plan before anyone else can "
            "be added - ask us to move it across and nothing already in here "
            "is touched."
        )
    seats = plan["user_limit"]
    whose = "This company is" if joining else "Your company is"
    return (
        f"{whose} on the {plan['name']} plan ({seats} "
        f"user{'' if seats == 1 else 's'}) and is already at that limit. "
        "The plan needs to be upgraded before anyone new can be added."
    )


def _current_year_month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def get_chat_usage(company_id: int, year_month: str | None = None) -> int:
    year_month = year_month or _current_year_month()
    with _cursor() as conn:
        row = conn.execute(
            "SELECT count FROM chat_usage WHERE company_id = ? AND year_month = ?",
            (company_id, year_month),
        ).fetchone()
    return row["count"] if row else 0


def increment_chat_usage(company_id: int) -> int:
    """Call once per successful chat reply, never per attempt - a failed or
    rejected call shouldn't count against what the company is paying for."""
    year_month = _current_year_month()
    with _cursor() as conn:
        conn.execute(
            "INSERT INTO chat_usage (company_id, year_month, count) VALUES (?, ?, 1) "
            "ON CONFLICT(company_id, year_month) DO UPDATE SET count = count + 1",
            (company_id, year_month),
        )
        row = conn.execute(
            "SELECT count FROM chat_usage WHERE company_id = ? AND year_month = ?",
            (company_id, year_month),
        ).fetchone()
    return row["count"]


def can_use_chat(company_id: int) -> dict:
    """Whether this company can send another chat message right now, under
    its plan's gating - checked before every /api/chat call, alongside
    whether any AI provider is configured on the server at all."""
    plan = plan_for_company(company_id)
    if not plan["chat_enabled"]:
        return {"allowed": False, "reason": "not_included", "plan": plan["name"]}
    limit = plan["chat_monthly_limit"]
    used = get_chat_usage(company_id)
    if limit is not None and used >= limit:
        return {"allowed": False, "reason": "quota_exceeded", "used": used, "limit": limit, "plan": plan["name"]}
    return {"allowed": True, "used": used, "limit": limit, "plan": plan["name"]}


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
            raise AuthError(_at_limit_message(company_id, joining=True))
    status = "approved" if role == "finance_supervisor" else "pending"

    salt = _new_salt()
    now = time.time()
    with _cursor() as conn:
        cur = conn.execute(
            """INSERT INTO users
               (company_id, name, email, password_hash, salt, role, status,
                onboarding_complete, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
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
            """SELECT u.id, u.name, u.email, u.role, u.status, u.created_at,
                      COALESCE(s.state_value, 0) AS last_active
               FROM users u LEFT JOIN user_notification_state s
                 ON s.user_id = u.id AND s.state_key = 'presence'
               WHERE u.company_id = ? AND u.status = 'approved' ORDER BY u.created_at""",
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
            raise AuthError(_at_limit_message(company_id, joining=False))
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
# is initially bootstrapped directly or at process startup. After that, an
# authenticated owner can add and manage the back-office team.

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


PLATFORM_ADMIN_ROLES = ("owner", "operations", "sales", "support", "billing")


def create_platform_admin(name: str, email: str, password: str, role: str = "owner") -> dict:
    """Create a Command Center identity after caller authorization."""
    name = name.strip()
    email = email.strip().lower()
    if len(name) < 2:
        raise AuthError("Name is too short.")
    if "@" not in email:
        raise AuthError("That doesn't look like an email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    role = role.strip().lower()
    if role not in PLATFORM_ADMIN_ROLES:
        raise AuthError("Choose a valid back-office role.")
    if _get_admin_by_email(email):
        raise AuthError("A platform admin with that email already exists.")

    salt = _new_salt()
    now = time.time()
    with _cursor() as conn:
        cur = conn.execute(
            """INSERT INTO platform_admins
               (name, email, password_hash, salt, created_at, role, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (name, email, _hash_password(password, salt), salt.hex(), now, role),
        )
        admin_id = cur.lastrowid
    return _get_admin(admin_id)


def authenticate_admin(email: str, password: str) -> dict:
    admin = _get_admin_by_email(email)
    if not admin:
        raise AuthError("No platform admin with that email.")
    if admin.get("status", "active") != "active":
        raise AuthError("This back-office account is inactive.")
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
    result = dict(admin) if admin else None
    return result if result and result.get("status", "active") == "active" else None


def list_platform_admins() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            "SELECT id, name, email, role, status, created_at FROM platform_admins ORDER BY name"
        ).fetchall()
    return [dict(row) for row in rows]


def record_admin_activity(admin: dict | None, action: str, entity_type: str,
                          entity_id: object = "", entity_label: str = "",
                          details: str = "") -> None:
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO admin_activity_log
               (admin_id, admin_name, admin_email, action, entity_type, entity_id,
                entity_label, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (admin.get("id") if admin else None, admin.get("name", "System") if admin else "System",
             admin.get("email", "") if admin else "", action.strip(), entity_type.strip(),
             str(entity_id or ""), entity_label.strip(), details.strip(), time.time()),
        )


def list_admin_activity(page: int = 1, per_page: int = 20, entity_type: str = "",
                        action: str = "") -> dict:
    page, per_page = max(1, page), min(100, max(1, per_page))
    where, params = [], []
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    if action:
        where.append("action = ?")
        params.append(action)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _cursor() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM admin_activity_log" + clause, params).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM admin_activity_log" + clause + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        entity_types = [r["entity_type"] for r in conn.execute(
            "SELECT DISTINCT entity_type FROM admin_activity_log ORDER BY entity_type"
        ).fetchall()]
        actions = [r["action"] for r in conn.execute(
            "SELECT DISTINCT action FROM admin_activity_log ORDER BY action"
        ).fetchall()]
    return {"rows": [dict(r) for r in rows], "total": total, "page": page,
            "per_page": per_page, "entity_types": entity_types, "actions": actions}


def record_application_error(source: str, message: str, details: str = "") -> None:
    with _cursor() as conn:
        conn.execute("INSERT INTO application_errors (source,severity,message,details,created_at) VALUES (?,'error',?,?,?)",
                     (source[:80], message[:1000], details[:5000], time.time()))


def list_application_errors(limit: int = 300, severity: str = "", query: str = "") -> list[dict]:
    where, params = [], []
    if severity:
        where.append("severity = ?"); params.append(severity)
    if query:
        where.append("(message LIKE ? OR details LIKE ? OR source LIKE ?)")
        params.extend([f"%{query}%"] * 3)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with _cursor() as conn:
        rows = conn.execute("SELECT * FROM application_errors" + clause + " ORDER BY created_at DESC LIMIT ?",
                            params + [min(1000, max(1, limit))]).fetchall()
    return [dict(r) for r in rows]


def clear_application_errors() -> None:
    with _cursor() as conn:
        conn.execute("DELETE FROM application_errors")


def list_admin_inbox() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            """SELECT i.id, i.company_id, c.name AS company_name, i.contact_id,
                      COALESCE(ct.name,'') AS contact_name, i.interaction_type AS source,
                      i.direction, i.subject, i.body, i.author_name, i.occurred_at,
                      COALESCE(s.state,'unread') AS state
               FROM crm_interactions i JOIN companies c ON c.id=i.company_id
               LEFT JOIN crm_contacts ct ON ct.id=i.contact_id
               LEFT JOIN admin_inbox_states s ON s.item_type='interaction' AND s.item_id=i.id
               WHERE i.interaction_type IN ('email','message','call','meeting')
               ORDER BY i.occurred_at DESC LIMIT 500"""
        ).fetchall()
    return [dict(r) for r in rows]


def update_admin_inbox_state(item_id: int, state: str) -> None:
    if state not in ("unread", "read", "flagged", "archived"):
        raise AuthError("Choose a valid inbox state.")
    with _cursor() as conn:
        if not conn.execute("SELECT id FROM crm_interactions WHERE id=?", (item_id,)).fetchone():
            raise AuthError("Inbox item not found.")
        conn.execute("""INSERT INTO admin_inbox_states(item_type,item_id,state,updated_at)
                        VALUES('interaction',?,?,?) ON CONFLICT(item_type,item_id)
                        DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at""",
                     (item_id, state, time.time()))


def list_admin_invoices() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute("""SELECT i.*,c.name company_name,COALESCE(SUM(x.quantity*x.unit_amount),0) total
            FROM admin_invoices i JOIN companies c ON c.id=i.company_id LEFT JOIN admin_invoice_items x ON x.invoice_id=i.id
            GROUP BY i.id ORDER BY i.created_at DESC""").fetchall()
    return [dict(r) for r in rows]


def save_admin_invoice(fields: dict) -> dict:
    company_id=int(fields.get("company_id")); company=get_company(company_id)
    if not company: raise AuthError("Choose a valid company.")
    items=[]
    for pos,x in enumerate(fields.get("items") or []):
        desc=str(x.get("description") or "").strip()[:200]
        try: qty=float(x.get("quantity") or 0); amount=float(x.get("unit_amount") or 0)
        except (TypeError,ValueError): continue
        if desc and qty>0 and amount>=0: items.append((desc,qty,amount,pos))
    if not items: raise AuthError("Add at least one valid line item.")
    now=time.time(); number=str(fields.get("invoice_number") or "").strip()[:40] or f"INV-{time.strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"
    customer=str(fields.get("customer_name") or company["name"]).strip()[:160]
    with _cursor() as conn:
        cur=conn.execute("""INSERT INTO admin_invoices(invoice_number,company_id,customer_name,customer_email,currency,issue_date,due_date,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",(number,company_id,customer,str(fields.get("customer_email") or "").strip()[:254],str(fields.get("currency") or "GHS")[:6].upper(),str(fields.get("issue_date") or time.strftime('%Y-%m-%d'))[:10],str(fields.get("due_date") or "")[:10],str(fields.get("notes") or "")[:2000],now,now))
        invoice_id=cur.lastrowid
        conn.executemany("INSERT INTO admin_invoice_items(invoice_id,description,quantity,unit_amount,sort_order) VALUES(?,?,?,?,?)",[(invoice_id,*x) for x in items])
    return next(x for x in list_admin_invoices() if x["id"]==invoice_id)


def update_admin_invoice_status(invoice_id: int, status: str) -> dict:
    if status not in ("draft","sent","paid","void"): raise AuthError("Choose a valid invoice status.")
    with _cursor() as conn:
        if not conn.execute("SELECT id FROM admin_invoices WHERE id=?",(invoice_id,)).fetchone(): raise AuthError("Invoice not found.")
        conn.execute("UPDATE admin_invoices SET status=?,updated_at=? WHERE id=?",(status,time.time(),invoice_id))
    return next(x for x in list_admin_invoices() if x["id"]==invoice_id)


def update_platform_admin(admin_id: int, role: str, status: str, actor_id: int) -> dict:
    admin = _get_admin(admin_id)
    if not admin:
        raise AuthError("Team member not found.")
    role, status = role.strip().lower(), status.strip().lower()
    if role not in PLATFORM_ADMIN_ROLES or status not in ("active", "inactive"):
        raise AuthError("Choose a valid role and status.")
    if admin_id == actor_id and status != "active":
        raise AuthError("You cannot deactivate your own account.")
    with _cursor() as conn:
        owners = conn.execute(
            "SELECT COUNT(*) AS n FROM platform_admins WHERE role = 'owner' AND status = 'active'"
        ).fetchone()["n"]
        if admin["role"] == "owner" and admin["status"] == "active" and owners <= 1 \
                and (role != "owner" or status != "active"):
            raise AuthError("The Command Center must keep at least one active owner.")
        conn.execute("UPDATE platform_admins SET role = ?, status = ? WHERE id = ?", (role, status, admin_id))
        if status != "active":
            conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))
    return _get_admin(admin_id)


def reset_platform_admin_password(admin_id: int, new_password: str) -> None:
    if not _get_admin(admin_id):
        raise AuthError("Team member not found.")
    if len(new_password) < 8:
        raise AuthError("Temporary password must be at least 8 characters.")
    salt = _new_salt()
    with _cursor() as conn:
        conn.execute("UPDATE platform_admins SET password_hash = ?, salt = ? WHERE id = ?",
                     (_hash_password(new_password, salt), salt.hex(), admin_id))
        conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))


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
        conn.execute("DELETE FROM payments WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM crm_subscriptions WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM crm_tasks WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM crm_interactions WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM crm_opportunities WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM crm_contacts WHERE company_id = ?", (company_id,))
        conn.execute("DELETE FROM crm_accounts WHERE company_id = ?", (company_id,))
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
                      p.user_limit AS plan_user_limit, p.audience AS plan_audience,
                      a.legal_name, a.industry, a.website, a.phone, a.location,
                      a.address, a.registration_number, a.tax_id,
                      COALESCE(a.lifecycle_status, 'customer') AS lifecycle_status,
                      a.relationship_owner, a.primary_contact_name,
                      a.primary_contact_email, a.summary, a.updated_at AS crm_updated_at,
                      COALESCE(NULLIF(a.lifecycle_changed_at, 0), c.created_at) AS lifecycle_changed_at,
                      COALESCE(s.subscription_status, 'active') AS subscription_status,
                      COALESCE(s.billing_cycle, 'monthly') AS billing_cycle,
                      COALESCE(s.renewal_date, '') AS renewal_date,
                      COALESCE(s.payment_status, 'not_connected') AS payment_status,
                      COALESCE(s.customer_reference, '') AS customer_reference,
                      COALESCE(s.notes, '') AS subscription_notes,
                      s.updated_at AS subscription_updated_at
               FROM companies c JOIN plans p ON c.plan_id = p.id
               LEFT JOIN crm_accounts a ON a.company_id = c.id
               LEFT JOIN crm_subscriptions s ON s.company_id = c.id
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
            contacts = conn.execute(
                """SELECT id, name, job_title, email, phone, is_primary, notes,
                          created_at, updated_at FROM crm_contacts
                   WHERE company_id = ? ORDER BY is_primary DESC, name""",
                (c["id"],),
            ).fetchall()
            interactions = conn.execute(
                """SELECT i.id, i.company_id, i.contact_id, i.interaction_type,
                          i.direction, i.subject, i.body, i.occurred_at,
                          i.author_name, i.created_at, i.updated_at,
                          ct.name AS contact_name
                   FROM crm_interactions i
                   LEFT JOIN crm_contacts ct ON ct.id = i.contact_id
                   WHERE i.company_id = ?
                   ORDER BY i.occurred_at DESC, i.id DESC LIMIT 50""",
                (c["id"],),
            ).fetchall()
            tasks = conn.execute(
                """SELECT t.*, u.name AS assigned_user_name, u.email AS assigned_user_email
                   FROM crm_tasks t LEFT JOIN users u ON u.id = t.assigned_user_id
                   WHERE t.company_id = ?
                   ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,
                            CASE WHEN t.due_date = '' THEN 1 ELSE 0 END,
                            t.due_date, t.id DESC LIMIT 100""",
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
                "contacts": [dict(contact) for contact in contacts],
                "interactions": [dict(interaction) for interaction in interactions],
                "tasks": [dict(task) for task in tasks],
                "subscription": {
                    "subscription_status": c["subscription_status"],
                    "billing_cycle": c["billing_cycle"],
                    "renewal_date": c["renewal_date"],
                    "payment_status": c["payment_status"],
                    "customer_reference": c["customer_reference"],
                    "notes": c["subscription_notes"],
                    "updated_at": c["subscription_updated_at"],
                },
                "crm": {
                    "legal_name": c["legal_name"] or "",
                    "industry": c["industry"] or "",
                    "website": c["website"] or "",
                    "phone": c["phone"] or "",
                    "location": c["location"] or "",
                    "address": c["address"] or "",
                    "registration_number": c["registration_number"] or "",
                    "tax_id": c["tax_id"] or "",
                    "lifecycle_status": c["lifecycle_status"],
                    "relationship_owner": c["relationship_owner"] or "",
                    "primary_contact_name": c["primary_contact_name"] or "",
                    "primary_contact_email": c["primary_contact_email"] or "",
                    "summary": c["summary"] or "",
                    "updated_at": c["crm_updated_at"],
                    "lifecycle_changed_at": c["lifecycle_changed_at"],
                },
                "plan": {
                    "id": c["plan_id"],
                    "name": c["plan_name"],
                    "user_limit": c["plan_user_limit"],
                    "audience": c["plan_audience"],
                },
                # Individual plans are single-user workspaces. Flag both a
                # pending join and legacy/imported data that already has more
                # than one approved member so the Command Center can correct it.
                "needs_team_plan": c["plan_audience"] == "individual"
                                   and (len(team) > 1 or len(pending) > 0),
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


def get_user_instructions(user_id: int) -> str:
    with _cursor() as conn:
        row = conn.execute("SELECT briefing FROM user_instructions WHERE user_id = ?", (user_id,)).fetchone()
    return row["briefing"] if row else ""


def set_user_instructions(user_id: int, briefing: str) -> None:
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO user_instructions (user_id, briefing, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET briefing = excluded.briefing,
               updated_at = excluded.updated_at""",
            (user_id, briefing[:12000], time.time()),
        )


def list_reference_documents(user_id: int, include_content: bool = False) -> list[dict]:
    columns = "*" if include_content else "id, user_id, name, kind, media_type, size_bytes, created_at"
    with _cursor() as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM reference_documents WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_reference_document(user_id: int, name: str, kind: str, media_type: str,
                           text_content: str, data_base64: str, size_bytes: int) -> dict:
    with _cursor() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM reference_documents WHERE user_id = ?", (user_id,)).fetchone()["n"]
        total = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM reference_documents WHERE user_id = ?", (user_id,)).fetchone()["n"]
        if count >= 10:
            raise AuthError("Your reference library is limited to 10 documents.")
        if total + size_bytes > 25 * 1024 * 1024:
            raise AuthError("Your reference library is limited to 25 MB.")
        cur = conn.execute(
            """INSERT INTO reference_documents
               (user_id, name, kind, media_type, text_content, data_base64, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name[:160], kind, media_type, text_content, data_base64, size_bytes, time.time()),
        )
        doc_id = cur.lastrowid
    return next(row for row in list_reference_documents(user_id) if row["id"] == doc_id)


def delete_reference_document(user_id: int, document_id: int) -> bool:
    with _cursor() as conn:
        cur = conn.execute("DELETE FROM reference_documents WHERE id = ? AND user_id = ?", (document_id, user_id))
        return cur.rowcount > 0


def create_team_message(company_id: int, sender_id: int, body: str, files: list[dict],
                        recipient_id: int | None = None) -> dict:
    now = time.time()
    with _cursor() as conn:
        if recipient_id is not None:
            recipient = conn.execute(
                "SELECT id FROM users WHERE id = ? AND company_id = ? AND status = 'approved'",
                (recipient_id, company_id),
            ).fetchone()
            if not recipient or recipient_id == sender_id:
                raise AuthError("Choose another approved team member.")
        cur = conn.execute(
            "INSERT INTO team_messages (company_id, sender_id, recipient_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (company_id, sender_id, recipient_id, body[:4000], now),
        )
        message_id = cur.lastrowid
        for file in files[:3]:
            conn.execute(
                """INSERT INTO team_message_files
                   (message_id, company_id, uploader_id, name, kind, media_type,
                    text_content, data_base64, size_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, company_id, sender_id, file["name"], file["kind"],
                 file["media_type"], file["text_content"], file["data_base64"],
                 file["size_bytes"], now),
            )
    return get_team_message(company_id, message_id, sender_id)


def get_team_message(company_id: int, message_id: int, viewer_id: int) -> dict | None:
    with _cursor() as conn:
        row = conn.execute(
            """SELECT m.*, u.name AS sender_name, u.role AS sender_role
               FROM team_messages m JOIN users u ON u.id = m.sender_id
               WHERE m.company_id = ? AND m.id = ?
                 AND (m.recipient_id IS NULL OR m.sender_id = ? OR m.recipient_id = ?)""",
            (company_id, message_id, viewer_id, viewer_id),
        ).fetchone()
        if not row:
            return None
        files = conn.execute(
            """SELECT id, name, kind, media_type, size_bytes, created_at
               FROM team_message_files WHERE message_id = ? ORDER BY id""",
            (message_id,),
        ).fetchall()
    item = dict(row)
    item["files"] = [dict(file) for file in files]
    return item


def list_team_messages(company_id: int, viewer_id: int, recipient_id: int | None = None,
                       after_id: int = 0, limit: int = 100) -> list[dict]:
    with _cursor() as conn:
        conversation = "group" if recipient_id is None else str(recipient_id)
        clear = conn.execute(
            "SELECT cleared_through_id FROM team_conversation_clears WHERE user_id = ? AND conversation = ?",
            (viewer_id, conversation),
        ).fetchone()
        visible_after = max(after_id, clear["cleared_through_id"] if clear else 0)
        if recipient_id is None:
            rows = conn.execute(
                """SELECT id FROM team_messages WHERE company_id = ?
                   AND recipient_id IS NULL AND id > ? ORDER BY id DESC LIMIT ?""",
                (company_id, visible_after, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id FROM team_messages WHERE company_id = ? AND id > ?
                   AND ((sender_id = ? AND recipient_id = ?)
                     OR (sender_id = ? AND recipient_id = ?))
                   ORDER BY id DESC LIMIT ?""",
                (company_id, visible_after, viewer_id, recipient_id,
                 recipient_id, viewer_id, limit),
            ).fetchall()
    return [item for item in (get_team_message(company_id, row["id"], viewer_id) for row in reversed(rows)) if item]


def clear_team_conversation(company_id: int, viewer_id: int,
                            recipient_id: int | None = None) -> None:
    conversation = "group" if recipient_id is None else str(recipient_id)
    with _cursor() as conn:
        if recipient_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS last_id FROM team_messages WHERE company_id = ? AND recipient_id IS NULL",
                (company_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COALESCE(MAX(id), 0) AS last_id FROM team_messages
                   WHERE company_id = ? AND ((sender_id = ? AND recipient_id = ?)
                     OR (sender_id = ? AND recipient_id = ?))""",
                (company_id, viewer_id, recipient_id, recipient_id, viewer_id),
            ).fetchone()
        conn.execute(
            """INSERT INTO team_conversation_clears
                   (user_id, conversation, cleared_through_id, cleared_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, conversation) DO UPDATE SET
                   cleared_through_id = excluded.cleared_through_id,
                   cleared_at = excluded.cleared_at""",
            (viewer_id, conversation, row["last_id"], time.time()),
        )


def notification_summary(user: dict) -> dict:
    user_id, company_id, role = user["id"], user["company_id"], user["role"]
    with _cursor() as conn:
        seen_rows = conn.execute(
            """SELECT state_key, state_value FROM user_notification_state
               WHERE user_id = ? AND state_key LIKE 'team_seen:%'""",
            (user_id,),
        ).fetchall()
        seen = {row["state_key"].split(":", 1)[1]: row["state_value"] for row in seen_rows}
        legacy_seen_row = conn.execute(
            "SELECT state_value FROM user_notification_state WHERE user_id = ? AND state_key = 'team_seen'",
            (user_id,),
        ).fetchone()
        legacy_seen = legacy_seen_row["state_value"] if legacy_seen_row else 0
        messages = conn.execute(
            """SELECT id, sender_id, recipient_id FROM team_messages
               WHERE company_id = ? AND sender_id != ?
                 AND (recipient_id IS NULL OR recipient_id = ?)""",
            (company_id, user_id, user_id),
        ).fetchall()
        unread_conversations: dict[str, int] = {}
        for message in messages:
            conversation = "group" if message["recipient_id"] is None else str(message["sender_id"])
            if message["id"] > seen.get(conversation, legacy_seen):
                unread_conversations[conversation] = unread_conversations.get(conversation, 0) + 1
        unread_team = sum(unread_conversations.values())
        pending = 0
        if role == "finance_supervisor":
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE company_id = ? AND status = 'pending'",
                (company_id,),
            ).fetchone()["n"]
        awaiting = 0
        if role in ("senior_accountant", "finance_supervisor"):
            awaiting = conn.execute(
                """SELECT COUNT(*) AS n FROM vouchers
                   WHERE company_id = ? AND status = 'submitted' AND created_by != ?""",
                (company_id, user_id),
            ).fetchone()["n"]
        rejected = conn.execute(
            """SELECT COUNT(*) AS n FROM vouchers
               WHERE company_id = ? AND created_by = ? AND status = 'rejected'""",
            (company_id, user_id),
        ).fetchone()["n"]
        task_seen_row = conn.execute(
            "SELECT state_value FROM user_notification_state WHERE user_id=? AND state_key='crm_tasks_seen_at'",
            (user_id,),
        ).fetchone()
        task_seen_at = task_seen_row["state_value"] if task_seen_row else 0
        task_scope = "t.company_id = ?" if role == "finance_supervisor" else "t.company_id = ? AND t.assigned_user_id = ?"
        task_params = (company_id,) if role == "finance_supervisor" else (company_id, user_id)
        task_rows = conn.execute(
            f"""SELECT t.due_date, t.updated_at FROM crm_tasks t
                WHERE {task_scope} AND t.status='open'""",
            task_params,
        ).fetchall()
        today = time.strftime("%Y-%m-%d")
        task_open = len(task_rows)
        task_overdue = sum(1 for row in task_rows if row["due_date"] and row["due_date"] < today)
        task_due_today = sum(1 for row in task_rows if row["due_date"] == today)
        task_new = sum(1 for row in task_rows if row["updated_at"] > task_seen_at)
        task_attention = sum(
            1 for row in task_rows
            if (row["due_date"] and row["due_date"] <= today) or row["updated_at"] > task_seen_at
        )
    return {"team_messages": unread_team, "team_conversations": unread_conversations,
            "pending_users": pending,
            "awaiting_approval": awaiting, "rejected_vouchers": rejected,
            "follow_up_open": task_open, "follow_up_overdue": task_overdue,
            "follow_up_due_today": task_due_today, "follow_up_new": task_new,
            "follow_up_attention": task_attention}


def mark_team_messages_seen(user: dict, recipient_id: int | None = None) -> None:
    conversation = "group" if recipient_id is None else str(recipient_id)
    with _cursor() as conn:
        if recipient_id is None:
            row = conn.execute(
                """SELECT COALESCE(MAX(id), 0) AS last_id FROM team_messages
                   WHERE company_id = ? AND sender_id != ? AND recipient_id IS NULL""",
                (user["company_id"], user["id"]),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COALESCE(MAX(id), 0) AS last_id FROM team_messages
                   WHERE company_id = ? AND sender_id = ? AND recipient_id = ?""",
                (user["company_id"], recipient_id, user["id"]),
            ).fetchone()
        conn.execute(
            """INSERT INTO user_notification_state (user_id, state_key, state_value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, state_key) DO UPDATE SET
                 state_value = excluded.state_value, updated_at = excluded.updated_at""",
            (user["id"], f"team_seen:{conversation}", row["last_id"], time.time()),
        )


def touch_presence(user_id: int) -> None:
    now = int(time.time())
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO user_notification_state (user_id, state_key, state_value, updated_at)
               VALUES (?, 'presence', ?, ?)
               ON CONFLICT(user_id, state_key) DO UPDATE SET
                 state_value = excluded.state_value, updated_at = excluded.updated_at""",
            (user_id, now, time.time()),
        )


def clear_presence(user_id: int) -> None:
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO user_notification_state (user_id, state_key, state_value, updated_at)
               VALUES (?, 'presence', 0, ?)
               ON CONFLICT(user_id, state_key) DO UPDATE SET
                 state_value = 0, updated_at = excluded.updated_at""",
            (user_id, time.time()),
        )


def get_team_file(company_id: int, viewer_id: int, file_id: int, include_content: bool = True) -> dict | None:
    columns = "f.*" if include_content else "f.id, f.message_id, f.company_id, f.uploader_id, f.name, f.kind, f.media_type, f.size_bytes, f.created_at"
    with _cursor() as conn:
        row = conn.execute(
            f"""SELECT {columns} FROM team_message_files f
                JOIN team_messages m ON m.id = f.message_id
                WHERE f.company_id = ? AND f.id = ?
                  AND (m.recipient_id IS NULL OR m.sender_id = ? OR m.recipient_id = ?)
                  AND m.id > COALESCE((
                    SELECT c.cleared_through_id FROM team_conversation_clears c
                    WHERE c.user_id = ? AND c.conversation = CASE
                      WHEN m.recipient_id IS NULL THEN 'group'
                      WHEN m.sender_id = ? THEN CAST(m.recipient_id AS TEXT)
                      ELSE CAST(m.sender_id AS TEXT)
                    END
                  ), 0)""",
            (company_id, file_id, viewer_id, viewer_id, viewer_id, viewer_id),
        ).fetchone()
    return dict(row) if row else None


def record_terms_acceptance(user_id: int, version: str) -> None:
    with _cursor() as conn:
        conn.execute("UPDATE users SET terms_accepted_at=?, terms_version=? WHERE id=?",
                     (time.time(), version[:40], user_id))


def complete_user_onboarding(user_id: int) -> None:
    with _cursor() as conn:
        conn.execute("UPDATE users SET onboarding_complete=1 WHERE id=?", (user_id,))


def automation_states(user_id: int) -> dict[str, dict]:
    """Return arbitrary recipe states with their latest run, keyed by ID."""
    with _cursor() as conn:
        settings = conn.execute(
            "SELECT recipe_key, enabled, next_run_at, updated_at FROM automation_settings WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        runs = conn.execute(
            """SELECT r.* FROM automation_runs r
               JOIN (SELECT recipe_key, MAX(id) AS id FROM automation_runs
                     WHERE user_id = ? GROUP BY recipe_key) latest ON latest.id = r.id""",
            (user_id,),
        ).fetchall()
    out = {r["recipe_key"]: dict(r) for r in settings}
    for row in runs:
        item = out.setdefault(row["recipe_key"], {"recipe_key": row["recipe_key"], "enabled": 0, "next_run_at": None})
        run = dict(row)
        try:
            run["result"] = json.loads(run.pop("result_json") or "{}")
        except json.JSONDecodeError:
            run["result"] = {}
        item["latest_run"] = run
    return out


CRM_LIFECYCLE_STATUSES = {"lead", "trial", "customer", "at_risk", "paused", "churned"}


def get_company_profile(company_id: int) -> dict:
    with _cursor() as conn:
        row = conn.execute(
            """SELECT legal_name, industry, website, phone, location, address,
                      registration_number, tax_id,
                      primary_contact_name, primary_contact_email, updated_at
               FROM crm_accounts WHERE company_id = ?""",
            (company_id,),
        ).fetchone()
    return dict(row) if row else {
        "legal_name": "", "industry": "", "website": "", "phone": "",
        "location": "", "address": "", "registration_number": "", "tax_id": "",
        "primary_contact_name": "",
        "primary_contact_email": "", "updated_at": None,
    }


def update_company_profile(company_id: int, fields: dict) -> dict:
    values = {
        "legal_name": str(fields.get("legal_name") or "").strip()[:160],
        "industry": str(fields.get("industry") or "").strip()[:100],
        "website": str(fields.get("website") or "").strip()[:300],
        "phone": str(fields.get("phone") or "").strip()[:80],
        "location": str(fields.get("location") or "").strip()[:160],
        "address": str(fields.get("address") or "").strip()[:300],
        "registration_number": str(fields.get("registration_number") or "").strip()[:100],
        "tax_id": str(fields.get("tax_id") or "").strip()[:100],
        "primary_contact_name": str(fields.get("primary_contact_name") or "").strip()[:120],
        "primary_contact_email": str(fields.get("primary_contact_email") or "").strip().lower()[:254],
    }
    email = values["primary_contact_email"]
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise AuthError("Enter a valid primary contact email.")
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO crm_accounts
                   (company_id, legal_name, industry, website, phone, location,
                    address, registration_number, tax_id,
                    primary_contact_name, primary_contact_email, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                 legal_name=excluded.legal_name, industry=excluded.industry,
                 website=excluded.website, phone=excluded.phone,
                 location=excluded.location, address=excluded.address,
                 registration_number=excluded.registration_number,
                 tax_id=excluded.tax_id,
                 primary_contact_name=excluded.primary_contact_name,
                 primary_contact_email=excluded.primary_contact_email,
                 updated_at=excluded.updated_at""",
            (company_id, values["legal_name"], values["industry"], values["website"],
             values["phone"], values["location"], values["address"],
             values["registration_number"], values["tax_id"], values["primary_contact_name"],
             values["primary_contact_email"], now),
        )
    values["updated_at"] = now
    return values


def save_crm_contact(company_id: int, fields: dict) -> dict:
    if not get_company(company_id):
        raise AuthError("No such company.")
    try:
        contact_id = int(fields.get("contact_id") or 0)
    except (TypeError, ValueError):
        raise AuthError("Bad contact.")
    values = {
        "name": str(fields.get("name") or "").strip()[:120],
        "job_title": str(fields.get("job_title") or "").strip()[:120],
        "email": str(fields.get("email") or "").strip().lower()[:254],
        "phone": str(fields.get("phone") or "").strip()[:80],
        "notes": str(fields.get("notes") or "").strip()[:1000],
        "is_primary": int(bool(fields.get("is_primary"))),
    }
    if not values["name"]:
        raise AuthError("Contact name is required.")
    if values["email"] and ("@" not in values["email"] or "." not in values["email"].split("@")[-1]):
        raise AuthError("Enter a valid contact email.")
    now = time.time()
    with _cursor() as conn:
        if contact_id:
            existing = conn.execute(
                "SELECT id FROM crm_contacts WHERE id = ? AND company_id = ?",
                (contact_id, company_id),
            ).fetchone()
            if not existing:
                raise AuthError("Contact not found.")
        if values["is_primary"]:
            conn.execute("UPDATE crm_contacts SET is_primary = 0 WHERE company_id = ?", (company_id,))
        if contact_id:
            conn.execute(
                """UPDATE crm_contacts SET name=?, job_title=?, email=?, phone=?,
                     is_primary=?, notes=?, updated_at=? WHERE id=? AND company_id=?""",
                (values["name"], values["job_title"], values["email"], values["phone"],
                 values["is_primary"], values["notes"], now, contact_id, company_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO crm_contacts
                     (company_id, name, job_title, email, phone, is_primary, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (company_id, values["name"], values["job_title"], values["email"],
                 values["phone"], values["is_primary"], values["notes"], now, now),
            )
            contact_id = cur.lastrowid
        if values["is_primary"]:
            conn.execute(
                """INSERT INTO crm_accounts
                     (company_id, primary_contact_name, primary_contact_email, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(company_id) DO UPDATE SET
                     primary_contact_name=excluded.primary_contact_name,
                     primary_contact_email=excluded.primary_contact_email,
                     updated_at=excluded.updated_at""",
                (company_id, values["name"], values["email"], now),
            )
        row = conn.execute("SELECT * FROM crm_contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(row)


def delete_crm_contact(company_id: int, contact_id: int) -> bool:
    with _cursor() as conn:
        row = conn.execute(
            "SELECT is_primary FROM crm_contacts WHERE id = ? AND company_id = ?",
            (contact_id, company_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE crm_interactions SET contact_id = NULL WHERE company_id = ? AND contact_id = ?",
            (company_id, contact_id),
        )
        conn.execute("DELETE FROM crm_contacts WHERE id = ? AND company_id = ?", (contact_id, company_id))
        if row["is_primary"]:
            conn.execute(
                """UPDATE crm_accounts SET primary_contact_name='',
                     primary_contact_email='', updated_at=? WHERE company_id=?""",
                (time.time(), company_id),
            )
    return True


CRM_INTERACTION_TYPES = {"note", "call", "email", "meeting", "message"}
CRM_INTERACTION_DIRECTIONS = {"internal", "inbound", "outbound"}


def save_crm_interaction(company_id: int, fields: dict, author_name: str) -> dict:
    if not get_company(company_id):
        raise AuthError("No such company.")
    try:
        interaction_id = int(fields.get("interaction_id") or 0)
        contact_id = int(fields.get("contact_id") or 0) or None
        occurred_at = float(fields.get("occurred_at") or time.time())
    except (TypeError, ValueError):
        raise AuthError("Bad interaction.")
    interaction_type = str(fields.get("interaction_type") or "note").strip().lower()
    direction = str(fields.get("direction") or "internal").strip().lower()
    subject = str(fields.get("subject") or "").strip()[:160]
    body = str(fields.get("body") or "").strip()[:8000]
    if interaction_type not in CRM_INTERACTION_TYPES:
        raise AuthError("Choose a valid interaction type.")
    if direction not in CRM_INTERACTION_DIRECTIONS:
        raise AuthError("Choose a valid direction.")
    if not body:
        raise AuthError("Add details for this interaction.")
    if occurred_at != occurred_at or occurred_at <= 0 or occurred_at > time.time() + 86400:
        raise AuthError("Choose a valid interaction date.")
    now = time.time()
    with _cursor() as conn:
        if contact_id:
            contact = conn.execute(
                "SELECT id FROM crm_contacts WHERE id = ? AND company_id = ?",
                (contact_id, company_id),
            ).fetchone()
            if not contact:
                raise AuthError("Contact not found for this company.")
        if interaction_id:
            existing = conn.execute(
                "SELECT id FROM crm_interactions WHERE id = ? AND company_id = ?",
                (interaction_id, company_id),
            ).fetchone()
            if not existing:
                raise AuthError("Interaction not found.")
            conn.execute(
                """UPDATE crm_interactions SET contact_id=?, interaction_type=?,
                     direction=?, subject=?, body=?, occurred_at=?, updated_at=?
                   WHERE id=? AND company_id=?""",
                (contact_id, interaction_type, direction, subject, body,
                 occurred_at, now, interaction_id, company_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO crm_interactions
                     (company_id, contact_id, interaction_type, direction, subject,
                      body, occurred_at, author_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (company_id, contact_id, interaction_type, direction, subject, body,
                 occurred_at, str(author_name or "Command Center")[:120], now, now),
            )
            interaction_id = cur.lastrowid
        row = conn.execute(
            """SELECT i.*, ct.name AS contact_name FROM crm_interactions i
               LEFT JOIN crm_contacts ct ON ct.id = i.contact_id WHERE i.id = ?""",
            (interaction_id,),
        ).fetchone()
    return dict(row)


def delete_crm_interaction(company_id: int, interaction_id: int) -> bool:
    with _cursor() as conn:
        cur = conn.execute(
            "DELETE FROM crm_interactions WHERE id = ? AND company_id = ?",
            (interaction_id, company_id),
        )
    return bool(cur.rowcount)


CRM_TASK_PRIORITIES = {"low", "normal", "high", "urgent"}
CRM_TASK_STATUSES = {"open", "completed"}


def save_crm_task(company_id: int, fields: dict, admin_name: str) -> dict:
    if not get_company(company_id):
        raise AuthError("No such company.")
    try:
        task_id = int(fields.get("task_id") or 0)
        assigned_user_id = int(fields.get("assigned_user_id") or 0) or None
    except (TypeError, ValueError):
        raise AuthError("Bad follow-up task.")
    title = str(fields.get("title") or "").strip()[:180]
    details = str(fields.get("details") or "").strip()[:4000]
    owner = str(fields.get("owner") or "").strip()[:120]
    due_date = str(fields.get("due_date") or "").strip()[:10]
    priority = str(fields.get("priority") or "normal").strip().lower()
    status = str(fields.get("status") or "open").strip().lower()
    if not title:
        raise AuthError("Task title is required.")
    if due_date:
        try:
            time.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            raise AuthError("Choose a valid due date.")
    if priority not in CRM_TASK_PRIORITIES or status not in CRM_TASK_STATUSES:
        raise AuthError("Choose a valid priority and status.")
    now = time.time()
    with _cursor() as conn:
        assigned_user = None
        if assigned_user_id:
            assigned_user = conn.execute(
                """SELECT id, name FROM users WHERE id=? AND company_id=?
                   AND status='approved'""",
                (assigned_user_id, company_id),
            ).fetchone()
            if not assigned_user:
                raise AuthError("Choose an approved employee from this company.")
            owner = assigned_user["name"]
        else:
            owner = ""
        if task_id:
            existing = conn.execute(
                "SELECT status, completed_at FROM crm_tasks WHERE id=? AND company_id=?",
                (task_id, company_id),
            ).fetchone()
            if not existing:
                raise AuthError("Follow-up task not found.")
            completed_at = now if status == "completed" and existing["status"] != "completed" else existing["completed_at"]
            if status == "open":
                completed_at = None
            conn.execute(
                """UPDATE crm_tasks SET assigned_user_id=?, title=?, details=?, owner=?, due_date=?,
                     priority=?, status=?, completed_at=?, updated_at=?
                   WHERE id=? AND company_id=?""",
                (assigned_user_id, title, details, owner, due_date, priority, status, completed_at,
                 now, task_id, company_id),
            )
        else:
            completed_at = now if status == "completed" else None
            cur = conn.execute(
                """INSERT INTO crm_tasks
                     (company_id, assigned_user_id, title, details, owner, due_date, priority,
                      status, completed_at, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (company_id, assigned_user_id, title, details, owner, due_date, priority, status,
                 completed_at, str(admin_name or "Command Center")[:120], now, now),
            )
            task_id = cur.lastrowid
        row = conn.execute(
            """SELECT t.*, u.name AS assigned_user_name, u.email AS assigned_user_email
               FROM crm_tasks t LEFT JOIN users u ON u.id=t.assigned_user_id WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    return dict(row)


def delete_crm_task(company_id: int, task_id: int) -> bool:
    with _cursor() as conn:
        cur = conn.execute(
            "DELETE FROM crm_tasks WHERE id=? AND company_id=?", (task_id, company_id)
        )
    return bool(cur.rowcount)


CRM_SUBSCRIPTION_STATUSES = {"trial", "active", "paused", "cancel_at_period_end", "cancelled"}
CRM_BILLING_CYCLES = {"monthly", "annual", "custom"}
CRM_PAYMENT_STATUSES = {"not_connected", "current", "due", "overdue", "not_applicable"}


def save_crm_subscription(company_id: int, fields: dict) -> dict:
    if not get_company(company_id):
        raise AuthError("No such company.")
    subscription_status = str(fields.get("subscription_status") or "active").strip().lower()
    billing_cycle = str(fields.get("billing_cycle") or "monthly").strip().lower()
    payment_status = str(fields.get("payment_status") or "not_connected").strip().lower()
    renewal_date = str(fields.get("renewal_date") or "").strip()[:10]
    customer_reference = str(fields.get("customer_reference") or "").strip()[:120]
    notes = str(fields.get("notes") or "").strip()[:3000]
    if subscription_status not in CRM_SUBSCRIPTION_STATUSES:
        raise AuthError("Choose a valid subscription status.")
    if billing_cycle not in CRM_BILLING_CYCLES:
        raise AuthError("Choose a valid billing cycle.")
    if payment_status not in CRM_PAYMENT_STATUSES:
        raise AuthError("Choose a valid payment status.")
    if renewal_date:
        try:
            time.strptime(renewal_date, "%Y-%m-%d")
        except ValueError:
            raise AuthError("Choose a valid renewal date.")
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO crm_subscriptions
                 (company_id, subscription_status, billing_cycle, renewal_date,
                  payment_status, customer_reference, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                 subscription_status=excluded.subscription_status,
                 billing_cycle=excluded.billing_cycle,
                 renewal_date=excluded.renewal_date,
                 payment_status=excluded.payment_status,
                 customer_reference=excluded.customer_reference,
                 notes=excluded.notes, updated_at=excluded.updated_at""",
            (company_id, subscription_status, billing_cycle, renewal_date,
             payment_status, customer_reference, notes, now),
        )
        row = conn.execute("SELECT * FROM crm_subscriptions WHERE company_id=?", (company_id,)).fetchone()
    return dict(row)


def create_payment_intent(company_id: int, user_id: int, plan_id: int, amount_subunit: int,
                          currency: str, email: str, reference: str) -> dict:
    now = time.time()
    with _cursor() as conn:
        conn.execute("""INSERT INTO payments(reference,company_id,user_id,plan_id,amount_subunit,
                     currency,customer_email,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                     (reference, company_id, user_id, plan_id, amount_subunit, currency, email, now, now))
        row = conn.execute("SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
    return dict(row)


def record_paystack_payment(data: dict) -> dict | None:
    reference = str(data.get("reference") or "")
    authorization = data.get("authorization") or {}
    customer = data.get("customer") or {}
    with _cursor() as conn:
        expected = conn.execute("SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
        if not expected:
            return None
        amount = int(data.get("amount") or 0)
        currency = str(data.get("currency") or "").upper()
        verified = str(data.get("status") or "") == "success" and amount == expected["amount_subunit"] and currency == expected["currency"]
        status = "success" if verified else "failed"
        conn.execute("""UPDATE payments SET status=?,provider_id=?,customer_email=?,channel=?,
                     card_type=?,card_brand=?,card_last4=?,card_bank=?,card_exp_month=?,card_exp_year=?,
                     gateway_response=?,paid_at=?,updated_at=? WHERE reference=?""",
                     (status, str(data.get("id") or ""), str(customer.get("email") or expected["customer_email"]),
                      str(data.get("channel") or ""), str(authorization.get("card_type") or ""),
                      str(authorization.get("brand") or ""), str(authorization.get("last4") or "")[-4:],
                      str(authorization.get("bank") or ""), str(authorization.get("exp_month") or ""),
                      str(authorization.get("exp_year") or ""), str(data.get("gateway_response") or "")[:300],
                      str(data.get("paid_at") or ""), time.time(), reference))
        if verified:
            # A successful, amount-verified payment activates the exact plan
            # stored on the server-side payment intent. Client metadata is not
            # trusted for this change.
            conn.execute(
                "UPDATE companies SET plan_id=? WHERE id=?",
                (expected["plan_id"], expected["company_id"]),
            )
            conn.execute("""INSERT INTO crm_subscriptions(company_id,payment_status,updated_at)
                         VALUES(?, 'current', ?) ON CONFLICT(company_id) DO UPDATE SET
                         payment_status='current',updated_at=excluded.updated_at""",
                         (expected["company_id"], time.time()))
            conn.execute(
                "UPDATE users SET status='approved' WHERE id=? AND status='payment_pending'",
                (expected["user_id"],),
            )
        row = conn.execute("SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
    return dict(row)


def list_payments(company_id: int | None = None, limit: int = 200) -> list[dict]:
    with _cursor() as conn:
        where, params = ("WHERE py.company_id=?", (company_id, limit)) if company_id else ("", (limit,))
        rows = conn.execute(f"""SELECT py.*, c.name AS company_name, p.name AS plan_name, u.name AS user_name
                    FROM payments py JOIN companies c ON c.id=py.company_id JOIN plans p ON p.id=py.plan_id
                    JOIN users u ON u.id=py.user_id {where} ORDER BY py.created_at DESC LIMIT ?""", params).fetchall()
    return [dict(row) for row in rows]


def get_payment(reference: str) -> dict | None:
    with _cursor() as conn:
        row = conn.execute("SELECT * FROM payments WHERE reference=?", (reference,)).fetchone()
    return dict(row) if row else None


def list_user_crm_tasks(user: dict) -> list[dict]:
    with _cursor() as conn:
        if user["role"] == "finance_supervisor":
            rows = conn.execute(
                """SELECT t.*, u.name AS assigned_user_name, u.email AS assigned_user_email
                   FROM crm_tasks t LEFT JOIN users u ON u.id=t.assigned_user_id
                   WHERE t.company_id=? ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,
                   CASE WHEN t.due_date='' THEN 1 ELSE 0 END, t.due_date, t.id DESC""",
                (user["company_id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.*, u.name AS assigned_user_name, u.email AS assigned_user_email
                   FROM crm_tasks t LEFT JOIN users u ON u.id=t.assigned_user_id
                   WHERE t.company_id=? AND t.assigned_user_id=?
                   ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,
                   CASE WHEN t.due_date='' THEN 1 ELSE 0 END, t.due_date, t.id DESC""",
                (user["company_id"], user["id"]),
            ).fetchall()
        conn.execute(
            """INSERT INTO user_notification_state(user_id,state_key,state_value,updated_at)
               VALUES (?, 'crm_tasks_seen_at', ?, ?)
               ON CONFLICT(user_id,state_key) DO UPDATE SET state_value=excluded.state_value,
               updated_at=excluded.updated_at""",
            (user["id"], time.time(), time.time()),
        )
    return [dict(row) for row in rows]


def daily_briefing(user: dict) -> dict:
    """Factual work due today and activity since yesterday for one user."""
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    yesterday_start = now - 24 * 60 * 60
    with _cursor() as conn:
        scope_sql = "t.company_id=?" if user["role"] == "finance_supervisor" else \
                    "t.company_id=? AND t.assigned_user_id=?"
        scope_args = ((user["company_id"],) if user["role"] == "finance_supervisor"
                      else (user["company_id"], user["id"]))
        due = conn.execute(
            f"""SELECT t.*, u.name AS assigned_user_name FROM crm_tasks t
                LEFT JOIN users u ON u.id=t.assigned_user_id
                WHERE {scope_sql} AND t.status='open'
                ORDER BY CASE WHEN t.due_date!='' AND t.due_date<? THEN 0
                              WHEN t.due_date=? THEN 1 ELSE 2 END,
                         CASE WHEN t.due_date='' THEN 1 ELSE 0 END, t.due_date, t.id DESC
                LIMIT 12""",
            (*scope_args, today, today),
        ).fetchall()
        completed = conn.execute(
            f"""SELECT t.title, t.completed_at, u.name AS assigned_user_name
                FROM crm_tasks t LEFT JOIN users u ON u.id=t.assigned_user_id
                WHERE {scope_sql} AND t.status='completed' AND t.completed_at>=?
                ORDER BY t.completed_at DESC LIMIT 10""",
            (*scope_args, yesterday_start),
        ).fetchall()
        conversations = conn.execute(
            """SELECT m.id, m.body, m.created_at, u.name AS sender_name,
                      r.name AS recipient_name
               FROM team_messages m JOIN users u ON u.id=m.sender_id
               LEFT JOIN users r ON r.id=m.recipient_id
               WHERE m.company_id=? AND m.created_at>=?
                 AND (m.recipient_id IS NULL OR m.sender_id=? OR m.recipient_id=?)
               ORDER BY m.id DESC LIMIT 12""",
            (user["company_id"], yesterday_start, user["id"], user["id"]),
        ).fetchall()
    items = []
    for row in due:
        item = dict(row)
        item["due_state"] = ("overdue" if item["due_date"] and item["due_date"] < today
                             else "today" if item["due_date"] == today else "upcoming")
        items.append(item)
    return {"date": today, "due": items, "completed": [dict(row) for row in completed],
            "conversations": [dict(row) for row in conversations]}


def set_user_crm_task_status(user: dict, task_id: int, status: str) -> dict:
    if status not in CRM_TASK_STATUSES:
        raise AuthError("Choose a valid task status.")
    with _cursor() as conn:
        row = conn.execute(
            "SELECT * FROM crm_tasks WHERE id=? AND company_id=?",
            (task_id, user["company_id"]),
        ).fetchone()
        if not row:
            raise AuthError("Follow-up task not found.")
        if user["role"] != "finance_supervisor" and row["assigned_user_id"] != user["id"]:
            raise AuthError("This follow-up is not assigned to you.")
        now = time.time()
        completed_at = now if status == "completed" else None
        conn.execute(
            "UPDATE crm_tasks SET status=?, completed_at=?, updated_at=? WHERE id=?",
            (status, completed_at, now, task_id),
        )
        saved = conn.execute(
            """SELECT t.*, u.name AS assigned_user_name, u.email AS assigned_user_email
               FROM crm_tasks t LEFT JOIN users u ON u.id=t.assigned_user_id WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    return dict(saved)


CRM_OPPORTUNITY_STAGES = {"prospecting", "qualified", "proposal", "negotiation", "won", "lost"}


def list_company_choices() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def list_crm_opportunities() -> list[dict]:
    with _cursor() as conn:
        rows = conn.execute(
            """SELECT o.*, c.name AS company_name FROM crm_opportunities o
               JOIN companies c ON c.id = o.company_id
               ORDER BY CASE o.stage WHEN 'prospecting' THEN 0 WHEN 'qualified' THEN 1
                 WHEN 'proposal' THEN 2 WHEN 'negotiation' THEN 3
                 WHEN 'won' THEN 4 ELSE 5 END,
                 CASE WHEN o.expected_close_date = '' THEN 1 ELSE 0 END,
                 o.expected_close_date, o.updated_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def save_crm_opportunity(fields: dict) -> dict:
    try:
        opportunity_id = int(fields.get("opportunity_id") or 0)
        company_id = int(fields.get("company_id"))
        value = float(fields.get("value") or 0)
        probability = int(fields.get("probability") or 0)
    except (TypeError, ValueError):
        raise AuthError("Check the company, value, and probability.")
    if not get_company(company_id):
        raise AuthError("Choose a valid company.")
    name = str(fields.get("name") or "").strip()[:160]
    stage = str(fields.get("stage") or "prospecting").strip().lower()
    currency = str(fields.get("currency") or "GHS").strip().upper()[:6] or "GHS"
    close_date = str(fields.get("expected_close_date") or "").strip()[:10]
    if not name:
        raise AuthError("Opportunity name is required.")
    if stage not in CRM_OPPORTUNITY_STAGES:
        raise AuthError("Choose a valid pipeline stage.")
    if value < 0:
        raise AuthError("Opportunity value cannot be negative.")
    if probability < 0 or probability > 100:
        raise AuthError("Probability must be from 0 to 100.")
    if close_date:
        try:
            time.strptime(close_date, "%Y-%m-%d")
        except ValueError:
            raise AuthError("Enter a valid expected close date.")
    owner = str(fields.get("owner") or "").strip()[:120]
    notes = str(fields.get("notes") or "").strip()[:2000]
    now = time.time()
    with _cursor() as conn:
        if opportunity_id:
            existing = conn.execute("SELECT id FROM crm_opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if not existing:
                raise AuthError("Opportunity not found.")
            conn.execute(
                """UPDATE crm_opportunities SET company_id=?, name=?, stage=?, value=?,
                     currency=?, probability=?, expected_close_date=?, owner=?, notes=?, updated_at=?
                   WHERE id=?""",
                (company_id, name, stage, value, currency, probability, close_date,
                 owner, notes, now, opportunity_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO crm_opportunities
                     (company_id, name, stage, value, currency, probability,
                      expected_close_date, owner, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (company_id, name, stage, value, currency, probability, close_date,
                 owner, notes, now, now),
            )
            opportunity_id = cur.lastrowid
        row = conn.execute(
            """SELECT o.*, c.name AS company_name FROM crm_opportunities o
               JOIN companies c ON c.id=o.company_id WHERE o.id=?""",
            (opportunity_id,),
        ).fetchone()
    return dict(row)


def delete_crm_opportunity(opportunity_id: int) -> bool:
    with _cursor() as conn:
        cur = conn.execute("DELETE FROM crm_opportunities WHERE id = ?", (opportunity_id,))
        return cur.rowcount > 0


def update_crm_account(company_id: int, fields: dict) -> dict:
    company = get_company(company_id)
    if not company:
        raise AuthError("No such company.")
    lifecycle = str(fields.get("lifecycle_status") or "customer").strip().lower()
    if lifecycle not in CRM_LIFECYCLE_STATUSES:
        raise AuthError("Choose a valid customer status.")
    values = {
        "legal_name": str(fields.get("legal_name") or "").strip()[:160],
        "industry": str(fields.get("industry") or "").strip()[:100],
        "website": str(fields.get("website") or "").strip()[:300],
        "phone": str(fields.get("phone") or "").strip()[:80],
        "location": str(fields.get("location") or "").strip()[:160],
        "address": str(fields.get("address") or "").strip()[:300],
        "registration_number": str(fields.get("registration_number") or "").strip()[:100],
        "tax_id": str(fields.get("tax_id") or "").strip()[:100],
        "relationship_owner": str(fields.get("relationship_owner") or "").strip()[:120],
        "primary_contact_name": str(fields.get("primary_contact_name") or "").strip()[:120],
        "primary_contact_email": str(fields.get("primary_contact_email") or "").strip().lower()[:254],
        "summary": str(fields.get("summary") or "").strip()[:4000],
    }
    if values["primary_contact_email"] and ("@" not in values["primary_contact_email"] or "." not in values["primary_contact_email"].split("@")[-1]):
        raise AuthError("Enter a valid primary contact email.")
    now = time.time()
    with _cursor() as conn:
        existing = conn.execute(
            "SELECT lifecycle_status, lifecycle_changed_at FROM crm_accounts WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        lifecycle_changed_at = (
            (existing["lifecycle_changed_at"] or company["created_at"])
            if existing and existing["lifecycle_status"] == lifecycle else now
        )
        conn.execute(
            """INSERT INTO crm_accounts
                   (company_id, legal_name, industry, website, phone, location,
                    address, registration_number, tax_id,
                    lifecycle_status, relationship_owner, primary_contact_name,
                    primary_contact_email, summary, lifecycle_changed_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                 legal_name=excluded.legal_name, industry=excluded.industry,
                 website=excluded.website, phone=excluded.phone, location=excluded.location,
                 address=excluded.address, registration_number=excluded.registration_number,
                 tax_id=excluded.tax_id,
                 lifecycle_status=excluded.lifecycle_status,
                 relationship_owner=excluded.relationship_owner,
                 primary_contact_name=excluded.primary_contact_name,
                 primary_contact_email=excluded.primary_contact_email,
                 summary=excluded.summary,
                 lifecycle_changed_at=excluded.lifecycle_changed_at,
                 updated_at=excluded.updated_at""",
            (company_id, values["legal_name"], values["industry"], values["website"],
             values["phone"], values["location"], values["address"],
             values["registration_number"], values["tax_id"], lifecycle,
             values["relationship_owner"], values["primary_contact_name"],
             values["primary_contact_email"], values["summary"], lifecycle_changed_at, now),
        )
    values.update({"lifecycle_status": lifecycle, "lifecycle_changed_at": lifecycle_changed_at, "updated_at": now})
    return values


def set_automation(user_id: int, recipe_key: str, enabled: bool, next_run_at: float | None) -> None:
    with _cursor() as conn:
        conn.execute(
            """INSERT INTO automation_settings (user_id, recipe_key, enabled, next_run_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, recipe_key) DO UPDATE SET
                 enabled = excluded.enabled, next_run_at = excluded.next_run_at,
                 updated_at = excluded.updated_at""",
            (user_id, recipe_key, int(enabled), next_run_at, time.time()),
        )


def due_automations(now: float | None = None, limit: int = 20) -> list[dict]:
    now = now or time.time()
    with _cursor() as conn:
        rows = conn.execute(
            """SELECT s.user_id, s.recipe_key, s.next_run_at, u.company_id
               FROM automation_settings s JOIN users u ON u.id = s.user_id
               WHERE s.enabled = 1 AND u.status = 'approved'
                 AND s.next_run_at IS NOT NULL AND s.next_run_at <= ?
               ORDER BY s.next_run_at LIMIT ?""",
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def start_automation_run(user_id: int, company_id: int, recipe_key: str, next_run_at: float) -> int:
    """Advance the due time before doing network work to prevent duplicate cron runs."""
    now = time.time()
    with _cursor() as conn:
        conn.execute(
            "UPDATE automation_settings SET next_run_at = ?, updated_at = ? WHERE user_id = ? AND recipe_key = ?",
            (next_run_at, now, user_id, recipe_key),
        )
        cur = conn.execute(
            """INSERT INTO automation_runs
               (user_id, company_id, recipe_key, status, started_at)
               VALUES (?, ?, ?, 'running', ?)""",
            (user_id, company_id, recipe_key, now),
        )
        return cur.lastrowid


def finish_automation_run(run_id: int, result: dict | None = None, error: str = "") -> None:
    with _cursor() as conn:
        conn.execute(
            """UPDATE automation_runs SET status = ?, result_json = ?, error = ?, finished_at = ?
               WHERE id = ?""",
            ("failed" if error else "complete", json.dumps(result or {}), error[:1000], time.time(), run_id),
        )


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
