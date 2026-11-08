---
name: Auction France
version: v2 « Relevé » (2026-11-08)
brief: docs/redesign/v2/BRIEF.md
status: private preview, noindex; working name pending owner decision
colors:
  light: {paper: "#F3F3F0", surface: "#FFFFFF", inset: "#F7F7F4", carbon: "#121316", ink: "#121316", ink-2: "#474A51", ink-3: "#6A6E76", rule: "#DEDED8", hi: "#FFD23F", ok: "#146C43", major: "#B4470B", crit: "#A3161F", watch: "#8A5A00"}
  dark: {paper: "#101113", surface: "#18191C", inset: "#1F2024", carbon: "#0B0C0D", ink: "#F1F1EC", ink-2: "#B9BBC2", ink-3: "#8E929B", rule: "#2A2C31", hi: "#FFD23F", ok: "#6FD39A", major: "#FF9A57", crit: "#FF8A8A", watch: "#E6B34A"}
  rings: {green: "#1F8F50", yellow: "#A87C00", orange: "#D9620F", red: "#C8202F", track: "#ECECE7"}
typography:
  sans: Archivo variable (wdth 62–125, wght 100–900), self-hosted, OFL
  mono: JetBrains Mono variable (wght), self-hosted, OFL
  fallback: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Arial, sans-serif
---

# Auction France: design system v2 « Relevé »

**v1 is superseded.** v1 was the "printed catalogue on warm paper" design. It is replaced by v2, as briefed in
[`docs/redesign/v2/BRIEF.md`](redesign/v2/BRIEF.md). The brief is the authority; this file says how the build
applies it and where the rules live in code. The v1 logo study (`docs/redesign/logo-directions.*`) is
superseded as well.

## Overview

An inspection report set like a sale schedule: carbon ink, white paper, one yellow highlighter. **The data
is the decoration.**
- The 3×3 grid of the contrôle technique (CT) stands in for photos.
- A waffle chart of every lot opens the page.
- Each car carries a score ring.

Honesty is built into the structure, not into repeated disclaimers:
- a price that is not compared is hatched;
- a missing CT shows as dashed cells;
- the status line names each gap once.

## Pages and files

| File | What it is |
|---|---|
| `web/index.html` | The French page. Its texts carry `data-i18n` keys (and `-ph`, `-label`, `-content` for attributes) |
| `web/en/index.html` | The same page in English. **Generated, never edited**: run `python -m auction_agent.site_i18n` after any change to `index.html` or the dictionaries |
| `web/i18n/fr.json`, `web/i18n/en.json` | Every UI string, keyed. French typography (no-break spaces) lives here, not in code |
| `web/app.js` | Classic script, CSP-safe. Renders the snapshot. Pure helpers are exported for Node |
| `web/styles.css` | Tokens, fonts, components. No inline style anywhere: JS only sets `--w` and `--bar-h` |
| `web/about.html` | The method page, French only for now. The English pages link to it as « (in French) » |
| `web/fonts/` | Archivo and JetBrains Mono (latin subsets) with their OFL licences |
| `web/favicon.svg`, `web/favicon-32.png` | The grid mark at 16 px, with a PNG fallback |

## Colours

The tokens are in `styles.css` (`:root`, plus a dark block that redefines every one of them). The contrast
pairs are tested in `tests/test_site.py::test_token_contrast_ratios`.

**Text:**
- `ink`, `ink-2`, `ink-3` and the status colours are ≥ 4.5:1 on `paper`, `surface` and `inset`.
- Status colours are also ≥ 4.5:1 on their own washes.

**Marks:**
- Rings, CT cells and control edges are ≥ 3:1 on `surface`.

**Yellow (`--hi`) never carries status.** It appears only on:
- lot tags;
- the selected row (`--hi-wash`);
- the hero CTA on carbon;
- the « décodées » highlighter.

**CT states differ by lightness, not just hue:**

| Cell | Look |
|---|---|
| No defect | white |
| Minor | light grey |
| Major | orange |
| Critical | near-black bordeaux |
| CT absent or unreadable | dashed |

**Waffle on carbon:**
- favorable `#3CCB7F`;
- major `#8A5634`, deliberately muted because it is the bulk, with the legend carrying its count;
- critical `#FF5D5D`;
- absent: a 1 px outline.

**Score ring:** four colours over three tiers (owner decision, 2026-11-08).
- The bands are green 80–100, mustard 70–79, orange 60–69 and red < 60.
- Yellow and orange are both « correct ». They are a visual gradation, not a fourth tier.
- The number and the tier word are always shown; each tier also has its own shape (filled dot, ring, triangle).

## Type

