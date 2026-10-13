# 🚗 Auction-Agent: Multi-Auction Crawler & AI Car Deal Finder

> **Autonomous AI-powered intelligence platform for French automotive auctions.**
> Crawls weekly auction listings, downloads and inspects technical control sheets (*Contrôle Technique*), maintenance records, and battery certificates, and leverages AI agents to rank vehicles by deal quality using a traffic-light scoring system.

---

## 🌟 Vision & Project Scope

In the French automotive auction market (e.g., Alcopa Auction, BCAuto Enchères, VP Auto, Enchères VO, Interencheres), hundreds of vehicles are auctioned weekly. Sifting through hundreds of lots, cross-referencing mileage, age, price, and reading detailed French *Contrôle Technique* (CT) PDF reports to spot hidden mechanical defects is time-consuming and error-prone.

**Auction-Agent** solves this by establishing an autonomous, multi-agent pipeline:
1. **Weekly Automated Scraping**: Crawls upcoming auction catalogs across major French auction halls.
2. **Deep Document Inspection**: Automatically retrieves and parses technical inspection sheets (*Contrôle Technique*), maintenance histories, and EV/hybrid battery state-of-health (SoH) certificates.
3. **AI Deal Scoring & Ranking**: Synthesizes market pricing, vehicle age, mileage ratios, known engine reliability flaws, and CT inspection defects to surface the **single greatest deals of the week**, ranked from best to worst.
4. **Traffic-Light Tiering**:
   - 🟢 **Top Tier (Green — Super Affaires)**: Exceptional value, clean technical inspection, high mechanical reliability, and attractive starting bids.
   - 🟡 **Middle Tier (Yellow — Affaires Correctes)**: Acceptable daily drivers with fair pricing and minor cosmetic or non-critical maintenance items.
   - 🔴 **Avoid Tier (Red — Risqué / Carton Rouge)**: High financial/safety risk—vehicles with major/critical inspection failures, suspicious histories, or engines with known catastrophic failure patterns.

---

## 🔮 Future Roadmap & Extensions

- **On-Demand Custom Auction Ingestion**: Allow users to paste *any* arbitrary auction link. The crawler and AI agent dynamically parse the target page on the fly, analyze the lot, and deliver instant recommendations.
- **Full MVP Web Application**: A modern, interactive web platform (FastAPI + React/Vue/Tailwind) featuring faceted search, budget filters, visual lot galleries, and auction calendar timelines.
- **Universal Multi-Auction House Adapters**: Modular scraper plugins for all premier French and European auction houses (VP Auto, BCAuto Enchères, Enchères VO, Agorastore, etc.).
- **Live Market Price Benchmarks**: Automated integration with French used car market baselines (e.g., La Centrale, Leboncoin, Cote Argus) to dynamically compute true profit margins and bidding caps.
- **Automated Alerts & Notifications**: Instant notifications (Telegram, Discord, Email) when a 🟢 Green-tier deal matching user preferences appears in upcoming auctions.

---

## 🤖 Guide for Incoming AI Agents (Sparring Partner Protocol)

> ### 📢 Attention AI Agents (Claude, GPT, Gemini, DeepSeek, etc.):
> You are welcomed to this repository not merely as a task executor, but as an **active sparring partner** and **system architect**.
>
> When you engage with this codebase:
> 1. **Challenge Assumptions & Architecture**: Do not hesitate to critique the current file structure, data pipelines, database schema, or scoring heuristics. If you see a cleaner, more modular, or more performant design, propose and implement it.
> 2. **Propose Optimizations**:
>    - **Scraping**: Transition from synchronous `requests` to async concurrency (`httpx` / `aiohttp` / `asyncio`) with adaptive rate limiting and robust proxy/session rotation.
>    - **PDF Extraction**: Improve French *Contrôle Technique* parsing heuristics to handle scanned/rasterized PDFs or non-standard regional formats via OCR.
>    - **Scoring Rubric**: Refine the prompt engineering in `advisor/ai_advisor.py` to incorporate deeper automotive domain knowledge (e.g., known engine codes: PureTech 1.2 EB2 wet belts, 1.5 BlueHDi DV5 camshaft chains, Renault 1.2 TCe H5Ft oil consumption, Ford EcoBoost 1.0 wet belts).
>    - **Database**: Scale from SQLite to PostgreSQL with Alembic migrations if multi-user concurrency or persistent web serving is introduced.
> 3. **Maintain Modularity**: Ensure that adding new auction houses is as simple as subclassing an abstract `BaseAuctionScraper` interface.
> 4. **Keep Tests & Types Solid**: Preserve strict typing, docstrings, and comprehensive error resilience.

---

## 🏗️ Architecture & Component Design

