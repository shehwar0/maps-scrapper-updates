# Google Maps + Deep Web Lead Scraper

A powerful, local web application that scrapes Google Maps for business leads and performs deep enrichment by crawling business websites using a hybrid of fast HTTP and real browser (Playwright) rendering.

## Key Features (Production-Ready)

- **Maps Scraping**: Robust, non-sticking scraping up to 500+ results per run with smart scrolling, end detection, and anti-hang measures.
- **Deep Web Enrichment**: For every business with a website, performs powerful crawling (contact/about/services pages, sitemaps, structured data/JSON-LD, Playwright for JS-heavy sites) to extract:
  - Emails (multiple, ranked)
  - Phones & WhatsApp
  - Social profiles
  - About/description text
  - Services, hours, addresses from site
- **Multiple Power Modes**:
  - Basic (fast Maps only)
  - Enhanced
  - Deep (recommended)
  - Ultra (cross-verified maximum data)
  - `dry_run` (instant synthetic data for testing)
  - `card_only` (ultra-fast, Maps cards only)
- **Deduplication**: History + fuzzy matching (rapidfuzz) to avoid duplicates across runs.
- **Production Features**:
  - Non-blocking background scraping with live status polling
  - Configurable via .env (web depth, workers, timeouts)
  - Checkpoints/resume for long runs
  - Robust error handling, retries (tenacity), stealth (fake-useragent)
  - Progress bars (tqdm) for long operations
  - Powerful exports: CSV + Excel (via pandas)
- **UI**: Simple browser-based interface for search, monitoring, and download.
- **API**: Full control via /scrape, /status, /stop, /download.

Tested and hardened for reliable 100-500 result runs.

## Installation (One-Time)

```bash
# 1. Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies (includes all power tools)
pip install -r requirements.txt

# 3. Install Playwright browser (mandatory for real scraping power)
python -m playwright install chromium

# Optional system deps for headless Linux (if using xvfb for tests/CI)
# sudo apt-get install -y xvfb libnss3 ... (see previous notes)
```

## Quick Start (Production Use)

```bash
# Activate env
source .venv/bin/activate

# Copy example config (optional)
cp .env.example .env
# Edit .env for tuning (WEB_MAX_PAGES, etc.)

# Run the app
python backend/app.py

# Open in browser: http://127.0.0.1:5001
```

Use **Deep** mode for best balance of power and data quality. Enable "Dry Run" for instant testing of large jobs.

## Configuration (Production)

Use `.env` (loaded via python-dotenv):

```
WEB_MAX_PAGES=16          # How deep to crawl each website
WEB_TIMEOUT_SEC=30
EMAIL_ENRICHMENT_WORKERS=6
```

See .env.example for full options.

## Running Tests

```bash
# Full test suite (dry + web + prod readiness)
PYTHONPATH=backend python tests/test_scraper_power.py

# Or under xvfb for true headless simulation
xvfb-run -a python tests/test_scraper_power.py
```

Dry tests are instant and cover core power features. Live tests exercise real scraping (may hit anti-bot on some runs).

## Production Tips & Best Practices

- Always start with **headless=True** and **dry_run** or small `max_results` to validate.
- Use `card_only=true` for very large jobs (100-500) when you want speed over deep website data.
- The app is designed for local/single-user but can be fronted with a reverse proxy.
- Results are automatically deduplicated using history + fuzzy matching.
- Long runs use checkpoints; you can stop and resume.
- Export includes rich web fields (description, services, hours, etc.).
- For very large scale, tune web budgets and use more workers.

**Important Notes**:
- Intended for local/ethical use only.
- Google Maps and websites change; selectors may need occasional updates.
- Use realistic locations (City, State, Country) for better coverage.
- Tool does **not** bypass CAPTCHAs automatically — run non-headless and solve manually when prompted.

## API Quick Reference

- `POST /scrape` — Start a job (supports dry_run, card_only, extraction_mode, etc.)
- `GET /status` — Live progress and partial results
- `POST /stop` — Graceful stop
- `GET /download` — Latest CSV
- History endpoints for dedup management

See `app.py` or the UI for full payload options.

## Project Structure

- `backend/` — Core logic (multiple scraper engines, deep web extractor, history)
- `frontend/` — Simple self-contained UI
- `tests/` — Production readiness tests
- `output/` — CSVs, history, checkpoints

## Support & Shipping

This is now hardened for customer use:
- Comprehensive tests
- Config-driven
- Graceful degradation
- Rich, deduplicated output
- Clear docs and example config

For questions or customization, the code is modular (swap scrapers, extend extractors).

---

**Ready to ship.** Clone, venv + pip + playwright install, run, and deliver powerful leads.
