"""
strategies/structured_extractor.py — Tier 1: Structured Data Extraction

Extracts products from structured data embedded in HTML, checked in this order:
  1. JSON-LD (@type: Product, ItemList, @graph)
  2. Schema.org microdata (itemprop attributes)
  3. Open Graph meta tags (single-product pages)

This is the fastest and most reliable extraction method. It should always
be tried first before falling back to heuristic or LLM-based approaches.

Public API:
  extract(html, url) -> list[dict]
    Returns a list of product dicts. Empty list if no structured data found.
"""

import json
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ── JSON-LD extraction ──────────────────────────────────────────────────────────

def _is_product_type(item: dict) -> bool:
    """Check if a JSON-LD item is a Product (handles string or list @type)."""
    dtype = item.get("@type", "")
    if isinstance(dtype, list):
        return "Product" in dtype
    return dtype == "Product"


def _extract_from_jsonld_item(item: dict, base_url: str) -> dict | None:
    """Extract product fields from a single JSON-LD Product object."""
    product = {}
    product["product_name"] = (item.get("name") or "").strip()
    if not product["product_name"]:
        return None

    # Brand
    brand = item.get("brand", "")
    if isinstance(brand, dict):
        product["brand"] = brand.get("name", "")
    elif isinstance(brand, str):
        product["brand"] = brand
    else:
        product["brand"] = ""

    # Identifiers
    product["sku"] = item.get("sku", "") or item.get("mpn", "") or ""
    product["upc"] = (
        item.get("gtin12", "")
        or item.get("gtin13", "")
        or item.get("gtin8", "")
        or item.get("gtin", "")
        or item.get("gtin14", "")
        or item.get("productID", "")
        or ""
    )

    # Image
    img = item.get("image", "")
    if isinstance(img, list) and img:
        img = img[0]
    if isinstance(img, dict):
        img = img.get("url", "") or img.get("contentUrl", "")
    product["image_url"] = urljoin(base_url, img) if img else ""

    # URL
    product["product_url"] = (
        urljoin(base_url, item["url"]) if item.get("url") else ""
    )

    # Price from offers
    offers = item.get("offers", {})
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        price = offers.get("price", "")
        currency = offers.get("priceCurrency", "USD")
        if price:
            product["price"] = f"${price}" if currency == "USD" else f"{price} {currency}"
        if not product["product_url"] and offers.get("url"):
            product["product_url"] = urljoin(base_url, offers["url"])

    product["_source"] = "jsonld"
    return product


def _extract_jsonld(html: str, url: str) -> list[dict]:
    """
    Extract products from all JSON-LD blocks in the HTML.

    Handles: direct Product, ItemList, CollectionPage, @graph arrays,
    nested itemListElement with "item" wrappers, multiple script blocks.
    """
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            dtype = data.get("@type", "")
            dtype_set = set(dtype) if isinstance(dtype, list) else {dtype}

            if "Product" in dtype_set:
                items = [data]
            elif dtype_set & {
                "ItemList", "CollectionPage", "SearchResultsPage",
                "OfferCatalog", "ProductCollection",
            }:
                items = data.get("itemListElement", [])
            elif "@graph" in data:
                items = [
                    i for i in data["@graph"]
                    if isinstance(i, dict) and _is_product_type(i)
                ]

        for item in items:
            if not isinstance(item, dict):
                continue
            if _is_product_type(item):
                p = _extract_from_jsonld_item(item, url)
                if p:
                    products.append(p)
            elif "itemListElement" in item and isinstance(item["itemListElement"], list):
                for child in item["itemListElement"]:
                    if isinstance(child, dict) and "item" in child:
                        child = child["item"]
                    if isinstance(child, dict) and _is_product_type(child):
                        p = _extract_from_jsonld_item(child, url)
                        if p:
                            products.append(p)
            elif "item" in item:
                inner = item["item"]
                if isinstance(inner, dict) and _is_product_type(inner):
                    p = _extract_from_jsonld_item(inner, url)
                    if p:
                        products.append(p)

    return products


# ── Schema.org microdata extraction ─────────────────────────────────────────────

