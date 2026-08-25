# AstroAI Product Requirements

## Original problem statement
Build a production-quality, mobile-first AstroAI web application combining Vedic astrology, Kundli generation, AI interpretation, palm reading, chat, authentication, dashboard, privacy, responsible AI, and pricing UI. The initial MVP must prioritize working user flows over a marketing-only page.

## Architecture decisions
- React 19 + React Router-compatible view state for the mobile-first frontend.
- FastAPI + MongoDB with UUID application identifiers and projections excluding MongoDB `_id`.
- JWT email/password authentication with bcrypt hashing and a simple seven-day session.
- `AstrologyService`, `PalmReadingService`, `AstrologyInterpretationService`, and `KundliChatService` isolate replaceable integrations.
- GPT-5.4 through the Emergent universal key is used server-side for interpretation, chat, and palm vision.
- Swiss Ephemeris is the server-side calculation engine using Lahiri sidereal mode; AI receives only its structured output.

## User personas
- Curious first-time visitor exploring a palm reading or online Kundli.
- Returning user who wants a private chart, readings, and chart-grounded questions.

## Core requirements
- Homepage with palm and Kundli entry points, responsible astrology messaging, pricing, and privacy cues.
- Authenticated Kundli, palm reading, interpretation, chat, and dashboard flows.
- Sensitive data remains server-side and users are warned about traditional/non-scientific interpretation.

## Implemented (2025-02-14)
- Premium dark mystic visual system with responsive navigation and mobile layouts.
- Email/password registration and login, JWT-protected API endpoints, and dashboard.
- Palm upload preview, strict JPG/PNG/WEBP validation, hand selection, server-side GPT-5.4 vision service, result presentation, and honest 4xx/5xx failure states.
- Kundli form, geocoded birthplace/timezone resolution, Swiss Ephemeris chart calculation, chart summary, planetary positions, houses, nakshatra, Vimshottari schedule, and chart-grounded AI interpretation.
- Chart-grounded Ask Your Kundli chat with persisted conversation records.
- Compatibility and pricing surfaces clearly framed as next-phase/coming soon where not connected.

## Implemented (2025-02-15)
- Full E2E verified with live GPT-5.4 (Emergent LLM key) after credit recharge — /api/kundli, /api/palm, /api/chat all return 200/422 with no 503s.
- Mobile responsive audit — Kundli result and Home now measure clientWidth=scrollWidth at 375px (no horizontal overflow).
- In-app toast (data-testid=`app-toast`) replaces native `alert()` for /api/kundli and /api/palm errors.
- Signed-out palm/kundli submissions now open the Auth modal instead of failing silently.
- Home expanded with How It Works, Compatibility teaser, Sample Reports, Pricing cards, FAQ (matches PRD sections).

## Prioritized backlog
- P0: Add ephemeris data-file management and golden chart fixtures for ongoing astronomical regression validation.
- P0: Add secure object storage, image retention controls, account deletion, and production secret management.
- P1: Add real location autocomplete/timezone lookup, PDF reports, Google login, and compatibility calculations.
- P1: Add admin analytics and moderation/error observability.
- P2: Add Stripe premium checkout and richer report history.

## Remaining next tasks
1. Add ephemeris data-file management and golden chart fixtures for ongoing astronomical regression validation.
2. Add secure image storage and delete-account endpoint.
3. Add report generation/download and real compatibility flow.