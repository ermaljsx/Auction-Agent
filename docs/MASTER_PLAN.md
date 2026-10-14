# Auction-Agent: Master Plan

> Status: approved 2026-10-13. Phase 1 is implemented on branch `claude/relaxed-albattani-3w9fgh`; see Appendix C. Confidence labels (high / medium / low) follow every external fact. Nothing in this document is legal advice.

## Context
Auction-Agent is a CLI prototype: about 1,900 LOC and no tests. It crawls one hard-coded Alcopa sale, downloads CT PDFs and asks Gemini for a top 10. The goal is a multi-auction MVP web platform that:
- scores **every** vehicle into 🟢/🟡/🔴;
- keeps a zero-waste lifecycle: unsold relisted cars are retained without re-work, stale ones are purged.

**Owner decisions (confirmed):**
1. Purge heavy and identifying data, but keep a compact derived archive.
2. Never persist PDFs; extract in memory and keep structured data, a sha256 and the source URL. A short-TTL quarantine for unparseable PDFs is allowed.
3. Daily light poll per adapter plus a grace period before purging, instead of a single Tuesday job.
4. Commit this document and implement Phase 1 on `claude/relaxed-albattani-3w9fgh`.

**Deliberate deviations from the original brief (approved with this plan):**
- **(a) Relist detail check.** One detail-HTML re-fetch when a relisted car first appears in a new sale. No PDF and no LLM unless the CT reference changed.
- **(b) Engine-risk default.** Known engine flaws default to a heavy penalty plus max yellow, not automatic red. This is one config flag.
- **(c) Redacted CT text kept while active.** VIN/plate-redacted CT text is kept while the car is active, so the parser can be improved without re-fetching. It is NULLed at dormancy, then purged.
- **(d) No photos.** No photo URLs are stored or shown in any mode, for legal and plate-privacy reasons.
- **(e) No discovery in Phase 1.** Phase 1 keeps today's single-sale `--url` behaviour. Multi-sale discovery waits for live recon in Phase 2.

**Evidence base:**
- I read all the code myself.
- 9 research and verifier agents worked from about 40 code claims. They were reproduced with snippets on synthetic inputs and all were confirmed; D2, P6, P7, S7, S8 and A7 only partially.
- 4 adversarial critics reviewed the draft plan. Their fixes are incorporated, and the revised lifecycle SQL was re-simulated in SQLite over 12 days and behaved correctly.
- **No auction site could be fetched from this container** (proxy 403 and no DNS for WebFetch). Site facts come from search snippets and from third-party scrapers' fixtures dated Sept–Oct 2026, so each carries a confidence label (high / medium / low).

---

## 1. Executive critique

**Worth keeping:**
- the start of a normalised SQLite schema with FK cascades;
- the document-type split;
- local pre-disqualification before the LLM;
- a heuristic fallback;
- polite delays;
- a clear product vision.

**Verified defects** (mechanisms reproduced; site-side premises flagged where unverified):

| # | Sev | Defect | Where |
|---|---|---|---|
| 1 | CRIT | The PDF cache is keyed by `lot_{lot_number}` with skip-if-exists. Lot numbers restart every sale (lot 0 falls back to `veh_{id}`; numbered lots collide), so a **later car silently reuses an earlier car's CT**. | `scraper/document_downloader.py:35-37,95-109` |
| 2 | CRIT for purge | A partial listing crawl (page failure, selector drift, `--pages/--limit`) returns silently as if complete. | `scraper/listing_scraper.py:249-252,283` |
| 3 | HIGH | CT parser: plural headers (`DEFAILLANCES MAJEURES :`, seen on Enchères VO PVs; Alcopa layout unverified) never match. After a header, every line counts as a defect. The result is guessed by keyword: a legend leads to `CRITIQUE`, and NFD text turns `DÉFAVORABLE` into `FAVORABLE`. **The critical-defect gate can be bypassed.** | `pdf_extractor.py:63-115` |
| 4 | HIGH | The listing mileage regex spans lines, so `"1ère mise : 2024\n35 394 km"` becomes **202,435,394 km** (matches card markup quoted by third parties). | `listing_scraper.py:102` |
| 5 | HIGH | The `detail_url` identity plus `ON CONFLICT` overwrites every column, including **NULLs over good VIN/plate**. There is no listing history, no lifecycle fields and no scores table. | `database.py:58,133-178` |
| 6 | HIGH | Only `candidates[:80]` are sent (`batch_size` is unused) and only a top 10 is requested. Most cars get no score or tier, and red cars appear under "Top Recommandations". | `advisor/ai_advisor.py:115,135,282` |
| 7 | HIGH | Unescaped f-string HTML (XSS confirmed). A `None` from the LLM crashes rendering *after* the paid call, and the result is lost. | `ai_advisor.py:266-345` |
| 8 | HIGH | Detail failures are silent: no retry, and an empty document list is stored as "ingested". There is no per-vehicle error isolation. | `detail_scraper.py:85-91`, `crawler.py:73-124` |
| 9 | HIGH | `google-generativeai` support ended on 30 Nov 2025 (high confidence). `gemini-2.5-flash` shutdown dates conflict: Gemini API 16 Oct 2026 per a forum, Vertex 20 Oct, earlier 404s reported, and reportedly restricted to existing users (medium). The LLM path is about to break. | `ai_advisor.py:65`, `config.py:31` |
| 10 | MED | Other defects: <br>• connections are never closed (`with sqlite3.connect()`) and there is no WAL; <br>• `data-current-price` is stored as `starting_price`, but it holds the opening price until bids arrive (medium); <br>• the `.text-nowrap` price fallback returns garbage; <br>• `clean_int("1598 cm3")` returns 15983; <br>• the heuristic hard-codes 2026, invents defaults (2018 / 150k km / €10k) and scores any car of €25k or more at value 0. | various |

**Missing entirely:** sale discovery, per-house identity, completeness signals, run observability, an adapter interface, tests, a price-reference source, and a legal posture.

