# Proposal: « Plafond d'enchère indicatif » (indicative bidding ceiling)

> **Status: proposal only, 2026-11-08.** Nothing here is built. The owner decided the ceiling is developed separately from the site redesign and is **not shown anywhere in the site** (`web/`, the snapshot or the report) until the calculation can support it. This document changes no code. Nothing in it is legal or financial advice.
>
> **Evidence.** Every claim cites a `file:line` on branch `claude/relaxed-albattani-3w9fgh` (working tree of 2026-11-08) or a read-only query on `data/auction.db`. The database was opened as `file:…/auction.db?mode=ro` and the exact SQL is in the [appendix](#appendix-queries-run). `web/` is being edited by the redesign session, so its line numbers may drift.

## Summary
- **The cost side exists.** The site already shows an estimated all-in cost per lot. Inverting it from a buyer's maximum total spend *T* to a maximum bid *B* is exact arithmetic on the fee model (§2).
- **The value side does not exist.** Nothing in the data says what a car is worth.
  - The only price comparison (« Prix ») compares opening prices with other opening prices from the same week's catalogue.
  - On the current database it produces no comparison at all: 572 of 573 cars are `value_unknown:few_comparables` and the last is `value_unknown:price_unknown`.
  - No sale outcome or hammer price has ever been stored.
- **A defensible ceiling needs, in this order** (§3, §6):
  1. collected sale outcomes;
  2. dated, verified auction terms;
  3. a licensed or own reference value with enough comparables;
  4. sourced repair costs;
  5. a stated uncertainty margin;
  6. hard rules for withholding the number.

---

## 1. What exists today

### 1.1 The cost estimate (« Coût total estimé »)
```
total = P + VAT + fees + repairs + ct_redo
```
- **P**, the price the estimate starts from, is `max(starting_price, current_price_attr)` of the car's best listing (`auction_agent/scoring/service.py:188-192`). The listing values are read as follows:
  - the card's « Mise à prix » goes to `starting_price` (`auction_agent/adapters/alcopa.py:240-242`);
  - the card's `data-current-price`, or « Enchère courante » when a card shows that instead, goes to `current_price_attr` (`alcopa.py:262-270`);
  - the detail page does the same (`alcopa.py:435-441`).
  - Per the plan, `data-current-price` holds the opening price until bids arrive (`docs/MASTER_PLAN.md:279`, medium confidence).
- **The price basis** (`service.py:195-201`) is `"start"` when `starting_price` is known and at least P, otherwise `"bid"`. It is saved with the price parts (`service.py:145-146`) and published as `detail.price.basis` (`auction_agent/snapshot.py:95-96`).
- **VAT and fees** come from `AlcopaAdapter.fee_split` / `fee_model` (`alcopa.py:828-837`), with constants at `alcopa.py:666-670`:
  - `ROOM_FEE_RATE_TTC = 0.144`, `ROOM_FEE_MIN_TTC = 360.0`, `VAT_RATE = 0.20`. The source comment reads "per the CGV as read by a third party (medium confidence)" and carries **no date** (`alcopa.py:666-667`). The plan also calls these numbers "config, to be checked against the CGV" (`MASTER_PLAN.md:281`).
  - VAT is `0.20 × P` only when `vat_regime == "ht_recuperable"` and the buyer is `"particulier"` (`alcopa.py:831`). Every other regime, `unknown` included, adds no VAT.
  - Fees are `max(0.144 × P, 360)` in a room sale and `0` online. Online sale prices are taken to include fees (`alcopa.py:667, 832`).
  - `fee_model` rounds the sum to the cent (`alcopa.py:837`).
  - The buyer profile defaults to `particulier` (`auction_agent/config.py:244`).
- **The VAT regime** is read from the detail page's « TVA récupérable » row (`alcopa.py:375-377`):
  - « Oui » → `ht_recuperable`;
  - « Non » → `ttc`;
  - anything else → `unknown`, which is also the column default for a car never detailed (`auction_agent/db.py:78-79`).
  - `marge` is allowed by the schema but **never produced** by the adapter.
  - Today: 503 `ttc`, 70 `ht_recuperable`, 0 `unknown` or `marge` (query Q5).
- **Repairs** (`auction_agent/scoring/heuristics.py:30-34`, applied at `:523-539`):
  - a flat, **unsourced** cost per CT system for each *major* defect, for example `structure` 600 €, `direction` 300 €, `freinage` 250 €;
  - 150 € for a system not in the table (`:527`); a critical defect counts double (`:528`);
  - **minor defects count 0**, since only `major` and `critical` are summed (`:526`).
  - 518 of the 573 lots carry a repair estimate above 0 (query Q12).
- **CT redo**: a flat 80 € (`heuristics.py:34`). It is added when a private buyer would need a fresh CT, judged at the sale date + 21 days (`heuristics.py:449-460`). 29 lots carry it (query Q12).
- **Not modelled**:
  - fixed per-lot fees, which the site says are not included (`snapshot.py:66-70`, `web/about.html:52`);
  - the bid increment;
  - carte grise, transport and storage.
- **The AI never sees a price.** `llm_payload` sends none (`service.py:342-361`), and the system prompt forbids the topic (`auction_agent/scoring/llm.py:3, 54`). Any ceiling must therefore be computed in code.

### 1.2 The « Prix » comparison group and why it cannot give a ceiling
- **Which cars.** `build_cohort` (`heuristics.py:553-564`) takes every *cohort-eligible* car (`heuristics.py:179-186`): it has an effective price, no gate, and a CT result that is neither major nor critical. Each car is keyed by (brand, model family, fuel, gearbox), and separately by (brand, fuel).
- **What it compares.** `value_score` (`heuristics.py:567-591`) takes the **median of those cars' effective prices**. It tries three steps, needing n ≥ 5, 5 and 8 (`:576-580`; `MASTER_PLAN.md:315-319`). Below that, V = 50 and the basis is `value_unknown`.
- **Where the cars come from.** The facts are loaded only from `vehicles WHERE lifecycle = 'active'` (`service.py:261`), and the cohort is rebuilt from them on every run (`service.py:417-422`). In practice that is **the current Alcopa catalogue**.
- **Why it says nothing about worth.** The effective prices are built from P, which is the **opening price** or an unverified "bid" (§1.3). The group therefore measures how a seller priced a car against other sellers' opening prices in the same week. It says nothing about what cars actually sell for.
- **No sale outcome exists to fix this.**
  - `vehicles.outcome` (`db.py:95`) is NULL for 573 of 573 cars (query Q3).
  - The `listings` table has no hammer-price column (`db.py:105-117`; query Q2).
  - There is no `sale_outcomes` table yet (query Q1): it is a Phase 2 migration (`MASTER_PLAN.md:140-145, 401, 423`).

**Re-verified on 2026-11-08.** The cohort was rebuilt in memory exactly as `plan_reviews` does it, with `load_facts` → `assess` → `build_cohort` → `value_score` on a read-only connection (query Q13).

| Quantity | Today |
|---|---|
| Active cars | 573 (Q3) |
| Cohort-eligible cars | **46** |
| Model groups / largest | 40 groups / **2** cars (n ≥ 5 needed) |
| Brand+fuel groups / largest | 27 groups / 4 cars (n ≥ 8 needed) |
| `value_basis` | **572** `value_unknown:few_comparables`, 1 `value_unknown:price_unknown` (Q10) |
| Tier / confidence | 384 red, 189 yellow, 0 green; 573 `partial` (Q11) |

### 1.3 The "bid" basis is very likely the opening price, mislabelled
- 43 listings have basis `"bid"`, and **every one has `starting_price` NULL** (queries Q6, Q9).
- **No** listing has `current_price_attr > starting_price` (query Q7). For the 529 listings where both values are known, they are **equal** (query Q8).
- All listings were read between 2026-11-07 09:29Z and 09:39Z (query Q14), two days before the 9 November room sale (query Q4).
- So the "bid" lots are cards with no readable « Mise à prix » whose `data-current-price` is, by the plan's own reading (`MASTER_PLAN.md:279`), the opening price. Five of them are detailed cards in the current snapshot: lots 553, 554, 579, 1157 and 511.
- The redesign already shows « Prix affiché » rather than « Enchère actuelle » for these lots (`web/app.js:390-393`). The data label is still `"bid"`, and `tests/test_mvp.py:553-554` pins that behaviour.

---

## 2. From a maximum total spend *T* to a maximum bid *B*

### 2.1 Inversion of the code's fee model
**Notation**
- *T*: the most the buyer will spend in total on the car, in the same scope as « Coût total estimé ».
- *R*: repairs; *C*: CT redo (both §1.1).
- `X = T − R − C`
- `v = 0.20` only when the lot is « TVA récupérable » (`ht_recuperable`) **and** the buyer is a `particulier`; otherwise `v = 0` (`alcopa.py:831`).
- `f = 0.144` and `m = 360` (`alcopa.py:668-669`).

**Room sale.** The cost of a bid P is `g(P) = (1+v)·P + max(f·P, m) = max((1+v+f)·P, (1+v)·P + m)`. Both branches are increasing, so the inverse of the max is the min of the inverses:
```
B = min( X / (1 + v + f),  (X − m) / (1 + v) )
```
- The two branches meet at `P* = m / f = 2,500 €`, that is at `X* = 2,860 €` when v = 0 and `3,360 €` when v = 0.2.
- Above that point the 14.4 % rate binds; below it, the 360 € minimum binds.

**Online sale** (no fees, `alcopa.py:832`):
```
B = X / (1 + v)
```

**Rules**
1. **No viable bid** when `B ≤ 0`. This happens when repairs and the CT already use up *T*, or in a room sale when `X ≤ 360`.
2. **No viable bid at this spend** when `B <` the « Mise à prix ». The buyer cannot even open the bidding.
3. **Floor, never round.** Floor *B* to the cent, so that `fee_model(B) + R + C ≤ T` always holds; the checks below give 9,999.99 and 15,999.99. Then floor to the bid increment, which is **unknown today**. Until it is verified, a ceiling cannot be stated to a real bidding step (§3.4).
4. **Fixed per-lot fee.** Once a fixed per-lot fee *K* is verified, use `X = T − R − C − K`.
5. ***T* excludes** carte grise, transport, storage and any fixed per-lot fee, as « Coût total estimé » does today (`snapshot.py:66-70`).

### 2.2 Worked examples
The real lots come from `data/snapshot-local/snapshot/latest.json` (schema 2, generated 2026-11-08T01:10:50Z).
- Their `detail.price` is identical in the database rescored at 11:17:10Z (query Q15).
- All are room sales with a `particulier` buyer.
- *B* was recomputed with the formula above, floored to the cent, and checked by passing it back through the real `AlcopaAdapter.fee_model` / `fee_split`.

| Lot | Car | VAT regime | « Mise à prix » | *R* + *C* | *T* | *B* | Check: VAT + fees → total | Reading |
|---|---|---|---|---|---|---|---|---|
| 751 | MINI Hatch 5 portes F55 LCI, 2019 | `ttc` (v = 0) | 8,900 € | 0 | 10,181.60 € (its « Coût total estimé ») | **8,900.00 €** | 0 + 1,281.60 → 10,181.60 | Round trip exact |
| 751 | same | same | 8,900 € | 0 | 10,000 € | **8,741.25 €** | 0 + 1,258.74 → 9,999.99 | **Below the « Mise à prix »: no viable bid at this spend** |
| 751 | same | same | 8,900 € | 0 | 12,000 € | **10,489.51 €** | 0 + 1,510.49 → 12,000.00 | Rate branch (`X/1.144`) |
| 866 | Peugeot 308 SW PHEV 180 e-EAT8, 2022 | `ht_recuperable` (v = 0.2) | 11,400 € | 0 | 15,321.60 € (its « Coût total estimé ») | **11,400.00 €** | 2,280.00 + 1,641.60 → 15,321.60 | Round trip exact (`X/1.344`) |
| 866 | same | same | 11,400 € | 0 | 16,000 € | **11,904.76 €** | 2,380.95 + 1,714.29 → 16,000.00 | VAT costs about 2,081 € of bidding room against a pro |
| 866 | same, **pro** buyer | v = 0 | 11,400 € | 0 | 16,000 € | **13,986.01 €** | 0 + 2,013.99 → 16,000.00 | The code still charges a pro the TTC fee rate (§3.4) |
| 866 | same, if it were an **online** sale | v = 0.2 | — | 0 | 16,000 € | **13,333.33 €** | `X/1.2` | Illustration only: lot 866 is a room lot |
| 503 | Hyundai Tucson, 2019 (CT défavorable majeure) | `ttc` | 13,100 € | 600 (two major `nuisances` defects at 300 € each, `heuristics.py:32`) | 16,000 € | **13,461.53 €** | 0 + 1,938.46 + 600 → 15,999.99 | The repairs come straight off the bidding room |
| — | *synthetic* | `ttc` | — | 0 | 2,000 € | **1,640.00 €** | 0 + 360.00 → 2,000.00 | Minimum branch (`X − 360`) |
| — | *synthetic* | `ttc` | — | 0 | 2,860 € | **2,500.00 €** | 0 + 360.00 → 2,860.00 | The kink: both branches are equal |

**What these examples do *not* say.** None of these *T* values is a market value; each is just a spend chosen for the example. Lots 751, 866 and 503 are all `value_unknown:few_comparables` (query Q15). Today nothing tells a buyer whether 12,000 € or 16,000 € is a sensible spend for these cars. Supplying a justified *T* is what §3 is about.

---

## 3. What a defensible ceiling needs
The arithmetic in §2 is only as good as *T*. An **indicative ceiling** is the inversion applied to a *reference-based* spend:
```
T_ref = (a conservative quantile of comparable all-in values) × (1 − safety margin)
B     = invert(min(T_ref, T_buyer))      -- a buyer's own budget can only lower it
```
Each input below has a proposed rule. The thresholds are **proposals to calibrate in Phase P3** (§6), not findings.

### 3.1 A reference value
**Sold or hammer prices**
- **Alcopa.** Alcopa publishes no hammer prices (`MASTER_PLAN.md:64`, medium confidence). Whether a « VENDU » badge or a price appears after a sale is still an open recon item (`MASTER_PLAN.md:558`).
- **What an Alcopa-only archive can give.** The planned VENDU probe records only `sold` / `not_sold` / `unknown` (`MASTER_PLAN.md:181`). The planned `sale_outcomes` row holds `first_price`, `last_price`, `price_kind`, `all_in_last`, `bid_observed` and `outcome`, but no hammer price (`MASTER_PLAN.md:141-144`). These are **censored observations**, not prices:
  - "sold at an opening price of X" bounds the hammer price from below, *if* a car cannot sell below its « Mise à prix ». That is an assumption to verify in recon.
  - "not sold at X, relisted at Y < X and sold" brackets it.
  - The estimator must treat these as intervals, never as point prices.
- **VP Auto « Adjugé »** prices (medium confidence) are the planned first real price source (`MASTER_PLAN.md:64, 284`; Phases 5-6 at `:404-405`).

**External market sources.** Optional licensed L'argus or Autovista data (`MASTER_PLAN.md:64`). **Never scrape La Centrale or leboncoin** (`MASTER_PLAN.md:64`).

**Licensing and law are launch blockers, not details.**
- These include the sui generis database right, French aggregator case law and the Alcopa CGV (`MASTER_PLAN.md:65-71`).
- Phase 6 makes "licence outreach; lawyer review" its exit, with "Lawyer sign-off" (`MASTER_PLAN.md:405`).
- Publishing a ceiling on another house's lots, even privately, should be part of that review.

### 3.2 Minimum sample size and similarity rules (proposed)
- **Same key as today's model step** (`heuristics.py:562, 576-577`): brand, model family (`heuristics.py:284-293`), fuel and gearbox; year ±1; mileage band ±1 of 50,000 km.
- **Comparable on the same terms.** Use the same VAT regime and sale kind, and compare **all-in** values computed with the *same dated* fee terms (`fee_model_version` is already planned in `sale_outcomes`, `MASTER_PLAN.md:143`).
- **Exclusions**
  - gated cars and cars with a major or critical CT (today's eligibility, `heuristics.py:179-186`);
  - the car itself and its own earlier listings: relists and re-consigned cars, through `vin_hmac` / `vehicle_ids` (`MASTER_PLAN.md:145, 193`).
- **Size: n ≥ 8** sold comparables with a usable price or bracket. That is more than today's value-score minimum of 5, because a number a buyer bids against needs more support than a score nudge.
- **No widening to brand + fuel** for a ceiling: a brand-wide median says little about one model.
- **Spread.** If the interquartile range is more than 25 % of the median, withhold the number.
- Today none of this can be met: the largest model group is 2 and no group has a single outcome (§1.2).

### 3.3 Freshness window (proposed)
- Comparables sold within the last **6 months** count. Older ones up to 12 months count only with a stated decay, and are never the majority.
- The reference must be rebuilt at every scoring run. The rules already recompute everything each run (`heuristics.py:8-9`).
- The ceiling is for **before the sale** only. Withhold it once the sale is `in_progress` or `over` (`snapshot.py:251-263`): by then the opening price no longer describes the bidding.

### 3.4 Dated, verified auction terms
- **Room fees**: 14.4 % TTC with a 360 € minimum, at **medium confidence and undated** (`alcopa.py:666-667`). These need a dated copy of the CGV (recon item 2, `MASTER_PLAN.md:550`), stored with a source URL, read date and confidence.
- **Fixed per-lot fees** are not modelled (`snapshot.py:70`, `web/about.html:52`). They enter as *K* in §2.1 once known.
- **Bid increment**: unknown and needed for flooring *B* (§2.1, rule 3).
- **A pro buyer** is charged the TTC fee rate (`alcopa.py:832` does not depend on the buyer). A VAT-registered pro may recover the VAT inside the fee (12 % HT). To verify.
- **Online sales**: that displayed prices include fees comes from the same third-party reading (`alcopa.py:667`). Whether VAT on an `ht_recuperable` car applies to the fee-inclusive price is unverified.
- **Rule.** Until every term above has a source and a read date, the ceiling is withheld with `terms_unverified`.

### 3.5 VAT handling
- **`unknown`**: `fee_split` adds no VAT for it (`alcopa.py:831`). For a private buyer that can understate the cost by 20 % of P, so **withhold** (`vat_unknown`). There are 0 such lots today (query Q5), but any car whose detail page lacked the row would be one.
- **`marge`** (margin scheme): the adapter folds « Non » into `ttc` (`alcopa.py:377`), which costs the same as `marge` for a private buyer. Make it explicit if a source ever distinguishes them.
- **Profile**: `v` depends on the buyer profile (`config.py:244`). A ceiling must state which profile it assumes.

### 3.6 Sourced repair costs
- **Today**: a flat, unsourced table (`heuristics.py:30-34`) with a 150 € default (`:527`). Minor defects count 0 (`:526`), and the 80 € CT redo is unsourced (`:34`).
- **Why it matters here.** Repairs are subtracted one-for-one from the bidding room (lot 503, §2.2), and 518 of 573 lots carry them (Q12). An error in the table becomes an error in *B*.
- **Needed**: dated cost ranges per CT system with a source, which is Phase 3's "repair-cost table" (`MASTER_PLAN.md:402`). The ceiling should then use the **upper** end of each range, the conservative side for a maximum bid.
- **Unmodelled red gates.** A red gate such as « moteur HS », « boîte HS » or « non roulant » (`heuristics.py:53-57`; `MASTER_PLAN.md:327`) has no repair figure, so the ceiling is withheld for those cars.

### 3.7 Uncertainty margin and how it is shown (proposed)
- **Inputs**: `T_ref` uses the **25th percentile** of comparable all-in values (or of the interval estimate), minus a safety margin (proposed 5 %).
- **Range**: `[invert(P25), invert(P50)]`, with the lower bound as the headline.
- **Display**: the headline, the range, n and the comparables' date window, always floored to the bid increment and never to the cent. For example: « Plafond d'enchère indicatif : 10 400 € (fourchette 10 400 – 11 200 €, 12 ventes comparables, mai – oct. 2026) ».
- **Assumptions shown beside it**: buyer profile, VAT applied, fee terms and their date, repairs counted.

### 3.8 Explicit withholding rules
The ceiling is **withheld** (status `withheld`, with every applicable code) when any of these holds:

| Code | Condition | Today |
|---|---|---|
| `no_reference` | No outcome or licensed reference for the car's comparables | **Always**: `outcome` NULL 573/573 (Q3) |
| `few_comparables` | n < 8 after the similarity rules (§3.2) | Largest model group is 2 (Q13) |
| `wide_spread` | IQR > 25 % of the median | — |
| `stale_reference` | Newest comparable older than the freshness window (§3.3) | — |
| `terms_unverified` | Fee rate, minimum, fixed per-lot fee or increment without a source and read date (§3.4) | **Always** (`alcopa.py:666-667`) |
| `vat_unknown` | `vat_regime = 'unknown'` (§3.5) | 0 lots (Q5) |
| `ct_unread` | CT absent on a car ≥ 4 years, or unreadable / uncertain (`heuristics.py:401-427`): repairs and CT redo unknown | 5 `ct_unreadable` in the snapshot's `partial_why` |
| `unmodelled_risk` | A red gate, or a gate whose cost is not modelled: engine risk, battery SoH, mileage not guaranteed, odometer inconsistent (`heuristics.py:392-398, 447-448, 471-477, 498-506`) | 314 cars have at least one gate (Q13) |
| `opening_price_unknown` | No readable « Mise à prix » (`starting_price` NULL), so rule 2 of §2.1 cannot be checked | 43 listings (Q6) |
| `provisional` | Provisional snapshot (`snapshot.py:354`), a crawl that was not complete (`sync_runs.status` ≠ `ok`), or mileage unknown | The last crawls were `incomplete` (Q16) |
| `sale_started` | Sale `in_progress` or `over` (`snapshot.py:251-263`) | — |

---

## 4. Labels
Keep four labels distinct, each with one meaning:

| Label | Meaning | Data |
|---|---|---|
| « Mise à prix » | The opening price the house published | `starting_price` |
| « Enchère actuelle » | A bid **observed strictly above** the « Mise à prix ». Never used when the opening price is unknown. | `current_price_attr > starting_price` (0 such listings today, Q7) |
| « Coût total estimé » | What the buyer pays at a given price: P + VAT + fees + repairs + CT redo, **excluding** fixed per-lot fees, carte grise and transport | `detail.price` (`snapshot.py:88-97`) |
| « Plafond d'enchère indicatif » | The highest bid that keeps « Coût total estimé » within the reference-based spend of §3. Always with its range, n, date window and assumptions, or withheld with its reason. | Proposed `ceiling` (§5) |

**What to change first**
- **The data label.** Stop labelling a missing opening price `"bid"` (`service.py:195-201`). A distinct basis value or flag would let every reader tell « Prix affiché » from « Enchère actuelle » without guessing. The redesign does that guessing today (`web/app.js:390-393`). The change also updates `tests/test_mvp.py:553-554`.
- **UI wording.** The current UI uses « tout compris », « Coût estimé » and « Prix affiché » (`web/index.html:35`, `web/about.html:48`, `web/app.js:393`). Aligning them with the four labels is for the redesign session.

**Advice and liability risk** (not legal advice)
- **Why a number is riskier than a score.** A maximum-bid figure reads as a recommendation to bid up to it. If a buyer pays it for a car with hidden defects, or the reference was wrong, the site has stated a number that drove a purchase. A score or a list of facts does not do that.
- **Wording**
  - Always say « indicatif » and « selon nos hypothèses du [date] »;
  - add « ne constitue pas un conseil d'achat »;
  - list the assumptions next to the number;
  - never phrase it as a promise of value or resale.
  - This follows the plan's disparagement-safe labels (`MASTER_PLAN.md:361`).
- **Audience.** Keep it to the private, authenticated site (`MASTER_PLAN.md:388`). Never show it in public mode before the Phase 6 lawyer sign-off (`MASTER_PLAN.md:71, 405`), which should cover advice, consumer-protection and disparagement exposure, and whether the house objects to third-party ceilings on its lots.

---

## 5. Additive snapshot fields (schema stays 2)
**Constraints**
- The snapshot schema is 2 (`snapshot.py:29`), and schema changes are additive only: every field keeps its name and meaning (`snapshot.py:8-10`). The ceiling adds fields and changes none.
- **Card level, not inside `detail`.** `tests/test_mvp.py:521` asserts `set(detail) == DETAIL_KEYS` exactly, while cards may carry extra keys (`test_mvp.py:517`). Putting the object on the card keeps old tests and pages intact.
- **Key names.** Names must avoid the substrings the publish tests reject anywhere in the JSON, such as `registration`, `comments`, `photo` and a bare `"id"` key (`tests/test_mvp.py:88, 112`), as well as `BLOCKED_KEYS` (`tests/test_mvp.py:482-483`). The names below do.
- **Size and scope.** The snapshot is capped at 1.5 MB (`snapshot.py:36`). The object is only on the detailed cards (`top` + `cards`; 50 on the current snapshot), not on the `compact` lines.
- **Off by default.** It is not written until Phase P5 (§6).

**Per detailed card**
```jsonc
"ceiling": {
  "status": "withheld",               // "withheld" | "shown"
  "why": ["no_reference", "terms_unverified"],   // §3.8 codes, most important first; [] when shown
  "bid_max": null,                    // € headline, floored to `step`; null when withheld
  "range": null,                      // {"low": €, "high": €} = [invert(P25), invert(P50)]
  "step": null,                       // bid increment used for flooring (€); null = unknown
  "spend": null,                      // T_ref used (€)
  "reference": null,                  // {"kind": "sold_bracket"|"hammer"|"licensed", "n": 12,
                                      //  "p25": €, "p50": €, "p75": €, "from": "2026-05", "to": "2026-10"}
  "assumed": {                        // always present, so a withheld number still shows its basis
    "buyer": "particulier", "vat_rate": 0.2, "fee_rate": 0.144, "fee_min": 360, "fixed_fee": null,
    "repairs": 600.0, "ct_redo": 0.0, "sale_kind": "room"
  },
  "method": "bc-0.1"
}
```

**Top level**
```jsonc
"ceiling_method": {
  "version": "bc-0.1",
  "terms": {"read_on": null, "source": null, "confidence": "medium"},   // dated fee terms (§3.4)
  "shown": 0, "withheld": 50,
  "withheld_why": {"no_reference": 50, "terms_unverified": 50}
}
```

**One more additive field.** A `detail.price.start_known: true | false` flag, beside the existing `basis`, would carry the §1.3 fix without changing `basis`'s meaning for older pages.

**Where the values come from** (local only; nothing new leaves the PC):
- the ceiling, computed at scoring time and saved in `assessments.detail_json`, as the price parts already are (`service.py:136-152`, `MASTER_PLAN.md:392`);
- comparables from `sale_outcomes` (`MASTER_PLAN.md:141-144`) plus two additive columns there: `hammer_price` (NULL unless a house publishes it) and `price_source`.

---

## 6. Phased path
Each phase ends with something the owner can check. The thresholds are proposals.

| Phase | Work | Exit |
|---|---|---|
| **P0: prerequisites** | (a) Fix the `"bid"` label for a missing opening price (§1.3, §4; `service.py:195-201`, `tests/test_mvp.py:553-554`). (b) Keep any ceiling out of `web/` and the snapshot. | No listing with `starting_price` NULL has basis `"bid"`; no `ceiling` key in the published snapshot |
| **P1: collect sale outcomes** (MASTER_PLAN Phase 2, `:401`) | Lifecycle engine and dormancy (`MASTER_PLAN.md:78-189`); the VENDU outcome probe (`:181`); the `sale_outcomes` / `vehicle_ids` migration (`:140-145`); recon item 10, whether a badge or hammer price shows after a sale (`:558`); whether a car can sell below its « Mise à prix ». | ≥ 8 weekly Beauvais sales archived, ≥ 90 % of cars with `outcome ≠ unknown`, the below-« Mise à prix » question answered in writing |
| **P2: dated terms** | A dated copy of the CGV (recon item 2, `MASTER_PLAN.md:550`): fee rate, minimum, fixed per-lot fees, the increment table, VAT on fees for pros, online price inclusion. Stored as dated config with a `fee_model_version`. | Every §3.4 term has a source URL, read date and high confidence |
| **P3: shadow computation** | Compute `ceiling` at scoring time into `assessments.detail_json` and an owner-only report, **never** the snapshot. Apply §3.8. Backtest against P1 outcomes: how often a sold comparable's bracket sits below the ceiling; calibrate n, spread, the quantile and the margin. | The owner reviews ≥ 50 shadow ceilings; the withheld share and backtest results are written down |
| **P4: reference beyond Alcopa** | VP Auto « Adjugé » adapter (`MASTER_PLAN.md:404`) and/or licensed L'argus or Autovista data; licence outreach and lawyer review (Phase 6, `MASTER_PLAN.md:405`). Sourced repair-cost ranges (Phase 3, `:402`). | A written licence or legal view on using the source; repair table with sources |
| **P5: owner-only display, then decide** | Add the §5 fields behind a flag on the private site, with the §4 wording. Public display only after the Phase 6 lawyer sign-off. | Owner approval; for public mode, lawyer sign-off (`MASTER_PLAN.md:405`) |

---

## Appendix: queries run
Run on 2026-11-08 against `data/auction.db`, opened as `sqlite3.connect("file:…/data/auction.db?mode=ro", uri=True)` (read-only; no writes are possible on that connection).

| # | Query | Result |
|---|---|---|
| Q0 | `PRAGMA user_version` | 4 |
| Q1 | `SELECT name FROM sqlite_master WHERE type='table'` | assessments, documents, listings, llm_assessments, meta, sales, sqlite_sequence, sync_runs, vehicles (**no** `sale_outcomes`) |
| Q2 | `PRAGMA table_info(listings)` | id, vehicle_id, sale_id, lot_number, detail_url, starting_price, current_price_attr, closing_hint_at, first_seen_at, last_seen_at (**no** hammer price) |
| Q3 | `SELECT lifecycle, count(*) FROM vehicles GROUP BY lifecycle` · `SELECT outcome IS NULL, count(*) FROM vehicles GROUP BY 1` | active 573 · outcome NULL 573 |
| Q4 | `SELECT id, external_id, kind, room, starts_at, status, expected_lots FROM sales` | 1 sale: 10209, room, Beauvais, starts 2026-11-08T23:00:00Z (9 Nov, Paris), upcoming, 693 lots announced |
| Q5 | `SELECT vat_regime, count(*) FROM vehicles WHERE lifecycle='active' GROUP BY vat_regime` | ht_recuperable 70, ttc 503 |
| Q6 | `SELECT starting_price IS NOT NULL, current_price_attr IS NOT NULL, count(*) FROM listings GROUP BY 1, 2` | (no, no) 1 · (no, yes) 43 · (yes, yes) 529 |
| Q7 | `SELECT count(*) FROM listings WHERE current_price_attr > starting_price` | 0 |
| Q8 | `SELECT count(*) FROM listings WHERE starting_price = current_price_attr` · `… WHERE current_price_attr < starting_price` | 529 · 0 |
| Q9 | `SELECT json_extract(detail_json,'$.price.basis'), count(*) FROM assessments GROUP BY 1` | start 529, bid 43, NULL 1; all 43 "bid" rows have `starting_price` NULL (joined to `listings`) |
| Q10 | `SELECT value_basis, count(*) FROM assessments GROUP BY 1` | value_unknown:few_comparables 572, value_unknown:price_unknown 1 |
| Q11 | `SELECT tier, count(*) …` · `SELECT confidence, count(*) …` (assessments) | red 384, yellow 189 · partial 573 |
| Q12 | `SELECT count(*), sum(json_extract(detail_json,'$.price.repairs') > 0), sum(json_extract(detail_json,'$.price.ct_redo') > 0) FROM assessments` | 573, 518, 29 |
| Q13 | In memory on the same read-only connection: `service.load_facts` → `heuristics.assess` → `build_cohort` → `value_score`, as `service.plan_reviews` does (`service.py:417-422`) | 573 facts; 46 cohort-eligible; 314 with gates; 40 model groups, largest 2; 27 brand+fuel groups, largest 4; 572 few_comparables + 1 price_unknown |
| Q14 | `SELECT min(first_seen_at), max(last_seen_at) FROM listings` | 2026-11-07T09:29:21Z, 2026-11-07T09:39:44Z |
| Q15 | `SELECT l.lot_number, v.vat_regime, s.kind, json_extract(a.detail_json,'$.price'), a.value_basis FROM assessments a JOIN vehicles v … JOIN listings l … JOIN sales s … WHERE l.lot_number IN (503, 751, 866)` | 503 ttc room {13100, vat 0, fees 1886.4, repairs 600, total 15586.4}; 751 ttc room {8900, 0, 1281.6, 0, 10181.6}; 866 ht_recuperable room {11400, 2280, 1641.6, 0, 15321.6}; all `value_unknown:few_comparables`; assessments scored 2026-11-08T11:17:10Z |
| Q16 | `SELECT status, kind, started_at FROM sync_runs ORDER BY id DESC LIMIT 3` | incomplete (09:38Z), incomplete (09:34Z), blocked (09:29Z), all on 2026-11-07 |

The snapshot facts (lots 751 and 866, the 5 detailed "bid" lots, `counts.partial_why`, `counts.listed` = 50) were read from `data/snapshot-local/snapshot/latest.json` with `json.load`.
