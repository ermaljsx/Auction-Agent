# Auction France: redesign brief v2, « Relevé »

**For:** the build agent. **Supersedes:** the visual direction in `docs/DESIGN.md` (v1, "printed catalogue on warm paper"). Every behavioural, safety and accessibility rule in v1 still holds unless this brief overrides it.
**Reference designs:** `screens/` (PNG) and `reference-html/` (static HTML you can open in a browser) in this folder, rendered from the owner's design canvas: desktop home with lot 538 open, phone home in French and English, phone lot 816, and the design-system board. Read `README.md` first. Build from this brief. Treat the screens as the visual target, and this brief wins where they differ.

---

## 0. Why v1 failed and what v2 changes

The v1 redesign ran correctly but looks generic. That comes from four structural causes, not taste:

1. **The look most AI generators default to.** Cream paper (#F5F1E8), a Charter/Georgia serif headline and one blue button.
2. **Self-imposed bans in `tests/test_site.py`.** No `@font-face`, no `url(`, no uppercase, no `letter-spacing`, no `box-shadow`. With system fonts only, the type renders differently on every OS, and there is nothing distinctive left to work with.
3. **The page talks about itself instead of showing the sale.** The hero explains the product and repeats « selon nos critères », « provisoire » and « sans avis IA » in several places. The headline promises an AI review and the next line takes it back. The first car appears at y≈800.
4. **The visual emphasis sits on the weakest signal.** This week the top 50 lots score between 69 and 79, so the score barely separates them. What does separate them is the CT result, the engine and the cost.

v2 answers each cause:
- **A general headline for the platform**: the site covers car auctions in France, not one room. The **next analysed sale is the first card** beside the headline. Today the only covered sale is Alcopa Beauvais, and the page says so plainly.
- **Search sits in the hero.**
- **FR / EN**: every page has a language switch in the navbar (§5.5).
- **A score ring on every car**: the number sits inside a ring filled to the score, coloured green, yellow, orange or red (§5.4).
- **A waffle chart of all 573 lots** is the opening visual.
- **A 3×3 "CT grid"** (one cell per CT function) replaces photos as the visual anchor of every row.
- **Self-hosted variable type** gives the site a voice.
- **Honesty moves into the structure.** It goes into a hatched price bar and dashed cells for missing data, not into repeated disclaimers.

---

## 1. Blockers: fix before the redesign ships

These are data bugs. No layout can make the site trustworthy while they stand.

### B1. The engine-risk regex misses "130CH" / "110CH"
- **Where:** `auction_agent/scoring/engines.json`, entry `stellantis_puretech_eb2_turbo`, `text_regex` `…(?:100|110|130|155)\b…`.
- **Why it fails:** `\b` does not match between "130" and "CH".
- **Lots wrongly un-flagged in the current snapshot:**
  - 587 Peugeot 3008 1.2 PureTech 130CH, 2017, **in the top 10**
  - 554 Peugeot 308 PureTech 110CH, 2019
  - 631 Peugeot 208 PureTech 110CH, 2019
  - 505 Peugeot 5008 PureTech 130CH, 2022
- **Fix:** replace `\b` with `(?!\d)` (or allow `\s*CH`) in every `text_regex` and `weak_regex`, and audit them all.
- **Regression test:** these four trims must match, while "PURETECH 180" and "PURETECH 83" must not.
- **Then rescore and regenerate the snapshot.**
- The canvas leaves lot 587 out of the top 10 for this reason.

### B2. `ct_line` contradicts the CT for cars under 4 years old
- For cars under 4 years old, `ct_line` says « CT pas encore exigé (moins de 4 ans) » even when a voluntary CT was parsed.
- Examples:
  - lot 847: CT défavorable majeure with 6 majors, row reads "pas encore exigé", score 76;
  - also lots 755, 519, 776, 508, 924, 767 and 899.
- **Fix:** when `detail.ct.result` exists, derive the line from it, and say « CT volontaire » when `nature == "volontaire"`.

### B3. Snapshot additions
These are additive, so schema 2 stays compatible.

| Field | On | Purpose |
|---|---|---|
| `ct_grid` | every card **and** compact lot | 9-char string, functions 0–8, worst severity per function: `.` none, `n` minor, `M` major, `C` critical, `u` CT absent/unreadable. Function codes ≥ 9 (e.g. 12 « véhicule école ») count in `ct_counts` but not in the grid. |
| `ct_counts` | compact lots | `{minor, major, critical}` so a compact row can say « 3 majeures » without parsing `why`. |
| `fee_model` | sale level, plus a per-lot override when needed | `{rate: 0.144, min: 360, vat_rate: 0.2, venue: "room"}`, plus a per-lot `vat_applies: bool`. Drives the bid simulator (§6). The frontend must never hard-code fees. |
| `check_fr` | each entry of `engines.json`, shown in the sheet | Buyer action, e.g. « Demandez une facture de remplacement de la courroie et vérifiez si la garantie couvre la distribution. » |
| `sale.house`, `sale.category`, then `sales[]` | sale level | `house: "alcopa"`, `category: "VP"` today. Later, a `sales` array (one entry per covered sale) drives the hero sale card, the "Ventes couvertes" strip and the sale selector. The UI must work with one sale now and N later. |
| `reasons[].code` + `params`, `gates[].code` | cards and compact lots (`why_code`) | Language-neutral keys so the English page can render rule output without translating French sentences. `heuristics.py` already has the codes (`km_per_year`, `engine_kb`, `low_mileage`…). Keep `text` for older pages. |
| `title_display`, `subtitle_display` | cards and compact lots | Title-cased with a brand/trim dictionary (MINI, DS 7, TCe, PureTech, BlueHDi, T-GDi, S tronic, EAT8, ch). No ALL CAPS on screen. |

---

## 2. Keep from the current implementation

- **No new stack.** Static `web/` + JSON snapshot from Blob. No Next.js, Tailwind or Framer Motion.
- **Security:** strict CSP with no inline script or style (move styles to `styles.css`), and `safeUrl` with the `alcopa-auction.fr` host allowlist.
- **Listing links:** `rel="noopener noreferrer nofollow"` and a never-built-from-lot-number URL rule.
- **Deep links and keyboard:** `#lot-751` deep links, arrow/Home/End row navigation, and Esc to close.
- **States and themes:** the error/empty-state messages, light/dark themes, `forced-colors` and reduced motion.
- **Snapshot compatibility:** support for schema 1 and 2.
- **Privacy:** private and `noindex`.
- **Listing link before any analysis** in the sheet.

## 3. Test constraints to change in `tests/test_site.py`

| Current ban | New rule |
|---|---|
| `@font-face`, `url(` | Allowed for **same-origin** `.woff2` only (`font-src 'self'`). Keep a test that no external host appears. |
| `text-transform: uppercase` | Allowed on `.lbl` only (mono labels, 11–12px). |
| `letter-spacing` | Allowed on `.lbl` (+0.06em) and on display headings (−0.025em to −0.01em). |
| `box-shadow` | Allowed only through two tokens: `--elev-bar` (sticky catalogue bar) and `--elev-pane` (desktop sheet). |

Keep every contrast test, and add the tokens below to it.

---

## 4. Visual system

### 4.1 Colour tokens (light / dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | #F3F3F0 | #101113 | page |
| `--surface` | #FFFFFF | #18191C | rows, sheet, inputs |
| `--inset` | #F7F7F4 | #1F2024 | AI line, notes |
| `--carbon` | #121316 | #0B0C0D | hero band, primary button (light) |
| `--ink` | #121316 | #F1F1EC | text |
| `--ink-2` | #474A51 | #B9BBC2 | secondary text |
| `--ink-3` | #6A6E76 | #8E929B | captions (≥ 4.5:1 on paper and surface) |
| `--rule` | #DEDED8 | #2A2C31 | hairlines |
| `--hi` | #FFD23F | #FFD23F | lot tag, selected row wash (#FFF6CC / #2B2610), CTA on carbon |
| `--ok` / `--ok-fill` / `--ok-wash` | #146C43 / #3CCB7F / #E4F2E9 | #6FD39A / #3CCB7F / #13261B | CT favorable |
| `--minor-fill` | #C4C6C0, border #8E9198 | #5A5D64 | minor defect cell |
| `--major` / `--major-fill` / wash | #B4470B / #E26A16 / #FDEBDD | #FF9A57 / #E26A16 / #2E1C10 | major |
| `--crit` / `--crit-fill` / wash | #A3161F / #7F1D1D / #FBE3E3 | #FF8A8A / #FF5D5D / #331618 | critical, gates |
| `--watch` | #8A5A00, mark #B97A0B | #E6B34A | soft signals (km/an, VAT, battery) |

**Waffle chart on carbon:** favorable #3CCB7F, majeure #8A5634 (deliberately muted: it is the bulk), critique #FF5D5D, absent = 1px outline #8E929B.

**Score ring** (stroke on a #ECECE7 track; all ≥ 3:1 on white):

| Band | Ring | Tier word colour | Word FR / EN |
|---|---|---|---|
| 80–100 | #1F8F50 green | #146C43 | favorable / favourable |
| 70–79 | #A87C00 mustard yellow | #7A5A00 | correct / fair |
| 60–69 | #D9620F orange | #B4470B | correct / fair |
| 0–59 | #C8202F red | #A3161F | risque / high risk |

The ring's yellow is a dark mustard on purpose: the bright #FFD23F stays reserved for lot numbers, so a score is never mistaken for a lot tag. Dark theme: the same hues one step lighter (#3CCB7F, #E0B43A, #FF9A57, #FF8A8A) on a #2A2C31 track.

**Rules:**
- **Yellow never carries status.** It is used only on the lot number, the selected row and the CTA on carbon.
- **CT states differ by lightness, not just hue:** white → light grey → orange → near-black bordeaux.

### 4.2 Type
- **Archivo**, variable, OFL. Axes `wdth` 62–125 and `wght` 100–900. Self-host `fonts/archivo-latin-wdth-wght.woff2` from this folder (90 KB, latin subset with both axes), `font-display: swap`, preload it. Declare it with `font-weight: 100 900; font-stretch: 62% 125%`.
- **JetBrains Mono**, variable weight, OFL. Self-host `fonts/jetbrains-mono-latin-wght.woff2` (40 KB). Used for labels, CT codes, dates and sources.
- Copy the two `OFL-*.txt` licence files next to the fonts in `web/`. The latin subset covers French (é, «, », €, the narrow no-break space).
- **Fallback:** `Archivo, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Arial, sans-serif`.

| Role | Spec |
|---|---|
| Display (hero h1) | 800, `font-stretch:112%`, 66px desktop / 42px phone, line-height .95, −0.025em |
| Sheet title | 800, 108%, 30px desktop / 34px phone |
| Section h2 | 750, 28px desktop / 24px phone, −0.01em |
| Body | 400, 16px/1.5 (phone 15.5–16.5) |
| Figures (`.fig`) | 700, `font-stretch:75%`, `tabular-nums`: row cost 23px, sheet total 44–48px, lot tag 19/24px |
| Label (`.lbl`) | JetBrains Mono 500, 11px, uppercase, +0.06em |

### 4.3 Shape, space, elevation
- **Radii:** 3px tag, 4px controls/buttons, 6px panes.
- **Spacing:** 4/8/12/16/24/32/48/64.
- **Elevation:** two shadow tokens only (`--elev-bar: 0 6px 20px rgb(18 19 22 / .05)`, `--elev-pane: 0 1px 2px rgb(18 19 22 / .05), 0 12px 32px rgb(18 19 22 / .07)`). Everything else uses rules and surface steps.
- **Touch targets:** at least 44px on phones.

### 4.4 Logo
- **Mark:** a yellow rounded square holding a 3×3 grid of carbon cells, with the centre cell open from 32px up and filled at 16px.
- **Wordmark:** "Auction France" in Archivo 800 at 112% width.
- **Never a tick or check mark** in the brand (it reads as « vérifié »).
- **Favicon:** the 16px variant as SVG, plus a 32px PNG fallback.
- The name is still a working name (§10).

---

## 5. Screens

### 5.1 Home on desktop (≥ 64rem)

**1. Carbon band**
- **Masthead (72px):** mark, wordmark, « Ventes » (current) and « Méthode », a mono note « Gratuit · indépendant des maisons de vente », and the **FR | EN switch** (§5.5).
- **Hero, left column (general: about the platform, not one sale):**
  - mono eyebrow « Ventes aux enchères de voitures · France »;
  - h1 « Les voitures aux enchères, décodées avant la vente. », with « décodées » underlined by the yellow highlighter (the only decorative use of yellow);
  - lede « Pour chaque lot : ce que dit le contrôle technique, les moteurs à risque connus et le coût réel, frais d'achat compris. Puis l'annonce officielle, en un clic. »;
  - **search form** across all covered sales: text field « Marque, modèle ou n° de lot », field « Budget max, coût total », yellow button « Voir les lots »;
  - quick toggles: « CT favorable 45 », « Sans moteur à risque connu », « Boîte automatique », « Moins de 100 000 km », « Hybride ou électrique ».
- **Hero, right column: sale card « Prochaine vente analysée »** (the soonest upcoming sale in `sales[]`; today the only one):
  - a mono label and a « Demain » / « Aujourd'hui » / « Dans N jours » chip;
  - title « Lundi 9 novembre » and the line « Alcopa · salle de Beauvais · voitures particulières »;
  - three figures: Lots 573 · PV de CT lus 564 · CT favorables 45;
  - the waffle of 573 cells (48 columns, 9px cells, 2px gaps, lot-number order) and four legend buttons with counts that **filter the catalogue** (`aria-pressed`), plus the caption on what a major and a critical failure mean;
  - the status in mono: « Analyse publiée dim. 8 nov. à 02:10 · détails » and « Classement provisoire : prix pas encore comparés · pas d'avis IA » (each part only when true);
  - a secondary link « Voir les 573 lots de cette vente → ».
  - Counts come from the snapshot. Never hard-code them.
  - **When `sales[]` has several entries:** show the soonest as this card and the others as a compact list under it (house, room, date, lot count). Never show a house or room the data does not cover.
- **Coverage strip** at the bottom of the band: « Ventes couvertes · ● Alcopa · Beauvais · D'autres salles s'ajouteront dès que leurs données seront fiables. · Ce que nous couvrons ». No logos, no lists of future houses.

**2. Sticky catalogue bar** (white, `--elev-bar`)
- **Sale label** at the left of the bar: « Vente · Alcopa Beauvais · lun. 9 nov. ». It becomes a sale selector when `sales[]` has more than one entry; with one sale it is plain text, never a one-option dropdown.
- **Section index (anchor links):** « À regarder d'abord 10 · Autres fiches détaillées 40 · Les autres lots 523 ».
- **Filter row:** compact search, then Budget · Énergie · Boîte · Kilométrage · Année · Contrôle technique as dropdown buttons, then « Trier par ».

**3. List and sheet**
- The list takes `flex: 999 1 600px` and the sheet `flex: 1 1 460px; max-width: 520px`, sticky.
- **Detailed row** (CSS grid `60px 1fr 156px 112px 52px`):
  - the tag;
  - the vehicle cell: title, subtitle, « 2019 · 89 569 km · Essence · Manuelle », and **at most one** non-CT signal;
  - the CT cell: grid plus « Favorable / 2 mineures »;
  - the cost cell: total, plus its basis (« mise à prix 8 900 € », « 5 300 € HT + TVA », « prix affiché 9 200 € »);
  - the note cell: the 48px score ring (§5.4) with the tier word under it. The grid's last column becomes 64px.
- **Signal priority:** gate (engine, odometer, structural CT) > battery leased > km/an high > VAT not recoverable > own data gap. Never show the sale-wide « prix pas encore comparé » on a row.
- **The selected row** gets the `--hi` wash and `aria-current="true"`.
- **Compact row (523 lots):**
  - outline tag;
  - « Titre · 2021 · 114 622 km »;
  - `why`;
  - a CT state chip (filled square + « Défavorable · majeure »);
  - cost and a 36px score ring.
  - Collapsed behind « Afficher les 523 lots ».
- **Section intro copy:** « Les mieux notés selon nos critères de fiabilité et d'état. Provisoire tant que les prix ne sont pas comparés. »
- **Footer:**
  - « Méthode » is a link;
  - the four legal items stay plain text until the launch decision;
  - independence statement: « … sans lien avec Alcopa. Les annonces, les photos et les conditions de vente restent sur le site de la maison de vente. »;
  - « Détails de l'analyse » disclosure.

### 5.2 Lot sheet (desktop pane; full-screen view on phone)

The order is fixed, and the buyer must reach the listing without scrolling.

1. **Identity.** Large tag, « Alcopa · Beauvais / lundi 9 novembre » (mono), close button (icon, `aria-label="Fermer la fiche"`; on a phone it is the « ← Lots » back link), title, subtitle, and a 4-cell spec strip (Année · Km · Énergie · Boîte).
2. **Gate banner, only when gates exist.** Crit-wash background and triangle icon, e.g. « **Moteur à risque connu.** Le 1.2 PureTech turbo a une courroie de distribution « humide » qui se délite, et consomme parfois de l'huile. »
3. **Primary action.** « Voir l'annonce sur Alcopa ↗ » (carbon, 52px, full width, sr-only « (nouvel onglet) »), the line « Photos, enchères et conditions de vente sur l'annonce. », and « Copier le lien ».
   - With no URL: « Annonce introuvable. Ouvrir le catalogue de la vente ↗ ».
4. **En bref.**
   - Typed lines with a shape per tone (triangle for risk, check for positive, ? for to-verify) and the source in mono (« PV CT », « annonce », « base moteurs »).
   - « À vérifier : … » from the rules plus `check_fr`.
   - A score card: the large score ring (104px desktop, 100px phone) with the number and « /100 » inside, the tier word under it in the band colour, and three pillar bars.
   - The caption explains the bands: « Anneau vert dès 80 (favorable), jaune de 70 à 79, orange de 60 à 69, rouge sous 60 (risque élevé). »
   - **Prix is hatched and labelled « non comparé »**, with the caption « Le prix n'étant pas encore comparé, il compte pour 50, une valeur neutre. »
   - The AI line: « Pas d'avis IA cette semaine : cette fiche vient de nos règles seules. » When a verdict exists, show it labelled « Avis IA, peut se tromper ».
5. **Coût total estimé.**
   - The total in figures, plus its basis.
   - A stacked bar: mise à prix / TVA non récupérable / frais acheteur / réparations CT / CT à refaire.
   - The ledger.
   - « Hors carte grise, transport et frais fixes éventuels par lot. »
   - The **bid simulator** (§6).
6. **Contrôle technique.**
   - The result chip, and a mono line « Périodique · 14 oct. 2026 · 36 785 km · valable jusqu'au 13 oct. 2028 ».
   - A **3×3 labelled grid**: number, function name, « RAS » or « 1 mineure / 2 majeures… ». Functions: 0 Identification, 1 Freinage, 2 Direction, 3 Visibilité, 4 Feux et électricité, 5 Essieux, roues, pneus, suspension, 6 Châssis et carrosserie, 7 Équipements, 8 Nuisances.
   - The defect list: severity cell and word, the official label **sentence-cased** (not ALL CAPS), and location and code in mono.
   - Keep majors and criticals open; fold minors beyond 4.
   - Legend: « AV, AR : avant, arrière ; G, D : gauche, droite. Libellés repris du procès-verbal. »
   - Severity meanings: « Majeure : contre-visite sous deux mois. Critique : la voiture ne peut plus rouler après le jour du contrôle. » *(Verify the exact wording against the current arrêté before launch.)*
7. **À vérifier aussi.**
   - **Moteur.** When there is no match, say « Absent de notre liste de moteurs à risque, qui en suit trois à ce jour. » State the knowledge base's coverage. Never write « aucun risque ».
   - **Carrosserie:** « Non relevée pour ce lot : voir les photos de l'annonce » when `damages` is null.
   - **Batterie:** SoH and its source.
   - **Garantie.**
   - Then « Détail de la note » (folded).
8. **Sources.** « Annonce Alcopa et procès-verbal de contrôle technique, lus le 8 novembre. Analyse automatique : vérifiez sur place avant d'enchérir. »

**Phone specifics:**
- The sheet is a **full-screen view** pushed onto history (`#lot-816`). It slides in from the right over 240ms (fade only with reduced motion), and Back closes it and **restores the list scroll position**.
- This replaces v1's inline expansion, which loses context inside a 573-row list.
- A **sticky bottom bar** carries a 40px score ring, « 5 262 € · coût estimé », and the listing button.

### 5.3 Home on a phone
- Carbon band: masthead (mark, name, « Méthode »), eyebrow, h1 at 42px, a short lede, a 52px search field, then Budget and « Voir », and horizontally scrolling toggles.
- The waffle becomes a single **stacked bar** (45 / 460 / 59 / 9) with a 2×2 legend.
- The status line takes two mono lines.
- Then a sticky bar: section tabs, plus [search icon] [Filtres] [Trier].
- **Row layout:** tag and score on line 1; title and subtitle; specs; a gate banner when present; the CT grid with its label on the left and the cost on the right.
- The hero follows §5.1: general h1 at 36px, then the sale card with a **stacked bar** in place of the waffle (45 / 460 / 59 / 9) and a 2×2 legend, the status lines, « Voir les 573 lots de cette vente », and the coverage strip.
- The sticky bar adds a thin sale line above the tabs: « Vente · Alcopa Beauvais · lun. 9 nov. ».
- Header: mark, name at 16px, « Méthode », and the FR | EN switch (two 40×44px segments).
- Each row shows the tier word and a 44px score ring on its first line, next to the lot tag.
- **Targets at 375×812:** the search box is visible without scrolling, and the first row is reachable within one scroll.

### 5.4 Score ring

- **One SVG** per car: a track circle and a value circle, `stroke-dasharray = 2πr × score/100`, rotated −90° so it starts at 12 o'clock, round caps. The number sits in the centre in `.fig` (700–800, 75% width, tabular).
- **Sizes:** 36px compact row, 44px phone row, 48px desktop row, 40px phone sticky bar, 100–104px sheet.
- **Colour bands** are in §4.1. The number and the tier word are always present, so colour is never the only cue. The ring is `aria-hidden`; the row's accessible name includes « note 78 sur 100, profil correct ».
- **No glow, no gradient, no count-up animation.** The ring is drawn at its final value. It may draw in once (300ms) on the sheet only, not in lists, and never with reduced motion.
- **Sparring note for the owner:** this week the top 50 lots sit between 69 and 79, so the list is mostly yellow rings. That is honest, but it means the ring will not separate cars until the price pillar is real. Keep the CT grid and the cost as the main sorting cues.

### 5.5 Language: French and English

- **Switch in the navbar on every page:** a two-segment control « FR | EN » (desktop: 34px high in the masthead; phone: two 40×44px segments in the header). Each segment is a link (`<a lang hreflang>`), the current one has `aria-current="true"` and a filled background. Accessible names « Français » and « English ».
- **URLs:** `/` (French, default) and `/en/` (English), the same structure under both; `#lot-751` deep links work in both. Add `<link rel="alternate" hreflang>` for each page, even while the site is `noindex`. Set `<html lang>`.
- **Choice of language:** use `navigator.languages` only to *suggest* English on the first visit (a one-line notice « This site is also in English »), never redirect automatically. Remember the choice in `localStorage` (try/catch). Switching keeps the current sale, filters and open lot.
- **Strings:** all UI copy lives in two dictionaries `web/i18n/fr.json` and `web/i18n/en.json`, keyed by id. Rule output (reasons, gates, `why`) is rendered from the codes added in B3, never by translating the French sentence. A missing English key falls back to French and is logged in a test.
- **What stays French on purpose:** official CT defect labels from the procès-verbal (they are legal text). In English, show the function name and severity in English, then the original label with a « French wording from the inspection report » note. Car model and trim names stay as the maker writes them.
- **French typography:** a no-break space inside « » and before ; : ! ?, and between a figure and its unit (« 7 123 € », « 36 785 km »), so nothing breaks onto a line of its own. Put these in `fr.json`, not in code.
- **Formats:** `Intl.NumberFormat` and `Intl.DateTimeFormat` with the page locale: « 10 182 € » and « lundi 9 novembre » in French; « €10,182 » and « Monday 9 November » in English (en-GB).
- **Glossary** (keep it in `en.json` and the method page):

| French | English |
|---|---|
| contrôle technique (CT) | French roadworthiness test (CT) |
| CT favorable / défavorable | CT passed / failed |
| défaillance mineure / majeure / critique | minor / major / critical defect |
| contre-visite | re-inspection |
| mise à prix | starting price |
| enchère actuelle / prix affiché | current bid / displayed price |
| frais acheteur | buyer's premium |
| TVA non récupérable | non-recoverable VAT |
| coût total estimé | estimated total cost |
| profil favorable / correct / risque élevé | favourable / fair / high risk |
| À regarder d'abord | Look at these first |
| Voir l'annonce sur Alcopa | View the listing on Alcopa (in French) |

- **The listing link in English** says « in French »: Alcopa's pages are French only.
- The method page and the legal pages need an English version before an English launch. Until then, the English footer links to the French pages with « (en français) ».

---

## 6. Bid simulator (in scope now)

This is **arithmetic on the buyer's own number, not a ceiling recommendation**, so it is consistent with `docs/BIDDING_CEILING.md` §2. It must never show a « plafond » we chose.

**Inputs:**
- « Si j'enchéris jusqu'à [P] € » → « je paierai environ [g(P)] »;
- « Si mon budget total est [T] € » → « j'enchéris au plus [B] ».

**Formulas** (with `fee_model` and the lot's `vat_applies`):
- `v = vat_applies ? vat_rate : 0`, `R` = repairs, `C` = CT redo.
- **Room sale:**
  - `g(P) = (1+v)·P + max(rate·P, min) + R + C`
  - `B = min((T−R−C)/(1+v+rate), (T−R−C−min)/(1+v))`
- **Online sale:**
  - `g(P) = (1+v)·P + R + C`
  - `B = (T−R−C)/(1+v)`
- **Floor B** to the euro, and never round up.

**Default values:** P = the starting price rounded up to the next 1 000 €, T = the current estimated total rounded up to the next 1 000 €.

**Edge cases:**
- `B ≤ 0` → « Ce budget ne couvre pas les frais et réparations. »
- `B < mise à prix` → « Sous la mise à prix (X €) : ce budget ne permet pas d'ouvrir les enchères. »

**Fixed copy:**
- the sensitivity line « Chaque 100 € d'enchère ajoute N € » (N = 100·(1+v+rate) on the rate branch; hide it on the minimum-fee branch);
- « Le pas d'enchère de la salle n'est pas connu. Ce calcul ne dit pas ce que vaut la voiture. »

**Implementation:**
- `<input type="number" inputmode="numeric">`, results in `<output aria-live="polite">`;
- computed on input with no animation;
- values persist per lot in `sessionStorage` (wrapped in try/catch).

**Check values:**

| Lot | Input | Expected output |
|---|---|---|
| 538 (v = 0.2) | P = 6 000 | 8 064 € |
| 538 (v = 0.2) | T = 8 000 | 5 952 € |
| 816 (v = 0) | P = 5 000 | 5 720 € |
| 816 (v = 0) | T = 6 000 | 5 244 € |
| 751 | T = 10 000 | 8 741 €, below the start → the edge-case message |

---

## 7. Motion

| Moment | Effect | Duration |
|---|---|---|
| Phone sheet open/close | translateX(100% → 0), list keeps its scroll | 240ms |
| Desktop: another lot selected | sheet content crossfade | 120ms |
| Waffle legend filter | non-matching cells to 20 % opacity | 160ms |
| Row hover (fine pointers) / button press | background; scale .97 | 120ms |
| Reduced motion | no transforms; fades ≤ 120ms | — |

- **Easing:** `cubic-bezier(0.23, 1, 0.32, 1)`.
- **Remove v1's hero choreography.**
- Nothing animates on keyboard navigation, on scroll jumps, or on numbers.

## 8. Accessibility (on top of v1)
- The waffle has `role="img"` with an aria-label that carries all four counts, and the legend buttons are the interactive layer.
- Each row's CT grid is `aria-hidden`, because the adjacent text carries the meaning.
- In the sheet, the grid is a `<ul>` of 9 `<li>`, each with name and state.
- Score tiers use different shapes (filled dot, ring, triangle), and colour is never the only cue.
- Focus ring: `outline: 2px solid var(--ink); outline-offset: 2px`, on yellow too.
- Contrast: every token pair is listed in §4.1. Extend the existing contrast test.

## 9. Out of scope, rejected on purpose

| Idea (from brief 2) | Why not |
|---|---|
| Car photos, hover carousels | Owner decision (d) in MASTER_PLAN: no photos, for legal reasons (Alcopa CGV, database right, plates). Photos stay on the listing. |
| « Arbitrage spread », « −28 % vs market », La Centrale comparison | 572 of 573 lots have no value reference, and scraping a classifieds site adds database-right risk. See BIDDING_CEILING §3. |
| « Super Affaire » / « Deal of the week » hero with a glowing score ring | 0 green lots this week, and a "deal" promise is a liability for a free second opinion. The plain score ring is kept (§5.4), without glow or "deal" wording. |
| Multi-house pill carousel with lot counts | Only Alcopa Beauvais is covered. The sale pill becomes a switcher when the data has more than one sale. |
| Ticking « Closes in 2d 14h 22m » countdown | Urgency theatre, and the data is a weekly snapshot. « demain » is enough. |
| Fee copy « ~14.5 % + 450 € » | Wrong: the code uses 14.4 % TTC with a 360 € minimum. Always read fees from `fee_model`. |
| « 65 % of buyers browse on mobile » | Unsourced. Design for the phone anyway. |
| Crit'Air filter | Not in the snapshot. It would need the Euro norm and first-registration date. Revisit with data. |
| Car silhouette with defects mapped to body parts | CT functions are not body locations. The 3×3 function grid is accurate and scannable. |
| Maintenance timeline from the dealer PDF | Not parsed into the snapshot. |
| Next.js, shadcn, Framer Motion, Tailwind | A rewrite with no buyer benefit. It breaks the CSP and no-Node constraints. |
| Glassmorphism, mesh gradients, glow, 3D tilt | The generic 2024 AI-SaaS look, and costly on low-end phones. |

## 10. Owner decisions still open
1. **Public name.** "Auction France" is generic, English, hard to protect, and could read as an official body. Shortlist a French alternative and check it at INPI before launch.
2. **Self-hosted fonts.** About 130 KB in total (provided in `fonts/`). Accept or reject.
3. **Phone sheet as a full-screen view** instead of inline expansion.
4. **Bid simulator.** Ship it now as buyer arithmetic, while keeping « Plafond d'enchère indicatif » withheld.
5. **Launch, indexing, legal pages.** Unchanged: a separate decision, with a lawyer.
6. **Four ring colours, three tiers.** The scoring has three tiers (favorable ≥ 80, correct 60–79, risque < 60). The ring splits « correct » into yellow (70–79) and orange (60–69). Either keep that as a visual gradation only, as designed, or make it a fourth named tier in the scoring and the method page.
7. **Scope of English.** UI and rule output in English now; method and legal pages translated before any English launch; CT defect labels kept in French with an English function name.

## 11. Acceptance checklist
- [ ] B1 and B2 fixed with regression tests. Snapshot regenerated, and lot 587 no longer in « À regarder d'abord ».
- [ ] B3 fields present. The page still renders a schema-1 and an older schema-2 snapshot, falling back to a CT chip when there is no `ct_grid`.
- [ ] At 1440×900, the hero h1, the search form and the waffle are visible without scrolling. Opening any row shows « Voir l'annonce sur Alcopa » without scrolling the pane.
- [ ] At 375×812, the search field is visible without scrolling. On a lot, the listing button is visible both in the header and in the sticky bar.
- [ ] Waffle counts equal `counts` and the per-lot `ct_result`. Legend filters change the list and the live count.
- [ ] The simulator reproduces the §6 check values.
- [ ] The hero headline names no sale or room. The sale card, coverage strip and sale label read from `sale` / `sales[]`, and work with one sale.
- [ ] Score rings: 79 → yellow, 69 → orange, 57 → red, 84 → green; the dash length matches the score; the number is readable at 36px.
- [ ] FR | EN switch on every page. `/en/` renders every string from `en.json` with no French left except CT labels, model names and the « in French » listing note. Switching keeps the open lot and filters. Numbers and dates follow the locale.
- [ ] Test cases:
  - lot 751, 0 defects;
  - lot 538, VAT plus minors;
  - lot 816, engine gate;
  - a compact red lot;
  - lot 501, no CT;
  - a missing URL;
  - `#lot-1212` (absent);
  - an empty filter result;
  - dark theme;
  - reduced motion;
  - forced colours.
- [ ] `tests/test_site.py` updated per §3, all existing suites green, and before/after screenshots in `data/redesign-review/v2/`.