### Sparring: assumptions challenged
1. **"Tuesday sync after Monday auctions."** Alcopa room sales run Mon (Beauvais / Paris Sud / Rennes), Wed (Nancy), Thu (Lyon) and Fri (Marseille / Tours), plus near-daily online sales. Catalogues fill progressively: lot n°0 and a `--` price appear up to about 2 weeks ahead (medium). → Daily per-adapter polls (decided).
2. **"Absent from the new catalogue = sold = purge."** Absence can also mean a partial crawl, a WAF block, or that the car is not yet re-attached to a new sale. → A two-stage lifecycle (dormant, then archive) with completeness gates and outcome-aware grace periods (§2).
3. **"Exact match = touch nothing."** Prices are filled in or changed after first listing, and relists may carry a renewed CT or new damage comments (frequency unknown). → **Deliberate deviation from the brief:** on the *first sighting in a new sale*, re-fetch **one detail HTML page** (no PDF, no LLM). Download a CT only if its reference changed. Configurable via `RELIST_DETAIL_CHECK`.
4. **"Keep LLM costs near zero."** At Alcopa scale, trigger-only LLM use is single-digit €/week (unverified prices). The real reasons for heuristics-first are **determinism, testability and auditability**. The real costs are scraping reliability, data quality and legal exposure.
5. **"Known engine flaw → red."** Stellantis announced cover of 10y/180k km for PureTech belts and 10y/240k km for 1.5 BlueHDi chains (high confidence for the announced terms). However, claims require service invoices and network repair, auction cars rarely carry those, and end dates are unverified. → **Changed default:** a heavy penalty plus `max_tier=yellow`, without coverage credit until maintenance evidence is parsed. Your brief wanted red; this is a single config flag (`ENGINE_RISK_MAX_TIER`).
6. **"Fair price."** No price data exists today, so the LLM guesses from memory. Alcopa publishes no hammer prices; VP Auto shows "Adjugé" prices (medium). → Price in code from cohorts, then VP Auto results, then optional licensed L'argus or Autovista data. **Never scrape La Centrale or leboncoin**: both actively sue scrapers.
7. **"Public platform."** Legal risk is **high**:
   - the EU/French sui generis database right, including the CJEU *Innoweb v Wegener* (C-202/12) ruling against a dedicated car-ads meta-search;
   - French aggregator losses: Leboncoin v Entreparticuliers, and Jinka v SeLoger/leboncoin in 2025–26 (per research summaries);
   - the Alcopa CGV photo and description clause;
   - CNIL on plates and VINs.

   → A **private/invite MVP plus outreach for data licences** before going public. *Not legal advice; take this to a French IP and data-protection lawyer.*
8. **Anti-bot.** Third parties report an AWS WAF CAPTCHA on Alcopa in Aug–Sep 2026, with cloud IP ranges blocked (medium; another source scraped fine in late September). → **Never circumvent it.** Detect it and fail closed. If the deployment VPS is blocked, treat that as access denied, a partnership question, and do not relocate the crawler to evade it.
9. **Scale.** About 5.8k lot URLs appeared in one third-party read of the sitemap (low confidence). About 3.6–3.9k new product IDs per week across all categories (derived, medium), against about 3.1k cars sold per week. → The archive grows by about **20–50 MB/yr** (a simulation gave about 38 MB), not the "few MB" from my question.
10. **README ideas.** Async gains nothing at roughly 1 request per 2–3 s per host. Postgres isn't needed (single-writer SQLite in WAL mode). Pasting arbitrary links should work for supported houses only, with no generic LLM scraping in the MVP.

---

## 2. Lifecycle engine: "Smart Diff, Dormancy & Purge" (Phase 2; schema lands in Phase 1)

**State machine per vehicle:**
`stub` → `complete` (ingest) → `active` ⇄ `dormant` (missing after its sale ended, or 7 days withdrawn) → **archived and deleted**.

A vehicle is archived and deleted when either:
- `outcome='sold'` and it has been missing for at least 36 h; or
- it has been missing for at least the relist window: **28 days by default**, to be calibrated in recon (P95 relist gap).

Dormancy strips heavy and identifying fields: `documents.text` and `comments`. It keeps specs, parsed CT, `vin_hmac`, listings and the LLM cache (about 1 KB per car). **A relisted car therefore costs 0 PDF downloads and 0 LLM calls.**

### Schema v2 (abridged)
**Connection setup:**
- Every connection: `isolation_level=None` plus an explicit `write_tx()` that issues `BEGIN IMMEDIATE`.
- On a fresh DB, `PRAGMA auto_vacuum=INCREMENTAL` must run **before** `journal_mode=WAL`, otherwise it is silently ignored. Assert both values.
- Per connection: `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`.
- All `*_at` values use the format `YYYY-MM-DDTHH:MM:SSZ` (UTC) via one `utc_iso()` helper.

```sql
sales(id INTEGER PRIMARY KEY AUTOINCREMENT, house, external_id, kind /*room|online|multisite*/,
  audience /*public|pro*/, room, title, url, starts_at, ends_at,
  status /*upcoming|closed*/, expected_lots, first_seen_at, last_listed_at,
  UNIQUE(house, external_id))

vehicles(id INTEGER PRIMARY KEY AUTOINCREMENT,      -- AUTOINCREMENT: async LLM results can't hit a reused id
  house, external_id /*Alcopa product_id*/, category /*voiture|utilitaire|materiel*/,
  vin_hmac /*keyed HMAC; key auto-generated in data/identity.key*/,
  brand, model, trim, fuel, gearbox, body_type, first_registration, year, mileage_km,
  engine_cc, power_kw, co2, vat_regime /*ttc|ht_recuperable|marge|unknown*/,
  battery_soh_pct, battery_leased, warranty,
  comments /*VIN/plate-redacted*/, comments_sha256,
  raw_specs_json /*th→td dict EXCLUDING VIN/plate*/, photo_count,
  ingest_state /*stub|complete|failed*/, ingest_attempts, next_attempt_at, last_error,
  detail_fetched_at, ct_state /*present|absent|failed*/,
  lifecycle /*active|dormant*/, outcome /*sold|not_sold|unknown*/,
  first_seen_at, last_seen_at, missing_since, missed_snapshots,
  UNIQUE(house, external_id))

listings(id, vehicle_id → vehicles ON DELETE CASCADE, sale_id → sales ON DELETE CASCADE,
  lot_number, detail_url, starting_price, current_price_attr, closing_hint_at,
  first_seen_at, last_seen_at, UNIQUE(vehicle_id, sale_id))
  + INDEX(sale_id)

documents(vehicle_id → CASCADE, doc_type, source_url, sha256, size_bytes,
  fetch_status, parse_status, parser_version, parse_confidence,
  text /*redacted; NULLed at dormancy*/, parsed_json, fetched_at,
  UNIQUE(vehicle_id, doc_type))

llm_assessments(id, vehicle_id → CASCADE, kind /*adjust|verdict|ct_extract*/,
  llm_input_hash, prompt_version, llm_model, adjustment CHECK(-15..5),
  reasons_json, verdict, status /*ok|failed*/, tokens_in, tokens_out, created_at)

assessments(vehicle_id PK → CASCADE,             -- DERIVED: rebuilt deterministically, never trusted as cache
  R, C, V, base_score, gates_json, max_tier, llm_adjustment, final_score, tier,
  confidence /*full|partial*/, reasons_json /*[{code, pillar, delta_points, evidence:{src, ref}}]*/,
  value_basis, heuristic_version, scored_at)

sync_runs(id, house, kind, status /*running|ok|incomplete|blocked|failed|purge_blocked*/,
  scope_json, started_at, heartbeat_at, finished_at, counts…, error)
  + UNIQUE INDEX (house, kind) WHERE status='running'

-- Phase 2 migration adds:
sale_outcomes(house, brand, model, trim_family, fuel, gearbox, year, km_bucket_5k, engine_cc,
  times_listed, first_listed_month, last_sale_month, first_price, last_price, price_kind,
  sale_kind, vat_regime, all_in_last, fee_model_version, bid_observed,
  ct_result, ct_major, ct_critical, final_score, tier, outcome, archived_at)
vehicle_ids(house, external_id, vehicle_id)        -- aliases for re-consigned cars (new product_id, same VIN)
```
`photos` is dropped: no photo URLs are stored at all, only `photo_count` and a link-out.

