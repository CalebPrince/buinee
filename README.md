# Buinee

A multi-tenant back-office approval workspace: prepare a voucher from an
invoice, get it approved, issue the letter — with real roles, a real
approval trail, and a signature recorded in the system rather than printed
and scanned. Vouchers are the first document type built on this, not the
only thing it's for — the role/approval-trail model (Preparer → Approver →
Supervisor, visibility running downward only) is deliberately generic, not
finance-specific.

This is the product pivot from two earlier bespoke, single-client builds —
see [Where this came from](#where-this-came-from) below. Nothing here talks
to those projects; it's a fresh codebase that reuses their proven logic
(`voucher.py`, `providers.py`) and lessons.

**Status as of 2026-07-21: landing page + full auth (register/join/approve/
login) are built and browser-tested. The voucher preparation and approval
loop (manual entry, not AI extraction yet) is built and browser-tested —
see [Vouchers](#vouchers) below. Invoice upload/AI extraction and the
payment letter are still not built.**

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

## Deployment — two transports, one route table

`server.py` splits into a transport-agnostic `RouteHandlerMixin` (every
route lives here, and it never touches a socket — it just sets
`self._status`/`self._resp_headers`/`self._resp_body`) plus two thin
transports that read those attributes afterward:

- **`Handler(RouteHandlerMixin, BaseHTTPRequestHandler)`** — a real
  `ThreadingHTTPServer`, used only by `python server.py` for local dev.
- **`application(environ, start_response)`** — a WSGI callable, used by
  `passenger_wsgi.py` under cPanel's Python Selector (Phusion Passenger).
  `WSGIRequest` fakes just enough of `BaseHTTPRequestHandler`'s interface
  (`self.path`, `self.headers.get(name)`, `self.rfile`, `self.client_address`)
  for the exact same route code to run unmodified under either transport.

Both were verified independently: the socket transport via the usual
browser flow (login, dashboard, Command Center all still work after the
refactor); the WSGI transport via `wsgiref.simple_server` serving
`server.application` directly on a throwaway port and hitting it with curl
— GET/HEAD/404s, a full login with cookie-jar persistence, `/api/me`
correctly 401ing with no cookie, and the admin/company endpoints all
checked, with zero code differences from what Passenger will actually run.

**Live at [buinee.app](https://buinee.app)**, on Namecheap Stellar Plus —
cPanel's Python Selector (CloudLinux) running `passenger_wsgi.py` under
Phusion Passenger, i.e. the WSGI transport above. The socket transport is
local dev only and doesn't run in production at all.

Two things follow from being on ordinary cPanel hosting rather than a
container PaaS, and both are load-bearing:

- **`storage/ledgerline.db` sits on the account's normal, permanent disk.**
  Nothing is torn down between deploys, so there is no ephemeral-filesystem
  problem to design around and no separate persistent-volume add-on to pay
  for — it's included in the plan already being paid for. This is why the
  whole app can be SQLite-on-disk and stay that way.
- **Passenger owns the socket.** `PORT`/`HOST` in `server.py` are for local
  dev only; nothing in production reads them.

Setting it up again from scratch (or on any other cPanel host with the
Python Selector — look for "Setup Python App" under cPanel's Software
section before assuming this applies):

1. cPanel → Setup Python App → Create Application. Pick the app root (where
   the code lives on the account), the Python version, and the
   domain/subdomain/path it answers on.
2. Get the code onto the server — cPanel's Git Version Control feature if
   available (pulls straight from
   [github.com/CalebPrince/buinee](https://github.com/CalebPrince/buinee)),
   otherwise File Manager/FTP.
3. Install `openpyxl` into the app's virtualenv — the Python Selector page
   gives you the exact pip command once the app exists.
4. Make sure `passenger_wsgi.py` is at the app root and exposes
   `application` — it already does; nothing to edit unless the app root
   differs from this repo's layout.
5. Set the environment variables the app needs (`.env` at the app root, or
   cPanel's own env var fields): the AI provider key, and
   `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD` on a fresh database —
   see [Command Center](#command-center-adminhtml--the-platform-owners-view).
6. Restart the app from the Python Selector page after any code change —
   Passenger doesn't pick up edited files on its own.

---

## What's here

| Path | What it is |
|---|---|
| `index.html` | Public landing page, with a rate-limited public demo agent |
| `register.html` | Register a new company, or request to join one that exists |
| `login.html` | Sign in |
| `dashboard.html` | Post-login workspace — sidebar app shell, role-scoped views, see below |
| `admin.html` | Command Center: Overview — platform stat tiles, the 5 newest signups, system status |
| `admin-companies.html` | Command Center: Companies — every company in full, Supervisor + complete team/pending |
| `admin-login.html` | Platform owner sign in — separate identity from company login, see below |
| `admin-settings.html` | Command Center: Settings — change the platform owner's own password |
| `server.py` | Everything: static pages, the demo agent, and all `/api/*` auth routes — routing logic is transport-agnostic, see Deployment above |
| `passenger_wsgi.py` | WSGI entry point for cPanel's Python Selector — not used by local dev |
| `requirements.txt` | Just `openpyxl` — everything else is stdlib or raw `urllib` (`providers.py`) |
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

- **Supervisor** — oversees the company. Sees everyone's work.
  Approves join requests.
- **Approver** — approves and signs vouchers. Sees their own work
  and preparers' work.
- **Preparer** — prepares vouchers and letters. Sees only their own.

**Registering** a company asks the registrant their *actual* role — Supervisor
is not assumed just because they're the one filling in the form.
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
Supervisor at that specific company approves it from their
dashboard — **with one bootstrap exception**: claiming Supervisor
when the company doesn't have one yet (`db.has_approved_supervisor`) is
approved immediately too, same reasoning as registration — there'd be no one
to approve it either way. The moment a company has an approved supervisor,
that exception closes for everyone else: `request_to_join` rejects further
Supervisor claims for that company from then on. (Known gap: this
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
place in Buinee that isn't — it's a cross-company view for whoever
actually runs Buinee itself.

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
access no matter its role — verified directly: a Supervisor logged
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
- **No shell access** (some shared-hosting plans don't offer SSH, and
  cPanel's Terminal isn't always enabled):
  `server.maybe_bootstrap_admin()` runs once at process startup. Set
  `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` (and optionally
  `BOOTSTRAP_ADMIN_NAME`) as environment variables and restart the app from
  cPanel's Python Selector. It's a no-op the instant
  `db.count_platform_admins() > 0` — so
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
  registered companies as compact cards in a grid (name, date, Supervisor
  or "no Supervisor yet", approved/pending counts —
  several fit per row, more wrap to new rows as companies are added), and
  the system panel (is the demo agent configured, are FX rates loaded, the
  sqlite path). A command-center attention queue appears first for access
  requests, plan-blocked teams, missing supervisors, and system configuration
  warnings. A "View all companies" link goes to the full list.
- **Companies** (`admin-companies.html`) — every company, newest first, as
  the same kind of compact grid card. Click one (or Enter/Space — it's a
  real `role="button"`) to expand it in place: it spans the full grid width
  and reveals the Supervisor's email plus the complete team and
  pending list. Click again to collapse. Nothing is truncated once
  expanded; collapsed cards stay small so many companies fit on screen.
  Each expanded company now starts with a persistent **Account 360** CRM
  profile: lifecycle status (Lead, Trial, Customer, At risk, Paused, Churned),
  legal name, industry, website, phone, location, Buinee relationship owner,
  primary contact, and an internal account summary. CRM fields live in
  `crm_accounts`, are visible only to authenticated platform admins, and save
  through `/api/admin/company/crm`; they never leak into the customer workspace.
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

## `dashboard.html`'s app shell

Rebuilt to match the sidebar-rail + topbar layout used across the other
Prince Caleb agent dashboards (`outlook-agent`/`excel-agent`'s
Clerk/Gridwise consoles) — same skeleton, Buinee's own teal/ochre
tokens instead of their slate/amber or emerald/iris ones. Nine views,
switched client-side with no page reload:

- **Overview** — greeting, KPI tiles, and card(s) for "My vouchers" (and
  "Awaiting your approval" for Approver/Supervisor). Every
  number shown is real, drawn from `/api/vouchers`, never an invented demo
  stat — the reference dashboards are sales prototypes and use fabricated
  activity/metrics; this is a real product, so nothing here is illustrative.
  The inbox card is live and renders the latest ten headers, unread first.
  Semantic-only metrics such as emails triaged and replies drafted remain
  "—" and explicitly say that their workflow is not enabled yet.
- **Needs your attention** — a role-aware queue before the KPI tiles on every
  login. Supervisors see pending access and submitted vouchers; approvers see
  vouchers awaiting review; preparers see returned vouchers; mailbox users see
  unread inbox mail; team-plan users see unread team messages. Counts refresh
  every 30 seconds and link directly
  to the relevant work area. Opening Team chat marks its incoming-message count
  seen for that conversation and user. Team chat sends a presence heartbeat with
  the notification refresh: members active within 75 seconds show a green dot;
  offline members show a red dot and an explicit note that they will see the
  new-message alert when they return. Unread counts also appear beside the exact
  group or direct conversation, and the browser tab shows the total open count.
  Signing out clears presence immediately; an abandoned tab falls back to the
  75-second activity timeout.
- **Vouchers** — prepare, submit, approve/reject. See
  [Vouchers](#vouchers) below.
- **Flagged** — every voucher visible to this person where `voucher.py`'s
  `review()` found something arithmetically or procedurally worth a second
  look (a fallback FX rate, a net payable that doesn't reconcile, etc.) -
  real data already computed for the Vouchers view, just filtered and
  surfaced on its own page. Nav item carries a live count badge.
- **To fix** — the broader actionable exception queue: rejected vouchers,
  deterministic voucher review notes, issues from user-triggered email
  analysis, and structured issues saved by read-only automation runs. It
  never treats an unread header as an issue and deduplicates an email reviewed
  by both Triage and an automation. Its nav badge and Overview KPI are live.
- **Triage** — a live split-pane work queue based on the connected mailbox:
  All/Unread filters, unread count, sender, subject, received time and a
  detail desk. Triage requests a safe, length-limited plain-text body while
  the Overview remains header-only; neither view changes mailbox state.
  A person can run Ada's structured analysis in that same detail desk to get
  a summary, category, priority, next action, concrete issues, and an editable
  reply draft when a reply is genuinely needed. Results stay in the browser
  session and consume the same plan allowance as Ask Ada.
- **Ask Ada** — an authenticated version of the landing page's demo agent. A
  general assistant for the person's finance/back-office work - any business
  question, not a voucher-lookup tool - grounded in their real, role-scoped
  vouchers for factual claims (`build_voucher_digest`/`build_chat_system` in
  `server.py`, `/api/chat`). Can also take an attached document: a paperclip
  button reads a text file client-side (`.txt`/`.md`/`.csv`, capped at 20k
  chars) and sends it through `providers.py`'s existing `split_docs`/`docs`
  plumbing, tagged server-side as "attached" - never trusted as reference
  material, since there's no template library feature to draw from yet.
  Shows a clear disabled state if no AI provider is configured, rather than a
  chat box that silently fails - also surfaced as a status pill in the
  topbar (`assistantPill`, checked via `/api/demo/status`), matching
  `outlook-agent`'s "Mailbox not connected" pattern. This only ever shows
  connection *status*, never the key itself - the key stays server-side in
  `.env`/cPanel env vars, unlike `outlook-agent`'s own Settings page, which
  by its own code comment stores keys in browser `localStorage` and flags
  itself as an insecure prototype pattern not meant for production.
- **Automations** — two real, read-only recipes: Morning triage & brief and
  Invoice cross-check. Enablement and run history are stored per user; every
  run checks that user's mailbox connection, company AI plan, and the shared
  server-side provider configuration. Results are saved for review and no run
  moves, sends, deletes, or marks mail as read. Recipe definitions live in
  `AUTOMATION_RECIPES`, while `recipe_key` is open-ended in SQLite, so adding
  another recipe does not require redesigning the persistence or API.
  `automation_runner.py` executes due recipes and is intended for a cPanel
  Cron Job every five minutes using the application's virtualenv Python, for
  example: `/path/to/virtualenv/bin/python /path/to/app/automation_runner.py`.
  The page also offers Run now for testing. Auto-file and weekly-send remain
  visibly unavailable because they would change external state.
- **Activity** — the real approval trail: every prepare/submit/approve/reject
  event, who did it and when, scoped by the same downward-only visibility as
  everywhere else. Backed by a genuine append-only `voucher_events` table
  (`db.py`), not derived from the vouchers table's own timestamp columns -
  those only reflect *current* state and are cleared on rejection
  (`approved_by`/`approved_at` go back to `NULL` so a rejected voucher isn't
  shown as approved by anyone), which would have silently lost who rejected
  a voucher and when. `list_activity`/`/api/activity`. Existing vouchers
  created before this table existed have no history, honestly - nothing was
  backfilled or invented for them.
- **Instructions** separates shared governance from each person's working
  context. The company provider and briefing remain Supervisor-only; every
  approved user can save private personal instructions and a private reference
  library (10 files, 25 MB total, 5 MB each). Text/data files and modern Office
  formats (`.docx`, `.xlsx`, `.pptx`) are extracted to readable text on upload;
  PDF and supported images are passed natively to models that accept them.
  RTF is normalized to plain text. Legacy `.doc`/`.xls` files must first be
  saved in their modern formats. Every document query is scoped by `user_id`,
  so company role does not grant access to somebody else's files:
  - **AI provider** - a per-company preference among whichever providers
    have a key configured on this deployment (`db.set_company_model`,
    `/api/company/model-options`, `/api/company/set-model`). Falls back to
    the server default the instant the saved choice isn't configured here
    (`resolve_provider_model` in `server.py`) - a stale preference can never
    hard-fail. An optional model-string override sits alongside it. This is
    `outlook-agent`'s model picker, done as a real per-company setting
    instead of a per-browser one, since one shared deployment key serves
    every company - see [Vouchers](#vouchers)'s "Fixed while building Chat"
    note on why `outlook-agent`'s own approach (keys in `localStorage`)
    isn't appropriate here.
  - **Custom instructions** - free text folded into every Chat conversation
    at the company via `providers.with_briefing` (`db.set_company_briefing`,
    `/api/company/briefing`) - policies, terminology, tone. Cannot switch off
    Chat's grounding/safety rules, only add context on top of them.
  - **Personal instructions and documents** - `user_instructions` and
    `reference_documents` in SQLite, managed through `/api/user/instructions`
    and `/api/user/reference-documents/*`. Personal instructions and text
    references also inform mailbox triage and automations; the full private
    library is available in Ask Ada.
- **Team** — the approved roster and role guide are visible to every approved
  user; the pending-approval queue, plan capacity, and Approve/Reject controls
  remain Supervisor-only. The signed-in person is marked “You”. Join decisions
  require confirmation and refresh the roster, queue, empty state, seat count,
  and live nav badge together; the badge is hidden entirely at zero.
- **Team chat** — available only when the company is on a plan whose audience
  is `team` (enforced in both UI and API). It is included automatically in
  every Team pricing tier and excluded from every Individual/Solo tier; it is
  not controlled by the separately metered AI assistant setting. Approved
  members of the same company
  appear in a conversation rail and can exchange messages with the whole team
  or privately with one selected colleague. Each message supports up to three
  files. Downloads are authenticated and restricted to members of that group or
  direct conversation. “Add to Ada” copies a shared file into the current user's
  private reference library; it does not expose another user's private
  instructions or library. Messages are append-only and the dashboard polls for
  new ones while Team chat is open. The selected conversation survives a page
  refresh. “Clear conversation” stores a per-user visibility marker: it hides
  existing messages only for the person clearing them and never deletes another
  member's copy.

The rail also has the other two visual pieces from the reference dashboards:
a prominent "New voucher" compose button (opens the Vouchers form directly,
also mirrored as a hero button next to the Overview greeting), and a
"Connected" sources panel at the bottom showing **Email inbox — Coming
soon**. Unlike the reference dashboards, that panel is honest about not
being wired up to anything — no fake "Connect" button, no fabricated
connected/live state — since real per-user mailbox OAuth (Microsoft Graph/
Gmail API) is a genuinely separate, unbuilt feature, not a styling exercise.
See [What's genuinely missing](#whats-genuinely-missing-dont-assume-it-exists).

**Fixed while building Chat**: `providers.py`'s `chat()` was hardcoded to a
leftover outlook-agent persona - literally "You are Ada, an assistant for
Rufus" - unconditionally prepended to *every* call, including the landing
page's own demo agent (which was then telling the model it was simultaneously
"Ada, for Rufus" and "the assistant on Buinee's landing page", with the
latter absurdly framed as "written by Rufus himself"). This was already live
on the public demo, not something introduced by this feature. Fixed by making
`chat()` take the caller's full system prompt as a required argument instead
of a hardcoded default - the landing page passes its own visitor-facing
prompt, the dashboard's Chat passes `build_chat_system()`, and `CHAT_SYSTEM`
itself was rewritten to describe Buinee/vouchers generically instead of
Rufus/BDDG specifically. Verified the new call shape reaches Anthropic's real
API correctly (a deliberately invalid key returns their actual 401, not a
Python `TypeError` from a signature mismatch).

Caught one bug while verifying the KPI tiles: the original draft tried to
update a `#kpiTeam` span's `textContent` from inside `loadTeam()` before
that span had even been inserted into the DOM (the KPI HTML was built
afterward), so the team-member count silently stayed blank. Fixed by
having `loadTeam`/`loadPending` both return their counts directly, used to
build the KPI list up front — verified live (logged in as Rufus: "Team
members" correctly showed 2, approving a pending request correctly moved
them into the roster and cleared the nav badge; logged in as Doreen, an
Preparer: Team nav item and its KPIs correctly don't appear).

---

## Pricing tiers

Four decisions were made explicitly with the project owner before building
this: **Paystack** as the eventual payment processor (not wired up yet -
see below), a **free tier that unlocks more users on paid plans** rather
than a hard paywall at registration, **demo pricing** to be replaced
with real numbers later, and tiers sold to **two different buyers** — a
company registering for its members, and one person working alone.

**`plans`** (`db.py`): `name`, `price`, `currency`, `user_limit`,
`sort_order`, `is_default`, `audience`. Seeded once, on first `init_db()`,
with:

| Plan | Sold to | Price | Users included |
|---|---|---|---|
| Solo Free | individual | GHS 0 | 1 |
| Solo Pro | individual | GHS 25/mo | 1 |
| Free (default) | team | GHS 0 | 3 |
| Starter | team | GHS 50/mo | 10 |
| Growth | team | GHS 150/mo | 30 |

These are placeholders, not real prices — `admin-plans.html` (Command
Center → Plans) exists specifically so they can be edited, or new tiers
added, without touching code. Every company gets `plan_id` pointing at the
tier chosen on the landing page (see [Pricing is the way
in](#pricing-is-the-way-in) below), falling back to whichever plan is
`is_default` (existing companies were backfilled to it via an idempotent
migration in `init_db()` — `ALTER TABLE companies ADD COLUMN plan_id` only
runs if the column isn't already there, then any `NULL` plan_id gets set to
the default).

**`audience`** splits *a company registering for its members* from *one
person working alone* — `'team'` or `'individual'`. It's deliberately thin:
an individual plan is a **1-seat plan**, and `can_add_user` was already the
thing that stops a second person joining, so almost nothing new enforces it.
What does follow from being alone is handled at registration
(`db.register_company`): the workspace is named after the person unless they
typed a name of their own, and they hold Supervisor whatever role the form
sent, because every other role needs somebody else present to approve the
work.

Solo workspaces **are** listed in the join picker (`find_companies_by_name`).
Someone who started alone and then hired is exactly the case that picker has
to serve — hiding them would only push the new colleague into registering a
duplicate company under the same name. See [Growing out of a solo
plan](#growing-out-of-a-solo-plan).

Audience is fixed at creation, not editable: flipping a team plan to
individual would strand every company already on it above the seat cap, and
widening an individual plan past one seat would quietly turn someone's
personal workspace into a joinable company. `create_plan` refuses an
individual plan with anything but 1 seat; `update_plan` refuses to widen
one. Databases predating the split get `audience='team'` on every existing
row (the honest default — everything before this was sold to a company) plus
the two Solo tiers, seeded only if no individual plan exists yet, so an owner
who reprices them keeps their edits. Verified on a copy of the live database:
3 existing plans classified as team, 2 solo added, 5 companies and 12 users
untouched, re-running `init_db()` twice more added nothing.

**Enforcement** (`db.can_add_user`, count of `status='approved'` users
against the company's plan `user_limit`) sits at both places a user can
become approved: `db.approve_user` (the normal path — a Supervisor
approving a pending request) and the bootstrap-supervisor branch of
`db.request_to_join` (claiming Supervisor when a company has none
yet). Both raise a plain-English `AuthError` rather than a generic
rejection. **Downgrading never removes anyone already in** — the check is
only ever "can one more be *added*," so a company that's over its new,
lower limit just can't grow further until it upgrades or someone leaves;
nobody gets auto-kicked. Verified directly, including this exact edge
case: raised BDDG's plan limit to approve someone, then reverted it,
leaving the company one user over its own Free-tier cap — confirmed
`can_add_user` correctly blocks further approvals in that state without
touching the existing (over-limit) team.

Where this is surfaced:
- **`admin-plans.html`** — the only place plans get created or edited
  (`/api/admin/plans`, `/api/admin/plans/create`, `/api/admin/plans/update`,
  all platform-admin only). Inline edit per card, plus an "add a new tier"
  form, where "Sold to" picks the audience and locks the seat count to 1 for
  an individual tier rather than leaving a way to get rejected. Cards carry
  an Individual/Team badge; the seat field is disabled on individual tiers.
  A banner states plainly that the prices are demo values.
- **`index.html` `#pricing`** — the public, unauthenticated pricing section
  every registration now starts from (see below).
- **`admin-companies.html`** — each company's card shows its plan name and
  `used/limit`, with an "— at limit" note once it's reached. Solo workspaces
  show "(one person)" instead of a seat count, and gain a "needs a team plan"
  flag once somebody is waiting to join one or more than one member is already
  approved. The Change plan dropdown prefixes every option with **Individual**
  or **Team**, so similarly named tiers cannot be confused.
- **`admin.html` Overview** — the condensed recent-signups cards show
  `used/limit` in place of a bare count.
- **`dashboard.html` Team view** — a Supervisor sees a plan banner
  above their team/pending panels (`renderPlanBanner`), which switches to a
  warning style once at the limit. The Approve button now surfaces the
  limit error via a plain alert instead of failing silently, since hitting
  the cap is an expected, recoverable state now, not an edge case to hide.
- **`admin-companies.html`**, expanded card — a "Change plan" dropdown
  (every existing plan, current one pre-selected) plus a Move button, wired
  to `db.set_company_plan` / `/api/admin/company/set-plan`. Same
  never-removes-anyone rule as everywhere else: moving a company onto a
  plan smaller than its current headcount doesn't kick anyone out, it just
  means no further approvals until it's back under the new limit or
  upgraded again. Verified directly: moved BDDG (4 users) from Free (limit
  3, so it was sitting over-limit) to Starter (limit 10) — the "at limit"
  warning disappeared immediately and the dropdown correctly pre-selected
  Starter on reload; moved it back to Free and the over-limit warning came
  right back, all without touching any of the 4 existing users. A company
  cannot change its own plan — this is Command Center-only, same as
  creating/editing the tiers themselves.

### Pricing is the way in

Registration is **gated behind choosing a tier**. `index.html` has a public
`#pricing` section rendered from `/api/plans` (unauthenticated on purpose —
pricing is marketing copy, and the landing page should never drift from what
the Command Center actually charges), split by `audience` behind a
*Just me / My team* toggle. Team is shown first; the toggle hides itself
entirely if the owner only sells to one audience, rather than offering an
empty tab. The cheapest **paid** tier in whichever group is showing gets the
"Most popular" flag — a rule rather than a hardcoded plan id, so it stays
correct however the tiers are repriced.

Each card links to `/register.html?plan=<id>`. Every other route to the
register page now points at `#pricing` instead — the nav button, the hero
CTA, the in-hero "Approve & sign", the closing CTA, and both links on
`login.html`. Landing on `register.html` **without** a valid `?plan=`
replaces the form with a gate screen pointing back at pricing; a bogus id
gates identically, since the plan is validated against `/api/plans` rather
than trusted. Joining an existing company is deliberately exempt (that
company already has a tier), so the gate offers "Join instead", and
`?join=1` still works directly.

With a valid tier, a chip above the form confirms what was chosen and
`plan_id` rides along on `/api/register`. On an **individual** tier the form
drops the two questions that only make sense alongside other people: the
company field becomes an optional "Workspace name" and the role picker
disappears. Note the gate is a **UI** rule — `db.register_company`
deliberately falls back to the default plan rather than failing a whole
registration over a missing or bad `plan_id`, on the grounds that a broken
link shouldn't cost a signup.

### Growing out of a solo plan

A workspace that starts as one person and then hires is a first-class path,
not an edge case. What happens when the second person turns up:

1. **They find it.** Solo workspaces are listed in the join picker like any
   other, so the colleague searches the name and requests to join.
2. **The request lands as `pending`.** No seat check runs when a request is
   *created* — only when one is approved — so the request reaches the owner
   rather than being refused at the door.
3. **The owner can't approve it yet**, and is told exactly why:
   `db._at_limit_message` speaks differently to a solo workspace ("covers
   just one person… has to move onto a team plan") than to a team at its cap
   ("already at that limit… needs to be upgraded"). The dashboard's plan
   banner says the same thing, and only mentions it once someone is actually
   waiting — a solo workspace is permanently "1 of 1 users", so counting
   seats at someone working alone would just be nagging.
4. **You move them.** `admin-companies.html` flags the company as needing a
   Team tier (`needs_team_plan`) when an Individual plan has pending requests
   **or already contains more than one approved member**. The existing Change
   plan dropdown does the move.
   Deliberately not automatic: Solo Pro → Starter doubles what they pay, and
   Solo Pro → team Free would cut their bill *and* silently take away the AI
   assistant. Nothing changes what someone is billed without a human
   deciding.
5. **They approve, and nothing else moved.** Same never-removes-anyone rule
   as every other plan change.

**Duplicate names are caught at registration**, which is what made the above
reachable. Registering a name a workspace already answers to (case- and
whitespace-insensitive) raises `DuplicateCompanyError` — an `AuthError`
subclass carrying the existing company — which `/api/register` returns as a
**409** with `{duplicate_name, company}`. The form turns that into a
question rather than an error: *That's my company — ask to join* (switches
to the join tab with the company pre-picked and the name/email/password
already typed carried across) or *Different company — keep going* (re-sends
with `allow_duplicate_name: true`). That confirmation is scoped to the name
that was confirmed — editing the field clears it, so a second, different
clash still gets asked about.

This closed a pre-existing gap that had nothing to do with solo plans:
registering an existing company's exact name used to silently create a
*second* company with the same name on the door, quietly splitting a team
across two workspaces.

**Known gap**: once someone confirms "different company", two workspaces
genuinely do share a name, and the join picker shows two identical rows with
no way to tell them apart. Disambiguating them would mean exposing something
about a company (its supervisor, its size) to someone who isn't in it yet,
which the picker deliberately never does — so this is left as-is.

**Fixed along the way**: `init_db()` crashed on any brand-new database —
`_seed_default_plans` inserts `chat_enabled`/`chat_monthly_limit`, but those
columns are only added by `_migrate_plan_chat_gating`, which ran *after* it.
Existing deployments survived only because their database predated the
migration; any brand-new database — a fresh install, a wiped
`storage/ledgerline.db`, a second environment — would not have booted. The
migrations now run before the seed.

**Not built**: any actual Paystack integration — no checkout flow, no
webhooks, no subscription lifecycle (trial, renewal, failed payment,
cancellation). The tier/limit machinery and the Command Center's ability to
move a company between tiers are both real and working; what's missing is
the part where a company would actually pay to get moved there themselves.

**The AI assistant is gated by plan, because it isn't free to run.** Every
company's AI conversations run on the platform owner's own AI provider key (see
[Vouchers](#vouchers)'s "AI provider" section under Instructions) - there's
no bring-your-own-key option, so usage has to tie back to what a company's
plan actually pays for, the same reasoning `user_limit` was always built on.
Two new plan fields:

- **`chat_enabled`** - whether the AI assistant is available on this plan. Seeded
  demo default: off on Free, on for Starter/Growth.
- **`chat_monthly_limit`** - a message cap per company per calendar month,
  `NULL` for unlimited. Seeded demo default: 200/mo on Starter, unlimited on
  Growth.

These two fields do not control Team chat. Team chat is an automatic entitlement
of `audience='team'`, while `audience='individual'` always excludes it. Both the
landing-page pricing cards and Command Center plan cards state that entitlement.

Usage is a genuine persisted counter (`chat_usage` table, one row per
company per `YYYY-MM`), not the in-memory per-IP rate limiter the landing
page's demo agent uses - that one resets on every server restart and isn't
scoped to a calendar month, neither of which is acceptable for something a
plan's price is supposed to cover. `db.can_use_chat` is checked before every
`/api/chat` call; `db.increment_chat_usage` only runs after a reply actually
succeeds, so a failed provider call never counts against what the company is
paying for. Both gates fail closed with a `402` and a specific reason
(`not_included` vs `quota_exceeded`) that the dashboard's Chat page surfaces
as a banner and a disabled input, not a generic error. Editable per plan
from `admin-plans.html`, same place `user_limit` already lives.

---

## Vouchers

The prepare → submit → approve/reject loop, wired into `dashboard.html`'s
Vouchers view. Data entry is manual (typed by a preparer) — AI invoice
extraction is not built; see [What's genuinely missing](#whats-genuinely-missing-dont-assume-it-exists).

**`vouchers`** (`db.py`): everything a preparer types (supplier, invoice
number/dates, credit terms, line items as JSON, vatable amount, NHIL/VAT/
VRPO flags, non-taxable/overpayment deductions) plus `status` (`draft` →
`submitted` → `approved`/`rejected`), `created_by`, `approved_by`,
`rejection_reason`. It never stores a computed figure — `server.py`'s
`compute_voucher()` runs every voucher's raw inputs through `voucher.py`'s
`compute()`/`review()` fresh on every read, the same principle as the
landing page's demo (the model/preparer supplies inputs, code does the
arithmetic). This means a future tax-rate change re-derives every existing
voucher instead of leaving stale numbers behind.

**Visibility** (`db.list_vouchers`) follows the same downward-only rule as
everywhere else in this app: an Preparer sees only their own
vouchers, a Approver sees their own plus every Preparer's,
a Supervisor sees the company's entire voucher book.

**Segregation of duties**: `db.approve_voucher`/`reject_voucher` refuse if
the reviewer is also the preparer (`created_by == approver_id`), regardless
of role — a Approver or Supervisor can prepare a voucher
like anyone else, but can't be the one to sign off on their own. Only a
`submitted` voucher can be approved/rejected; only the preparer can submit
their own `draft` or `rejected` voucher (rejecting clears the reason and
puts it back in the queue on resubmission). Role gating (Approver/
Supervisor only) for the review endpoint lives in `server.py`,
consistent with how `/api/company/approve` gates Supervisor.

Verified end to end in the browser: created a voucher reproducing the real
BDDG sample invoice through the actual creation form — every computed
figure (NHIL 89.23, VAT 267.69, WHT 133.84, net payable 12,477.11, BOG FX
conversion) matched `voucher.py`'s own self-test exactly. Then: submit →
approve as a different Approver (succeeds); a second voucher
submit → reject with a reason → preparer sees the reason → resubmit clears
it; a Approver's own submitted voucher correctly refused
self-approval (400) but was approved fine by the Supervisor;
an Preparer calling the review endpoint directly got a 403; each
role's visible voucher list matched the downward-only rule exactly.

**Not built**: invoice upload, AI extraction into the form fields, the
payment letter, editing/deleting a voucher.

---

## What's genuinely missing (don't assume it exists)

- **Invoice upload and AI extraction.** The prepare/submit/approve/reject
  loop is built (see [Vouchers](#vouchers)), but every voucher is typed by
  hand — nothing reads an invoice file and fills the form yet.
- The payment **letter** itself doesn't exist — a voucher can be approved,
  but nothing generates the signed PDF that would actually go out (the invoice
  → voucher → letter chain's last step). The letter template and required
  signature format have not been supplied,
  still needed from Rufus.
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

## Connecting a mailbox

The old `outlook-agent` read Outlook through a local Windows COM bridge —
one machine, Outlook Classic open, no way to serve a company that just
signed up. `mailbox.py` replaces it with three ways in, behind one
interface:

| Provider | How | What it costs to enable |
|---|---|---|
| `microsoft` | Graph, OAuth | An Azure app registration |
| `google` | Gmail API, OAuth | A Google Cloud client **plus verification** — see below |
| `imap` | Direct IMAP | Nothing — works as soon as a key is set |

**IMAP is the one that covers most customers**: company mail on cPanel,
Zoho, Fastmail, Yahoo, and Gmail itself via an app password. No consent
screen, no vendor queue. The trade is that it holds a password rather than a
revocable token, which is what forced the encryption below.

**Gmail is not symmetrical with Outlook.** Google classifies `gmail.modify`
as a *restricted* scope: a production app needs verification plus an annual
third-party security assessment (CASA), and is capped at a handful of test
users until that clears. The code is ready; the queue is Google's. Confirm
the current policy before committing to a timeline.

Decisions worth knowing before changing anything here:

- **Delegated permissions, not application permissions.** Each person
  consents to their own mailbox. Buinee never asks a tenant admin for
  blanket read access across an organisation, and there's no code path that
  would use it if one were granted. `mailbox_connections` is keyed by
  `user_id` for the same reason — a supervisor can see *that* someone
  connected a mailbox, never read it through them.
- **Multitenant registration**, so any Microsoft 365 organisation can
  consent without being registered in Buinee's directory first. That's why
  the authority is `/common` rather than a fixed tenant id.
- **`Mail.ReadWrite`, not `Mail.Send`.** The agent can triage an inbox and
  put a draft in the person's own Drafts folder; sending stays something
  they do themselves. Adding `Mail.Send` later forces every already-connected
  user to re-consent, so it's a deliberate omission, not an oversight.

### Credentials are encrypted at rest, or not stored at all

`secretstore.py` (Fernet, from `cryptography`) encrypts whatever a provider
needs to keep — a refresh token, or an IMAP password — into
`mailbox_connections.credentials_enc`. Host, port and address stay readable:
they're settings, not secrets, and the UI needs them without a key.

**It fails closed.** With `BUINEE_SECRET_KEY` unset or invalid, connecting a
mailbox is refused outright and the dashboard says why. Falling back to
plaintext would be the one behaviour nobody notices and everybody regrets.

    pip install cryptography
    python -c "import secretstore; print(secretstore.generate_key())"

What this protects against: someone who ends up with a copy of
`storage/ledgerline.db` — a stray backup, a misconfigured File Manager, the
wrong attachment on a support ticket — and no copy of the key. It does *not*
protect against someone who has the server, since the key is on it. That's
the honest limit of any key-on-the-same-box scheme, and it's still worth
having, because database files travel far more easily than servers do.
Rotating the key makes existing connections unreadable; people reconnect, so
don't rotate it casually.

### Creating the Azure app registration

Done once, by the platform owner, in the Azure portal — the values it
produces go in `.env`/cPanel env vars, never in the repo.

1. [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** →
   **App registrations** → **New registration**.
2. Name it (`Buinee`). Under **Supported account types** pick **Accounts in
   any organizational directory (Any Microsoft Entra ID tenant) and personal
   Microsoft accounts** — that's what this deployment is registered as, and
   it's the audience `/common` in `mailbox.py` is the endpoint for. Anything
   single-tenant works only for your own directory and rejects every
   customer.

   The narrower **Any Entra ID tenant** (org accounts only) is also valid,
   but then the authority should be `/organizations` rather than `/common`,
   or people signing in with a personal account get through the picker and
   fail afterwards. Including personal accounts means a sole trader on
   Outlook.com or Hotmail can connect — which pairs with the Solo tiers —
   but also that someone at a company can attach a personal mailbox to a
   work workspace. That's a policy question, not a technical one.
3. **Redirect URI**: platform **Web**, value
   `https://buinee.app/api/mailbox/callback`. Add
   `http://localhost:8080/api/mailbox/callback` as a second Web redirect URI
   for local dev — `http` is allowed for `localhost` only. These must match
   `AZURE_REDIRECT_URI` character for character.
4. **Register**, then copy the **Application (client) ID** from Overview.
   That one is not a secret.
5. **Certificates & secrets** → **New client secret**. Copy the **Value**
   immediately — Azure shows it once and only ever displays the Secret ID
   afterwards. Note the expiry date; the connection breaks when it lapses.
6. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions** → add `offline_access`, `User.Read`,
   `Mail.ReadWrite`. `offline_access` is the one that returns a refresh
   token; without it every connection dies about an hour after it's made.
7. Put the two values in `.env` (or cPanel's env var fields):
   `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, plus `AZURE_REDIRECT_URI`.
   Restart the app from the Python Selector.

Some tenants require an administrator to approve a third-party app before
staff can consent for themselves. That surfaces as a "needs admin approval"
screen at sign-in and comes back as `?mailbox=denied` — it's the customer's
IT department to resolve, not a bug here.

**Google** is the same shape: a Google Cloud project → OAuth consent screen →
credentials → **Web application** client, the same
`https://buinee.app/api/mailbox/callback` redirect, and
`GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`/`GOOGLE_REDIRECT_URI`.
`access_type=offline` and `prompt=consent` are both set in `mailbox.py`
because Google only returns a refresh token when they are — without it the
connection silently dies within the hour.

**IMAP** needs no registration at all. The person supplies server, port,
address and password; `mailbox.connect_imap` proves them by logging in
before anything is stored, so credentials that don't work never reach the
database.

### The flow

`GET /api/mailbox/connect?provider=…` mints a single-use state row and
redirects to the provider. `GET /api/mailbox/callback` checks the state
**before trusting anything else**, and checks it against the session cookie
too: the state says which user asked, the cookie says who is actually
driving the browser, and if they disagree the callback is refused rather
than attaching a mailbox to whoever happens to be signed in. The state also
*carries the provider*, so the callback never takes that from a query
parameter. States are spent on read (deleted as they're consumed) so a
replayed callback finds nothing, and expire after ten minutes.

`server.live_mailbox()` is the only way anything reaches a mailbox: it
decrypts the stored credentials, refreshes when an access token is within
two minutes of expiring, re-encrypts the result, rotates the refresh token
when the provider returns a new one, and **deletes the connection outright**
if a refresh is rejected — a revoked grant should read as "not connected",
not fail forever.

Disconnecting deletes Buinee's copy. For the OAuth providers the consent
still exists until the person removes it at
[myapps.microsoft.com](https://myapps.microsoft.com) or
[myaccount.google.com/permissions](https://myaccount.google.com/permissions),
which the confirm dialog names.

The Overview now renders the latest ten inbox headers from
`mailbox.list_recent` (`GET /api/mailbox/messages`), putting unread messages
first without pretending that unread automatically means important. It does
not fetch bodies, change read state, or act on mail.

**Not built yet**: ranking across the whole inbox or any automation that
changes the mailbox. Per-email analysis and scheduled automation runs are
deliberately read-only; they can surface issues in To fix but cannot send,
move, delete, or mark mail as read.

---

## Where this came from

Buinee replaces two earlier single-client, single-tenant builds at
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
practical test of whether Buinee's company isolation actually holds
once both of them can register for real.

---

Design & build by [princecaleb.dev](https://princecaleb.dev)