| Role | Spec |
|---|---|
| Display (`.display`, hero h1) | 800, `font-stretch: 112%`. 66 px desktop, 56 px at 64–80rem, 42 px phone. Line-height .95, −0.025em |
| Sheet title | 800, 108%. 30 px desktop / 34 px phone |
| Section h2 | 750. 28 px / 24 px. −0.01em |
| Body | 400, 16 px / 1.5 |
| Figures (`.fig`) | 700, `font-stretch: 75%`, tabular. Row cost 23 px, sheet total 44 px, tag 19 / 24 px |
| Labels (`.lbl`) | JetBrains Mono 500, 11 px, uppercase, +0.06em. The **only** uppercase and positive letter-spacing in the CSS |

- The fonts are self-hosted (owner decision, 2026-11-08): `font-display: swap`, and Archivo is preloaded.
- Thousands are grouped with a no-break space (U+00A0), not U+202F: at 75 % width the narrow space nearly
  vanishes (`wide()` in `app.js`).

## Layout

- **Container:** max 1440 px. Gutters are 80 px on desktop, 40 px from 64 to 80rem, 32 px on tablet and
  16 px on phone.
- **Carbon band:** masthead (72 px), hero (left column) and the sale card (right column), then the coverage
  strip.
- **Sticky catalogue bar:** white, `--elev-bar`. It holds the sale label, the section index (aria-current
  follows the section being read) and the filters.
  - The filters are native `<select>`s styled as dropdown buttons.
  - The engine filter shows in the wide bar only while it is set; it is a hero shortcut.
  - On a phone the bar shows: search, « Filtres (n) », sort.
- **List and sheet:** the list is `flex: 999 1 600px`. From **75rem** up, the sheet is a sticky pane
  (`flex: 1 1 460px; max-width: 520px`, `--elev-pane`).
  - Why 75rem rather than the brief's 64rem: the list (600 px) and the pane (460 px) only fit side by side
    from there. Between 64 and 75rem the pane wrapped under the whole list, and a click seemed to do nothing
    (found in review, 2026-11-08).
  - The rest of the desktop layout still starts at 64rem.
- **Below 75rem the sheet is a full-screen view** (owner decision for phones, 2026-11-08), pushed onto
  history:
  - Back or « ← Lots » closes it and the list keeps its scroll position;
  - the rest of the page is `inert` while it is open;
  - a sticky bottom bar carries the ring, the cost and the listing button.

**Targets met** (checked 2026-11-08):

| Viewport | What is visible without scrolling |
|---|---|
| 1440×900 | the hero h1, the search form and the waffle; the listing button in the opened sheet |
| 375×812 | the search field; on a lot, the listing button both in the header (y≈331) and in the sticky bar |

## Components

**Lot tag:**
- yellow, 3 px radius;
- the compact rows use an outline tag;
- the sheet's tag is larger.

A future house code goes before the number (« ALC 751 »).

**Score ring:**
- One `svg()` per car (the only `createElementNS`), with `stroke-dasharray = 2πr × score/100`.
- Sizes: 36 compact, 44 phone row, 48 desktop row, 40 sticky bar, 100/104 sheet.
- The ring is drawn at its final value, with no animation. It is `aria-hidden`; the row's sr text says « note 78 sur 100, Profil correct ».

**CT grid:**
- Built from the snapshot's `ct_grid`: nine characters, functions 0–8, where `.` = none, `n` = minor,
  `M` = major, `C` = critical, `u` = CT absent or unreadable.
- Without it, the grid is rebuilt from the sheet's defects; with neither, a single chip is shown.
- In rows it is `aria-hidden`, because the text beside it says the same.
- In the sheet it is a `<ul>` of nine named cells.

**Waffle:**
- One square per lot in lot-number order: 48 columns of 9 px with 2 px gaps.
- `role="img"`, with an aria-label carrying all four counts.
- The four legend buttons (`aria-pressed`) filter the list and the live count.
- On a phone it becomes a stacked bar with a 2×2 legend.
- It is shown only when the snapshot lists every lot (not on schema 1, nor on a cut list).

**Rows:**
- Detailed rows use the grid `60px 1fr 156px 112px 64px`: tag, vehicle, CT, cost, score.
- The vehicle cell shows at most one non-CT signal, in this order: gate (engine, structural CT, odometer,
  battery, seller comment, mileage) > leased battery > battery SoH > high km/year > over ten years > VAT not
  recoverable > the lot's own data gap.
- The sale-wide « prix pas encore comparé » never appears on a row.
- Compact rows show `why`, written from `why_code` and `why_params`.

**Sheet**, in a fixed order (`renderSheet`, tested):
1. identity;
2. gate banner;
3. **listing link**;
4. En bref;
5. Coût total estimé (with the simulator);
6. Contrôle technique;
7. À vérifier aussi;
8. Sources.