### Poll: set-based snapshot apply (single `write_tx`; simulated over 12 days)
1. **Snapshot.** Walk every sale with `status='upcoming'` whose end time is unknown or in the future, *including sales that are already running*. Walk **unfiltered** pages and filter by category afterwards.
2. **Completeness, per sale.** A sale is complete when all of these hold:
   - all pages were fetched;
   - there were 0 card-parse failures;
   - the last page is short;
   - the unique count is at least `expected_lots − max(2, 2%)`.

   A known live sale missing from discovery is treated as incomplete (`sale_vanished`). Any `--sale-url/--pages/--limit` run is `scoped`: it never marks cars missing. A WAF or challenge response (405/403/captcha) sets `blocked` and stops the run.

   **Sitemap = cross-check only**, never the membership source.
3. **Stage stubs.** Insert stubs into `temp.snap(external_id TEXT NOT NULL, sale_id INTEGER NOT NULL, …, PRIMARY KEY(external_id, sale_id)) WITHOUT ROWID`.
4. **Upsert vehicles.** `INSERT INTO vehicles … SELECT … FROM temp.snap GROUP BY external_id ON CONFLICT DO UPDATE SET last_seen_at, missed_snapshots=0, missing_since=NULL, lifecycle='active'`. Lifecycle columns are only ever written here, never by the generic COALESCE upserts.
5. **Upsert listings for every stub, new ones included.** `… ON CONFLICT(vehicle_id, sale_id) DO UPDATE SET lot_number=COALESCE(excluded.lot_number, lot_number), starting_price=COALESCE(…), …`.
6. **Mark missing.** This uses `NOT EXISTS`, never `NOT IN`:
   ```sql
   UPDATE vehicles SET missed_snapshots = missed_snapshots + 1, missing_since = COALESCE(missing_since, :now)
    WHERE house = :h AND category IN (SELECT value FROM json_each(:scope))
      AND NOT EXISTS (SELECT 1 FROM temp.snap s WHERE s.external_id = vehicles.external_id)
      AND NOT EXISTS (SELECT 1 FROM listings l JOIN temp.incomplete_sales i ON i.sale_id = l.sale_id
                       WHERE l.vehicle_id = vehicles.id);
   ```
7. **Dormancy.** `LIVE(v)` means the car has a listing whose sale has `status='upcoming'` and `COALESCE(s.ends_at, l.closing_hint_at, s.starts_at)` is NULL or later than `:now`. An unknown date counts as live.
   ```sql
   UPDATE vehicles SET lifecycle='dormant', comments=NULL
    WHERE house=:h AND lifecycle='active' AND missed_snapshots >= 2
      AND ((missing_since <= :now_minus_36h AND NOT LIVE(vehicles)) OR missing_since <= :now_minus_7d);
   UPDATE documents SET text=NULL
    WHERE vehicle_id IN (SELECT id FROM vehicles WHERE lifecycle='dormant');
   ```
8. **Outcome probe** (outside the transaction, request-capped). Fetch the lot page of newly dormant cars and write `outcome`: a `VENDU` badge means `sold`; a passed sale with no badge means `not_sold`; a failed or blocked probe means `unknown` (medium: third-party report).
9. **Purge, inside `SAVEPOINT purge`.**
   - `purge_set` = dormant cars where (`outcome='sold'` AND `missing_since <= now−36h`) OR `missing_since <= now−relist_window`.
   - Archive with an `INSERT … SELECT`: one row per car, scalar subqueries over listings, `json_extract` on `parsed_json`, mileage bucketed to 5k, month-level dates.
   - `DELETE FROM vehicles WHERE id IN (SELECT id FROM temp.purge_set)`, which cascades.
   - Close sales whose end is in the past, and delete closed sales with no listings.
   - **Circuit breaker:** "unexpected" missing cars are those whose listing is still live. If they exceed max(25, 5% of active), `ROLLBACK TO purge` and the mark-missing step as well, and set `status='incomplete'` with reason `mass_missing`. Expected post-sale churn never trips it. Unblock with `purge --approve-run <id>`.
10. **After commit.** Loop `PRAGMA incremental_vacuum(2000)` with `.fetchall()` until `freelist_count = 0`, then run `wal_checkpoint(TRUNCATE)`.
11. **Weekly adapter-independent TTL.** Cars not seen for 45 days that have no live listing are archived as `unknown` and deleted. This also covers houses that are disabled or blocked.

### Ingest queue (budgeted, separate job)
- **Selection:** `missing_since IS NULL`, and either `ingest_state='stub'` or (`failed` AND `attempts < 5` AND `next_attempt_at <= now`), and the car has a live listing. Ordered by soonest close, with NULL dates last.
- **Re-consigned cars.** A new `external_id` whose `vin_hmac` matches an existing car (dormant included) is merged through `vehicle_ids`. No download and no re-score.
- **Late CT.** If `ct_state='absent'`, `detail_fetched_at` is older than 48 h and the car is still live, re-fetch the detail page once, within a per-run budget.
- **A parse failure is not an ingest failure.** The car is marked `ct_unparsed` and re-parsed from the redacted text when `parser_version` changes.
- **SQLITE_BUSY** retries do not increment the attempt counter.

