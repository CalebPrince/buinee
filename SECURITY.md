# Security

Status: REVIEW_REQUIRED
Baseline version: 0.1.0
Canonical record: `security-baseline.yaml`
Owner: site-owner
Last reviewed: 2026-09-28

This file is the human-readable view. `security-baseline.yaml` is authoritative for approval, scope, locks, controls, gates, and evidence. Nothing here is APPROVED or VERIFIED unless that file and its linked evidence say so.

## Posture and scope

Risk tier **MEDIUM**, mode **STANDARD**. This baseline covers the active landing-page redesign (new `/pricing` and `/case-studies` pages, extended CMS schema, new reference image assets) plus infrastructure findings surfaced while scoping that work. It does **not** re-audit the existing registration, billing (Paystack), or dashboard/admin surfaces — those are pre-existing and out of scope here.

In-scope assets: public marketing/CMS copy (public), the new `.opskeep/` design/security documents (internal), the owner-role admin CMS write path (confidential), and the site's deployment access (restricted, held by the account owner). In-scope identities: the owner-role admin and the anonymous site visitor.

Production posture: `ELIGIBLE_WHEN_APPROVED`. Development and deployment are currently `LOCKED` — no implementation of the redesign proceeds until this baseline is explicitly approved.

## Baseline controls

| Control ID | Requirement | Owner | Status | Evidence ID/location | Limitation or next step |
|---|---|---|---|---|---|
| CTL-GOV-001 | No implementation/deploy until this baseline is approved | site-owner | PLANNED | EVD-001 / NOT_YET_BUILT | Owner approval recorded in `security-baseline.yaml` |
| CTL-INFRA-001 | Deny public HTTP access to `.opskeep/` at the web-server level (nested `.htaccess`, same pattern as `storage/.htaccess`) | developer | REQUIRED | EVD-002 / NOT_YET_BUILT | Not yet implemented — see THR-001 |
| CTL-AUTHZ-001 | New `/pricing`/`/case-studies` CMS entries reuse the existing owner-role-gated save handler unmodified | developer | REQUIRED | EVD-003 / NOT_YET_BUILT | Enforce during implementation + code review |
| CTL-INFRA-002 | Compress/optimize reference images before ship; keep non-page files out of the public docroot | developer | PLANNED | — | Address alongside the redesign build |
| CTL-MON-001 | External health check on `server.py` itself (not just the docroot), alerting on a Passenger-down fallback | site-owner | PLANNED | EVD-004 / NOT_YET_BUILT | Provider/owner still open, see DEC-001 |
| CTL-INPUT-001 | New CMS fields keep going through the existing HTML-escaping render path | developer | IMPLEMENTED | — | Pre-existing behavior; just don't bypass it |

## Identity, data, and boundaries

Public pages are served two ways: through `server.py` (CMS-token rendering, HTML-escaped) and, independently, directly by LiteSpeed for any docroot file `.htaccess` doesn't deny — these are separate trust boundaries (`TB-001` vs `TB-002`), and a file being "not routed by the app" does not mean "not public." The CMS write path (`TB-003`) is gated by the existing owner-role admin session check and a per-page key allowlist; this review keeps that mechanism exactly as-is rather than introducing a new one for the two new pages.

## Gates and evidence

- **GATE-001** (pre-implementation, blocking): baseline approval — `NOT_YET_BUILT`.
- **GATE-002** (pull request, blocking): docroot exposure fix (`CTL-INFRA-001`) verified before merge — `NOT_YET_BUILT`.
- **GATE-003** (pull request, blocking): new admin-write surface code review (`CTL-AUTHZ-001`, `CTL-INPUT-001`) — `NOT_YET_BUILT`.
- **GATE-004** (pre-deploy, non-blocking): production health monitoring configured (`CTL-MON-001`) — `NOT_YET_BUILT`.

## Known risks and open decisions

- **THR-001 (HIGH likelihood / MEDIUM impact, OPEN):** `.opskeep/project-gate/*` — including an interactive `dashboard.html` and the full decision/risk record — is publicly reachable once pulled to production, because `.htaccess`'s dot-file rule only blocks a file whose own name starts with a dot, not files inside a dot-prefixed directory. Fix is `CTL-INFRA-001`.
- **THR-002 (MEDIUM/HIGH, OPEN):** hand-editing `server.py` for the two new CMS pages could accidentally weaken the owner-only write check. Mitigation: reuse the existing generic handler unmodified (`CTL-AUTHZ-001`).
- **THR-003 (MEDIUM/LOW, OPEN):** oversized raw reference images sitting in the public docroot. Mitigation: compress before ship (`CTL-INFRA-002`).
- **THR-004 (MEDIUM/MEDIUM, OPEN):** Passenger losing its registration (observed twice already on this project) silently falls back to raw static serving with no alert. Mitigation: external liveness monitoring (`CTL-MON-001`).
- **DEC-001 (open, non-blocking):** who runs the Passenger health check and where alerts go.
- **DEC-002 (open, non-blocking):** whether to also exclude `.opskeep/` from the production deploy itself, beyond the `.htaccess` fix, as defense in depth.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or open a private security advisory for this repository. Do not include exploit details, credentials, or personal data in a public issue.

Include the affected version, reproduction steps, impact, and any suggested mitigation. The maintainers will acknowledge the report and provide status updates through the private advisory.
