# 🚗 Auction-Agent: Multi-Auction Crawler & AI Car Deal Finder

> Crawls French vehicle auction catalogues, reads the official *Contrôle Technique* (CT) reports, and scores **every** vehicle 0–100 into 🟢 / 🟡 / 🔴 tiers, so buyers see the best deals of the week first.

The roadmap, architecture decisions, sources and known risks are in **[docs/MASTER_PLAN.md](docs/MASTER_PLAN.md)**. This README covers how to run the current code (Phase 1).

---

## What it does today (Phase 1)

| Step | What happens |
|---|---|
| Crawl | Walks one Alcopa sale, page by page, and **reports whether the walk was complete**. Each lot is identified by the product id in its URL, not by its lot number. |
| Ingest | New cars get their detail page and CT PDF (plus the battery certificate of electric and plug-in hybrid cars). A PDF is downloaded **into memory**, parsed, and VIN/plate-redacted, and is **never written to disk**. The detail page also gives the seller comment, the body-damage list ("Défauts esthétiques", zone and kind only) and the warranty formula. Known cars only get a cheap listing refresh. A relisted car gets one detail-page check, and its CT is downloaded again only if the CT link changed. |
| Score | Deterministic rubric with gates, run on every car. The AI review is optional: Anthropic Claude (`claude-haiku-5-5`) by default, OpenAI `gpt-6-luna` selectable. It writes a verdict for every car the site lists, and its output is cached. |
| Report | Self-contained HTML report: top picks (only cars whose sale is in the next 7 days), then cars grouped by tier, with the facts behind every score and every cap. |

| Tier | Score | Meaning |
|---|:---:|---|
| 🟢 Profil favorable | 80–100 | Clean or minor-only CT, no known deal-breaker, good value against comparable cars |
| 🟡 Profil correct | 60–79 | Acceptable, or data incomplete (capped here until the facts are known) |
| 🔴 Risque élevé | < 60 | Critical CT defect, deal-breaker in the comments (VGE, moteur HS…), or a degraded EV battery |

Known engine risks (1.2 PureTech wet belt, 1.5 BlueHDi chain, 1.2 TCe oil consumption) cost a heavy penalty and cap the car at 🟡 by default. Set `ENGINE_RISK_MAX_TIER=red` to send them to 🔴.

**What the condition score reads, besides the CT report:**
- **Battery health** (electric and plug-in hybrid cars): the SoH shown on the listing, cross-checked with the battery certificate when there is one (parser `bat-1.0`, Moba layout). The listing value is kept when both agree within 2 points; the certificate fills in when the listing has none; when they disagree, the lower value is used and the reason says so. It is shown rounded down (95.35 % reads "95 %"), so the 75 / 80 / 90 % thresholds are unchanged. A leased battery is never gated.
- **Warranty:** +5 condition points. The reason shows the formula read on the detail page ("Garantie Sérénité de 3 mois incluse"); when only the card's badge is known, it reads "Garantie mécanique incluse (formule non précisée)".
- **Body damage:** the detail page's list of cosmetic marks. Each distinct zone and kind outside the wheels counts 1 point, a dent or replacement 2; the first 3 points are free and the penalty is capped at 6 condition points (at most about 2 points of the final score). It never caps the tier: structural damage is judged from the CT. Calibrated on two real pages only, so re-check it after a few crawls.
- **Seller comments:** a short lexicon (VGE, moteur HS, kilométrage non garanti, voyant, bruit, fumée…). A negation only counts inside its own clause ("Km non garanti, moteur HS" is still a broken engine), and a red deal-breaker is cleared only by a negation right before it ("non VGE").

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

In PowerShell, `venv\Scripts\Activate.ps1` is often refused by the execution policy ("running scripts is disabled"). You do not need it: call the environment's Python directly, e.g. `venv\Scripts\python.exe -m auction_agent stats`.

```bash
# Fast test: first 5 vehicles of the default sale, no PDF downloads
python run_crawl.py --limit 5 --no-docs

# One listing page, with CT documents
python run_crawl.py --pages 1 --limit 10

# A specific sale (each Alcopa sale has its own id in the URL)
python run_crawl.py --url /salle-de-vente-encheres/beauvais/13026

# Score every vehicle and open the report (rules only without an API key; with ANTHROPIC_API_KEY
# set it also runs the AI review: add --no-llm to skip it)
python run_advisor.py --open
python run_advisor.py --max-price 12000 --open   # all-in budget, euros
python run_advisor.py --sale-id 3 --open         # one sale: id from `python -m auction_agent stats`
```