### Run lock and scheduling
- **Lock.** Acquire the lock inside `BEGIN IMMEDIATE`: first mark a stale run (heartbeat older than 15 min) as `failed`, then insert the new `running` row.
  - The heartbeat is refreshed about every 60 s, and every write is fenced with `WHERE id=:run AND status='running'`.
  - Cleanup uses `except BaseException`, so Ctrl+C frees the lock. `--force-unlock` is available.
- **Scheduler.** APScheduler `>=3.11.2,<4` (4.0 is still alpha) runs in one `worker` process: a systemd service in production, and also usable on the owner's Windows machine (needs `tzdata`). Times are Europe/Paris.

| Job | When | Notes |
|---|---|---|
| `poll` | 06:40 daily (optionally also 18:40) | — |
| `ingest` | every 2 h, 07:00–23:00 | request budget |
| `llm_submit` | 01:30 | Batch API |
| `llm_collect` | every 30 min | — |
| `maintenance` | Wed 04:10 | — |

- **Scheduler settings.** No job between 02:00 and 03:00 (DST gap). All jobs use `coalesce`, `max_instances=1` and `misfire_grace_time`.
- **Files.** No PDFs or photos are ever written. The only files on disk are in `data/quarantine/<sha256>.pdf`: at most 7 days and at most 50 files, swept by `maintenance`. If file storage is ever added, use the outbox pattern: insert into `file_gc` inside the purge transaction, unlink after commit, and run a weekly orphan sweep.

---

## 3. Multi-auction adapter pattern
One file per house plus fixtures. The core only sees normalised dataclasses, and parse functions are pure (`parse_sale_page(html) -> …`).
```python
@dataclass(frozen=True)
class SaleRef: house: str; external_id: str; url: str; kind: str = "room"; audience: str = "public"
    room: str | None = None; starts_at: datetime | None = None; ends_at: datetime | None = None
    expected_lots: int | None = None
@dataclass
class LotStub:  house: str; external_id: str; category: str; detail_url: str; sale: SaleRef
    lot_number: int | None; brand: str | None; model: str | None; trim: str | None; fuel: Fuel | None
    year: int | None; mileage_km: int | None; gearbox: str | None; starting_price: float | None
    current_price_attr: float | None; closing_hint_at: datetime | None
@dataclass
class DocumentRef: doc_type: Literal["ct", "maintenance", "battery"]; url: str
@dataclass
class LotDetail: specs: VehicleSpecs; vin: str | None; registration: str | None  # hashed/redacted by core, never stored clear
    documents: list[DocumentRef]; comments: str | None; photo_count: int; raw_specs: dict[str, str]
@dataclass
class SaleListing: sale: SaleRef; stubs: list[LotStub]; complete: bool; reasons: list[str]

class AuctionAdapter(ABC):
    slug: ClassVar[str]; display_name: ClassVar[str]; base_url: ClassVar[str]
    url_patterns: ClassVar[tuple[re.Pattern, ...]] = ()   # "paste a link" routing (Phase 5)
    poll_cron: ClassVar[str] = "40 6 * * *"                # Europe/Paris
    min_interval_s: ClassVar[float] = 2.0
    _registry: ClassVar[dict[str, type["AuctionAdapter"]]] = {}
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if getattr(cls, "slug", None):
            if cls.slug in AuctionAdapter._registry:
                raise ValueError(f"duplicate adapter {cls.slug}")
            AuctionAdapter._registry[cls.slug] = cls
    def __init__(self, http: "PoliteClient"): self.http = http
    @abstractmethod
    def discover_sales(self) -> list[SaleRef]: ...          # Phase 2 (after recon)
    @abstractmethod
    def list_sale(self, sale: SaleRef) -> SaleListing: ...  # completeness is mandatory
    @abstractmethod
    def fetch_detail(self, stub: LotStub) -> LotDetail: ...
    @abstractmethod
    def fee_model(self, price: float, sale: SaleRef, buyer: str, vat_regime: str) -> float: ...
    def handles(self, url: str) -> bool: return any(p.search(url) for p in self.url_patterns)
# adapters/__init__.py: pkgutil.iter_modules() imports every module, so subclasses self-register.
```

**`PoliteClient`** (one per host):
- A `requests` session with urllib3 `Retry` on 5xx only (`backoff_jitter`, `urllib3>=2,<3`).
- 429/503 handled manually: `Retry-After` is honoured up to 60 s, beyond that `RateLimitedError` (fail closed).
- 405/403/captcha markers raise `BlockedError`.
- `robots.txt` is fetched through the session and parsed; a 404 means allow.
- A per-host interval with jitter.
- Responses are always closed (`with`).
- `get_bytes(max_bytes)` for in-memory documents.
- Only `getDocument` hrefs present on public pages are followed, and no account cookies are ever sent.
- The UA defaults to the current one, with an env override. The honest, identifiable bot UA that the legal research recommends is a decision for after recon.

**Alcopa specifics** (medium confidence unless noted):
- **Product ID:** taken from `urlsplit(href).path` matching `^/(?:[a-z]{2}/)?[a-z-]+/[^/]+/[^/]+-(\d{5,8})/?$`, cross-checked against the damage-JSON `product_id`.
- **Normalisation:** mileage is parsed per line and bounds-checked; `lot 0` becomes NULL; a `--` price becomes NULL.
- **Prices:** `data-current-price` is stored as `current_price_attr` (the opening price until bids arrive); `bid_observed` = attr > start. The `.text-nowrap` fallback is removed.
- **Detail parsing:** the detail spec table is rendered twice, so it is de-duplicated. `<noscript>` is stripped.
- **Fees:** `fee_model` adds 14.4% TTC (minimum €360) for room sales; online prices already include fees; HT is handled for `TVA récupérable`. These numbers are config, to be checked against the CGV.

**Next adapters:**
- VP Auto: public server HTML, public CT/SE PDFs on its CDN, "Adjugé" prices (medium). Its lot-ID stability is unverified, so Phase 2 adds a core fingerprint fallback (make + model + first registration + km band + doc key).
- Then Enchères VO, then Agorastore.
- Interencheres, BCA and Enchères du Domaine need full browsers or are B2B: deferred.

**Contract tests** are parametrised over the registry (Phase 2).

---

## 4. Scoring engine (Phase 1 contract, Phase 3 depth)
**Principle:** recompute everything deterministic on every poll, ingest and LLM import (milliseconds for thousands of rows). Cache **only** what costs money: `llm_assessments`, keyed by the hash of the exact payload. As a result, age, CT age, cohort drift, thresholds and late LLM results can never leave a score stale.

