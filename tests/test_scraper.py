import json

import pytest

import scraper
from scraper import ProxyPool, extract_jsonld_product, looks_blocked, proxy_to_playwright, selectors_for


@pytest.mark.parametrize(
    "raw,expected",
    [("£51.77", 51.77), ("$1,234.56", 1234.56), ("1.234,56 €", 1234.56), ("1.234 TL", 1234.0), ("12,50", 12.5), ("N/A", None)],
)
def test_parse_amount(raw, expected):
    assert scraper.parse_amount(raw) == expected


def test_detect_currency():
    assert scraper.detect_currency("£5") == "GBP"
    assert scraper.detect_currency("5 TL") == "TRY"
    assert scraper.detect_currency("5 EUR") == "EUR"
    assert scraper.detect_currency("5") == "N/A"


def test_jsonld_product_graph_and_offers_list():
    block = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "BreadcrumbList"},
            {"@type": ["Product"], "name": "Lamp", "sku": "L1",
             "offers": [{"@type": "Offer", "price": "1299.90", "priceCurrency": "try",
                         "availability": "https://schema.org/InStock"}]},
        ],
    })
    assert extract_jsonld_product(["not json", block]) == {
        "name": "Lamp", "price": 1299.9, "currency": "TRY", "availability": "InStock", "sku": "L1",
    }


def test_jsonld_aggregate_offer():
    block = json.dumps({"@type": "Product", "name": "X", "offers": {"@type": "AggregateOffer", "lowPrice": 10, "priceCurrency": "USD"}})
    assert extract_jsonld_product([block])["price"] == 10.0


def test_jsonld_missing():
    assert extract_jsonld_product([json.dumps({"@type": "Organization"})]) is None


def test_looks_blocked():
    assert looks_blocked(403, "", "")
    assert looks_blocked(200, "Just a moment...", "Please verify you are human")
    assert not looks_blocked(200, "Lamp — Shop", "Add to cart")


def test_proxy_to_playwright_splits_credentials():
    assert proxy_to_playwright("http://us%40er:p%3Ass@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080", "username": "us@er", "password": "p:ss",
    }
    assert proxy_to_playwright("5.6.7.8:3128") == {"server": "http://5.6.7.8:3128"}
    assert scraper.mask_proxy("http://u:p@1.2.3.4:8080") == "1.2.3.4:8080"


def test_proxy_pool_benches_failing_proxy():
    pool = ProxyPool(["a", "b"], max_failures=2, cooldown=999)
    for _ in range(2):
        pool.report("a", ok=False)
    assert {pool.get() for _ in range(20)} == {"b"}
    assert ProxyPool([]).get() is None


def test_proxy_pool_exclude():
    pool = ProxyPool(["a", "b"])
    assert pool.get(exclude={"a"}) == "b"


def test_site_selectors_prepend(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"www.shop.com": {"price": [".my-price"]}}))
    sel = scraper.load_site_selectors(str(f))
    names, prices = selectors_for("https://shop.com/p/1", sel)
    assert prices[0] == ".my-price" and names == scraper.DEFAULT_NAME_SELECTORS
