# Redesign acceptance sweep — results (issue #140)

Closing verification gate of the [#133 redesign](../spec-dashboard.md).
Run: `uv run pytest tests/test_redesign_acceptance_sweep.py -v`

## Gate results

| Gate | Tests | Status | Notes |
|---|---|---|---|
| AA contrast (>= 4.5:1) | 7 | ✅ PASS | One low-impact gap filed (#156) |
| 375px bar | 5 | ✅ PASS | CSS structural checks only; browser verification deferred (#157) |
| Reduced motion | 7 | ✅ PASS | Universal `*` selector + `!important` |
| htmx swap parity | 4 | ✅ PASS | Editor fragment vs full page structural match |
| Screen coverage | 9 | ✅ PASS | Every screen renders, member page + editor included |

**Total: 32 checks, all pass.** Full suite: 1178 passed, 2 skipped.

## Per-screen summary

### Roster — Table (`GET /?view=table`)

- **AA contrast**: All text/surface pairs pass. `--ink` on `--bg`, `--ink-2` on `--elevation-1-bg`, `--ink-3` on surfaces — all >= 4.5:1.
- **375px**: Media query at 500px stacks rows, shrinks tiles to 36px, numerals to 20px. Search flexes to full width below 700px.
- **Reduced motion**: Row hover `transform: translateY(-1px)`, `transition` all zeroed by the `prefers-reduced-motion` block.
- **htmx swap**: N/A — Table is not an htmx surface.

### Roster — Cards (`GET /?view=cards`)

- **AA contrast**: Band headers (coral/amber/magenta/cyan on their tints) all pass AA. Card surfaces (`--elevation-1-bg`) carry `--ink` text at 18.3:1.
- **375px**: Grid collapses to single column at ≤ 500px. Day grid squares shrink to 16px.
- **Reduced motion**: Card hover lift + glow transition zeroed.
- **htmx swap**: N/A.

### Roster — Split (`GET /?view=split`)

- **AA contrast**: Same as Table (rail uses the same row components).
- **375px**: Below 899px, split stacks to single column — rail collapses to 45vh max with scroll, pane below.
- **Reduced motion**: Same as Table rows.
- **htmx swap**: N/A.

### Member page (`GET /members/{id}?view=table`)

- **AA contrast**: Hero `h1` at `--ink` on `--bg` (21:1). Chips on surface. Safety banner: black text on white (21:1). Numerals (amber/coral) pass on `--bg`.
- **375px**: Media query at 420px stacks columns, shrinks numerals to 24px, safety banner buttons go full-width.
- **Reduced motion**: All transitions zeroed.
- **htmx swap**: N/A.

### Routine editor (`GET /members/{id}/routine`)

- **AA contrast**: Day-edit blocks on `--elevation-1-bg` with `--ink` text pass. Error block: `--coral` on `--coral-tint` (6.60:1). Success notice: `--magenta` on `--magenta-tint` (6.92:1).
- **375px**: Media query at 399px reduces padding, shrinks heading, full-width selects/buttons.
- **Reduced motion**: Transitions on day-edit hover/focus-within zeroed.
- **htmx swap parity**: ✅ Fragment `#editor-root` from POST matches the same div from GET. Structural elements (editor-root id, base_routine_id, weekday, workout_name, exercises) present in both. Stale-save and validation-refusal fragments also match.

### Presets (`GET /presets`)

- **AA contrast**: Preset cards on `--elevation-2-bg` with `--ink` text (15.8:1). Default border: magenta with glow.
- **375px**: Media query at 600px stacks actions vertically, full-width buttons and pick labels.
- **Reduced motion**: Transitions zeroed.
- **htmx swap**: The Preset apply POST also checks `_is_htmx` and returns fragments; structural test covers the edge.

### Settings (`GET /settings`)

- **AA contrast**: Settings cards on `--elevation-2-bg`. Consequential (regenerate) cards use magenta left-border + heading. QR on white block — no text-contrast issue (black on white, 21:1).
- **375px**: Media query at 420px: QR constrained to 200px max, code font shrinks, form inputs full-width.
- **Reduced motion**: Transitions zeroed.
- **htmx swap**: N/A.

### Door pages (`GET/POST /login/{token}`, bounce)

- **AA contrast**: Door cards on `--elevation-1-bg`. Primary button: white on black (21:1). Secondary text: `--ink-2` on surface (7.5:1).
- **375px**: `max-width: 26rem` + `padding: 0 var(--gut)` — fits at 375px.
- **Reduced motion**: Transitions zeroed.
- **htmx swap**: N/A.

## Gaps filed

| Issue | Title | Severity |
|---|---|---|
| [#156](https://github.com/ivzc07/agentg/issues/156) | `--ink-3` on `--elevation-3-bg` fails AA contrast (4.17:1 < 4.5:1) | P3 — surface not currently text-bearing |
| [#157](https://github.com/ivzc07/agentg/issues/157) | Add skipped Playwright 375px visual regression tests | P2 — browser verification deferred |

## Owner's visual verification procedure

The automated sweep covers the structural gates. The Owner completes the final visual verdict per screen by:

1. **Start the dashboard server** with demo fixtures (`uv run agentg` with a seeded database).
2. **Sign in** as Coach (magic link from the bot).
3. **For each screen**, open the reference ([Dribbble shot](https://dribbble.com/shots/26910715-Blockchain-Data-Network-Dashboard-UI)) side by side with the dashboard:
   - Roster Table (`/?view=table`)
   - Roster Cards (`/?view=cards`)
   - Roster Split (`/?view=split` — click a Member)
   - Member page (click any Member from Table/Cards)
   - Routine editor (`Edit` from Member page)
   - Presets (`/presets`)
   - Settings (`/settings`)
   - Login/interstitial (sign out, request a new magic link)
4. **At desktop width** (1280px+): judge each screen against the reference's level of polish — layered near-black surfaces, soft rounded corners, visible elevation (shadow + inner stroke), magenta hero accent with glow, gradient accent fills, icon chips, large bold numerals, generous spacing.
5. **At 375px** (browser DevTools responsive mode, set to 375×812): confirm no horizontal scroll, all primary actions reachable, layout adapts cleanly.
6. **Record the verdict** per screen: "matches reference level" or note specific divergences.

The automated gates ensure the technical invariants hold; the Owner's eye is the final authority on visual quality.

## Automated checks — regression guide

To verify the sweep machinery catches real regressions:

- **Break a contrast token**: Change `--ink-3` in `:root` from `#85858a` to `#7a7a80` → `test_ink3_against_all_surfaces` FAILS.  Changing `--ink-3` also fails `test_all_expected_token_pairs_pass_aa` because that test now resolves tokens from the live CSS.
- **Remove a 375px responsive rule**: Delete the Split stacking rule inside `@media (max-width: 899px)` → `test_per_screen_responsive_rules` FAILS for the `(899, ".split", "grid-template-columns: 1fr")` triple.
- **Remove reduced-motion**: Delete the `prefers-reduced-motion` block → `test_prefers_reduced_motion_block_exists` FAILS.
- **Change htmx fragment structure**: Alter the `editor-root` div in `_routine_editor_page` without updating the fragment → `test_editor_fragment_matches_full_page_region` FAILS.  The `_assert_shared_structure` helper now also asserts that no unexplained structural elements are missing from the fragment.

All confirmed during development — each gate was sanity-checked by temporarily breaking the corresponding token/style.

## Known limitations

- **Inherited color/background pairs**: The CSS parser collects text/surface pairs only from rules where `color` (or `caret-color`) and `background` (or `background-color`) are declared on the *same* selector, plus the explicit token matrices.  Pairs where color is inherited from a parent and background is set on a child are **not** auto-discovered.  The explicit token-pair and per-surface tests cover the important combinations, but a new rule that sets only `background` and relies on inherited `color` from `body` will not be checked automatically.