`base = 0.35·R + 0.35·C + 0.30·V`, then the LLM adjustment, then the `max_tier` caps, then the tier: 🟢 ≥ 80, 🟡 60–79, 🔴 < 60 (configurable). Gates use `max_tier` semantics: the cap is derived as `yellow_min−1` or `green_min−1`, minus a severity offset.

**Reliability (R).** Mileage per year against about 15k (diesel-aware), absolute km bands, and age in months at the reference date.
- `engines.json` (stdlib, no PyYAML): sourced entries keyed by engine code via trim regex, cc, fuel, years and power. Gearboxes get a separate table.
- **Seed entries:** PureTech EB2 turbo, 1.5 BlueHDi DV5 (about 2017–2023), Renault 1.2 TCe H5Ft (2012–2016).
- **Phase 3 adds** Ford 1.0 EcoBoost, VW EA111, BMW N47, 1.6 THP, DV6 and DQ200 (medium-confidence sources).

**Condition (C).**
- **CT result and defects**, weighted by system: brakes, steering and structure count more.
- **CT freshness for the buyer profile** (`BUYER_PROFILE` = particulier by default, or pro). Reference date = sale + 21 days. A CT redo is needed if it is 6 months old or more (2 months after a défavorable). Exempt if the car is under 4 years old or the buyer is a pro.
- **Inspection type:** a `volontaire` inspection is treated as an UNKNOWN result.
- **Odometer:** check the history and flag any decrease.
- **EV battery:** SoH, plus `battery_leased` detection.
- **Other inputs:** warranty, a comment lexicon, and Alcopa damage zones.

**Value (V).** Price-dependent; computed in code and never sent to the LLM.
- `effective_price = fee_model(max(start, current_price_attr)) + repair_estimate(defect systems) + ct_redo_cost`.
- The cohort excludes gated and major-defect cars. Fallback chain, recorded in `value_basis`:
  1. (family, fuel, gearbox, ±1 yr, km band), n ≥ 5;
  2. widened to ±2 yr, n ≥ 5;
  3. (brand + fuel), n ≥ 8;
  4. otherwise **V = 50 + `value_unknown`**.

**Confidence gate.** Any of these makes the result `partial`, which caps it at max yellow and excludes it from the Top N: price unknown, `value_unknown`, CT absent/unreadable (and the car not exempt), or mileage unknown. The UI shows the pill "Données incomplètes".

**Gates:**

| max_tier | Conditions |
|---|---|
| **red** | CT critique; VGE/VEI/épave; pour pièces; sans carte grise; moteur HS / casse moteur; boîte HS; non roulant / ne démarre pas; EV SoH < 75% (owned battery) |
| **yellow** | CT unreadable or `parse_confidence` < 0.8 (`ct_non_lu`); major defect on brakes, steering or structure; kilométrage non garanti; odometer inconsistent; catastrophic engine entry (configurable) |

**Until ≥ 20 real Alcopa CTs are validated (Phase 2), the defect-code suffix never overrides the section header.**

**LLM role (trigger-only):**
- **Triggers:**
  - `ct_extract` when CT parsing fails or has low confidence. This can only *tighten* gates; validated codes must match `^\d\.\d+(\.\d+)*\.[a-z]\.[123]`.
  - `adjust` when the pre-value base score is within ±5 of a boundary and no gate binds.
  - an unknown `engine_key`, cached in `engine_profiles` (origin `kb|llm|owner`, survives purges).
  - `verdict` text for 🟢 and Top N cars only.
- **Adjustment** is bounded to **[−15, +5]** and the reasons are stored. Anything from the LLM is labelled "Avis IA".
- **Payload:** parsed codes and labels plus redacted comments inside delimited data blocks. **Never** raw CT text, VIN, plate or price.
- **Client:**
  - `google-genai >=2,<3`, imported lazily;
  - `response_mime_type` JSON plus a schema;
  - `temperature 0` and a minimal thinking budget;
  - in Phase 1, synchronous batches of 20 with `--llm-limit 60`; Batch API in Phase 3.
- **Configuration:**
  - The model comes from `GEMINI_MODEL` and has **no default**. The owner confirms a current ID in AI Studio; a Flash-Lite-class model is suggested but unverified.
  - A 404 / NOT_FOUND triggers a fast, clear error and heuristic-only scoring.
  - LLM calls require `GEMINI_PAID_TIER=1`. The research says Gemini's terms bar personal data on the free tier and require the paid tier for EEA use (medium; only the Dec 2025 terms were seen).
- **Cost:** about 3.6–3.9k new IDs/week × 40% assumed triggers × (about 1.5k input + 300 output tokens) ≈ **$0.5–1.5/week** on 3.5 Flash-Lite Batch, or about $2–5/week on 3.8 Flash Batch after the reported price rise of 1 Jan 2027. These are unverified snippet prices.

**Top N of the week.** Ranked over the *filtered* eligible set: sales in the next 7 days, tier ≠ red, `confidence='full'`, and audience matching the buyer profile.

**Labels.** "Profil favorable / correct selon nos critères" and "Risque élevé selon nos critères", with the facts shown, to limit disparagement risk. Each card shows the top 3 `reasons_json` entries by |delta| with their source.

**Quality control (Phase 3):**
- A golden set of at least 150 owner-labelled cars, stratified with at least 40 per tier. The owner labels blind, before seeing scores, and re-labels 20 of them after 2 weeks.
- Inputs are frozen in `tests/golden/*.json` so they survive purges.
- **Exit:** quadratic-weighted κ ≥ 0.6 and **zero owner-red cars scored green**.

---

## 5. MVP web stack and UI (Phase 4)
**Stack:**
- **Starlette** + Jinja2 (autoescape) + HTMX 2.0.x (4.0 is only tagged "next") + Tailwind v4 standalone CLI (no Node).
- FastAPI is not needed for server-rendered GET filters, and dropping it saves pydantic. Starlette + uvicorn measured about 32 MB RSS here, so the target is **< 100 MB**.
- One uvicorn worker. SQLite is opened `file:…?mode=ro`, and the web user can create the `-shm` file. Search uses FTS5.
- **Hosting:** one EU VPS, for example Hetzner CX23 at about €5.49/mo excluding VAT and IPv4 (June 2026 list). Caddy handles TLS; systemd runs `web` and `worker`; nightly `VACUUM INTO` backups.
- Hosting is chosen **after** recon confirms the VPS IP isn't blocked.

