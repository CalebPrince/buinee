# Ledgerline

A multi-tenant workspace for finance/back-office teams: prepare a payment
voucher from an invoice, get it approved, issue the letter — with real
roles, a real approval trail, and a signature recorded in the system rather
than printed and scanned.

This is the product pivot from two earlier bespoke, single-client builds —
see [Where this came from](#where-this-came-from) below. Nothing here talks
to those projects; it's a fresh codebase that reuses their proven logic
(`voucher.py`, `providers.py`) and lessons.

**Status as of 2026-07-21: landing page + full auth (register/join/approve/
login) are built and browser-tested. The actual voucher workspace — invoice
upload, AI extraction, approval, the letter — is not built yet. Dashboards
are correct, role-scoped *shells* with empty states, not working tools.**

---

## Running it

```
python server.py     ->  http://127.0.0.1:8080
```

No extra install beyond what's already on the machine — stdlib `http.server`
and `sqlite3`, plus `openpyxl` (FX workbook) and an LLM SDK (`anthropic`/
`google-generativeai`) only if you want the landing page's demo agent live.
Copy `.env.example` to `.env` and add one API key to enable it; the site
works without it, the demo box just says it isn't configured.

The database is a single SQLite file at `storage/ledgerline.db`, created
automatically on first run. Gitignored. Delete it to reset all companies/
users and start clean.

A `.claude/launch.json` is set up so `preview_start` (name: `ledgerline`)
runs `python server.py` on port 8080.

---

## What's here

| Path | What it is |
|---|---|
| `index.html` | Public landing page, with a rate-limited public demo agent |
| `register.html` | Register a new company, or request to join one that exists |
| `login.html` | Sign in |
| `dashboard.html` | Post-login workspace — role-scoped shells, see below |
| `admin.html` | Command Center: Overview — platform stat tiles, the 5 newest signups, system status |
| `admin-companies.html` | Command Center: Companies — every company in full, Finance Supervisor + complete team/pending |
| `admin-login.html` | Platform owner sign in — separate identity from company login, see below |
| `admin-settings.html` | Command Center: Settings — change the platform owner's own password |
| `server.py` | Everything: static pages, the demo agent, and all `/api/*` auth routes |
| `db.py` | SQLite schema + all auth logic (passwords, sessions, approvals) |
| `providers.py` | LLM calls (Anthropic / Google / OpenRouter), shared with the demo agent |
| `voucher.py` | Deterministic voucher math — NHIL/GETFL, VAT, WHT, BOG FX lookup. AI reads an invoice; this computes the voucher. Verified against a real BDDG voucher in its own self-test (`python voucher.py`) |
| `bog-fx-rates.xlsx` | Sample Bank of Ghana FX rates workbook, read by `voucher.py`/the demo |

`login.html` and `register.html` share a two-column layout: a fixed-palette
dark brand panel on the left (a small team/approval-trail mockup using
fictional sample people — A. Boateng, S. Owusu, K. Asante — reused from the
landing page's own sample voucher), the actual form on the right. Collapses
to a single column under 900px, hiding the mockup.

**All landing-page sample data is fictional.** Earlier drafts used Rufus's
real BDDG invoice numbers — that's been scrubbed from anything public.
`voucher.py`'s self-test still uses the real reconciled figures deliberately,
as a correctness check; it's never served over HTTP.

---

## Auth model

Three roles, visibility running downward only:

- **Finance Supervisor** — oversees the company. Sees everyone's work.
  Approves join requests.
- **Senior Accountant** — approves and signs vouchers. Sees their own work
  and account assistants' work.
- **Account Assistant** — prepares vouchers and letters. Sees only their own.

**Registering** a company asks the registrant their *actual* role — Finance
Supervisor is not assumed just because they're the one filling in the form.
A junior person can register the company on the boss's behalf and pick their
own real (lower) role; whatever they pick, that account is approved
immediately, since there's nobody else at a brand-new company who could
approve it (`db.register_company`, all three roles allowed).

**Joining** an existing company is always by typing its name
(`db.find_companies_by_name`, a loose `LIKE` search — no email-domain
matching). This is a deliberate decision, made explicitly with the project
owner: *company name alone is public knowledge and is never sufficient to
grant access.* Every join request lands as `status='pending'` and cannot log
in (`db.authenticate` raises `AuthError` for pending accounts) until a
Finance Supervisor at that specific company approves it from their
dashboard — **with one bootstrap exception**: claiming Finance Supervisor
when the company doesn't have one yet (`db.has_approved_supervisor`) is
approved immediately too, same reasoning as registration — there'd be no one
to approve it either way. The moment a company has an approved supervisor,
that exception closes for everyone else: `request_to_join` rejects further
Finance Supervisor claims for that company from then on. (Known gap: this
check-then-insert isn't wrapped in a transaction/lock, so two people
claiming the role for the same still-supervisor-less company at the exact
same instant could theoretically both get approved — a real risk only in
the narrow window between registration and the real supervisor showing up,
not worth engineering around yet at this scale.)

Every other approval/rejection/list-pending query is scoped server-side by
the *session's own* `company_id` — never a client-supplied one — so a
supervisor at Company A cannot see or act on Company B's queue even by
guessing a user id. This was verified directly (registered two companies,
confirmed cross-company approve attempts get rejected).

Passwords: PBKDF2-HMAC-SHA256, stdlib `hashlib`, no bcrypt dependency —
consistent with the rest of this project's no-extra-dependency approach.
Sessions: random token in its own `sessions` table (not a JWT), so they can
be revoked without touching the password. Cookie is `HttpOnly`,
`SameSite=Lax`; no `Secure` flag yet because there's no HTTPS locally — **add
that before this is ever deployed over the open internet.**

---

## Command Center (`admin.html`) — the platform owner's view

Everything above is scoped to one company. The Command Center is the one
place in Ledgerline that isn't — it's a cross-company view for whoever
actually runs Ledgerline itself.

**A completely separate identity, not a layer on top of a company account.**
Earlier this was an email allowlist checked against an ordinary company
user; that's been retired. It's now its own table, `platform_admins`
(name, email, password hash+salt), with its own sessions
(`admin_sessions`), its own cookie (`ledgerline_admin_session`, distinct
from the company `ledgerline_session` cookie), and its own login page,
`admin-login.html` — separate from `login.html` both in URL and in look
(fixed dark/violet theme, doesn't follow the site's light/dark toggle,
deliberately reads as "a different system"). A platform admin doesn't need
a company account at all, and a company account confers zero platform
access no matter its role — verified directly: a Finance Supervisor logged
into their own company (Rufus, via `ledgerline_session`) gets a 401 from
`/api/admin/overview`, and having a separate valid `ledgerline_admin_session`
cookie in the same browser continues working independently of whatever the
company cookie is doing — the two never interact.

**There is no HTTP route that creates a `platform_admins` row, on purpose.**
There are two ways to create one, both offline/startup-only, never over
the network:

- **Shell access** (local dev, or any host with a shell/SSH tab):
  ```
  python -c "
  import secrets, db
  db.init_db()
  password = secrets.token_urlsafe(12)
  db.create_platform_admin('Your Name', 'you@example.com', password)
  print(password)
  "
  ```
- **No shell access** (e.g. Render's free tier, which doesn't offer one):
  `server.maybe_bootstrap_admin()` runs once at process startup. Set
  `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` (and optionally
  `BOOTSTRAP_ADMIN_NAME`) as environment variables and redeploy/restart the
  service. It's a no-op the instant `db.count_platform_admins() > 0` — so
  it only ever fires once, and it's safe to leave the env vars in place
  afterward (verified directly: calling it twice in a row with the vars
  still set only creates the account the first time). Worth removing
  `BOOTSTRAP_ADMIN_PASSWORD` afterward anyway, just so a real password
  isn't sitting in a dashboard longer than it needs to.

The Command Center is a persistent left sidebar (Overview, Companies,
Settings, then Toggle theme / Sign out) shared identically across
`admin.html`, `admin-companies.html` and `admin-settings.html`, collapsing
to an icon-only rail under 640px. All three pages call the same single
`/api/admin/overview` (`db.list_companies_with_stats`, `db.platform_stats`)
— there's deliberately no separate per-company detail endpoint; that data
was already in the one response, so a second route would just be dead
weight in a security-sensitive area of the code.

- **Overview** (`admin.html`) — the four stat tiles (companies, approved/
  pending users platform-wide, supervisor count), the 5 most-recently
  registered companies as compact cards in a grid (name, date, Finance
  Supervisor or "no Finance Supervisor yet", approved/pending counts —
  several fit per row, more wrap to new rows as companies are added), and
  the system panel (is the demo agent configured, are FX rates loaded, the
  sqlite path). A "View all companies" link goes to the full list.
- **Companies** (`admin-companies.html`) — every company, newest first, as
  the same kind of compact grid card. Click one (or Enter/Space — it's a
  real `role="button"`) to expand it in place: it spans the full grid width
  and reveals the Finance Supervisor's email plus the complete team and
  pending list. Click again to collapse. Nothing is truncated once
  expanded; collapsed cards stay small so many companies fit on screen.
  Expanded, there's also a **Delete company** button (`db.delete_company`,
  `/api/admin/company/delete`) — permanently removes the company and every
  user in it (and their sessions), with a native confirm dialog first.
  There's no undo and no soft-delete. The confirm message and the button's
  click handler both look the company's name up from the already-fetched
  list rather than round-tripping it through an HTML attribute — an
  earlier version embedded the name directly in `onclick="..."`, which
  silently broke (truncated the whole handler, no error, no console
  warning) the moment a company name contained a literal `"`, since the
  attribute's own quoting collided with `JSON.stringify`'s. Caught by
  deliberately testing a delete against a company named
  `ZZTEST Delete Me Again "Ltd"`.

`admin-settings.html` lets the signed-in admin change their own password
(`/api/admin/change-password`, requires the current password — verified
directly: wrong current password is rejected, correct one changes it, and
the old password stops working immediately after).

`index.html`'s footer has a quiet "Owner sign in" link to
`admin-login.html` — the only place it's surfaced publicly. Nothing in
`dashboard.html` references the Command Center at all anymore; the two
are fully separate front doors.

---

## What's genuinely missing (don't assume it exists)

- **The actual voucher workspace.** Uploading an invoice, having an agent
  extract the fields, running it through `voucher.py`, the approve/return/
  re-submit loop, issuing the letter — none of it is wired into
  `dashboard.html` yet. The dashboard's "My vouchers" panel is a real,
  honest empty state, not a stub hiding broken functionality.
- The payment **letter** template itself (the invoice → voucher → letter
  chain's last step) — was never sent, still needed from Rufus.
- No password reset, no email verification, no "edit a teammate's role"
  UI, no removing/deactivating a user. The platform admin can change their
  *own* password in `admin-settings.html`, but if it's lost entirely the
  only recovery is the same direct-database-script route used to create
  the account in the first place.
- No per-company settings (tax regime, currency, letterhead) — everything
  currently hardcodes Ghana's rates (5% NHIL/GETFL, 15% VAT, 7.5% WHT) in
  `voucher.py`. A non-Ghana tenant needs this to be config, not code.
- The chat/file-sharing feature described in the landing page copy
  ("Send a file to a colleague. Or to an agent.") doesn't exist anywhere
  yet — it's landing-page aspiration, not a built feature.
- Rate limiting on `/api/register`, `/api/join`, `/api/login` is a basic
  per-IP sliding window (20 attempts / 10 min) shared with the demo agent's
  limiter — adequate for now, not hardened brute-force protection.

---

## Where this came from

Ledgerline replaces two earlier single-client, single-tenant builds at
`D:/Websites/outlook-agent` and `D:/Websites/excel-agent`. Both are separate
projects, not touched by anything here:

- **outlook-agent** — built for **Rufus Ayertey** at Befesa Desalination
  Developments Ghana Ltd (BDDG). Reads Outlook via a local COM bridge
  (Windows + Outlook Classic only — cannot serve a signed-up tenant; a real
  multi-tenant mailbox connection needs Microsoft Graph + OAuth instead).
  This is where `voucher.py`'s tax/FX logic came from and was verified
  against Rufus's real payment voucher and Bank of Ghana FX workbook.
- **excel-agent** — built for **Jessey** at Tema Oil Refinery (TOR). Still
  just a clickable `dashboard.html` mockup; nothing functional was ever
  built there.

Both are real people at real (and different) companies — the first
practical test of whether Ledgerline's company isolation actually holds
once both of them can register for real.

---

Design & build by [princecaleb.dev](https://princecaleb.dev)
