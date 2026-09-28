# components.md — Buinee landing redesign

Components the Studio Green direction needs. Token names refer to
`tokens.json`. "Legacy" = existing component from the current `index.html`,
restyled to the new tokens per PG-008; "New" = introduced by this redesign.

## Navigation

- **Top pill nav** (existing shell, restyled). Sticky, `color.surface`
  background with blur on light pages, `color_dark.surface` on the dark hero
  before scroll if the hero nav starts transparent-on-dark (match mockup).
  Radius: pill (`radius.lg` rounded to 999px for the nav bar itself).
- **Nav link** — text uses `color.text`/`color_dark.text`; active/hover uses
  `color.primary`/`color_dark.primary`. Variants: anchor link (Home, Services,
  How It Works, Industries) vs page link (Pricing, Case Studies) — same visual
  treatment, different `href` target.
- **Mobile drawer** (legacy, restyled) — full-height slide-out, same
  `.drawer`/`.drawer-links` structure as today, new tokens.
- **Primary CTA button** ("Get Started" in nav) — filled `color.primary` /
  `color_dark.primary`, pill radius, `primary_contrast` text.

## Data display

- **Service card** (New) — icon badge (tinted chip, one accent-tinted
  background per service per the ad creative's colour-coded icons), title,
  one-line description, arrow-link affordance. Grid: 4 columns desktop, 2
  tablet, 1 mobile. Uses `radius.md` (14px), `color.surface` background,
  `color.border` hairline.
- **Step card** (New, "How It Works") — numbered badge (01-04) in
  `color.primary`, title, description. Horizontal connector on desktop
  (chevron/arrow between steps), stacked with a vertical line on mobile.
- **Industry card** (Legacy-ish, restyled) — photo + label + short use-case
  list. Used in both the trusted-by strip (icon-only variant) and the "Used
  by businesses like yours" section (photo variant).
- **Stat tile** (Legacy, restyled) — big number + label, used in the
  testimonial band (hours saved, productivity, etc.) and optionally the
  pricing page.
- **Pricing card** (Legacy, restyled) — tier name, price, "most popular"
  badge variant, feature checklist (reuses the existing `CMS_CHECK_SVG`
  bullet rendering), CTA button. Three variants: Free, Starter (highlighted /
  "most popular" — uses `color.primary` border or badge), Growth.
- **Case study card** (New) — industry tag chip, descriptor, one-line result,
  link. Empty-state variant: centered prompt + CTA, not a bare blank grid.
- **Integrations logo strip** (Legacy, restyled) — horizontal marquee of
  partner/tool logos, unchanged behaviour, new surface tokens.
- **Workspace demo panel** (Legacy, restyled) — the invoice-approval demo
  card with its approval-trail timeline; keep the timeline component as-is,
  just re-skin colours/radius.

## Forms

- **Currency toggle** (Legacy) — GHS/USD segmented control on pricing
  content, reused as-is on the new `/pricing` page.
- **Newsletter/contact inputs** — if present in the footer or CTA band,
  reuse existing input styling with new border/focus tokens.

## Feedback

- **Loading state** — skeleton blocks or a spinner for `/api/plans` and
  `/api/exchange-rate` on Home's pricing teaser and on `/pricing`.
- **Error state** — inline text, `color.danger`, e.g. "Couldn't load pricing
  just now, please refresh." (existing copy, keep it).
- **Toast/inline confirmation** — not needed for this redesign; nothing here
  is destructive (PG-011).

## Overlays

- **FAQ accordion item** (Legacy, restyled) — `<details>`-based, keep the
  existing expand/collapse chevron icon, re-skin border/text tokens. Sourced
  from the `qa_list` CMS field type, unchanged parsing logic
  (`_render_cms_value`).
- **Mobile nav drawer** — see Navigation above; functions as an overlay on
  small screens.

## Components carrying the character

The **service card grid** and the **dark hero band with the composited
device/photo image** are the two components that most define this direction
— get their spacing, icon-badge colour variety, and photo/device composition
right before polishing anything else. The **pricing card** and **FAQ
accordion** carry the least new visual risk since they're direct restyles of
components that already work today.