**Pages:**
- `/` "Best deals of the week": filters as shareable GET params (`hx-get`, `hx-push-url`, 300 ms debounce) for all-in budget, fuel, gearbox, km, year, house/room, sale date and tier.
  - **Cards:** score ring, tier label, year · km · fuel · gearbox, effective price ("estimation frais inclus"), sale date and room, CT summary, engine chip, top 3 reasons, **"Voir l'annonce" link-out**.
  - **No photos in any mode**, and no VIN or plate.
- `/v/{id}`: pillar breakdown, gates, CT defects grouped by system, engine notes with sources, price history across relists, the label "Source : PV de contrôle technique sur le site de la maison de vente" (no direct link to the PDF).
- `/methode`: method transparency, takedown contact and opt-out.
- `/admin` (basic auth): sync runs, completeness, breaker events, DB size, read-only-open health check, houses that have data but no enabled adapter.

**Public mode (only after licences):** show a top-N subset per filter with a prominent link-out. **No full-catalogue FTS** (*Innoweb*), and no claims of exhaustiveness. Full filters are available only in private/invite mode.

---

## 6. Roadmap
| Phase | Scope | Exit criteria |
|---|---|---|
| **1** | Foundation and correctness (commits C1–C10 below) | All tests green offline; connection-leak and encoding guards pass; README commands work unchanged |
| 2 | **Live recon** from the owner's network *and* the intended VPS. It covers: robots.txt and CGV; the WAF; `getDocument` anonymous access and token stability; product_id stability across relists; relist-gap P95; sitemap; calendar.<br>Then: `discover_sales`, the lifecycle engine (§2), the `sale_outcomes`/`vehicle_ids` migration, the VENDU probe, the `worker`, `sync/purge --dry-run` and contract tests. | 3 simulated weeks correct: relist → 0 PDF/LLM calls and one archive row; no purge from incomplete or scoped runs; ≥ 95% CT A/S/R agreement on ≥ 20 real PDFs |
| 3 | Scoring depth: full `engines.json`, `engine_profiles`, repair-cost table, Batch API, golden set | κ ≥ 0.6, 0 red→green |
| 4 | Private web MVP and deployment | p95 < 200 ms, RSS < 100 MB |
| 5 | VP Auto adapter; on-demand links; saved-search alerts | VP Auto added without editing scoring or web code |
| 6 | Price model from VP Auto "Adjugé" + archive; licence outreach; lawyer review; public-launch decision | Lawyer sign-off |

---

## Phase 1: execution plan (each commit green on its own)
- **C1 docs:** `docs/MASTER_PLAN.md`, i.e. this plan plus an appendix of sources with confidence levels and a live-recon checklist.
- **C2 skeleton and dependencies:**
  - `auction_agent/__init__.py`.
  - `config.py`: `PROJECT_DIR=Path(__file__).resolve().parents[1]`; env settings; `utc_iso()`; HMAC key bootstrapped into `data/identity.key`; Python ≥ 3.10 guard.
  - `requirements.txt`:
    - drop `google-generativeai`;
    - add `google-genai>=2,<3`, `jinja2>=3.1`, `tzdata`, `urllib3>=2,<3`;
    - raise floors to `pdfplumber>=0.10.4` and `requests>=2.31`.
  - `requirements-dev.txt`: `pytest>=8`.
  - `pytest.ini` (`error::EncodingWarning`).
  - `.gitignore`: `data/*` except the `.gitkeep` files, which covers the WAL/SHM files, the key and the quarantine.
- **C3 `db.py`:**
  - `connect()` uses `contextlib.closing` and `isolation_level=None`; auto_vacuum is set before WAL, with asserts; `write_tx()`.
  - Schema v2 without `sale_outcomes` or `vehicle_ids` (those come in Phase 2).
  - A `user_version` migration runner.
  - **v1 handling:** detected via `pragma_table_info('vehicles')` having `detail_url`. It is moved to `data/auction.v1-<ts>.db` after `UPDATE … SET vin=NULL, registration=NULL; VACUUM`. A `cleanup-legacy` command deletes it and `data/documents/lot_*`.
  - Repository functions use COALESCE upserts; lifecycle columns are excluded.
  - Tests: `test_db`, with an autouse conftest fixture that counts connection opens and closes.
- **C4 documents and CT parsing:**
  - `documents.py`: `get_bytes` → `%PDF-` magic check → sha256 → `pdfplumber.open(BytesIO, pages=1..cap)` → NFC on the text → VIN/plate redaction (`[A-HJ-NPR-Z0-9]{17}`, SIV/FNI plate regexes) → capped quarantine.
  - `ct_parser.py` v2: NFKD fold; anchored result; singular/plural headers; code-only defect lines; severity from the section header, with the suffix recorded as a cross-check; inspection type, labelled date/km and odometer history; `parse_confidence`; `parser_version`.
  - Fixtures: a committed `tests/fixtures/pdf/ct_min.pdf` and CT text fixtures (favourable with legend, plural majeure, critique, NFD, footer noise, volontaire).
- **C5 `http.py`:** `PoliteClient` as in §3. `test_http` runs against a local `ThreadingHTTPServer` and covers retry, Retry-After capping, blocked detection and robots handling.
- **C6 adapters:**
  - `adapters/base.py`, the registry, and `adapters/alcopa.py` **porting the existing selectors** with only the listed fixes (rows 1, 2, 4, 8, 10 and the lot/comments/price selectors).
  - `cli capture --url <sale> --detail <url>` saves real pages into `tests/fixtures/alcopa/real/` with VIN and plate redacted, for the owner to run.
  - Synthetic fixtures modelled on the third-party-quoted markup, plus a WAF page.
  - `test_alcopa_adapter`. `discover_sales` raises `NotImplementedError` until the Phase 2 recon.
- **C7 ingest:**
  - `ingest.py` + `cli crawl` + the `run_crawl.py` shim, **with the same flags and meaning**: `--url` is one sale (default `DEFAULT_SALE_URL`, VP), and `--pages`, `--limit`, `--no-docs` are unchanged.
  - Flow: an identity-aware upsert by `(house, product_id)`, listing rows for every stub, then:
    - known complete cars get a listing refresh only, except for one detail check when first seen in a new sale;
    - new or failed cars go through detail, then CT and battery doc in memory, then parsing.
  - Late-CT re-check; per-vehicle error isolation; `sync_runs` with lock, heartbeat, `BaseException` cleanup and `--force-unlock`.
  - Runs are always `scoped`, so nothing is marked missing in Phase 1.
  - `test_ingest`: a rollover triggers 0 PDF fetches; a partial listing is flagged; Ctrl+C frees the lock; a late CT changes the tier.
