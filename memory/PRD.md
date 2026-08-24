# AstroAI Product Requirements

## Original problem statement
Build a production-quality, mobile-first AstroAI web application combining Vedic astrology, Kundli generation, AI interpretation, palm reading, chat, authentication, dashboard, privacy, responsible AI, and pricing UI. The initial MVP must prioritize working user flows over a marketing-only page.

## Architecture decisions
- React 19 + React Router-compatible view state for the mobile-first frontend.
- FastAPI + MongoDB with UUID application identifiers and projections excluding MongoDB `_id`.
- JWT email/password authentication with bcrypt hashing and a simple seven-day session.
- `AstrologyService`, `PalmReadingService`, `AstrologyInterpretationService`, and `KundliChatService` isolate replaceable integrations.
- GPT-5.4 through the Emergent universal key is used server-side for interpretation, chat, and palm vision.
- Astrology calculations are explicitly marked development/mock data until a verified astronomical engine is connected.

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
- Palm upload preview, hand selection, AI service boundary, result presentation, and marked fallback.
- Kundli form, development calculation engine, chart summary, planetary positions, dashas, yogas, and AI interpretation.
- Chart-grounded Ask Your Kundli chat with persisted conversation records.
- Compatibility and pricing surfaces clearly framed as next-phase/coming soon where not connected.

## Prioritized backlog
- P0: Connect verified astronomical/Jyotish calculation engine and validate planetary positions.
- P0: Add secure object storage, image retention controls, account deletion, and production secret management.
- P1: Add real location autocomplete/timezone lookup, PDF reports, Google login, and compatibility calculations.
- P1: Add admin analytics and moderation/error observability.
- P2: Add Stripe premium checkout and richer report history.

## Remaining next tasks
1. Replace `AstrologyService` development seed calculations with a verified ephemeris provider.
2. Add secure image storage and delete-account endpoint.
3. Add report generation/download and real compatibility flow.