"""
Smart Web Scraper with Rotating Proxies
Extracts product/price data from any URL with anti-ban logic:
rotating proxies with health tracking, randomized browser fingerprints,
block detection, retries with backoff and bounded concurrency.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY_LIST: list[str] = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY: float = float(os.getenv("RETRY_DELAY", "2.0"))
CONCURRENCY: int = int(os.getenv("CONCURRENCY", "2"))
NAV_TIMEOUT_MS: int = int(float(os.getenv("NAV_TIMEOUT", "30")) * 1000)
PROXY_MAX_FAILURES: int = int(os.getenv("PROXY_MAX_FAILURES", "3"))
PROXY_COOLDOWN: float = float(os.getenv("PROXY_COOLDOWN", "300"))
SELECTORS_FILE: str = os.getenv("SELECTORS_FILE", "selectors.json")
OUTPUT_DIR: Path = Path(os.getenv("OUTPUT_DIR", "output"))

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]
VIEWPORTS: list[dict] = [
    {"width": 1366, "height": 768}, {"width": 1440, "height": 900},
    {"width": 1536, "height": 864}, {"width": 1920, "height": 1080}, {"width": 1280, "height": 800},
]
LOCALES: list[str] = ["en-US", "en-GB"]

DEFAULT_NAME_SELECTORS = [
    "h1[itemprop='name']", "#productTitle", "h1.product-title", "h1.product_title",
    ".product-name h1", "h1.title", "h1",
]
DEFAULT_PRICE_SELECTORS = [
    "[itemprop='price']", "meta[property='product:price:amount']", ".a-price .a-offscreen",
    "#priceblock_ourprice", ".product-price", ".current-price", ".price_color", ".price",
]
BLOCK_MARKERS = (
    "captcha", "are you a robot", "access denied", "unusual traffic", "request blocked",
    "verify you are human", "cf-challenge", "px-captcha", "please enable cookies",
)
CURRENCY_SYMBOLS = {"US$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY", "₹": "INR", "¥": "JPY"}
CURRENCY_CODES = ("USD", "EUR", "GBP", "TRY", "INR", "JPY", "CHF", "CAD", "AUD", "TL")


class BlockedError(Exception):
    """The target served a block / captcha page."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Product:
    url: str
    name: str
    price: Optional[float]
    currency: str
    availability: str = ""
    sku: str = ""
    source: str = ""          # json-ld | selectors
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def parse_amount(raw: str) -> Optional[float]:
    """Locale-independent number parsing: 1,234.56 / 1.234,56 / 1 234,56 / 1.234 / 12,50."""
    match = re.search(r"\d[\d.,'\s  ]*", raw or "")
    if not match:
        return None
    s = re.sub(r"[\s  ']", "", match.group(0)).rstrip(".,")
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        parts = s.split(sep)
        s = s.replace(sep, "") if len(parts) > 2 or len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(s)
    except ValueError:
        return None


def detect_currency(raw: str) -> str:
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in (raw or ""):
            return code
    upper = (raw or "").upper()
    for code in CURRENCY_CODES:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper):
            return "TRY" if code == "TL" else code
    return "N/A"


# ---------------------------------------------------------------------------
# Structured data extraction (schema.org JSON-LD)
# ---------------------------------------------------------------------------

