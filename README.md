# 🚗 Auction-Agent: Multi-Auction Crawler & AI Car Deal Finder

> Crawls French vehicle auction catalogues, reads the official *Contrôle Technique* (CT) reports, and scores **every** vehicle 0–100 into 🟢 / 🟡 / 🔴 tiers, so buyers see the best deals of the week first.

The roadmap, architecture decisions, sources and known risks are in **[docs/MASTER_PLAN.md](docs/MASTER_PLAN.md)**. This README covers how to run the current code (Phase 1).

---

## What it does today (Phase 1)

| Step | What happens |
|---|---|
| Crawl | Walks one Alcopa sale, page by page, and **reports whether the walk was complete**. Each lot is identified by the product id in its URL, not by its lot number. |
| Ingest | New cars get their detail page and CT PDF. The PDF is downloaded **into memory**, parsed, and VIN/plate-redacted, and is **never written to disk**. Known cars only get a cheap listing refresh. A relisted car gets one detail-page check, and its CT is downloaded again only if the CT link changed. |
| Score | Deterministic rubric with gates, run on every car. The AI review (OpenAI, default model `gpt-6-luna`) is optional, only called on triggers, and its output is cached. |
| Report | Self-contained HTML report: top picks (only cars whose sale is in the next 7 days), then cars grouped by tier, with the facts behind every score and every cap. |

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

# Score every vehicle and open the report (rules only without an API key; with OPENAI_API_KEY
# set it also runs the AI review: add --no-llm to skip it)
python run_advisor.py --open
python run_advisor.py --max-price 12000 --open   # all-in budget, euros
python run_advisor.py --sale-id 3 --open         # one sale: id from `python -m auction_agent stats`
```

`--sale-id` takes the database id printed by `crawl` and `stats`; the auction house's own sale number (e.g. `13026`) is accepted too. `--no-llm` scores with heuristics only and also ignores LLM output cached by earlier runs.

The same commands are available as `python -m auction_agent crawl | score | report | advise | stats | capture | cleanup-legacy`. Add `--help` to any command for its flags.

### Upgrading from the prototype
On first run, the old `data/auction.db` is moved to `data/auction.v1-<date>.db` with VIN, plate, comments, raw CT text and photo URLs emptied, and a new database is created. To delete the old database (including the backup made by the same command) and the prototype's `data/documents/lot_*` / `veh_*` PDFs:

```bash
python -m auction_agent cleanup-legacy            # dry run: shows what would be deleted
python -m auction_agent cleanup-legacy --yes --delete-backups
```

### Optional: OpenAI review
The AI writes short verdicts for the best cars and reviews cars near a tier boundary (adjustment bounded to −15…+5). It never overrides a 🔴 gate. It receives specs, parsed CT defects and seller comments with VINs, plates, amounts in euros (or HT/TTC), phone numbers, e-mails and links removed; never the price, the listing URL or the raw CT text. Without a key, everything works with the rules only.

**1. Prepare your OpenAI account** (one time, on platform.openai.com):
- Add prepaid credits (the minimum was reported as $5). **Auto-recharge is on by default**: switch it off or cap it.
- Create a project, e.g. `auction-agent`, and give it a **hard monthly spend limit** (e.g. $2–5). When the limit is reached, the app stops calling the AI and keeps scoring with the rules.
- Under *Data controls → Sharing*, leave every sharing option off and decline any "free tokens for sharing data" offer.
- Create an API key in that project.

**2. Put the key in `.env`** (Windows, in the project folder):

```bat
copy .env.example .env
notepad .env
```

PowerShell: `Copy-Item .env.example .env; notepad .env`. Paste your key after `OPENAI_API_KEY=` (no quotes needed; a ` # comment` after it is ignored) and save. Make sure Windows did not save it as `.env.txt`; the app warns you if it did. `.env` is git-ignored, so the key never goes to GitHub. Never put the key in `.env.example`, which is committed. A variable already set in your system environment (e.g. with `setx`) takes precedence over `.env`.

**3. Check it with a small run:**

```bash
python -m auction_agent score --llm-limit 5
python -m auction_agent report --open
```

The first line of `score` says which model runs and where the key came from (`key from .env`, `key from the environment` or `key not set`); the key itself is never printed. `report` writes the report without scoring again. Read a few French verdicts ("Avis IA") before raising the limit.

