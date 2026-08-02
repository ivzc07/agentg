# ADR 0003: Dashboard stays server-rendered - React rejected, htmx capped, f-strings blessed

- **Status:** Superseded by [ADR 0004](0004-dashboard-react-spa.md) (2026-08-01) — the dashboard moves to a React SPA + JSON API after three in-stack design attempts fell short of the visual bar.
- **Date:** 2026-07-31
- **Decision drivers:** Owner explored a Node + React rebuild for a "premium" dashboard (shadcn/Svelte before it).
  A design interview (2026-07-31) resolved what premium concretely means and whether it justifies overturning the stack decision in [#84](https://github.com/ivzc07/agentg/issues/84) / spec-dashboard §Stack.

## Context

Spec-dashboard §Stack (#84) chose a dashboard living inside the bot's process: server-rendered HTML, no frontend build step, no SPA, no API layer, one container, one deploy.
The spec's text promised Jinja2 templates, but the code that shipped renders through typed Python f-string functions sharing one `_document()` shell - and ~1130 tests assert those HTML fragments.
Two restylings then competed: `fix/shadcn`, a light Pico CSS cleanup, and `feat/UXUI`, a research-backed dark redesign that also fixes usability (severity in words, shape-coded attendance grid, labeled editor fields, submit guards).
Against that backdrop the owner proposed rebuilding the frontend on Node + React.

## Decision

1. **#84 stands - no React, no SPA, no JSON API layer.**
   "Premium" is defined as interaction quality, not a framework: instant roster search, saves that never lose the Coach's typed work or scroll position, no duplicate side effects from double submits, visible success feedback, and full usability at 375px.
   None of these requires client-held state.
2. **Revisit trigger:** the day a feature genuinely needs client-held state (e.g. a live drag-and-drop program builder), that feature's ADR reopens this decision.
   "Premium looks" alone never does.
3. **`feat/UXUI` merges as the dashboard's base** - dark-only, with the responsive bar verified at 375px (Table and Cards fully usable, header wraps deliberately, names ellipsize, Split stacks the rail above the pane instead of faking a desktop layout).
   A light theme waits for a real coach-reported legibility problem; the token layer makes it a swap, not a redesign.
4. **`fix/shadcn` is retired unmerged.**
   Its one keeper - stylesheets vendored into the package and served browser-cacheable from `/static/` instead of a large inline `<style>` block - lands as a small follow-up on top of the merged base.
5. **htmx, vendored into `/static/`, is the one sanctioned mechanism for new partial-page interactivity.**
   Hard cap: endpoints keep returning HTML fragments from the existing renderers - no client templating, no JSON endpoints, no build step.
   The existing vanilla snippets (search filter, copy buttons, typed confirm, submit guard) stay as they are.
6. **F-strings are blessed as the rendering layer.**
   The spec's Jinja2 line is dropped; no template-engine migration.

## Rationale

- **The interaction ceiling was the only honest argument for React, and it isn't reached.**
  Every capability behind "premium" is deliverable with fragment-returning endpoints plus htmx; feat/UXUI already delivers the visual half inside the constraint.
- **React's real price bought nothing current features need:** a second toolchain and build step, a JSON API layer with duplicated auth and i18n, a rewrite of a ~1130-test suite that asserts server HTML, and two stacks to maintain for single-digit gyms.
- **F-strings over Jinja2:** the renderers are typed, pure, directly unit-tested functions.
  A migration would churn the whole test suite for zero user-visible payoff, and htmx needs exactly the small composable fragment renderers these already are.
- **One researched theme done properly beats two half-QA'd ones** at this stage of the product.

## Consequences

- Spec-dashboard §Stack is amended in this change: the Jinja2 line is replaced by f-string renderers plus vendored htmx, citing this ADR.
- The `fix/shadcn` branch is closed without merging.
- Follow-up order: 1. this merge, 2. the `/static/` stylesheet extraction, 3. the htmx interaction cluster (in-place editor saves, success feedback, a live roster count) - behavior-changing, multi-session work that gets its own spec and tickets.
  The merged editor already renders a spare block per unused weekday, so a whole week is one save and one Telegram notice.
- Anyone proposing a frontend framework here again starts from the revisit trigger in this ADR.
