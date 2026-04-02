"""
strategies/listing.py — Strategy 1: Listing Page Heuristics

How it works:
  Scans the category/listing page HTML for repeating elements whose CSS class
  names contain common e-commerce keywords (product, item, card, tile, etc.).
  Extracts name, brand, price, URL, image, and SKU from each matched card.

  When the page exposes a row/table-style catalog with labeled columns (IMAGE,
  DESCRIPTION, ORDER, PRICE, QTY, etc.) the structured row extractor is used
  first for highest-fidelity extraction.

Pagination:
  After extracting page 1, the strategy automatically follows pagination links
  (rel=next, aria-label, "Next" text) through all remaining pages and merges
  the results.  Deduplication by product_url, sku, and name+brand+price ensures
  the same row is never counted twice across pages.

When to use:
  As the first attempt on any page. Works well on traditional server-rendered
  e-commerce sites where product cards are clearly marked in the HTML.

When it falls short:
  - Sites that render products via JavaScript (React, Vue, Next.js SPAs)
  - Heavily customised layouts with unusual class naming
  - Sites that use obfuscated/minified class names

Future extensions:
  - JSON-LD / microdata structured data extraction
  - Schema.org Product markup parsing
"""

import logging
from urllib.parse import urljoin
from scraper import extract_products
from strategies.row_extractor import extract_products_from_page as extract_structured
from strategies.pagination import crawl_listing_pages

logger = logging.getLogger(__name__)

# Strategy metadata — used by the router for logging and response labelling
ID = 1
NAME = "Listing Page Heuristics"


def _extract_from_jsonld(item: dict, base_url: str) -> dict:
    """Extract product fields from a JSON-LD Product object."""
    product = {}
    product["product_name"] = item.get("name", "")
    product["brand"] = ""
    if isinstance(item.get("brand"), dict):
        product["brand"] = item["brand"].get("name", "")
    elif isinstance(item.get("brand"), str):
        product["brand"] = item["brand"]

    product["sku"] = item.get("sku", "") or item.get("mpn", "")
    product["upc"] = item.get("gtin12", "") or item.get("gtin13", "") or item.get("gtin", "")

    # Image
    img = item.get("image", "")
    if isinstance(img, list) and img:
        img = img[0]
    if isinstance(img, dict):
        img = img.get("url", "") or img.get("contentUrl", "")
    product["image_url"] = urljoin(base_url, img) if img else ""

    # URL
    product["product_url"] = urljoin(base_url, item.get("url", "")) if item.get("url") else ""

    # Price from offers
    offers = item.get("offers", {})
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        price = offers.get("price", "")
        currency = offers.get("priceCurrency", "USD")
        if price:
            product["price"] = f"${price}" if currency == "USD" else f"{price} {currency}"
        product["product_url"] = product["product_url"] or (urljoin(base_url, offers.get("url", "")) if offers.get("url") else "")

    # Only return if we have at least a name
    if product.get("product_name"):
        return product
    return None


def _extract_page(html: str, url: str) -> list:
    """
    Extract products from a single listing page HTML.

    Extraction hierarchy:
      1. Structured row/table detection (row_extractor) — used when the page
         has a clearly labeled column layout (IMAGE, DESCRIPTION, ITEM, PRICE).
         This is the highest-fidelity path: it uses the explicit page structure
         instead of guessing from CSS class names.
      2. Generic CSS class heuristics (scraper.extract_products) — fallback
         used when no structured layout is detected.

    Called once per page by crawl_listing_pages() in pagination.py.
    Always returns a list — never raises.
    """
    # ── Layer 1: Structured row/table extraction ──────────────────────────────
    try:
        structured = extract_structured(html, url)
        if structured is not None:
            logger.info(
                f"[Strategy 1] Structured layout used — "
                f"{len(structured)} product(s) extracted from explicit column structure"
            )
            return structured
    except Exception as e:
        logger.warning(f"[Strategy 1] Structured extraction error (falling back): {e}")

    # ── Layer 2: Generic CSS heuristics ───────────────────────────────────────
    try:
        logger.info(f"[Strategy 1] No structured layout — running generic heuristics on {url}")
        products = extract_products(html, url)
        logger.info(f"[Strategy 1] Generic heuristics found {len(products)} product(s)")
        if products:
            return products
    except Exception as e:
        logger.error(f"[Strategy 1] Generic heuristics failed: {e}")

    # ── Layer 3: JSON-LD / Schema.org Product data ───────────────────────
    try:
        from bs4 import BeautifulSoup
        import json
        soup = BeautifulSoup(html, "html.parser")
        products = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    if data.get("@type") == "Product":
                        items = [data]
                    elif data.get("@type") in ("ItemList", "CollectionPage", "SearchResultsPage"):
                        items = data.get("itemListElement", [])
                    elif "@graph" in data:
                        items = [i for i in data["@graph"] if isinstance(i, dict) and i.get("@type") == "Product"]

                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        product = _extract_from_jsonld(item, url)
                        if product:
                            products.append(product)
                    elif isinstance(item, dict) and "item" in item:
                        inner = item["item"]
                        if isinstance(inner, dict) and inner.get("@type") == "Product":
                            product = _extract_from_jsonld(inner, url)
                            if product:
                                products.append(product)
            except (json.JSONDecodeError, Exception):
                continue

        if products:
            logger.info(
                f"[Strategy 1] JSON-LD extraction found {len(products)} product(s)"
            )
            return products
    except Exception as e:
        logger.warning(f"[Strategy 1] JSON-LD extraction error (non-fatal): {e}")

    return []


def run(html: str, url: str, use_playwright: bool = False) -> list:
    """
    Extract products from a listing/catalog page, automatically traversing
    all pagination pages and returning a deduplicated combined result.

    Args:
        html: HTML of the first (already-fetched) listing page.
        url:  URL of that page.
        use_playwright: If True, use Playwright for all subsequent page
                       fetches (for sites that block regular HTTP requests).

    Returns:
        Deduplicated list of product dicts from all pages.
        Never raises — errors are caught and logged by pagination layer.
    """
    return crawl_listing_pages(html, url, _extract_page, use_playwright=use_playwright)