```
Auction-Agent/
├── config.py                  # Global settings, delays, base URLs, Gemini config
├── database.py                # SQLite schema (sales, vehicles, documents, photos) & CRUD
├── crawler.py                 # Pipeline orchestrator (listing -> detail -> docs -> DB)
├── pdf_extractor.py           # pdfplumber text extraction & CT defect classifier
├── run_crawl.py               # CLI entry point: run the crawler
├── run_advisor.py             # CLI entry point: run the AI advisor & HTML generator
├── requirements.txt           # Python package dependencies
├── .gitignore                 # Excludes local databases, scraped PDFs, and caches
├── scraper/
│   ├── __init__.py
│   ├── listing_scraper.py     # Paginated card crawler & metadata extractor
│   ├── detail_scraper.py      # Spec table extractor & document link finder
│   └── document_downloader.py # Binary/PDF stream downloader with retry logic
├── advisor/
│   ├── __init__.py
│   └── ai_advisor.py          # Gemini AI evaluation, heuristic engine & HTML reporter
├── data/                      # Local data directory (.gitkeep)
│   ├── auction.db             # Local SQLite database (ignored by git)
│   └── documents/             # Downloaded CT, maintenance, battery PDFs
└── reports/                   # Output recommendation reports (.gitkeep)
    └── recommendation_*.html  # Beautiful visual HTML reports
```

### Database Schema

```
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│      sales       │       │     vehicles     │       │    documents     │
├──────────────────┤       ├──────────────────┤       ├──────────────────┤
│ id (PK)          │1     *│ id (PK)          │1     *│ id (PK)          │
│ sale_room        │──────<│ sale_id (FK)     │──────<│ vehicle_id (FK)  │
│ sale_date        │       │ lot_number       │       │ doc_type (ct/mh) │
│ sale_title       │       │ brand, model     │       │ download_url     │
│ sale_url         │       │ year, mileage_km │       │ local_path       │
│ total_lots       │       │ fuel_type,gearbox│       │ extracted_text   │
│ crawled_at       │       │ starting_price   │       └──────────────────┘
└──────────────────┘       │ current_bid      │       ┌──────────────────┐
                           │ warranty         │       │      photos      │
                           │ battery_health   │       ├──────────────────┤
                           │ comments         │1     *│ id (PK)          │
                           │ detail_url       │──────<│ vehicle_id (FK)  │
                           │ crawled_at       │       │ photo_url        │
                           └──────────────────┘       │ sort_order       │
                                                      └──────────────────┘
```

---

## 🚦 Traffic-Light Deal Scoring

Vehicles are scored on a 0–100 scale across three core pillars:
1. **Mechanical Reliability (35%)**:
   - Manufacturer/engine longevity track record.
   - Kilometers per year ratio (French normal: ~15,000 km/year).
   - Engine warning penalties (problematic wet timing belts, injector issues, etc.).
2. **Physical & Inspection Condition (35%)**:
   - *Contrôle Technique* (CT): Favorable vs. Défavorable.
   - Tally of critical, major, and minor defects extracted directly from the PDF.
   - Battery state-of-health (SoH) for electric and hybrid vehicles.
   - Presence of an Alcopa warranty.
3. **Value for Money (30%)**:
   - Starting price / current bid compared to French market benchmarks.
   - Estimated repair costs factored in for noted defects.

| Tier | Score Range | Color Code | Description |
|:---|:---:|:---:|:---|
| **Super Affaires** | 80 – 100 | 🟢 Green | Prime recommendations. Highest reliability, clean inspection, outstanding price. |
| **Affaires Correctes** | 60 – 79 | 🟡 Yellow | Solid opportunities. Minor manageable wear, fair pricing, typical daily drivers. |
| **Risqué / À Éviter** | < 60 or Critical | 🔴 Red | High risk. Major defects, severe CT remarks, or high-failure-rate engine families. |

---

## 🚀 Quick Start Guide

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/ermaljsx/Auction-Agent.git
cd Auction-Agent

# Set up a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate     # On Windows
# source venv/bin/activate  # On Linux/macOS

# Install dependencies
pip install -r requirements.txt
```

### 2. Crawl Auction Listings

```bash
# Fast test: Scrape the first 5 vehicles (skip downloading PDFs)
python run_crawl.py --limit 5 --no-docs

# Test crawl: Scrape the first page with all PDF documents
python run_crawl.py --pages 1 --limit 10

# Full production crawl: All paginated listings and documents for the current sale
python run_crawl.py
```

**CLI Flags for `run_crawl.py`**:
- `--url <path>`: Auction URL path (default: Alcopa Beauvais VP sale).
- `--limit N`: Limit total vehicles processed (useful for rapid testing).
- `--pages N`: Limit listing pagination pages.
- `--no-docs`: Skip downloading PDFs for rapid inventory sync.

### 3. Run the AI Advisor & Generate Deal Reports

```bash
# Run with heuristic fallback (no API key required) and open report in browser
python run_advisor.py --open

# Run with Google Gemini AI analysis
set GEMINI_API_KEY=your_gemini_api_key   # Windows PowerShell: $env:GEMINI_API_KEY="your_key"
python run_advisor.py --open

# Filter by maximum budget (e.g., max €12,000)
python run_advisor.py --max-price 12000 --open
```

Reports are automatically saved as responsive HTML files in the `reports/` folder.

---

## 🤝 Contributing

Contributions, feature requests, and architectural discussions from human developers and AI agents are welcome!
Please feel free to submit issues or pull requests.

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
