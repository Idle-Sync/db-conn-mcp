# db-conn-mcp dashboard design system

The dashboard is one page (`src/db_conn_mcp/gui/static/`: `index.html`, `style.css`, `app.js`). `style.css` is the source of every value below. No build step, no framework, no dependencies.

## Direction

1. **Who and when.** A developer with a terminal open beside the page, finding out why a client or database is not wired up. They click a check and read the verdict.
2. **Feel: quiet and blunt.** Quiet everywhere except state; blunt about state.
3. **Signature: the status lamp.** A 9px circle before every card title, hollow when idle, lit by the verdict, pulsing while working. The page reads as a bench instrument: engraved labels, dense rows, a lamp per row.
4. **Refuses** the product page: floating cards, pills, shadows, webfonts and colour used as decoration. Above all it refuses a green that was not earned. A lamp or chip that shows success over a failure is a lying instrument.

## Hard constraints

- CSP is `default-src 'self'`, so a webfont, CDN asset or inline block is silently blocked. `test_static_css_fetches_nothing_from_the_network` pins no `http://`, `https://` or `url(` in the stylesheet.
- Nothing is assigned as HTML. Server data reaches the page through `textContent` or a cloned `<template>`; `tests/test_gui_app.py` pins no `innerHTML` in `app.js`.
- No inline `<style>`, `<script>` or `onclick=`.
- Click to run: page load performs two reads and spawns nothing.
- `badge()` and `message()` rewrite `className`, so a painted chip is `badge <state>` and a painted line is `msg <state>`. CSS keys off state classes and position, never off template hooks such as `.db-mode` or `.c-msg`.
- Lamp selectors use direct-child combinators, so a failed card nested in a client card does not light its parent's lamp.
- A browser without `:has()` keeps hollow lamps; the chip text still states the result.

## Type

Two system stacks, no webfont, because the CSP blocks one. The system stacks are the deliberate exception to pairing a display face: mono carries the instrument voice, sans carries reading.

| Token | Value |
|---|---|
| `--mono` | `ui-monospace, "Cascadia Mono", Consolas, "SF Mono", "Liberation Mono", monospace` |
| `--sans` | `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` |

| Step | Style | Use |
|---|---|---|
| 12 / 1.4 | mono | badge, nav |
| 12 / 1.4 | mono 600, uppercase, 0.08em tracking | section label |
| 13 / 1.4 | mono | button, `pre`, `summary`, summary line |
| 13 / 1.5 | mono | `.msg` status line, dead banner, inputs |
| 13 | inherits body stack | `.muted` inside a card, `.note`, form labels |
| 14 / 1.5 | sans | body |
| 15 / 1.4 | sans 600 | card title |
| 20 / 1.2 | mono 600, -0.01em tracking | page title |

Prose runs at most `68ch`.

## Colour roles

Components use roles, never raw values. Dark values are re-declared once under `prefers-color-scheme: dark`. Chips are pre-mixed literals so the ratios hold without `color-mix()`.

| Role | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#f6f7f9` | `#141a23` | page |
| `--panel` | `#ffffff` | `#1a2230` | card, input |
| `--well` | `#f1f3f6` | `#10161e` | code, `pre`, readonly input |
| `--ink` | `#1b2432` | `#dee5ef` | text |
| `--muted` | `#5c6879` | `#96a1b3` | annotation, idle lamp |
| `--line` | `#d9dee6` | `#28323f` | hairline, card and control edge |
| `--accent` | `#336791` | `#6fa3c7` | link, hover, focus ring, in-flight lamp |
| `--ok` | `#146f44` | `#3dbe7b` | success text and lamp |
| `--warn` | `#8f6209` | `#d9a03f` | warning text and lamp, write mode |
| `--fail` | `#b23a31` | `#e8837a` | failure text and lamp |
| `--accent-chip` | `#ebf0f4` | `#243142` | accent chip |
| `--ok-chip` | `#e8f1ec` | `#1e3539` | ok chip |
| `--warn-chip` | `#f4efe6` | `#313132` | warn chip |
| `--fail-chip` | `#f7ebea` | `#332e39` | fail chip |
| `--skip-chip` | `#eff0f2` | `#293140` | skip chip and unclassified verdict |
| `--fail-well` | `#efe4e5` | `#29242c` | dead banner |

### Measured contrast

Computed with the WCAG 2.x formula. Text needs at least 4.5:1.

