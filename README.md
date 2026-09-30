# Anti-Ban E-Commerce Scraper

A resilient, production-grade web scraping engine designed to bypass modern anti-bot protections and extract product data at scale flawlessly.

✔ Eliminates IP bans and bot-detection blocks using intelligent proxy and fingerprint rotation
✔ Saves hours of babysitting scripts thanks to robust automatic retry limits and backoff logic
✔ Provides clean, analysis-ready CSV data directly from complex dynamic Javascript-heavy websites

## Use Cases
- **Competitor Intelligence:** Reliably scrape thousands of product prices daily without getting blocked by Cloudflare.
- **Lead Generation:** Securely extract B2B contact information from directories that deploy strict anti-scraping measures.
- **Market Dynamics:** Build massive datasets for AI training or predictive pricing models using continuously harvested web data.

## Project Structure

```
smart-web-scraper/
├── scraper.py              # Main scraper logic
├── selectors.example.json  # Optional per-domain CSS selectors
├── tests/                  # pytest suite
├── requirements.txt
├── .env.example
└── output/             # Generated CSV files (git-ignored)
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install Playwright browser
playwright install chromium

# 3. Configure environment
cp .env.example .env
# Edit .env with your proxy list and target URLs
```

## Configuration (`.env`)

| Variable | Description | Default |
|---|---|---|
| `TARGET_URLS` | Comma-separated URLs to scrape | demo URL |
| `PROXY_LIST` | Comma-separated proxy URLs | none |
| `MAX_RETRIES` | Retry attempts per URL | `3` |
| `RETRY_DELAY` | Base delay between retries (seconds) | `2.0` |
| `CONCURRENCY` | Pages scraped in parallel | `2` |
| `NAV_TIMEOUT` | Navigation timeout (seconds) | `30` |
| `PROXY_MAX_FAILURES` | Consecutive failures before a proxy is benched | `3` |
| `PROXY_COOLDOWN` | Seconds a benched proxy is skipped | `300` |
| `SELECTORS_FILE` | Per-domain selector overrides | `selectors.json` |
| `OUTPUT_DIR` | Output directory for CSV/JSON files | `output` |

## How the anti-ban logic works

- **Proxy health tracking** – proxies rotate per attempt; one that keeps failing is benched for a cooldown,
  and a retry never reuses the proxy that just failed. Authenticated proxies (`user:pass@`) are supported.
- **Fresh fingerprint per attempt** – new browser context with random user agent, viewport and locale;
  `navigator.webdriver` is hidden.
- **Block detection** – 403/429/503 responses and captcha/"access denied" pages are treated as failures and retried.
- **Backoff & pacing** – exponential backoff between retries, staggered starts, random human-like pauses and scrolling.
- **Lean requests** – images, fonts and video are not downloaded (faster, less proxy bandwidth).
- **Clean shutdown** – one browser per run, every context closed, Playwright stopped even on errors.

## Data extraction

1. **schema.org JSON-LD** (`Product` → `offers`) is read first — name, price, currency, availability, SKU.
   Works on most modern shops without any selector.
2. **CSS selectors** as a fallback: site-specific ones from `selectors.json`, then generic defaults.
3. Prices are parsed locale-independently (`1.234,56 €`, `$1,234.56`, `1.234 TL`, `12,50 ₺`).

## Usage

```bash
# URLs from .env (TARGET_URLS)
python scraper.py

# URLs on the command line, also write JSON
python scraper.py https://shop.com/p/1 https://shop.com/p/2 --json

# URLs from a file (one per line, # for comments), visible browser
python scraper.py --urls-file urls.txt --headful --concurrency 3
```

## Example Output

```
2024-05-15 10:23:01 [INFO] Attempt 1/3 | https://example.com/product | via direct
2024-05-15 10:23:04 [INFO] Scraped: A Light in the Attic | GBP 51.77 [selectors]
2024-05-15 10:23:04 [INFO] Saved 1 records -> output/products_20240515_102304.csv
```

**CSV output:**
```csv
url,name,price,currency,availability,sku,source,scraped_at
https://...,A Light in the Attic,51.77,GBP,,,selectors,2024-05-15T10:23:04+00:00
```

## Extending

Copy `selectors.example.json` to `selectors.json` and add the domain with its `name` / `price` selectors —
no code changes needed. For deeper customization, override `_extract_product()` in `SmartScraper`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Tech Stack

`playwright` · `asyncio` · `python-dotenv`

## Screenshot

![Preview](screenshots/preview.png)

