# screens.md — Buinee landing redesign

Screen inventory for the Studio Green direction. "Screen" here means a routed
page in `STATIC_PAGES` (`server.py`). Home is a single scrolling page with
anchor sections; Pricing and Case Studies are new standalone pages (PG-006).

## Navigation map

Top pill nav, same shell on every page in this set:

```
buinee.app          Home  Services  How It Works  Pricing  Industries  Case Studies      [Get Started]
```

- Home, Services, How It Works, Industries -> anchor links (`#services`,
  `#how`, `#industries`) on `/` (PG-005).
- Pricing -> `/pricing` (new page).
- Case Studies -> `/case-studies` (new page).
- `Get Started` -> existing `/register` flow, unchanged.

Mobile: nav collapses into the existing slide-out drawer pattern already used
on the current site (`.drawer`), restyled to the new tokens, same mechanism,
new colours/type.

## Screen: Home (`/`)

Purpose: convert an evaluating SMB owner into a registration.
Primary job: "What does Buinee actually do for a business like mine, and can
I trust it?"
Layout pattern: dark hero band, then light content sections, then a dark
testimonial/CTA band, then a dark footer.

Sections, top to bottom:

1. Hero (dark, `color_dark` tokens) — eyebrow, headline ("Stop spending time
   formatting and doing reports." per the mockup, via CMS token), sub-copy,
   primary CTA ("Get Muse for Your Business" style) plus secondary ("See How
   It Works"), trust checkmarks row, `hero-image1.png` composited with
   laptop/phone dashboard mockups and WhatsApp/Instagram/Facebook badges
   (PG-015).
2. Trusted-by / industries strip (light) — icon row: Retail & Shops,
   Restaurants, Health & Clinics, Schools & Institutions, Churches, Hotels &
   Travel, Professional Services.
3. Services (`#services`, light) — 8-card grid, one card per service, each
   with an icon badge, title, one-line description, arrow link (PG-002,
   PG-017). Muse's card can carry a "Flagship" tag but uses the same card
   shape as the rest.
4. How It Works (`#how`, light) — 4 numbered steps, horizontal on desktop,
   stacked on mobile (PG-018).
5. Used by businesses like yours (light) — 6 industry photo cards (Retail,
   Restaurants & Cafés, Health & Clinics, Schools & Institutions, Churches,
   Hotels & Travel), each with 2-3 example use cases.
6. Testimonial band (dark) — quote, attribution ("Business Owner, Accra,
   Ghana" per the mockup), 4 stat tiles (hours saved, productivity, happier
   customers, organized business), carousel dots.
7. Legacy sections, restyled and kept (PG-008), in this order beneath the
   above: integrations logo strip (existing marquee), the detailed
   invoice-approval workspace demo (existing `#workspace` content), FAQ
   accordion (existing `#faq` content, `qa_list` CMS field type).
8. Final CTA band (light mint tint, matches the mockup's "Ready to work
   smarter?" band) — headline, sub-copy, primary CTA.
9. Footer (dark) — logo plus tagline, Services/Company/Support link columns,
   social icons, copyright.

States: the "Available in your dashboard once your company is registered"
assistant-preview card in the hero keeps its existing logged-out placeholder
behaviour. Pricing tiles, if a live pricing teaser stays on this page, reuse
the existing `/api/plans`-driven loading/error/populated states already in
`index.html` — "Couldn't load pricing just now, please refresh." on error,
otherwise render tiers.

Entry points: direct traffic, ads (the ad creative in `buinee-ad.png` links
back here), social.
Exit points: `/register`, `/pricing`, `/case-studies`, `/contact`, social
links in the footer.

## Screen: Pricing (`/pricing`)

Purpose: let a visitor who already understands the product choose a tier
without re-reading the whole home page.
Primary job: compare Free / Starter / Growth and start registration at the
right tier.
Layout pattern: short header (reuses hero band styling, condensed), then the
full pricing table/cards (same content as the current home-page pricing
section, promoted to its own page), then FAQ items relevant to billing (a
subset of the FAQ, or a link back to `/#faq`), then the final CTA band.
Key components: GHS/USD currency toggle (existing), 3 pricing cards, "Most
popular" badge on Starter (per current site).
States: loading (skeleton or spinner while `/api/plans` and
`/api/exchange-rate` resolve), error ("Couldn't load pricing just now, please
refresh."), populated.
Entry points: nav, home page CTA, direct link from ads/quotes.
Exit points: `/register?plan=...`, `/contact`.
New in `server.py`: `STATIC_PAGES["/pricing"] = "pricing.html"`,
`CMS_PAGE_ROUTES["/pricing"] = "pricing"`, new `SITE_CONTENT_SCHEMA["pricing"]`
entries for header eyebrow/headline/subtext.

## Screen: Case Studies (`/case-studies`)

Purpose: proof, for a visitor who wants evidence before registering.
Primary job: "Has this worked for a business like mine?"
Layout pattern: short header (same condensed hero pattern as Pricing), then a
grid of case study cards (industry tag, business name/location or anonymized
descriptor, one-line result, "Read more" — a detail view is out of scope for
this pass, cards can link to `/contact` or stay as summary cards if no detail
template exists yet), then a testimonial band (can reuse the home page's
testimonial component with different quotes), then the final CTA band.
States: empty (no case studies published yet — show a "We're gathering our
first stories, want to be one of them?" prompt with a CTA, don't ship a blank
page), populated.
Entry points: nav, home page testimonial band "read more" if added.
Exit points: `/contact`, `/register`.
New in `server.py`: `STATIC_PAGES["/case-studies"] = "case-studies.html"`,
`CMS_PAGE_ROUTES["/case-studies"] = "case_studies"`, new
`SITE_CONTENT_SCHEMA["case_studies"]` entries, likely a `qa_list`-style or
repeating-card CMS field for the case entries themselves.

## Unchanged screens

Registration, login, dashboard, admin (`admin-*.html`), legal pages, contact,
payment success/failure — out of scope for this redesign (PG-001). Only
touch point: the site-wide nav/footer chrome should read consistently if this
redesign updates shared header/footer partials, but functional behaviour on
these pages does not change.
