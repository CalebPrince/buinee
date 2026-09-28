# DESIGN.md — Buinee landing redesign (source of truth)

Read this before touching `index.html`, `server.py`'s `SITE_CONTENT_SCHEMA`/CMS
rendering, or any new page created by this redesign. Do not edit `decisions.yaml`,
`tokens.json` or `state.json` by hand — reopen a decision with `gate.py reopen`
instead and re-run this package's workflow.

## 1. Product, in one paragraph

Buinee is an AI implementation studio for Ghana-based SMB owners and operators
(retail, restaurants, clinics, schools, churches, hotels, professional services).
It sets up, integrates and customizes AI tools those businesses already use or
want — Muse Personal Assistant is the flagship offering, alongside WhatsApp
automation, social media management, dashboards, customer support agents,
document/data automation, scheduling, and bespoke AI solutions. The public
landing page's job is to get an owner who is evaluating options to understand
what's on offer, see proof it works, and register a company. (PG-001, PG-002,
PG-009)

## 2. Direction: Studio Green

**Tagline:** The supplied mockup, built out — mint identity, dark photographic
hero, services-first structure.

**Why it fits:** it is a direct build-out of the reference material the user
supplied (`Buinee-landing page.png`, `buinee-ad.png`, `hero-image1.png`,
`buinee-logo.png`) rather than an approximation of it. Every accent colour and
type choice below was pixel-sampled from those files, not guessed. (PG-003,
PG-004, PG-014, PG-015)

**What it deliberately is not:** it is not a copy-only reframe of the current
teal/serif/cream identity (that was direction B, "Minimal Reframe", and was not
chosen). Do not blend the two — the current site's `--teal`, `--teal-deep`,
`--teal-soft` and serif display font are retired for the sections this redesign
touches.

## 3. Foundation table

| | |
|---|---|
| Theme | Light sections (white/`#f8f9fa`) + dark hero/footer/testimonial band (near-black, faint green cast) |
| Navigation | Top pill nav, anchor links on the home page (Home/Services/How It Works/Industries); Pricing and Case Studies are separate pages, linked from the same nav |
| Content width | Centered, ~1200px max |
| Density | Medium, generous section padding |
| Radius | 8px small controls, 14px cards, 999px pills (buttons, badges, nav) |
| Typography | Bold grotesque sans for headlines; current body sans retained |
| Primary interaction | Service cards (8-card grid) + numbered how-it-works steps + proof/testimonial band |
| Mobile strategy | Full single-column reflow, no hidden content |
| Animation | Subtle entrance/hover only — no decorative motion |
| Icons | Rounded line icons inside tinted colour-chip badges, matching the ad creative |

## 4. Rules

1. Headlines use the bold sans stack, never the current `--serif` token. (PG-004)
2. Body text, links and small UI text on light backgrounds use `color.primary`
   (`#087a4b`) or `color.text`/`color.muted` — never the bright mint
   (`#27f997`) directly; that value is for buttons, badges, icon fills and
   dark-surface accents only, since it fails 4.5:1 text contrast on white.
   (PG-003, PG-013, R-03 in `state.json`)
3. Dark sections (hero, footer, testimonial band) use `color_dark.bg`
   (`#000b0f`) with `color_dark.primary` (`#27f997`) as their accent — this is
   where the bright mint belongs. (PG-003, PG-014)
4. The hero image is `hero-image1.png` as supplied, not a redrawn illustration
   or stock photo swap. (PG-015)
5. The services grid has exactly 8 cards, in this order: Muse Personal
   Assistant, WhatsApp Automation, Social Media Management, Business
   Dashboards, Customer Support Agents, Document & Data Automation, Scheduling
   & Reminders, Custom AI Solutions. Muse is visually first but styled the same
   as the other 7 — not larger, not in a different card shape. (PG-002, PG-017)
6. "How it works" is exactly 4 numbered steps: Understand Your Business → Set
   Up & Integrate → Customize Workflows → You Stay in Control. (PG-018)
7. Every new string of copy is added as a `cms:key`-style double-brace token in the relevant
   page's HTML and a matching entry in `SITE_CONTENT_SCHEMA` in `server.py`
   (see `render_site_content`), the same way the current page already works.
   No hardcoded marketing copy. (PG-007)
8. The current single-page anchor architecture is preserved for
   Home/Services/How It Works/Industries. Do not split these into subpages.
   (PG-005)
9. Pricing and Case Studies become real subpages (`/pricing`, `/case-studies`)
   with their own `STATIC_PAGES` entries, their own `SITE_CONTENT_SCHEMA`
   pages, and their own nav links — not anchors on the home page. (PG-006)
10. The Muse name and logo mark may be reproduced as shown in the mockup —
    rights are confirmed. Do not hedge copy to avoid naming Muse. (PG-016)
11. The current integrations logo strip, the invoice-approval workspace demo,
    and the FAQ accordion are restyled to the new tokens and kept — positioned
    below the new Services/How-it-works/Industries sections, not deleted.
    (PG-008)
12. One primary CTA per section maximum; secondary actions use the ghost/
    outline button style, never a second filled-primary button competing for
    attention.
13. Every section reflows to single-column on mobile with no hidden content
    and no horizontal scroll. (PG-012)
14. No destructive or irreversible action exists anywhere on this page;
    registration/payment flows are out of scope and unchanged. (PG-011)
15. Full keyboard operability and visible focus rings on every interactive
    element, including the new service cards and FAQ accordion items.
    (PG-013)

## 5. Do / don't

- **Do** derive every colour from `tokens.json`. **Don't** hand-pick a nearby
  green from the mockup image for a one-off element — use the deep
  `color.primary` / bright `color_dark.primary` pair consistently so contrast
  stays valid everywhere.
- **Do** keep dark-section text on the `color_dark` palette and light-section
  text on `color`. **Don't** mix them (e.g. bright mint text on a white
  section, or the deep contrast-safe green on a near-black section — it reads
  muddy and was never validated for that pairing).
- **Do** treat the mockup as authoritative for the sections it renders (hero,
  services grid, how-it-works, industries strip, testimonial, CTA, footer).
  **Don't** invent new sections it doesn't show, beyond the explicitly-kept
  legacy sections in rule 11.

## 6. Tokens

See `tokens.json` in this directory — `color` for light sections, `color_dark`
for dark sections, plus `radius`, `font`, `space`, `density`. Do not restate or
duplicate these values in CSS as literals; wire them as CSS custom properties
the same way the current `:root` block does.

## 7. Change process

Do not edit `decisions.yaml`, `tokens.json`, `state.json` or this file by hand
to reflect a new decision. Reopen the specific decision with
`python gate.py reopen --id PG-XXX --reason "..."`, go back through the
dashboard, re-apply, and re-seal. Any coding agent should run
`python gate.py verify` before making UI changes and treat a failed verify as
a sign this file is stale.
