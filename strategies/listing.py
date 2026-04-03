"""
strategies/listing.py — Strategy 1: Listing Page Heuristics

Extraction hierarchy (most reliable → least reliable):
  1. JSON-LD / Schema.org Product structured data — present on ~70% of
     e-commerce sites.  Highest fidelity: schema-validated, consistent fields.
  2. Structured row/table detection (row_extractor) — for B2B wholesale sites
     with labeled column layouts (IMAGE, DESCRIPTION, ITEM, PRICE).
  3. Generic CSS class heuristics — fallback for sites with no structured data
     and non-standard layouts.

Pagination:
  After extracting page 1, the strategy automatically follows pagination links
  (rel=next, aria-label, "Next" text) through all remaining pages and merges
  the results.  Deduplication by product_url, sku, and name+brand+price ensures
  the same row is never counted twice across pages.
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


def _extract_jsonld_products(html: str, url: str) -> list:
    """
    Extract products from JSON-LD / Schema.org structured data.

    Handles:
      - Direct @type: Product objects
      - ItemList / CollectionPage / SearchResultsPage wrappers
      - @graph arrays (common in WordPress/WooCommerce)
      - Nested itemListElement with "item" wrappers
      - Multiple <script type="application/ld+json"> blocks
    """
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
                dtype = data.get("@type", "")
                # Handle single type string or list of types
                if isinstance(dtype, list):
                    dtype_set = set(dtype)
                else:
                    dtype_set = {dtype}

                if "Product" in dtype_set:
                    items = [data]
                elif dtype_set & {"ItemList", "CollectionPage", "SearchResultsPage",
                                  "OfferCatalog", "ProductCollection"}:
                    items = data.get("itemListElement", [])
                elif "@graph" in data:
                    items = [i for i in data["@graph"]
                             if isinstance(i, dict) and _is_product_type(i)]

            for item in items:
                if isinstance(item, dict):
                    if _is_product_type(item):
                        product = _extract_from_jsonld(item, url)
                        if product:
                            products.append(product)
                    elif "item" in item:
                        inner = item["item"]
                        if isinstance(inner, dict) and _is_product_type(inner):
                            product = _extract_from_jsonld(inner, url)
                            if product:
                                products.append(product)
        except (json.JSONDecodeError, Exception):
            continue

    return products


def _is_product_type(item: dict) -> bool:
    """Check if a JSON-LD item is a Product (handles string or list @type)."""
    dtype = item.get("@type", "")
    if isinstance(dtype, list):
        return "Product" in dtype
    return dtype == "Product"


def _extract_page(html: str, url: str) -> list:
    """
    Extract products from a single listing page HTML.

    Extraction hierarchy (most reliable → least reliable):
      1. JSON-LD / Schema.org structured data — present on ~70% of e-commerce
         sites, schema-validated, consistent field names.
      2. Structured row/table detection (row_extractor) — for B2B wholesale
         sites with labeled column layouts.
      3. Generic CSS class heuristics — last resort fallback.

    Called once per page by crawl_listing_pages() in pagination.py.
    Always returns a list — never raises.
    """
    # ── Layer 1: JSON-LD / Schema.org Product data (highest priority) ────────
    try:
        jsonld_products = _extract_jsonld_products(html, url)
        if jsonld_products:
            logger.info(
                f"[Strategy 1] JSON-LD extraction found {len(jsonld_products)} product(s) — "
                f"using structured data (most reliable path)"
            )
            return jsonld_products
    except Exception as e:
        logger.warning(f"[Strategy 1] JSON-LD extraction error (falling back): {e}")

    # ── Layer 2: Structured row/table extraction ─────────────────────────────
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

    # ── Layer 3: Generic CSS heuristics ──────────────────────────────────────
    try:
        logger.info(f"[Strategy 1] No structured data or layout — running generic heuristics on {url}")
        products = extract_products(html, url)
        logger.info(f"[Strategy 1] Generic heuristics found {len(products)} product(s)")
        if products:
            return products
    except Exception as e:
        logger.error(f"[Strategy 1] Generic heuristics failed: {e}")

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
