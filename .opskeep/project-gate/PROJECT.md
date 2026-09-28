# Buinee — AI Solutions Studio landing page — Project brief

Reposition buinee.app from a standalone AI assistant to an AI implementation studio that sets up and customizes tools clients already use (Muse and others) — new landing page identity, structure and copy.

## Intent

- **Product:** Public marketing site for an AI implementation/integration studio (buinee.app)
- **Primary user:** Ghana-based SMB owners and operators evaluating whether to hire Buinee
- **Usage:** Occasional, evaluation-driven visits before registering a company
- **Primary job:** Understand the services on offer, see proof it works, and start onboarding
- **Platform:** Responsive marketing website — redesign of the existing single-page buinee.app landing page, not a new product

## Product character

- Trustworthy: 85%
- Operational / proof-led: 80%
- Bold: 65%
- Approachable: 60%
- Minimal: 40%

## Chosen direction: Studio Green

The supplied mockup, built out: mint identity, dark photographic hero, services-first structure.

Directly matches the reference the user supplied — new logo, new accent, new headline system, new services-grid framing. Lowest translation risk between what was approved and what gets shipped.

## Users & jobs

- **PG-009 Primary audience: Ghana-based SMB owners/operators** — Retail, restaurants, health/clinics, schools, churches, hotels and professional-services owners — matches the mockup's "Trusted by local businesses" industry strip, GHS-denominated pricing, and the Accra-attributed testimonial.
- **PG-010 Occasional, evaluation-driven visits** — Visitors browse a handful of times to decide, then register once — not a daily-use surface.

## Project

- **PG-001 Marketing-site redesign, not a new product** — This redesigns buinee.app's public landing page (index.html) and its CMS copy schema. The underlying product — registration, dashboard, billing, admin — is unchanged.
- **PG-002 Muse is the flagship service, not the only one** — Muse Personal Assistant leads the services list as the flagship integration, alongside WhatsApp Automation, Social Media Management, Business Dashboards, Customer Support Agents, Document & Data Automation, Scheduling & Reminders, and Custom AI Solutions.
- **PG-016 Rights to display Muse's name and logo** — Yes, rights confirmed
- **PG-007 Keep the CMS-editable text system** — All new headline/body copy is still routed through the existing {{cms:...}} token system, with SITE_CONTENT_SCHEMA in server.py updated to match the new sections, so the admin Site Contents page keeps working for the new copy.

## Risks

- (high) Using Muse's name/logo without confirmed rights is a real legal exposure — blocked on PG-016.
- (med) Scope of Pricing/Case Studies (anchor vs subpage, PG-006) changes build effort meaningfully — subpages mean new routes, new CMS schema pages, new SITE_CONTENT_SCHEMA entries.
- (low) The bright mint (#27F997) fails text contrast on white and must only be used for large text, buttons or dark-surface contexts — the deeper #0e9f63 carries body text and links.
- (low) Threading the new identity through sections the mockup didn't render (pricing cards, FAQ, integrations strip) means some visual decisions will be extrapolated, not directly copied.

