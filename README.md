# 🚗 Auction-Agent: Multi-Auction Crawler & AI Car Deal Finder

> Crawls French vehicle auction catalogues, reads the official *Contrôle Technique* (CT) reports, and scores **every** vehicle 0–100 into 🟢 / 🟡 / 🔴 tiers, so buyers see the best deals of the week first.

The roadmap, architecture decisions, sources and known risks are in **[docs/MASTER_PLAN.md](docs/MASTER_PLAN.md)**. This README covers how to run the current code (Phase 1).

---

## What it does today (Phase 1)

| Step | What happens |
|---|---|
| Crawl | Walks one Alcopa sale, page by page, and **reports whether the walk was complete**. Each lot is identified by the product id in its URL, not by its lot number. |
| Ingest | New cars get their detail page and CT PDF. The PDF is downloaded **into memory**, parsed, and VIN/plate-redacted, and is **never written to disk**. Known cars only get a cheap listing refresh. A relisted car gets one detail-page check, and its CT is downloaded again only if the CT link changed. |
| Score | Deterministic rubric with gates, run on every car. The LLM (Gemini) is optional, only called on triggers, and its output is cached. |
| Report | Self-contained HTML report: top picks, then cars grouped by tier, with the facts behind every score. |

| Tier | Score | Meaning |
|---|:---:|---|
| 🟢 Profil favorable | 80–100 | Clean or minor-only CT, no known deal-breaker, good value against comparable cars |
| 🟡 Profil correct | 60–79 | Acceptable, or data incomplete (capped here until the facts are known) |
| 🔴 Risque élevé | < 60 | Critical CT defect, deal-breaker in the comments (VGE, moteur HS…), or a degraded EV battery |

Known engine risks (1.2 PureTech wet belt, 1.5 BlueHDi chain, 1.2 TCe oil consumption) cost a heavy penalty and cap the car at 🟡 by default. Set `ENGINE_RISK_MAX_TIER=red` to send them to 🔴.

## Quick start

Python 3.10+ is required.

```bash
git clone https://github.com/ermaljsx/Auction-Agent.git
cd Auction-Agent
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

```bash
# Fast test: first 5 vehicles of the default sale, no PDF downloads
python run_crawl.py --limit 5 --no-docs

# One listing page, with CT documents
python run_crawl.py --pages 1 --limit 10

# A specific sale (each Alcopa sale has its own id in the URL)
python run_crawl.py --url /salle-de-vente-encheres/beauvais/13026

# Score every vehicle and open the report (heuristics only, no API key needed)
python run_advisor.py --open
python run_advisor.py --max-price 12000 --open   # all-in budget, euros
```

The same commands are available as `python -m auction_agent crawl | score | report | advise | stats | capture | cleanup-legacy`. Add `--help` to any command for its flags.

### Upgrading from the prototype
On first run, the old `data/auction.db` is moved to `data/auction.v1-<date>.db` with VIN and plate columns emptied, and a new database is created. To delete the old database and the prototype's `data/documents/lot_*` PDFs:

```bash
python -m auction_agent cleanup-legacy            # dry run: shows what would be deleted
python -m auction_agent cleanup-legacy --yes --delete-backups
```

### Optional: Gemini review
The LLM writes short verdicts for the best cars and reviews cars near a tier boundary (adjustment bounded to −15…+5). It never overrides a 🔴 gate, and it never receives prices, VINs, plates or URLs.

```bash
set GEMINI_API_KEY=...            # PowerShell: $env:GEMINI_API_KEY="..."
set GEMINI_MODEL=<model id>       # pick a current model in Google AI Studio
set GEMINI_PAID_TIER=1            # required: free-tier terms restrict personal data / EEA use
python run_advisor.py --open
```

The old default `gemini-2.5-flash` was reportedly being retired around 16–20 October 2026 (sources conflict), which is why no model is set by default. If the model id is wrong, scoring continues with heuristics only and prints a message.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `AUCTION_AGENT_DATA_DIR` | `data/` | Database, identity key, quarantine |
| `AUCTION_AGENT_DB` | `data/auction.db` | Database file |
| `AUCTION_AGENT_REPORTS_DIR` | `reports/` | HTML reports |
| `AUCTION_AGENT_MIN_INTERVAL` | `2.0` | Seconds between two requests to the same site (plus jitter) |
| `AUCTION_AGENT_USER_AGENT` | browser string | Consider an honest bot name with a contact address (see the legal note) |
| `BUYER_PROFILE` | `particulier` | `pro` drops the private-buyer rules (CT under 6 months, VAT) |
| `ENGINE_RISK_MAX_TIER` | `yellow` | `red` sends known risky engines to 🔴 |
| `TIER_GREEN_MIN` / `TIER_YELLOW_MIN` | `80` / `60` | Tier thresholds |
| `RELIST_DETAIL_CHECK` | `1` | One detail-page check when a car is relisted |
| `IDENTITY_HMAC_KEY` | auto | Key for VIN hashing; otherwise generated once in `data/identity.key` |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_PAID_TIER`, `LLM_LIMIT` | unset, unset, 0, 60 | Optional LLM review |