| Text on background | Light | Dark |
|---|---|---|
| ink on paper | 14.57 | 13.78 |
| ink on panel | 15.61 | 12.59 |
| ink on well | 14.05 | 14.33 |
| ink on fail-well | 12.56 | 11.97 |
| muted on paper | 5.28 | 6.69 |
| muted on panel | 5.66 | 6.12 |
| muted on well | 5.09 | 6.96 |
| muted on skip-chip | 4.96 | 5.00 |
| accent on paper | 5.61 | 6.44 |
| accent on panel | 6.02 | 5.88 |
| ok on ok-chip | 5.38 | 5.45 |
| ok on paper | 5.78 | 7.36 |
| ok on panel | 6.20 | 6.73 |
| warn on warn-chip | 4.68 | 5.60 |
| warn on paper | 5.00 | 7.53 |
| warn on panel | 5.36 | 6.88 |
| fail on fail-chip | 5.09 | 5.00 |
| fail on paper | 5.53 | 6.62 |
| fail on panel | 5.93 | 6.05 |
| fail on well | 5.33 | 6.89 |
| fail on fail-well | 4.77 | 5.75 |

Lamps and state edges (3:1 needed) use the same state colours on panel: ok 6.20 / 6.73, warn 5.36 / 6.88, fail 5.93 / 6.05, accent 6.02 / 5.88, muted 5.66 / 6.12.

## Space, radius, elevation, motion, breakpoints

- **Space (px):** 2, 4, 5, 6, 8, 9, 10, 12, 14, 16, 17, 20, 30. The values are literals, not tokens. 17 exists so a banner with a 3px rule stays on the 20px gutter.
- **Column:** max 900px, 20px gutter, shared by banner, head and body so left edges align down to about 360px.
- **Radius:** 3px on everything; 50% on the lamp only. Chips, not pills.
- **Elevation:** none and no `box-shadow`. A raised surface is `--panel` on `--paper` with a 1px `--line` edge.
- **Motion:** `color` and `border-color` at 120ms `ease` on hover; `lamp-pulse` at 1.2s `ease-in-out` while in flight, removed under `prefers-reduced-motion`. Nothing else moves.
- **Breakpoints:** one, `max-width: 480px`: the page head stacks and `check_database` lines may wrap.
- **Focus:** one ring everywhere, `:focus-visible`, 2px `--accent`, 2px offset.

## Components

| Component | Where | For |
|---|---|---|
| Card | `.card`, `tpl-db`, `tpl-client`, `tpl-verify`, `tpl-finding` | One row: title, chip, detail, actions |
| Lamp | `::before` on card titles, `h1`, dead banner | State at a glance, keyed off the header chip |
| Badge | `.badge.ok`, `.warn`, `.fail`, `.skip` | Verdict in words |
| Setting chip | `#db-list > .card > header > .badge` | Outlined mode and yolo; settings, never a verdict |
| Status line | `.msg`, `.msg.in-flight`, `.fail-text` | Working state or a diagnosis |
| Note | `.note`, `.warn-note` | Suggested action or caution, with a left rule |
| Well | `pre`, `code` | Literal output; `pre` scrolls so a launch line stays one line |
| Disclosure | `details`, `summary` | Secondary output and the add form |
| Button | `button`, `button.danger` | Outlined; danger colours only on hover |
| Mains strip | `#dead-banner` | Host process gone; turns the page-title lamp red |
| Empty slot | `#db-empty` | Dashed row where the first database will sit |

### Verdict map

`VERDICT_LABEL` and `VERDICT_CLASS` in `app.js` hold the words and the class for every verdict: `ok` for a real pass, `fail` when the check did not get an answer it can use, `warn` when something answered but differs from what was expected. A verdict missing from the map falls back to the grey `skip` chip with a hollow lamp, so every verdict added to `verify.py` gets its entries in the same change.

## Libraries

None.

## Known gaps

- `--line` measures 1.26 / 1.35 on paper and 1.35 / 1.23 on panel (light / dark), under the 3:1 for UI boundaries. Button text and field labels name each control, but the edge of a text input is that line alone. Unchanged: a fix alters every card and control, so it belongs in its own change.
- Buttons are about 32px tall, under the 44px touch target. The page is served on loopback for a desktop browser beside a terminal.
- Verdict cards appear without a live region, so a screen reader is not told when a check finishes.