`--sale-id` takes the database id printed by `crawl` and `stats`; the auction house's own sale number (e.g. `13026`) is accepted too. `--no-llm` scores with heuristics only and also ignores LLM output cached by earlier runs.

The same commands are available as `python -m auction_agent crawl | publish | recon | score | report | advise | stats | llm-estimate | preview | capture | cleanup-legacy`. Add `--help` to any command for its flags.

### Upgrading from the prototype
On first run, the old `data/auction.db` is moved to `data/auction.v1-<date>.db` with VIN, plate, comments, raw CT text and photo URLs emptied, and a new database is created. To delete the old database (including the backup made by the same command) and the prototype's `data/documents/lot_*` / `veh_*` PDFs:

```bash
python -m auction_agent cleanup-legacy            # dry run: shows what would be deleted
python -m auction_agent cleanup-legacy --yes --delete-backups
```

### Optional: AI review (Claude by default)
The AI writes a short French verdict ("Avis IA") for every car the site lists (about 50 per run) and reviews cars near a tier boundary (adjustment bounded to −15…+5). It never overrides a 🔴 gate. Without a key, everything works with the rules only.

**What is sent, per car:** brand, model, trim, fuel, gearbox, year, mileage and engine size; the known engine-risk flags; the CT result, date and mileage, the counts and **every** CT defect (code, severity, system such as "freinage", label), most serious first; the body-damage list (zone, kind, count); the battery health %; whether a warranty is included; and the seller comments with VINs and plates (however they are separated), amounts in euros (k€ and HT/TTC included), phone numbers (+33, 0033 included), e-mails and links removed (at most 1,000 characters). Empty fields are left out. **Never** the price, the listing URL, the VIN, the plate or the raw CT text.

**1. Prepare your Claude account** (one time, in the Claude Console at platform.claude.com):
- Add credits to the organisation.
- Create a workspace for this project, e.g. `auction-agent`, and give it a **monthly spend limit** (e.g. $2). When the limit is reached, the app stops calling the AI and keeps scoring with the rules.
- Create an API key in that workspace (it starts with `sk-ant-`).

**2. Put the key in `.env`** (Windows, in the project folder):

```bat
copy .env.example .env
notepad .env
```

PowerShell: `Copy-Item .env.example .env; notepad .env`. Paste your key after `ANTHROPIC_API_KEY=` (no quotes needed; a ` # comment` after it is ignored) and save. Make sure Windows did not save it as `.env.txt`; the app warns you if it did. `.env` is git-ignored, so the key never goes to GitHub. Never put the key in `.env.example`, which is committed. A variable already set in your system environment (e.g. with `setx`) takes precedence over `.env`. If your `.env` predates November 2026, remove any `LLM_MODEL=gpt-…` or `LLM_REASONING_EFFORT=off` line: with Claude they stop the AI review (the run says so and exits 4).

**3. Check it with a small run** (after `venv\Scripts\python.exe -m pip install -r requirements.txt`, which installs the `anthropic` SDK):

```bash
python -m auction_agent score --llm-limit 5
python -m auction_agent report --open
python -m auction_agent llm-estimate
```

The first line of `score` says which provider and model run and where the key came from (`ANTHROPIC_API_KEY from .env`, `from the environment` or `not set`); the key itself is never printed. `report` writes the report without scoring again. Read a few French verdicts before raising the limit.

