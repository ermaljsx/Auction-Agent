# Auction France: redesign v2 « Relevé », handoff package

This folder is everything needed to build the redesign. Read it in this order.

| # | File or folder | What it is | How to use it |
|---|---|---|---|
| 1 | `BRIEF.md` | The specification: blockers, tokens, screens, simulator, language, motion, acceptance checklist. | **Authoritative.** Wins over everything else here. |
| 2 | `screens/` | PNG renders of each design screen (2× pixel density). | The visual target. Match layout, hierarchy, spacing and colour. |
| 3 | `reference-html/` | The same screens as static HTML. Open them in a browser. | Copy exact values (font sizes, colours, gaps, radii) with the browser inspector. **Not production code** (see below). |
| 4 | `fonts/` | Archivo (variable width and weight) and JetBrains Mono, latin subset, with their OFL licences. | Self-host these in `web/`. |

**Precedence when two sources disagree:** `BRIEF.md` > `screens/` > `reference-html/`.

## The screens

| File | Shows | Viewport |
|---|---|---|
| `01-desktop-home-first-screen.png` | What a desktop visitor sees before scrolling: navbar with FR/EN switch, general headline, search, next-sale card with the 573-lot chart. | 1440×900 |
| `01-desktop-home-full.png` | The whole desktop page: hero, coverage strip, sticky catalogue bar, the three catalogue sections with score rings, and the lot sheet for lot 538 open on the right (cost, bid simulator, CT grid). | 1440 wide |
| `02-phone-home-fr-first-screen.png` / `-full.png` | Phone home in French. | 390×844 |
| `03-phone-home-en-first-screen.png` / `-full.png` | The same page in English: shows the wording, glossary and number formats from BRIEF §5.5. | 390×844 |
| `04-phone-lot-816-first-screen.png` / `-full.png` | A lot opened on a phone, full screen: gate banner, listing button, score ring, cost, simulator, CT grid. The first-screen file shows the sticky bottom bar in place; the full file shows it at the end of the page. | 390×844 |
| `05-design-system-full.png` | Logo, colour tokens, type scale, CT grid states, score ring bands, components, empty states, motion. | 1440 wide |

## What the screens do not show (build it from BRIEF.md anyway)

- The dark theme (tokens in §4.1), English on desktop, and the lot sheet in English.
- The empty, missing-link, unreadable-CT and absent-lot states (described on the system board and in §5.2).
- A sale selector with several sales (§5.1). Today there is one sale.
- The method page and the legal pages.
- Interaction: in the static HTML the simulator, filters and switches do not work. Their behaviour is specified in §5–§7.

## About the data in the screens

- Real lots from the snapshot of 8 November 2026 (Alcopa Beauvais, 573 lots).
- **Lot 587 (Peugeot 3008 1.2 PureTech) is deliberately left out of « À regarder d'abord ».** It belongs there only because of blocker B1, the engine regex bug. Fix B1 first.
- On the system board, the rings showing 84 and 66 and the last two CT grid examples are illustrative, not real lots.
- In the build, every number comes from the snapshot. Nothing in the screens is to be hard-coded.

## About `reference-html/`

- Each page links the fonts in `../fonts/` and works offline.
- The styles are inline because the pages were generated from design files. The site's CSP forbids inline styles: move everything to `web/styles.css` as tokens and classes (§4).
- The rows, cards and rings are written out for the sample lots. In the build they are generated from the snapshot by `web/app.js`.
- The listing links point to the Alcopa home page as placeholders. In the build, use each lot's `url` through `safeUrl`.

## Suggested order of work

1. Blockers B1 and B2, with regression tests, then regenerate the snapshot.
2. B3 snapshot fields (additive, schema 2 kept).
3. `tests/test_site.py` changes (§3), fonts and tokens (§4).
4. Home: masthead and language switch, hero, sale card, coverage strip.
5. Catalogue: sticky bar, rows, CT grid, score ring.
6. Lot sheet, then the bid simulator (§6, with its check values).
7. English: `fr.json` / `en.json`, `/en/`, formats (§5.5).
8. Update `docs/DESIGN.md` to describe v2 (it still describes v1).
9. The acceptance checklist (§11), with before and after screenshots in `data/redesign-review/v2/`.

Superseded: `docs/redesign/logo-directions.*` (v1 logo study) and the visual sections of the v1 `docs/DESIGN.md`.
