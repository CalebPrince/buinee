# accessibility.md — Buinee landing redesign

WCAG 2.2 AA is the baseline for every screen in this package (PG-013). No
additional access requirement (screen-reader priority, RTL, low-vision) was
raised for this project, so AA is also the ceiling for this pass — don't
over-build beyond it, but don't ship under it either.

## Contrast (validated in tokens.json)

- Light sections: `color.text` (#0b1210) on `color.bg`/`color.surface`
  passes comfortably. `color.primary` (#087a4b) on white is 5.39:1 for text
  use — safe for links and small text. `color.primary_contrast` (#ffffff) on
  `color.primary` is 5.39:1 for button labels.
- Dark sections: `color_dark.text` (#eef4f1) on `color_dark.bg` (#000b0f)
  passes comfortably. `color_dark.primary` (#27f997, the bright mint) is
  for buttons/badges/icons on dark surfaces, where its contrast against
  `color_dark.bg` is high; `color_dark.primary_contrast` (#00150d) is the
  correct label colour on top of a bright-mint-filled button.
- **Rule, not a suggestion:** the bright mint (#27f997 / `color_dark.primary`)
  must never carry body text or small UI text on a light/white background —
  it fails 4.5:1 there. This was the exact defect `gate.py validate` caught
  and forced a fix for during this project's gate (see PG-003, R-03).
- Any new colour introduced during implementation (a 5th service-card icon
  tint, say) must be checked against its actual background before shipping,
  not assumed safe because it's "close to" an approved token.

## Keyboard

- Every interactive element (nav links, drawer toggle, service card links,
  FAQ accordion headers, pricing CTAs, carousel controls, currency toggle)
  is reachable via Tab in a logical order and operable via Enter/Space.
- The mobile drawer traps focus while open and returns focus to its trigger
  on close, matching the current site's existing drawer behaviour.
- The testimonial carousel is operable via keyboard (arrow keys or
  focusable dot controls), not mouse/touch-only.

## Focus style

- Visible focus ring on every interactive element, using the existing
  site-wide focus treatment (don't remove `outline` without replacing it).
  Focus ring must itself be visible against both light and dark section
  backgrounds — verify it isn't the same near-invisible mint-on-dark or
  green-on-white combination the contrast rule above warns against.

## Labels and semantics

- Service card icons are decorative (`aria-hidden`); the card's accessible
  name comes from its title text, not the icon.
- FAQ accordion uses native `<details>/<summary>` (as today) so
  expand/collapse state is exposed to assistive tech for free — don't
  replace this with a custom div-based accordion.
- Pricing cards: the "Most popular" badge must not be the only signal —
  it should also be reflected in accessible text (e.g. visually-hidden
  "(most popular)" appended to the tier name for screen readers), not just a
  colour/position difference.
- Case study "empty state" prompt is a real, announced message (not just a
  blank grid), so a screen-reader user isn't left wondering if the page
  failed to load.
- Images: `hero-image1.png` and industry photos get meaningful `alt` text
  (not empty, not the filename); purely decorative background images get
  empty `alt=""`.

## Error messaging

- Pricing/exchange-rate load failures keep the existing plain-language error
  copy ("Couldn't load pricing just now, please refresh.") and it must be
  programmatically associated with the region it describes (e.g. via
  `aria-live="polite"` on the pricing panel), not just visually placed near
  it.

## Reduced motion

- Any entrance/hover animation introduced by this redesign (rule 12 in
  DESIGN.md: "subtle entrance/hover only") respects
  `prefers-reduced-motion: reduce` — fall back to an instant state change,
  no parallax or scroll-triggered animation that can't be disabled.

## Verify before release

- [ ] Run an automated contrast/AA check (e.g. axe or Lighthouse) against
      Home, Pricing and Case Studies in both the light sections and the dark
      hero/footer/testimonial bands.
- [ ] Full keyboard walkthrough of all three pages, including the mobile
      drawer and FAQ accordion.
- [ ] Screen-reader spot check (VoiceOver or NVDA) of the hero, service
      grid, pricing cards and FAQ accordion.
- [ ] Confirm no bright-mint-on-white or deep-green-on-near-black text pairs
      shipped anywhere (the one contrast mistake this palette makes easy).
- [ ] Confirm the case-studies empty state is present and doesn't ship as a
      blank page if no case studies are published at launch.
