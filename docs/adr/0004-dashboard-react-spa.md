# ADR 0004: Dashboard moves to a React SPA + JSON API — ADR 0003 superseded

- **Status:** Accepted
- **Date:** 2026-08-01
- **Supersedes:** [ADR 0003](0003-dashboard-stays-server-rendered.md)
- **Decision drivers:** After three in-stack design attempts (`fix/shadcn` Pico cleanup, `feat/UXUI`, and the [#134](https://github.com/ivzc07/agentg/issues/134)–[#139](https://github.com/ivzc07/agentg/issues/139) token redesign), the Owner judges the dashboard still reads competent-but-flat — short of the reference-level look ([#133](https://github.com/ivzc07/agentg/issues/133)). A design interview (2026-08-01) reopened ADR 0003 and resolved the shape of the move.

## Context

ADR 0003 rejected React, capped htmx, blessed f-strings, and defined "premium" as *interaction quality*, not a framework. Its revisit trigger was "a feature that genuinely needs client-held state; premium looks alone never does."

Three design pushes later, the visual bar still isn't met. Live screenshots of the running dashboard (seeded demo gym) show timid, near-black-on-black elevation, weak hierarchy, and hollow screens — the layered depth, gradient accents, and glow the spec promised do not read. Two findings sharpened the decision:

1. **The failure is repeated and evidenced**, not hypothetical. Bespoke CSS and even a full token redesign did not reach the bar. A utility-first + component ecosystem (Tailwind + shadcn/Radix) plus animation/charts is the lever that hasn't been tried, and it also unlocks the interaction/data-viz ceiling the product will want next.
2. **ADR 0003's cost objections were largely choices, not facts.** The dashboard runs inside the bot's aiohttp process; auth is a signed cookie; i18n is a server-side `STRINGS` dict; deploy is one container. The test suite is **~685 test functions, overwhelmingly domain logic** — only ~30–50 assertions touch HTML classes/structure. "Rewrite ~1130 HTML tests" was overstated: the domain/store layer and its tests are reusable behind an API.

## Decision

1. **Adopt a React SPA + JSON API for the dashboard, superseding ADR 0003.** The reopening trigger is met not by client-held state but by **repeated, evidenced failure of the in-stack approach to reach the product's visual bar** — recorded here as an additional, sufficient trigger alongside the original one.
2. **One process, one deploy (topology 4A).** JSON `/api/*` routes are added to the existing aiohttp app, reusing `DashboardStore`/`LinkingStore` directly; the Vite-built React bundle is served as static assets by that same app. The **existing signed session cookie** authenticates the API (same-origin). One container, one DB, one auth, one i18n source. No separate frontend service.
3. **Stack:** Vite + TypeScript + Tailwind + shadcn/ui (Radix) + lucide + TanStack Query + React Router + react-hook-form/zod + Framer Motion + Recharts. **No global-state library** — TanStack Query holds server state, `useState` covers the rest.
4. **i18n (7a):** the Python `STRINGS` dict remains the **single source of truth**, bootstrap-injected as `window.__I18N__` for the active language; cookie-based language selection is retained.
5. **Migration (5b):** parallel build to parity, **flag-gated cutover with instant rollback** (don't serve the bundle). The gate is **`DASHBOARD_SPA_ENABLED`** (with `DASHBOARD_SPA_DIST` locating the built bundle for container deploys); both are named in spec-dashboard §Stack too, so the ADR and the spec cannot drift apart on them. The **roster screen (all three views) is the pilot**, judged on *populated* data against the [#133](https://github.com/ivzc07/agentg/issues/133) reference before any fan-out. The other screens (member, routine editor, presets, settings, login) migrate after the pilot clears the look bar. htmx is retired screen-by-screen as it goes.

## Rationale

- **The interaction/aesthetic ceiling is now the honest argument, and three CSS attempts show it isn't reachable in-stack for this team.** shadcn/Tailwind raise the floor for the look; Recharts/Framer unlock charts and motion the product will want.
- **Topology 4A neutralizes two of ADR 0003's four cost objections and reduces a third.** 0003 priced React as: (a) a second toolchain and build step, (b) a JSON API with duplicated auth and i18n, (c) a rewrite of the ~1130-test suite, (d) two stacks to maintain. 4A removes (b) — the API reuses the existing signed cookie and the Python `STRINGS` dict — and largely removes (c), since the domain/store layer and its tests are unchanged and only the thin HTML web layer flips to returning JSON. It reduces (d) to one process, one container, one deploy. **(a) stands and is accepted**, recorded in Consequences below. The genuinely new surface is JSON serialization + the React app.
- **Pilot-first de-risks the aesthetic bet:** one screen proves toolchain + build + deploy + auth + look before the other four are touched. If the pilot fails the look bar, we iterate the pilot — not the stack decision.

## Consequences

- ADR 0003's "no React / no SPA / no JSON API" and htmx-cap clauses no longer bind; 0003 is marked superseded. htmx is removed as each screen migrates.
- A **frontend build step enters the repo** (Node/Vite), served as static assets from the same container — the "no build step" constraint from spec-dashboard §Stack is lifted by this ADR. **Spec-dashboard §Stack is amended in this change** (as ADR 0003 amended it in its own): the Rendering bullet now describes the SPA and cites this ADR, and the *In-place interactions* cluster is marked historical, since leaving them citing a superseded ADR as binding would contradict this decision. Both remain documented while the cutover flag is off.
- **Web-layer tests that assert rendered HTML/text are re-targeted to assert JSON responses**; a frontend test stack (Vitest + React Testing Library, plus a Playwright smoke) is added. Domain/store tests stay put.
- **URLs, the login-token flow, QR deep links, and the signed cookie are preserved** so the Telegram/bot integration is unaffected.
- **Revisit trigger for this ADR:** if React with this ecosystem *also* fails to beat the in-stack result on the roster pilot, reopen this decision rather than fan out.