- **C8 scoring and report:**
  - `scoring/heuristics.py` with `today` injected, gates, confidence, the value fallback chain and `reasons_json`.
  - `scoring/engines.json` (3 seed entries).
  - `scoring/service.py`: deterministic rebuild.
  - `report.py` + `templates/report.html.j2` via `PackageLoader`, with autoescape and None-safe formatting. It shows the Top N, the cars by tier, and a "Risque élevé selon nos critères" section listing the facts.
  - `cli score/report/stats` + the `run_advisor.py` shim (`--sale-id`, `--max-price` with NULL prices shown as "prix inconnu", `--open`).
  - `test_scoring`, `test_report` (XSS escaped), and an end-to-end CLI test on fixtures.
- **C9 `scoring/llm.py`:** a GeminiClient behind a Protocol, trigger-only calls, `llm_assessments` caching, untrusted text inside data blocks, and the `GEMINI_MODEL`/`GEMINI_PAID_TIER` guards. Tests use a FakeClient and check that an unchanged payload makes 0 calls and that importing works without `google.genai`.
- **C10 README and legacy removal:**
  - README: architecture, commands, `python -X warn_default_encoding -m pytest -q`, a legal note and the Gemini setup.
  - The legacy modules (`config.py`, `crawler.py`, `database.py`, `pdf_extractor.py`, `scraper/`, `advisor/`) are deleted.
  - `main` keeps the working crawler until you validate this branch on Windows with `python run_crawl.py --limit 5` and `python run_advisor.py --open`, and merge.
- Push with `git push -u origin claude/relaxed-albattani-3w9fgh`. No PR unless you ask for one.

## Verification
1. Run `pip install -r requirements.txt -r requirements-dev.txt`, then `python -X warn_default_encoding -m pytest -q -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning`. All tests must pass. The conftest open/close counter proves no connections leak.
2. The regression tests prove:
   - mileage parses as 35394, not 202435394;
   - no on-disk PDF cache, so a reused lot number cannot leak a CT;
   - plural-header and NFD CTs give the correct result and severities;
   - an unreadable CT is capped at yellow;
   - XSS is escaped;
   - scoped and partial runs never mark cars missing;
   - an unchanged LLM payload makes 0 calls;
   - `auto_vacuum=2` and `journal_mode=wal` on a fresh DB.
3. An end-to-end pytest runs `cli crawl` → `score` → `report` against fixture-backed HTTP and asserts the row counts and the rendered output.
4. **Not verifiable here:** live Alcopa access (blocked by the proxy) and real CT layouts. The owner runs `cli capture` and `run_crawl.py --limit 5` locally. Phase 2 starts with the recon checklist.

---

## Appendix A: sources by topic

**How these were gathered:** the research agents could not open most pages directly (the container proxy blocks them and WebFetch had no DNS). Unless noted, each claim rests on a web-search summary of the URL listed, not on a full reading of it. Re-check primary texts before relying on them, especially the legal ones.

### Alcopa and other auction houses (site facts: medium/low)
- Alcopa CGV (photo and description clause, buyer premium): https://www.alcopa-auction.fr/cgv · https://www.alcopa-auction.fr/en/gtcs · legal notice: https://www.alcopa-auction.fr/mentions
- Alcopa sales calendar and room pages: https://www.alcopa-auction.fr/calendrier-des-ventes · https://www.alcopa-auction.fr/salle-de-vente-encheres/beauvais
- Card markup, product_id URL pattern, double-rendered detail table, `getDocument` tokens, VENDU badge and AWS WAF reports come from public third-party scrapers and their Sept 2026 fixtures, read only as GitHub code-search fragments. Treat them as hypotheses until the live recon (Appendix B) confirms them.
- Market weight in 2025 (Alcopa €946M, BCAuto €784M, VPauto €496M, EnchèresVO €403M): https://journalauto.com/distribution/apres-une-annee-2025-record-jusquou-les-encheristes-monteront-ils/
- VP Auto public listing: https://www.vpauto.fr/vehicule/liste · Enchères VO: https://encheres-vo.com/

### Contrôle technique (medium)
- Severities and results: https://www.stych.fr/code/elements-mecaniques-5/le-controle-technique-476
- Defect code format and severity suffix (single secondary source): https://moncarnetauto.fr/codes-defaillance-controle-technique
- PV template (UTAC): https://www.utac-otc.com/REGLEMENTATION/Fac-simil%C3%A9s%20des%20panneaux%20et%20documents/Format%20PV%20VL_2014-45-doc%2020231120.pdf
- 6-month rule for sale to a private buyer (arrêté du 18 juin 1991): https://www.seine-et-marne.gouv.fr/index.php/contenu/telechargement/2016/14034/file/Arrete_18_juin_1991.pdf

### Engine reliability (confidence per entry)
- PureTech belt and cover programme (high): https://www.peugeot.fr/entretien-et-services/moteurs-puretech-solutions.html · https://www.opel.fr/entretien-services/garantie-assistance/extended-warranty-for-puretech.html
- 1.5 BlueHDi chain recall (high): https://fr.motor1.com/news/764852/moteur-bluehdi-stellantis-rappel/ · https://www.largus.fr/actualite-automobile/casse-chaine-1-5-bluehdi-stellantis-etend-sa-prise-en-charge-a-7-ans-ou-180-000-km-30038271.html
- Renault 1.2 TCe H5Ft oil consumption (high): https://www.quechoisir.org/actualite-moteur-1-2-renault-400-000-voitures-en-danger-n67215/
- Ford 1.0 EcoBoost (medium): https://fordauthority.com/2026/08/nhtsa-upgrades-ford-1-0l-ecoboost-engine-failure-investigation/
- VW 1.2/1.4 TSI chain (medium; aggregators): https://www.fiches-auto.fr/articles-auto/fiabilite-moteurs-essence/s-2288-fiabilite-des-12-tsi-tfsi.php
- BMW N47 (medium): https://en.wikipedia.org/wiki/BMW_N47
- DQ200 recall (medium): https://www.largus.fr/actualite-automobile/rappel-historique-chez-volkswagen-3326377.html