**Model and cost** (prices and dates from OpenAI's pricing and deprecation pages as reported by search results and price tables on 2026-10-14; OpenAI's site could not be opened directly, so check the current figures on platform.openai.com):
- `gpt-6-luna` is the cheapest OpenAI model not scheduled for retirement: $0.10 per 1M input tokens and $0.50 per 1M output tokens. `gpt-5-nano` costs less per token but shuts down on 2026-12-11, and `gpt-4.1-nano` on 2026-10-23.
- It is a reasoning model whose default effort is "medium" (hidden reasoning tokens are billed as output). The app sends reasoning effort `none`; if the model ever spends reasoning tokens anyway, the run prints a note.
- Estimated cost: about **$0.004 per run of 60 cars** (about $0.02 with conservative token estimates). Cars already reviewed cost nothing on later runs. Your hard spend limit is the real cap.
- `gpt-6-luna` has no dated snapshot, so OpenAI can change its behaviour under the same name; the model that actually answered is stored with each review.

**Data:** OpenAI does not train on API data unless you opt in. It may keep requests for up to 30 days for abuse monitoring, and processing happens in the US (EU data residency requires OpenAI's approval). The app sends `store=false` and asks OpenAI not to use prompt caching (an option supported from the gpt-5.6 generation; if a model rejects it, the run prints a note). *Not legal advice.*

If the key, model or credits are wrong, scoring continues with the rules only and prints a message saying what to fix. `GEMINI_*` variables from earlier versions are no longer read.

## Configuration (environment variables)

Every variable can also go in the `.env` file (one `NAME=value` per line).

| Variable | Default | Purpose |
|---|---|---|
| `AUCTION_AGENT_DATA_DIR` | `data/` | Database, identity key, quarantine |
| `AUCTION_AGENT_DB` | `data/auction.db` | Database file |
| `AUCTION_AGENT_REPORTS_DIR` | `reports/` | HTML reports |
| `AUCTION_AGENT_MIN_INTERVAL` | `2.0` | Seconds between two requests to the same site (plus jitter) |
| `AUCTION_AGENT_USER_AGENT` | browser string | Consider an honest bot name with a contact address (see the legal note) |
| `AUCTION_AGENT_ROBOTS_TOKEN` | `AuctionAgent` | Product token matched against `User-agent:` groups in robots.txt (RFC 9309 matching: longest rule wins, `*` and `$` supported) |
| `AUCTION_AGENT_RESPECT_ROBOTS` | `1` | Keep at 1; an unreadable robots.txt stops the crawl |
| `AUCTION_AGENT_REQUEST_TIMEOUT` | `30` | Seconds per HTTP request |
| `BUYER_PROFILE` | `particulier` | `pro` drops the private-buyer rules (CT under 6 months, VAT) |
| `ENGINE_RISK_MAX_TIER` | `yellow` | `red` sends known risky engines to 🔴 |
| `TIER_GREEN_MIN` / `TIER_YELLOW_MIN` | `80` / `60` | Tier thresholds |
| `RELIST_DETAIL_CHECK` | `1` | One detail-page check when a car is relisted |
| `LATE_CT_BUDGET` | `50` | Maximum detail pages re-checked per crawl for cars whose CT appeared late |
| `IDENTITY_HMAC_KEY` | auto | Key for VIN hashing (hex, at least 32 bytes); otherwise generated once in `data/identity.key`. Never change it on an existing database |
| `OPENAI_API_KEY` | unset | Enables the optional AI review. Put it in `.env` (see above) |
| `LLM_MODEL` | `gpt-6-luna` | OpenAI model for the AI review |
| `LLM_REASONING_EFFORT` | `none` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max` (anything above `none` costs more), or `off` for a model without reasoning |
| `LLM_LIMIT` | `60` | Maximum vehicles sent to the AI per run |
| `AUCTION_AGENT_ENV_FILE` | `.env` | Settings file read at start-up (values already in the environment win) |

## Architecture

```
auction_agent/
├── config.py          # Settings from env, one UTC timestamp format
├── db.py              # SQLite schema v2 (WAL, incremental vacuum), upserts, run lock
├── http_client.py     # PoliteClient: retries, Retry-After cap, WAF detection, per-hop robots checks
├── robots.py          # RFC 9309 robots.txt matcher (same result on every Python version)
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
│   ├── llm.py         # optional OpenAI review (openai SDK, strict JSON schema)
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