**Bid simulator (§6):**
- It is buyer arithmetic, never a ceiling we choose.
- The fees come from `sale.fee_model`. Without it the simulator is hidden; the page never hard-codes a rate
  (a test greps for 0.144).
- B is floored to the euro.
- Edge messages for B ≤ 0 and for B below the starting price.
- The per-100 € line shows only on the percentage branch.
- Values are kept per lot in `sessionStorage`.

## Language

- `/` is French and `/en/` is English, with the same structure, ids and keys.
- The switch is two links, `lang`/`hreflang` with `aria-current`.
- On the first visit, `navigator.languages` only shows a notice on the French page; it never redirects.
- The choice is remembered in `localStorage`. Switching keeps `#lot-N` (in the link) and the filters
  (a one-shot `sessionStorage` handoff).
- Rule output (reasons, gates, `why`) is written in English from `code` + `params` (snapshot v2, B3), never
  translated from the French sentence. French keeps the snapshot's own sentence.

**What stays in French:**
- CT defect labels: legal text, shown with « French wording from the inspection report »;
- AI verdicts;
- warranty names from the listing;
- the method and legal pages, linked as « (in French) » (owner decision on English scope, 2026-11-08).

**Dictionaries:**
- A missing English key falls back to French.
- `test_dictionaries_have_the_same_keys` fails while any key is missing.

## Motion

| Moment | Effect |
|---|---|
| Phone sheet | `sheet-in`: translateX(100% → 0), 240 ms |
| Desktop lot change | `fade-in`, 120 ms |
| Waffle filter | non-matching cells to 20 % opacity, 160 ms |
| Row hover (fine pointers) / press | background 120 ms / `scale(.97)` |

- Easing: `cubic-bezier(0.23, 1, 0.32, 1)`.
- Reduced motion: no transforms; fades of 120 ms at most.
- Nothing animates on keyboard navigation, scroll jumps or numbers (tested).

## Accessibility

- Focus is `outline: 2px solid var(--ink)` with a 2 px offset (band text colour on the carbon band).
- Touch targets are ≥ 44 px on phones.
- Every filter control is wrapped in a `<label>`.
- The count is `role="status"`; the simulator outputs are `aria-live`.
- `forced-colors` maps cells, rings, tags and pressed states to system colours.

## Data contract the page reads (snapshot schema 2)

- Fields added for v2 (B3, see `auction_agent/snapshot.py`):
  `ct_grid`, `ct_counts`, `sale.house` / `category` / `fee_model`, `detail.price.vat_applies`, `sales[]`,
  `engines{ref: {label, check}}`, `engine`, `reasons[].code` / `params`, `gate_codes` / `gate_params`,
  `why_code` / `why_params`, `title_display` / `subtitle_display`.
- The page still renders schema 1 and older schema-2 snapshots. These are checked with the fixtures
  described under Verification.

## Multi-house lot identity (unchanged plan)

A lot is identified by house + sale `external_id` + lot number:
- the future hash is `#lot-<house>-<sale>-<lot>`;
- the current `#lot-751` keeps resolving to the featured sale.

With several sales, the bar's sale label becomes a `<select>`; it is never a one-option dropdown.

**Not done yet: several sales at once.** The lots in the snapshot carry no sale id. With two or more
`sales[]`:
- every lot would be counted under the featured sale;
- the select has no action wired to it;
- the fees and sale line of the featured sale would apply to every lot.

A `sale` key on each lot in `snapshot.py` comes first, before the select can filter. Today there is one
sale, so nothing is wrong on the page.

## Do's and don'ts

**Do:**
- Read every number from the snapshot.
- Say a missing value once, where it applies (hatched price, dashed cells).
- Put the listing link before any analysis.

**Don't:**
- Use yellow for status.
- Hard-code fees.
- Write « tout compris », « aucun risque », a « deal », or a ceiling we choose.
- Animate numbers.
- Paste markup or inline styles from `docs/redesign/v2/reference-html/`.

## Verification

1. Run `venv\Scripts\python.exe -m pytest tests/test_site.py tests/test_preview.py -q`. This covers the CSS
   rules of BRIEF §3, contrast, the dictionaries, page parity, CSP and the fetch list.
2. The Node helper test (`test_page_helpers`) runs on CI. Locally, run the same `NODE_BODY` in the browser
   on the preview, with `m = window` and `dict(lang)` fetching `/i18n/<lang>.json`, then check the output
   with `check_page_helpers`.
3. Browser QA covers lots 751, 538, 816 and 501, a compact red lot, a missing URL, `#lot-1212`, an empty
   filter, dark, reduced motion and forced colours.
   - The fixture snapshots (mixed, schema1, dropped, stale, prev2) are built outside the repo, from the real
     one.
