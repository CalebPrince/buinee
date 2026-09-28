# responsive.md — Buinee landing redesign

Mobile strategy: full parity (PG-012). Nothing is hidden, degraded, or
made read-only on a phone — every section on Home, Pricing and Case Studies
works the same, just reflowed.

## Breakpoints

Match the current site's existing breakpoints rather than inventing new ones:

- Desktop: >= 1024px — full multi-column layouts (4-col service grid,
  horizontal how-it-works, multi-column footer).
- Tablet: 641px - 1023px — 2-column service grid, 2-column industry cards,
  nav may already need the drawer depending on how much the new nav items
  (6 links + CTA) fit; test at 768px specifically since that's tight with 6
  nav items.
- Mobile: <= 640px — single column throughout, drawer nav, stacked
  how-it-works with a vertical connector line instead of horizontal arrows.

## Navigation adaptation

- >= the point where the 6 nav links + logo + CTA stop fitting on one line
  (test early — "How It Works" and "Case Studies" are the two longest
  labels), collapse straight to the hamburger/drawer pattern already used on
  the current site. Don't truncate labels or wrap the nav bar to two lines.
- Drawer content: same 6 links plus the CTA button, full height, same
  slide-in pattern as today.

## Section-by-section mobile behaviour

- **Hero:** headline and sub-copy stack above the device/photo composite
  image; the composite image scales down as one unit (don't try to
  re-arrange the phone/laptop/badges independently on small screens — treat
  `hero-image1.png`'s composition as a single responsive image asset, or a
  simplified mobile-specific crop if the full composite is too busy at phone
  width). CTAs stack full-width.
- **Services grid:** 4 -> 2 -> 1 columns across the three breakpoints.
- **How It Works:** horizontal 4-step row -> stacked steps with a vertical
  connector on mobile.
- **Industry strip / "Used by" cards:** horizontal scroll or wrap to 2 -> 1
  columns; icon strip can go horizontally scrollable on mobile if 7 items
  don't wrap cleanly.
- **Testimonial band:** stat tiles go 2x2 -> 1 column; carousel dots stay
  functional via touch swipe.
- **Pricing cards (`/pricing` and any home teaser):** 3 columns -> 1 column,
  stacked, "most popular" badge stays visible at the top of its card.
- **Case study grid:** multi-column -> 1 column.
- **Footer:** multi-column link groups -> stacked, social icons row stays
  horizontal.

## Touch targets

All interactive elements (nav links in the drawer, service card arrow links,
FAQ accordion headers, pricing CTA buttons, carousel dots) meet at least
44x44px touch target size on mobile, consistent with the existing site's
button sizing.

## Slow-network behaviour

No explicit low-bandwidth requirement was raised for this project (PG's
`x_access` was not flagged beyond baseline), but since this is a public
marketing page reachable over mobile data in Ghana:

- `hero-image1.png` and other photo assets should be served at reasonable
  web-optimized sizes/formats (WebP with a fallback), not the raw multi-MB
  source PNGs currently sitting at the project root.
- Pricing/exchange-rate API calls already have a loading and error state
  (see screens.md) — keep both visible states rather than a silent
  indefinite spinner.