**Model and cost** (prices from the claude-api skill's Claude Haiku 5.5 guide, read on 2026-11-07; check the current figures at https://platform.claude.com/docs/en/about-claude/pricing):
- `claude-haiku-5-5` is the cheapest current Claude model: **$0.10 per 1M input tokens and $0.50 per 1M output tokens** while a request's prompt is 100K tokens or fewer, $0.50 / $2.50 above. The app splits batches at 20 cars or 150,000 characters (about 50K tokens), so a request stays far below 100K (about 7K tokens for 20 listed cars today).
- Thinking is billed as output. The default `LLM_REASONING_EFFORT=low` lets the model think a little (adaptive thinking at low effort); `none` turns thinking off and is the cheapest; `medium` and `high` cost more.
- Each run prints a line such as `AI review: 3 call(s), 15,551 input + 12,000 output tokens, about $0.0076 (claude-haiku-5-5)`. The cost is stored with each batch; `stats` shows the total.
- **Estimated cost** (`llm-estimate` on the 2026-11-09 sale, 50 listed cars, before comments and damage lists are crawled): about **$0.008–0.016 per run**, $0.015–0.03 a week for 2 runs, **$0.07–0.14 a month**. Unchanged cars are not reviewed again, so the second run of a week usually costs less. A one-off review of all 573 cars would cost about $0.10–0.19. Payloads will grow once comments and damage lists are read (projected about 1,400 characters per listed car instead of about 760 today); the figures stay in cents. Your spend limit is the real cap.
- `llm-estimate` builds the real payloads from the database and prints characters, estimated tokens and $ per run, week and month for every priced model. It makes no API call. `--all` prices every car of the sale, `--runs-per-week` changes the weekly multiplier, `--sale-id` picks a sale.
- The model takes no temperature setting, so the wording of a verdict can vary; it only changes when the car's data changes, because results are cached by payload.
- A batch the model declines (a safety refusal) or cuts off is recorded as failed; its cars are retried alone on the next run, then given up after two failures until their data, the model, the effort or the prompt changes.
- The SDK's base URL is pinned to `https://api.anthropic.com` (a stray `ANTHROPIC_BASE_URL` is ignored), and `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_PROFILE` are ignored because the key is passed explicitly. `ANTHROPIC_CUSTOM_HEADERS` and `ANTHROPIC_LOG` still apply in the SDK, so the run prints a note when they are set.

**Data:** Anthropic's data-use and retention terms for the API were **not** checked for this change (no network access while it was written). Read Anthropic's commercial terms (https://www.anthropic.com/legal/commercial-terms) and privacy pages before the first run. Only the vehicle data listed above is sent. *Not legal advice.*

If the key, model or credits are wrong, scoring continues with the rules only and prints a message saying what to fix. `GEMINI_*` variables from earlier versions are no longer read.

#### Switching back to OpenAI
Set `LLM_PROVIDER=openai` and `OPENAI_API_KEY=` in `.env` (the commented block in `.env.example`). `LLM_MODEL` then defaults to `gpt-6-luna` and `LLM_REASONING_EFFORT` to `none`. Every car gets one fresh review after a switch (the model is part of the cache key). If only `OPENAI_API_KEY` is set while the provider is Claude, the run says so.

**OpenAI account** (one time, on platform.openai.com): add prepaid credits (the minimum was reported as $5; **auto-recharge is on by default**, switch it off or cap it); create a project with a **hard monthly spend limit**; under *Data controls → Sharing*, leave every sharing option off; create an API key in that project.

**OpenAI model and cost** (prices and dates from OpenAI's pricing and deprecation pages as reported by search results and price tables on 2026-10-14; OpenAI's site could not be opened directly, so check the current figures on platform.openai.com):
- `gpt-6-luna` is the cheapest OpenAI model not scheduled for retirement, at the same list price as Claude Haiku 5.5: $0.10 per 1M input tokens and $0.50 per 1M output tokens. `gpt-5-nano` costs less per token but shuts down on 2026-12-11.
- It is a reasoning model whose default effort is "medium" (hidden reasoning tokens are billed as output). The app sends reasoning effort `none`; if the model ever spends reasoning tokens anyway, the run prints a note.
- `gpt-6-luna` has no dated snapshot, so OpenAI can change its behaviour under the same name; the model that actually answered is stored with each review.

**OpenAI data:** OpenAI does not train on API data unless you opt in. It may keep requests for up to 30 days for abuse monitoring, and processing happens in the US (EU data residency requires OpenAI's approval). The app sends `store=false` and asks OpenAI not to use prompt caching (an option supported from the gpt-5.6 generation; if a model rejects it, the run prints a note). *Not legal advice.*

## Online MVP: this week's best cars, on a private site

The site **https://auction-agent-mvp.vercel.app** shows this week's Monday sale at Alcopa Beauvais (passenger cars) under the working public name **Auction France** (pending the owner's approval). It follows redesign v2 « Relevé » ([docs/redesign/v2/BRIEF.md](docs/redesign/v2/BRIEF.md)); the design system and the rules behind each screen are in [docs/DESIGN.md](docs/DESIGN.md).

**The page, top to bottom:**
- **A carbon band.** It holds:
  - a general headline (« Les voitures aux enchères, décodées avant la vente. »);
  - the search and five shortcuts;
  - the card of the next analysed sale: date, house, room, lots, CT reports read, a waffle with one square per lot coloured by CT result (its legend filters the list), and the analysis status, each gap said once.
- **A sticky catalogue bar.** It holds the sale, the three sections (« À regarder d'abord », « Autres fiches détaillées », « Les autres lots »), the filters and the sort.
- **One row per lot.** Each row shows:
  - the lot tag;
  - the car, with one signal (an engine at risk, a leased battery, high mileage, non-recoverable VAT, a gap in the lot's own data);
  - the CT as a 3×3 grid of its nine functions;
  - the estimated cost and what it starts from;
  - a score ring.
- **The lot sheet.** It is a sticky pane from 64rem and a full-screen view on a phone (Back closes it). In order:
  1. **« Voir l'annonce sur Alcopa ↗ » first**;
  2. *En bref*;
  3. *Coût total estimé*, with a **bid simulator**: the buyer's own arithmetic, not a ceiling;
  4. the labelled CT grid and its defects;
  5. what else to check.

  `#lot-751` in the address opens that lot.

**French and English:** `/` and `/en/` are the same page, with the copy in `web/i18n/fr.json` / `en.json`. `web/en/index.html` is generated by `python -m auction_agent.site_i18n`. Rule output is written from the snapshot's codes, never translated.

**Accessibility and safety:**
- Light and dark modes, keyboard use, reduced motion and Windows high-contrast mode are supported.
- The fonts (Archivo, JetBrains Mono) are served by the site itself; nothing loads from another site, and there is no inline code (strict CSP).

**Errors:** one clear message per problem: no analysis published yet, storage not linked or not answering, analysis too large or unreadable, expired Vercel session, no connection.

**Méthode** (`/about.html`, French) explains the coverage, the score and its ring colours, the CT grid, the engines we track, the estimated cost, the simulator, the AI review and a French-English glossary.

**Access:** it is protected by Vercel Authentication, so only you (signed in to Vercel) can open it. To show it to a tester, ask for a temporary share link.

**How it updates:**
1. Your Windows PC runs `python -m auction_agent publish` every **Thursday at 19:17** and **Saturday at 09:43**, through Task Scheduler. If the PC was off, the run starts as soon as it is back on.
2. Each run finds this week's Monday sale on the Beauvais room page, crawls it politely, scores every car and asks the AI for verdicts.
3. It then uploads a snapshot to the project's private Vercel Blob store, which the site reads.
4. The database stays on your PC. Only the snapshot leaves it: specs, scores and the points behind them, the price breakdown, the CT reading (defect labels redacted again), the body-damage list (zone and kind only), battery health, AI verdict and a link to each lot. No VIN, plate, seller comment, photo or internal id. (The AI review also sends the vehicle data listed under "What is sent" to the AI provider.) The snapshot is JSON schema 2 (schema 1 plus new fields, so an old page still reads it and the new page still reads a schema-1 snapshot); about 400 KB for a 573-lot sale (with the v2 fields: CT grids, codes, fee model), and the one-line list is dropped above 1.5 MB.

**AI verdicts on every listed car:** the AI writes a verdict for every car the site lists (the top 10, then up to 40 more; about 50 per run), in page order, so the run budget (`LLM_LIMIT`, 80 by default) goes first to what the page shows. While a sale's data is still incomplete (often on Thursday), the provisional top 10 are listed and reviewed too, so the page is never empty. Unchanged cars are not reviewed again; a car whose data changed (a price or CT report that appeared by Saturday) gets one new review. Estimated cost: about $0.008–0.016 per run, $0.07–0.14 a month (see "Model and cost" above). The snapshot's status block shows how many listed cars have a verdict, the tokens used and the cost of the run.

**Why your PC and not the cloud:** on 2026-11-06, Alcopa's firewall refused GitHub's cloud servers at the very first request (HTTP 405 on robots.txt). From the owner's PC on 2026-11-07, robots.txt allowed every path the crawler uses and the full sale was crawled. The crawler stops whenever it is blocked or challenged, and never tries to get around it: no proxies, no CAPTCHA solving, no disguised browser. If your own connection is refused too, stop and treat it as Alcopa saying no.

**CT documents come from Amazon S3:** each CT link on Alcopa redirects to a signed download on `s3.eu-west-3.amazonaws.com`, whose root answers robots.txt with a plain `403 AccessDenied`. For a host the auction site redirects to, the crawler reads a plain 4xx robots.txt as "no robots.txt" (RFC 9309 §2.3.1.3). On Alcopa itself a 403/405 robots.txt still stops the run, and a challenge page or a refused download stops it everywhere.

### One-time setup on Windows
Every command below uses the project's virtual environment directly (`venv\Scripts\python.exe`), so it works in cmd and PowerShell without activating anything. A bare `python` may be another Python on your PC (for example Anaconda 3.9, too old for this project).

1. **Update the code:** run `git pull`, then `venv\Scripts\python.exe -m pip install -r requirements.txt`.
2. **Check access from your connection** (2–4 polite requests):
   ```bat
   venv\Scripts\python.exe -m auction_agent recon
   ```
   - It prints the robots.txt verdicts and `chosen: <sale id> on <date>`.
   - If it prints `blocked or challenged`, stop here.
   - If it prints `chosen: none` (the room page layout differs from what the code expects), send me the output, or pass the sale link yourself with `--url /salle-de-vente-encheres/beauvais/<id>`.
3. **Fill in `.env`:**
   - `ANTHROPIC_API_KEY` (see "Optional: AI review" above);
   - `BLOB_READ_WRITE_TOKEN`, from Vercel → Storage → `auction-agent-state` (it starts with `vercel_blob_rw_`).
4. **Local test, nothing uploaded** (needs only `ANTHROPIC_API_KEY`):
   ```bat
   venv\Scripts\python.exe -m auction_agent publish --out data\snapshot-local
   venv\Scripts\python.exe -m auction_agent preview --open
   ```
   - The first command crawls this week's sale, scores it, runs the AI review and writes the snapshot to `data\snapshot-local\` instead of uploading it. Cars already in the database only get a quick refresh, so a second run is much faster than the first.
   - The second serves the site at `http://127.0.0.1:8765/` with that snapshot: the same page, filters and status block as online. Only this computer can open it; press Ctrl+C to stop it. In Claude Code, `.claude/launch.json` starts the same preview as "site-preview".
   - After an upload (next step), `venv\Scripts\python.exe -m auction_agent preview --blob --open` shows the snapshot stored in Vercel Blob, read through the site's own function.
5. **First upload, by hand:**
   ```bat
   venv\Scripts\python.exe -m auction_agent publish
   ```
   A first full sale takes about 15–90 minutes (polite pace); later runs of the same week only refresh known cars.

   | Exit code | Meaning |
   |---|---|
   | 0 | ok |
   | 2 | blocked, failed or upload refused |
   | 3 | no Monday sale found this week |
   | 4 | the sale is live but nothing could be put forward, or the AI review did not run |

   To see the snapshot without uploading it, use step 4.
6. **Schedule it:**
   ```bat
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_task.ps1
   ```
   - Test it with `Start-ScheduledTask -TaskName "Auction Agent weekly crawl"`.
   - Each run writes a log to `data\logs\`.
   - The task runs while you are logged in, at the PC's local time, so keep Windows on Paris time.
7. **Open the site.** A red banner appears if the data is more than 8 days old (for example, if the PC stayed off), or if the last collection was refused.

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
| `AUCTION_AGENT_RESPECT_ROBOTS` | `1` | Keep at 1; an unreadable robots.txt stops the crawl (a plain 4xx robots.txt on a host the auction site redirects to, such as its S3 document storage, counts as none) |
| `AUCTION_AGENT_REQUEST_TIMEOUT` | `30` | Seconds per HTTP request |
| `BUYER_PROFILE` | `particulier` | `pro` drops the private-buyer rules (CT under 6 months, VAT) |
| `ENGINE_RISK_MAX_TIER` | `yellow` | `red` sends known risky engines to 🔴 |
| `TIER_GREEN_MIN` / `TIER_YELLOW_MIN` | `80` / `60` | Tier thresholds |
| `RELIST_DETAIL_CHECK` | `1` | One detail-page check when a car is relisted |
| `LATE_CT_BUDGET` | `50` | Maximum detail pages re-checked per crawl for cars whose CT appeared late |
| `IDENTITY_HMAC_KEY` | auto | Key for VIN hashing (hex, at least 32 bytes); otherwise generated once in `data/identity.key`. Never change it on an existing database |
| `LLM_PROVIDER` | `anthropic` | AI reviewer: `anthropic` (Claude) or `openai`. Any other value stops the program |
| `ANTHROPIC_API_KEY` | unset | Enables the AI review with Claude. Put it in `.env` (see above) |
| `OPENAI_API_KEY` | unset | Enables the AI review with `LLM_PROVIDER=openai` |
| `LLM_MODEL` | `claude-haiku-5-5` (OpenAI: `gpt-6-luna`) | Model of the chosen provider |
| `LLM_REASONING_EFFORT` | `low` (OpenAI: `none`) | Claude: `none` (no thinking, cheapest), `low`, `medium` or `high`. OpenAI: `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`, or `off` for a model without reasoning. Anything higher costs more |
| `LLM_LIMIT` | `80` | Maximum vehicles sent to the AI per run (about 50 are listed) |
| `AUCTION_AGENT_ENV_FILE` | `.env` | Settings file read at start-up (values already in the environment win) |
| `BLOB_READ_WRITE_TOKEN` | unset | Lets `publish` upload the weekly snapshot to the site's private Vercel Blob store |

## Architecture

```
auction_agent/
├── config.py          # Settings from env, one UTC timestamp format
├── db.py              # SQLite schema v4 (WAL, incremental vacuum), upserts, run lock
├── http_client.py     # PoliteClient: retries, Retry-After cap, WAF detection, per-hop robots checks
├── robots.py          # RFC 9309 robots.txt matcher (same result on every Python version)
├── documents.py       # in-memory PDF → text → redaction (no files on disk)
├── ct_parser.py       # CT procès-verbal parser v2 (results, defects, odometer)
├── battery_parser.py  # battery-certificate parser bat-1.0 (SoH, capacity, diagnostic date and km)
├── privacy.py         # VIN/plate redaction, keyed HMAC identity
├── ingest.py          # one-sale crawl, identity-aware, per-car error isolation
├── adapters/
│   ├── base.py        # AuctionAdapter contract + self-registering registry
│   └── alcopa.py      # Alcopa listing/detail parsing and fee model
├── scoring/
│   ├── heuristics.py  # rubric, gates, cohort value
│   ├── engines.json   # sourced engine-risk knowledge base
│   ├── llm.py         # AI review core: prompt, schema, cache hash, make_client (no SDK import)
│   ├── llm_anthropic.py  # Claude client (anthropic SDK, structured output), the default
│   ├── llm_openai.py  # OpenAI client (openai SDK, strict JSON schema), LLM_PROVIDER=openai
│   ├── pricing.py     # model prices, rate cards, cost of a call
│   ├── estimate.py    # llm-estimate: offline cost estimate from the stored cars
│   └── service.py     # rebuild assessments, site selection, AI review plan, LLM cache
├── report.py + templates/report.html.j2
├── snapshot.py        # the weekly JSON the site shows (schema 2, whitelisted fields only, size guard)
├── blobstore.py       # upload to the private Vercel Blob store (or a local folder)
├── preview.py         # the site on 127.0.0.1 with a local or uploaded snapshot
└── cli.py             # python -m auction_agent …
web/                   # the private site on Vercel: catalogue page (index.html, app.js, styles.css),
                       # about.html, favicon.svg, api/snapshot.py (stdlib only)
scripts/windows/       # Task Scheduler: run_publish.ps1 and install_task.ps1
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