## Architecture

```
auction_agent/
├── config.py          # Settings from env, one UTC timestamp format
├── db.py              # SQLite schema v2 (WAL, incremental vacuum), upserts, run lock
├── http_client.py     # PoliteClient: retries, Retry-After cap, WAF detection, robots.txt
├── documents.py       # in-memory PDF → text → redaction (no files on disk)
├── ct_parser.py       # CT procès-verbal parser v2 (results, defects, odometer)
├── privacy.py         # VIN/plate redaction, keyed HMAC identity
├── ingest.py          # one-sale crawl, identity-aware, per-car error isolation
├── adapters/
│   ├── base.py        # AuctionAdapter contract + self-registering registry
│   └── alcopa.py      # Alcopa listing/detail parsing and fee model
├── scoring/
│   ├── heuristics.py  # rubric, gates, cohort value
│   ├── engines.json   # sourced engine-risk knowledge base
│   ├── llm.py         # optional Gemini review (google-genai)
│   └── service.py     # rebuild assessments, LLM cache
├── report.py + templates/report.html.j2
└── cli.py             # python -m auction_agent …
```

**Adding an auction house** means adding one module in `auction_agent/adapters/` that subclasses `AuctionAdapter` (it registers itself), plus HTML fixtures under `tests/fixtures/<slug>/`. Scoring and storage need no changes.

## Tests

```bash
pip install -r requirements-dev.txt
python -X warn_default_encoding -m pytest -q
```

The tests never use the network. The Alcopa fixtures are **synthetic**, built from markup that third parties quoted from real pages. Capture real pages (VIN and plate are redacted) and re-run the tests against them:

```bash
python -m auction_agent capture --url /salle-de-vente-encheres/beauvais/13026 --detail /voiture-occasion/peugeot/<slug>-<id>
```

## ⚖️ Legal and ethical notes (not legal advice)
- Systematically extracting and republishing an auction catalogue can infringe the EU/French **database right**. French courts have ruled against listing aggregators, and the CJEU's *Innoweb* ruling concerned a car-ads meta-search engine. Alcopa's terms of sale reserve its descriptions and photos.
- Until there is a licence or partnership: keep the tool **private**, store only derived data, link to the source listing, and never rehost photos or PDFs.
- Licence plates and VINs are personal data under CNIL guidance. This code never stores them in clear text, never displays them, and never sends them to an LLM.
- Respect robots.txt and rate limits. **Never bypass a block or CAPTCHA**: the crawler stops by design when it meets one.
- Have a French IP and data-protection lawyer review the project before any public launch. The details are in docs/MASTER_PLAN.md §1 and Appendix A.

## Roadmap
Phase 2 adds the daily lifecycle engine: sale discovery, snapshot diff, dormancy and purge with a compact derived archive, and an APScheduler worker on Europe/Paris time. Phase 3 deepens the scoring, Phase 4 adds a private web MVP (Starlette + Jinja2 + HTMX), and Phase 5 adds VP Auto and on-demand links. Details are in [docs/MASTER_PLAN.md](docs/MASTER_PLAN.md).

## License
MIT. See [LICENSE](LICENSE).