### LLM (high for the SDK; medium for models and prices)
- `google-generativeai` deprecated, support ended 30 Nov 2025 (read directly from PyPI): https://pypi.org/project/google-generativeai/
- `google-genai` (read directly from PyPI and the wheel source): https://pypi.org/project/google-genai/
- Pricing and Batch (snippets): https://ai.google.dev/gemini-api/docs/pricing · https://ai.google.dev/gemini-api/docs/batch-api
- gemini-2.5-flash shutdown dates (conflicting): https://discuss.ai.google.dev/t/gemini-2-5-flash-and-gemini-2-5-flash-lite-returning-404-no-longer-available-today-july-9-contradicts-oct-16-2026-shutdown-date/174267
- Gemini API terms, paid vs unpaid tier (archived Dec 2025 version only): https://ai.google.dev/gemini-api/terms-archive/terms_12_18_25

### Stack (high; versions read from PyPI)
- APScheduler 3.11.x stable, 4.0 still alpha: https://pypi.org/project/APScheduler/
- Python sqlite3: the context manager does not close the connection; ResourceWarning in 3.13: https://raw.githubusercontent.com/python/cpython/main/Doc/library/sqlite3.rst
- SQLite WAL, auto_vacuum and FTS5: https://www.sqlite.org/wal.html · https://www.sqlite.org/pragma.html · https://www.sqlite.org/fts5.html
- urllib3 Retry (backoff_jitter, Retry-After): https://raw.githubusercontent.com/urllib3/urllib3/main/src/urllib3/util/retry.py
- htmx release channels: https://registry.npmjs.org/htmx.org · Hetzner prices: https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/

### Legal (not legal advice; summaries only)
- Innoweb v Wegener (C-202/12), car-ads meta-search: https://www.scl.org/2984-database-right-innoweb-v-wegener-cjeu-judgment/
- CV-Online Latvia v Melons (C-762/19): https://www.scl.org/12290-cjeu-search-engine-copying-of-databases-infringes-sui-generis-right-where-it-adversely-affects-database-maker-investment/
- Ryanair v PR Aviation (C-30/14): https://www.ippt.eu/sites/ippt/files/2015/IPPT20150115_ECJ_Ryanair_v_PR_Aviation.pdf
- Leboncoin v Entreparticuliers (Cass. 1re civ., 5 Oct 2022, n°21-16.307): https://cms.law/en/fra/news-information/arret-leboncoin-web-scraping-droit-sui-generis-sur-les-bases-de-donnees
- Jinka v SeLoger (CA Versailles, Dec 2025): https://www.mysweetimmo.com/2025/12/17/immobilier-seloger-fait-condamner-jinka-pour-extraction-et-reutilisation-illicites-de-ses-annonces/
- CNIL, licence plates are personal data: https://www.cnil.fr/fr/cnil-direct/question/1816
- VIN as personal data (C-319/22): https://www.matheson.com/insights/detail/cjeu-considers-whether-vehicle-identification-numbers-constitute-personal-data
- Hashing is pseudonymisation (C-413/23 P): https://cm.twobirds.com/en/insights/2025/eu-the-srb-decision-a-new-era-for-personal-data-and-data-processing-agreements
- CNIL on scraping (moissonnage): https://www.cnil.fr/fr/focus-interet-legitime-collecte-par-moissonnage
- Photos: re-hosting vs linking (Renckhoff, Svensson, VG Bild-Kunst): https://www.scl.org/10275-website-photos-and-copyright/
- Disparagement case law: https://cms.law/fr/FRA/Publication/Denigrement-fautif

---

## Appendix B: Phase 2 live-recon checklist
Run from **both** the owner's network **and** the intended VPS. At most about 12 polite requests per site. Never log in, never solve a CAPTCHA, never bypass a block.

1. Fetch `robots.txt`. Record the full text and its rules for `/salle-de-vente-encheres/`, `/*-occasion/`, `/getDocument/` and `*.pdf`.
2. Read the CGV, the mentions légales and any CGU clause on automated extraction or reuse. Save a dated copy.
3. Check for a WAF or challenge: status codes (405/403), `aws-waf-token` cookies, captcha markup. If the VPS is blocked, record "access denied" and stop. Do not relocate the crawler to get around it.
4. Fetch one sale page and one detail page with `python -m auction_agent.cli capture`. This saves redacted fixtures, and the tests are then re-run against them.
5. Check whether a `getDocument` URL can be downloaded anonymously, whether its token is stable across days, and whether it is rate-limited.
6. Check product_id stability: follow 10 unsold cars from one sale into their relist, recording the same product_id, a new id with the same VIN, and the gap in days. Then set `relist_window` = P95 of the gap.
7. Check whether `sitemap.items-fr.xml` exists and how many entries it has, and compare it with a full walk of the sales.
8. Look at the calendar or sale-discovery page structure. This unblocks `discover_sales()`.
9. Collect at least 20 real CT PDFs and label the A/S/R result and the defects by hand, to validate `ct_parser`. Only then allow the defect-code suffix to set severity.
10. Check whether the VENDU badge appears after a sale, and whether any hammer price is shown.

---

## Appendix C: Phase 1 implementation status
See the commit history on `claude/relaxed-albattani-3w9fgh` and the README "Architecture" section. Known limits of Phase 1:
- Every crawl is `scoped` (one sale), so no car is ever marked missing or purged. The lifecycle engine is Phase 2.
- Alcopa selectors are ported from the original prototype and tested against synthetic fixtures built from third-party-quoted markup. They must be re-validated on real captured pages (`cli capture`).
- The LLM is optional and runs only on triggers. It needs `GEMINI_API_KEY`, `GEMINI_MODEL` and `GEMINI_PAID_TIER=1`.
- Redirects are followed by hand. Every hop is robots-checked and throttled, but a redirect to another host is not refused, because the CT links' real redirect behaviour is unknown until recon. Decide in Phase 2.
- The Gemini call sets a zero thinking budget and retries once without it if the model rejects the setting. The real behaviour of the model id the owner picks is unverified.
- CT parser v2 is still `ct-2.0`. Its fixes (legend lines, `(s)` headers, page furniture, three-level codes) are tested on synthetic text only. The ≥ 20 real-PV validation in Phase 2 remains the gate before the defect-code suffix may override section headers.

### Phase 1 adversarial review (2026-10-14)
A read-only review in five dimensions (storage, parsing, security/network, scoring, plan conformance), each with its own verifier, reported 58 findings. The verifier confirmed all of them; some overlap across dimensions.
- Every finding has a fix in `fix: address Phase 1 review findings`, with tests in `tests/test_regressions_*.py`.
- 94 of the 103 new tests fail on the pre-fix commit `d1b1e3d`. The other 9 are guard cases that must pass on both versions, for example "a CT defect line is not redacted".
- Two findings are only partly addressed: cross-host redirects (above), and the request for tests on real captured pages, which needs the owner to run `cli capture`.