def _extract_microdata(html: str, url: str) -> list[dict]:
    """
    Extract products from Schema.org microdata (itemscope/itemprop attributes).

    Looks for elements with itemtype containing "schema.org/Product" and
    reads itemprop attributes for field values.
    """
    soup = BeautifulSoup(html, "html.parser")
    products = []

    # Find all Product itemscope elements
    product_scopes = soup.find_all(
        attrs={"itemtype": re.compile(r"https?://schema\.org/Product", re.I)}
    )

    for scope in product_scopes:
        product = {}

        # Name
        name_el = scope.find(attrs={"itemprop": "name"})
        if name_el:
            product["product_name"] = (
                name_el.get("content", "") or name_el.get_text(strip=True)
            )

        if not product.get("product_name"):
            continue

        # Brand
        brand_el = scope.find(attrs={"itemprop": "brand"})
        if brand_el:
            # Could be nested itemscope with its own name
            inner_name = brand_el.find(attrs={"itemprop": "name"})
            if inner_name:
                product["brand"] = (
                    inner_name.get("content", "") or inner_name.get_text(strip=True)
                )
            else:
                product["brand"] = (
                    brand_el.get("content", "") or brand_el.get_text(strip=True)
                )

        # SKU
        for prop in ["sku", "mpn", "productID"]:
            el = scope.find(attrs={"itemprop": prop})
            if el:
                val = (el.get("content", "") or el.get_text(strip=True)).strip()
                if val:
                    product["sku"] = val
                    break

        # UPC/GTIN
        for prop in ["gtin12", "gtin13", "gtin8", "gtin", "gtin14", "productID"]:
            el = scope.find(attrs={"itemprop": prop})
            if el:
                val = (el.get("content", "") or el.get_text(strip=True)).strip()
                if val and re.fullmatch(r"\d{8,14}", val):
                    product["upc"] = val
                    break

        # Image
        img_el = scope.find(attrs={"itemprop": "image"})
        if img_el:
            img_src = (
                img_el.get("content", "")
                or img_el.get("src", "")
                or img_el.get("href", "")
            )
            if img_src:
                product["image_url"] = urljoin(url, img_src)

        # URL
        url_el = scope.find(attrs={"itemprop": "url"})
        if url_el:
            href = url_el.get("href", "") or url_el.get("content", "")
            if href:
                product["product_url"] = urljoin(url, href)

        # Price — may be inside an Offer itemscope
        offer = scope.find(
            attrs={"itemtype": re.compile(r"https?://schema\.org/Offer", re.I)}
        )
        price_scope = offer if offer else scope
        price_el = price_scope.find(attrs={"itemprop": "price"})
        if price_el:
            price_val = (
                price_el.get("content", "") or price_el.get_text(strip=True)
            )
            if price_val:
                currency_el = price_scope.find(attrs={"itemprop": "priceCurrency"})
                currency = ""
                if currency_el:
                    currency = (
                        currency_el.get("content", "")
                        or currency_el.get_text(strip=True)
                    )
                if currency == "USD" or not currency:
                    product["price"] = f"${price_val}" if not price_val.startswith("$") else price_val
                else:
                    product["price"] = f"{price_val} {currency}"

        product["_source"] = "microdata"
        products.append(product)

    return products


# ── Open Graph extraction (single-product pages) ───────────────────────────────

def _extract_opengraph(html: str, url: str) -> list[dict]:
    """
    Extract a single product from Open Graph meta tags.

    OG tags only describe one item per page, so this returns 0 or 1 products.
    Only returns a product if og:type is "product" or "og:product:*" tags exist,
    or if there's enough product-like OG data (title + price or title + image).
    """
    soup = BeautifulSoup(html, "html.parser")

    def _og(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop})
        if tag is None:
            return ""
        return (tag.get("content", "") or "").strip()

    og_type = _og("og:type").lower()
    og_title = _og("og:title")
    og_image = _og("og:image")
    og_url = _og("og:url")

    # Product-specific OG tags
    og_price = _og("product:price:amount") or _og("og:price:amount")
    og_currency = _og("product:price:currency") or _og("og:price:currency") or "USD"
    og_brand = _og("product:brand") or _og("og:brand")
    og_sku = _og("product:retailer_item_id") or _og("product:sku")
    og_upc = _og("product:upc") or _og("product:gtin")

    # Only treat as a product if there's a product signal
    is_product_page = (
        og_type in ("product", "og:product", "product.item")
        or bool(og_price)
        or bool(og_sku)
        or bool(og_upc)
    )

    if not is_product_page or not og_title:
        return []

    product = {
        "product_name": og_title,
        "image_url": urljoin(url, og_image) if og_image else "",
        "product_url": urljoin(url, og_url) if og_url else url,
        "brand": og_brand,
        "sku": og_sku,
        "upc": og_upc,
        "_source": "opengraph",
    }

    if og_price:
        product["price"] = (
            f"${og_price}" if og_currency == "USD" else f"{og_price} {og_currency}"
        )

    return [product]


# ── Public API ──────────────────────────────────────────────────────────────────

def extract(html: str, url: str) -> list[dict]:
    """
    Tier 1: Extract products from structured data in the HTML.

    Tries JSON-LD first (highest fidelity), then microdata, then Open Graph.
    Returns as soon as any method finds products.

    Returns:
        List of product dicts. Empty list if no structured data found.
    """
    # 1. JSON-LD
    try:
        products = _extract_jsonld(html, url)
        if products:
            logger.info(
                f"[Tier1] JSON-LD found {len(products)} product(s)"
            )
            return products
    except Exception as e:
        logger.warning(f"[Tier1] JSON-LD extraction error: {e}")

    # 2. Microdata
    try:
        products = _extract_microdata(html, url)
        if products:
            logger.info(
                f"[Tier1] Microdata found {len(products)} product(s)"
            )
            return products
    except Exception as e:
        logger.warning(f"[Tier1] Microdata extraction error: {e}")

    # 3. Open Graph
    try:
        products = _extract_opengraph(html, url)
        if products:
            logger.info(
                f"[Tier1] Open Graph found {len(products)} product(s)"
            )
            return products
    except Exception as e:
        logger.warning(f"[Tier1] Open Graph extraction error: {e}")

    logger.info("[Tier1] No structured data found")
    return []