def _walk_jsonld(node: Any):
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def extract_jsonld_product(jsonld_blocks: list[str]) -> Optional[dict]:
    """Return {name, price, currency, availability, sku} from the first schema.org Product found."""
    for block in jsonld_blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk_jsonld(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("@type") == "AggregateOffer" and "price" not in offers:
                offers = {**offers, "price": offers.get("lowPrice")}
            price = offers.get("price")
            if price is None and isinstance(offers.get("priceSpecification"), dict):
                price = offers["priceSpecification"].get("price")
            availability = str(offers.get("availability") or "").rsplit("/", 1)[-1]
            return {
                "name": str(node.get("name") or "").strip(),
                "price": parse_amount(str(price)) if price is not None else None,
                "currency": str(offers.get("priceCurrency") or "").upper() or "N/A",
                "availability": availability,
                "sku": str(node.get("sku") or node.get("mpn") or ""),
            }
    return None


def looks_blocked(status: Optional[int], title: str, body_sample: str) -> bool:
    if status in (403, 429, 503):
        return True
    text = f"{title} {body_sample}".lower()
    return any(marker in text for marker in BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# Proxy rotation with health tracking
# ---------------------------------------------------------------------------

def proxy_to_playwright(proxy_url: str) -> dict:
    """Convert http://user:pass@host:port into Playwright's proxy dict (credentials split out)."""
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    cfg: dict = {"server": server}
    if parsed.username:
        cfg["username"] = unquote(parsed.username)
        cfg["password"] = unquote(parsed.password or "")
    return cfg


def mask_proxy(proxy_url: Optional[str]) -> str:
    if not proxy_url:
        return "direct"
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else str(parsed.hostname)


class ProxyPool:
    """Round-robin-ish proxy rotation that benches proxies after repeated failures."""

    def __init__(self, proxies: list[str], max_failures: int = 3, cooldown: float = 300.0) -> None:
        self.proxies = list(proxies)
        self.max_failures = max_failures
        self.cooldown = cooldown
        self._failures: dict[str, int] = {p: 0 for p in self.proxies}
        self._benched_until: dict[str, float] = {}

    def get(self, exclude: Optional[set[str]] = None) -> Optional[str]:
        """Pick a healthy proxy (least failures first, random tie-break). None = go direct."""
        if not self.proxies:
            return None
        now = time.monotonic()
        healthy = [p for p in self.proxies if self._benched_until.get(p, 0) <= now and p not in (exclude or set())]
        if not healthy:
            healthy = [p for p in self.proxies if self._benched_until.get(p, 0) <= now] or self.proxies
        best = min(self._failures[p] for p in healthy)
        return random.choice([p for p in healthy if self._failures[p] == best])

    def report(self, proxy: Optional[str], ok: bool) -> None:
        if not proxy:
            return
        if ok:
            self._failures[proxy] = 0
            return
        self._failures[proxy] += 1
        if self._failures[proxy] >= self.max_failures:
            self._benched_until[proxy] = time.monotonic() + self.cooldown
            self._failures[proxy] = 0
            logger.warning("Proxy %s benched for %.0fs after repeated failures", mask_proxy(proxy), self.cooldown)


# ---------------------------------------------------------------------------
# Site-specific selectors
# ---------------------------------------------------------------------------

def load_site_selectors(path: str = SELECTORS_FILE) -> dict[str, dict]:
    """Optional JSON: {"example.com": {"name": ["h1.x"], "price": [".y"]}}"""
    p = Path(path)
    if not p.is_file():
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return {k.lower().removeprefix("www."): v for k, v in data.items()}


def selectors_for(url: str, site_selectors: dict[str, dict]) -> tuple[list[str], list[str]]:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    custom = site_selectors.get(host, {})
    return (
        list(custom.get("name", [])) + DEFAULT_NAME_SELECTORS,
        list(custom.get("price", [])) + DEFAULT_PRICE_SELECTORS,
    )


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

class SmartScraper:
    """Async scraper with rotating proxies, randomized fingerprints, block detection and retries."""

    def __init__(self, headless: bool = True, proxies: Optional[list[str]] = None, concurrency: int = CONCURRENCY) -> None:
        self.headless = headless
        self.pool = ProxyPool(PROXY_LIST if proxies is None else proxies, PROXY_MAX_FAILURES, PROXY_COOLDOWN)
        self.site_selectors = load_site_selectors()
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._playwright = None
        self._browser = None

    async def __aenter__(self) -> "SmartScraper":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self

    async def __aexit__(self, *exc) -> None:
        # Always release the browser and the Playwright driver, even after errors.
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    async def _new_context(self, proxy: Optional[str]):
        context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport=random.choice(VIEWPORTS),
            locale=random.choice(LOCALES),
            proxy=proxy_to_playwright(proxy) if proxy else None,
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = window.chrome || {runtime: {}};"
        )
        # Skip heavy assets: faster, cheaper on proxy bandwidth.
        await context.route(
            re.compile(r".*\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm)(\?.*)?$", re.I),
            lambda route: route.abort(),
        )
        return context

    async def scrape_url(self, url: str) -> Optional[Product]:
        """Scrape a single URL with retry logic and proxy rotation."""
        async with self._semaphore:
            tried: set[str] = set()
            for attempt in range(1, MAX_RETRIES + 1):
                proxy = self.pool.get(exclude=tried)
                if proxy:
                    tried.add(proxy)
                logger.info("Attempt %d/%d | %s | via %s", attempt, MAX_RETRIES, url, mask_proxy(proxy))
                context = None
                try:
                    context = await self._new_context(proxy)
                    page = await context.new_page()
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    await asyncio.sleep(random.uniform(1.0, 2.5))  # human-like pause
                    await page.mouse.wheel(0, random.randint(300, 900))

                    title = await page.title()
                    body_sample = (await page.inner_text("body"))[:3000] if await page.query_selector("body") else ""
                    if looks_blocked(response.status if response else None, title, body_sample):
                        raise BlockedError(f"blocked (status={response.status if response else '?'}, title={title[:60]!r})")

                    product = await self._extract_product(page, url)
                    self.pool.report(proxy, ok=True)
                    return product
                except Exception as exc:
                    self.pool.report(proxy, ok=False)
                    logger.warning("Attempt %d failed: %s", attempt, str(exc).splitlines()[0][:200])
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY * (2 ** (attempt - 1)) + random.random())
                finally:
                    if context:
                        await context.close()

            logger.error("All %d attempts failed for %s", MAX_RETRIES, url)
            return None

    async def _extract_product(self, page, url: str) -> Product:
        """Prefer schema.org JSON-LD; fall back to CSS selectors (site-specific first)."""
        blocks = await page.eval_on_selector_all(
            "script[type='application/ld+json']", "els => els.map(e => e.textContent)"
        )
        data = extract_jsonld_product(blocks)
        if data and data["price"] is not None:
            return Product(url=url, name=data["name"] or "N/A", price=data["price"], currency=data["currency"],
                           availability=data["availability"], sku=data["sku"], source="json-ld")

        name_selectors, price_selectors = selectors_for(url, self.site_selectors)
        name = (data or {}).get("name") or await self._first_text(page, name_selectors)
        price_raw = await self._first_text(page, price_selectors)
        return Product(
            url=url,
            name=name,
            price=parse_amount(price_raw),
            currency=detect_currency(price_raw),
            availability=(data or {}).get("availability", ""),
            sku=(data or {}).get("sku", ""),
            source="selectors",
        )

    @staticmethod
    async def _first_text(page, selectors: list[str]) -> str:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                text = (await el.get_attribute("content")) if sel.startswith("meta") else await el.inner_text()
                text = (text or "").strip()
                if text:
                    return text
            except Exception:
                continue
        return "N/A"

    async def scrape_many(self, urls: list[str]) -> list[Product]:
        async def one(i: int, url: str) -> Optional[Product]:
            await asyncio.sleep(i * random.uniform(0.5, 1.5))  # stagger start times
            return await self.scrape_url(url)

        results = await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))
        return [r for r in results if r]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def save_to_csv(products: list[Product], filename: str = "") -> Path:
    """Save a list of Product records to CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = f"products_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    path = OUTPUT_DIR / filename
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM: opens cleanly in Excel
        writer = csv.DictWriter(f, fieldnames=[fld.name for fld in fields(Product)])
        writer.writeheader()
        for p in products:
            writer.writerow(asdict(p))
    logger.info("Saved %d records -> %s", len(products), path)
    return path


def save_to_json(products: list[Product], filename: str = "") -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = f"products_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    path = OUTPUT_DIR / filename
    path.write_text(json.dumps([asdict(p) for p in products], ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved %d records -> %s", len(products), path)
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = list(args.urls or [])
    if args.urls_file:
        urls += [line.strip() for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    if not urls:
        urls = [u.strip() for u in os.getenv("TARGET_URLS", "").split(",") if u.strip()]
    if not urls:
        logger.info("No URLs given — using demo URL.")
        urls = ["https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"]
    valid = [u for u in dict.fromkeys(urls) if u.startswith(("http://", "https://"))]
    for bad in set(urls) - set(valid):
        logger.warning("Skipping invalid URL: %s", bad)
    return valid


async def main() -> None:
    parser = argparse.ArgumentParser(description="Anti-ban e-commerce scraper")
    parser.add_argument("urls", nargs="*", help="Product URLs (default: TARGET_URLS from .env)")
    parser.add_argument("--urls-file", help="Text file with one URL per line")
    parser.add_argument("--headful", action="store_true", help="Show the browser window")
    parser.add_argument("--json", action="store_true", help="Also write a JSON file")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = parser.parse_args()

    urls = collect_urls(args)
    async with SmartScraper(headless=not args.headful, concurrency=args.concurrency) as scraper:
        results = await scraper.scrape_many(urls)

    for p in results:
        logger.info("Scraped: %s | %s %s [%s]", p.name, p.currency, p.price, p.source)

    if results:
        csv_path = save_to_csv(results)
        if args.json:
            save_to_json(results)
        print(f"\nDone. {len(results)}/{len(urls)} products saved to {csv_path}")
    else:
        print("No data extracted.")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
