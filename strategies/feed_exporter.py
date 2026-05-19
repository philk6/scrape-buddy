"""
Feed-first product export strategy.

This is the fast path modeled after the ReGo spreadsheet export workflow:
detect a platform/feed, pull product data directly, enrich missing barcodes from
detail-page HTML, then return spreadsheet-ready rows.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .product_quality import normalize_products

logger = logging.getLogger(__name__)

ID = 60
NAME = "Feed-First Product Export"

HEADERS = {
    "User-Agent": "Mozilla/5.0 product data export for business use",
    "Accept": "application/json,text/html,application/xml,*/*",
}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except Exception:
        return default


def _sleep_before_detail_request() -> None:
    delay_ms = _positive_int_env("SCRAPEBUDDY_FEED_DETAIL_DELAY_MS", 150)
    if delay_ms:
        time.sleep(delay_ms / 1000)


def _root_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _clean_string(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"[\u0000-\u0008\u000B\u000C\u000E-\u001F]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", value or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_string(text)


def _money(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 999 and float(int(number)) == number:
            number = number / 100
        return f"${number:,.2f}"
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text)
        if number > 999 and "." not in text:
            number = number / 100
        return f"${number:,.2f}"
    except Exception:
        return _clean_string(text)


def _store_api_money(prices: dict | None) -> str:
    if not isinstance(prices, dict):
        return ""
    raw = prices.get("price") or prices.get("sale_price") or prices.get("regular_price")
    if raw in (None, ""):
        return ""
    try:
        minor_unit = int(prices.get("currency_minor_unit", 2))
    except Exception:
        minor_unit = 2
    try:
        amount = int(str(raw)) / (10 ** minor_unit)
    except Exception:
        return _money(raw)
    symbol = _clean_string(prices.get("currency_symbol") or "$")
    return f"{symbol}{amount:,.2f}"


def _looks_like_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"\d{8,14}", re.sub(r"\D", "", value or "")))


def _identifier_from_text(value: Any) -> str:
    text = _strip_html(str(value or ""))
    for match in re.finditer(
        r"\b(?:upc|ean|gtin|barcode)\b\s*(?:#|number|code)?\s*[:\-]?\s*([0-9][0-9\-\s]{7,24})",
        text,
        re.I,
    ):
        upc = _normalize_barcode(match.group(1))
        if _looks_like_identifier(upc):
            return upc
    return ""


def _detail_identifier_value(product: dict) -> tuple[str, str]:
    for field in ("upc", "ean", "gtin", "gtin_case", "barcode_raw"):
        value = _normalize_barcode(product.get(field))
        if _looks_like_identifier(value):
            return value, field
    return "", ""


def _enrich_rows_from_detail_pages(
    session: requests.Session,
    rows: list[dict],
    *,
    platform: str,
) -> int:
    missing = [
        row for row in rows
        if row.get("product_url") and not _looks_like_identifier(str(row.get("upc") or ""))
    ]
    if not missing or os.environ.get("SCRAPEBUDDY_FEED_DETAIL_ENRICHMENT", "1") == "0":
        return 0

    max_products = _positive_int_env("SCRAPEBUDDY_FEED_DETAIL_MAX_PRODUCTS", 10000)
    missing = missing[:max_products]
    concurrency = _positive_int_env("SCRAPEBUDDY_FEED_CONCURRENCY", 2)

    def fetch_and_enrich(row: dict) -> bool:
        from .detail import _extract_from_detail_page

        product_url = row.get("product_url")
        if not product_url:
            return False
        before_upc = row.get("upc") or ""
        page_html = _fetch_detail_text(session, product_url)
        detail = _extract_from_detail_page(page_html, product_url)
        changed = False
        for key in (
            "product_name", "brand", "sku", "price", "pack_size", "case_pack",
            "unit_size", "unit_price", "pricing_unit", "bulk_price",
            "minimum_order_qty", "raw_price_text", "image_url",
            "ean", "gtin", "gtin_case", "barcode_raw", "identifier_type",
        ):
            if not row.get(key) and detail.get(key):
                row[key] = detail.get(key)
                changed = True
        upc, source = _detail_identifier_value(detail)
        if upc and not before_upc:
            row["upc"] = upc
            row["upc_source"] = f"detail_{source}"
            row["upc_enriched"] = "1"
            row["missing_upc"] = "0"
            changed = True
        return changed

    changed_rows = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(fetch_and_enrich, row): row for row in missing}
        for future in as_completed(futures):
            try:
                if future.result():
                    changed_rows += 1
            except Exception as e:
                row = futures[future]
                logger.debug(f"[FeedExport] Detail row enrichment failed for {row.get('product_url')}: {e}")
    logger.info(
        f"[FeedExport] {platform} detail enrichment checked {len(missing)} row(s); "
        f"{changed_rows} gained data"
    )
    return changed_rows


def _title_case_brand(value: str) -> str:
    return _clean_string(value).replace("_", " ")


def _brand_from_shopify(product: dict) -> str:
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    for tag in tags:
        if re.match(r"^Brand_", str(tag), re.I):
            return _title_case_brand(re.sub(r"^Brand_", "", str(tag), flags=re.I))
    vendor = _clean_string(product.get("vendor"))
    return "" if vendor.lower() in {"rego trading inc"} else vendor


def _case_size_from_shopify(product: dict) -> str:
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    for tag in tags:
        if re.match(r"^case/", str(tag), re.I):
            return _clean_string(re.sub(r"^case/", "", str(tag), flags=re.I))
    title = _clean_string(product.get("title"))
    match = re.search(r"(?:^|\s|-)/?(\d+)\s*(?:pk|ct|case)\b", title, re.I)
    return match.group(1) if match else ""


def _categories_from_shopify(product: dict) -> str:
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",")]
    noise = {
        "Specials",
        "In Stock",
        "New Arrivals",
        "P&G",
        "P&G products",
        "PG",
        "Procter & Gamble",
        "Unilever",
    }
    cleaned = []
    for tag in tags:
        tag = _clean_string(tag)
        if not tag:
            continue
        if re.match(r"^Brand_", tag, re.I) or re.match(r"^case/", tag, re.I):
            continue
        if tag in noise or re.search(r"products$", tag, re.I):
            continue
        cleaned.append(tag)
    return "; ".join(cleaned)


def _normalize_barcode(value: Any) -> str:
    raw = _clean_string(value)
    digits = re.sub(r"\D", "", raw)
    if 8 <= len(digits) <= 14:
        return digits
    return raw


def _session_from_state_file(state_file: str | None, url: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    if not state_file:
        return session
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        logger.warning(f"[FeedExport] Could not read auth state file: {e}")
        return session

    host = urlparse(url).hostname or ""
    for cookie in state.get("cookies", []) if isinstance(state, dict) else []:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        session.cookies.set(str(name), str(value), domain=domain)
    return session


def _fetch_text(session: requests.Session, url: str, timeout: int = 20) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def _fetch_detail_text(session: requests.Session, url: str, attempts: int = 3) -> str:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            _sleep_before_detail_request()
            return _fetch_text(session, url, timeout=30)
        except Exception as e:
            last_error = e
            time.sleep(0.5 * attempt)
    raise last_error


def _fetch_json(session: requests.Session, url: str, attempts: int = 3) -> dict | None:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            text = _fetch_text(session, url, timeout=20)
            if not text.lstrip().startswith("{"):
                return None
            return json.loads(text)
        except Exception as e:
            last_error = e
            time.sleep(0.25 * attempt)
    logger.info(f"[FeedExport] JSON fetch failed for {url}: {last_error}")
    return None


def _shopify_feed_bases(url: str) -> list[str]:
    parsed = urlparse(url)
    root = _root_url(url)
    path = parsed.path.rstrip("/")
    bases = []
    if "/collections/" in path:
        collection = path.split("/products/", 1)[0].rstrip("/")
        if collection:
            bases.append(urljoin(root, collection + "/products.json"))
    bases.append(urljoin(root, "/products.json"))
    seen = set()
    result = []
    for base in bases:
        if base not in seen:
            seen.add(base)
            result.append(base)
    return result


def _fetch_shopify_products(session: requests.Session, feed_base: str) -> list[dict]:
    products: list[dict] = []
    max_pages = _positive_int_env("SCRAPEBUDDY_FEED_MAX_PAGES", 250)
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in feed_base else "?"
        url = f"{feed_base}{sep}limit=250&page={page}"
        payload = _fetch_json(session, url)
        page_products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(page_products, list) or not page_products:
            break
        products.extend(page_products)
        logger.info(f"[FeedExport] Product feed page {page}: +{len(page_products)} product(s)")
        if len(page_products) < 250:
            break
    return products


def _read_balanced_object(text: str, marker: str) -> str | None:
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    start = text.find("{", marker_index + len(marker))
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _enrich_shopify_product_from_html(product: dict, page_html: str) -> None:
    variants = product.get("variants") or []
    variant_by_id = {str(variant.get("id")): variant for variant in variants}

    selected_json = _read_balanced_object(page_html, '"selected_variant_drop"')
    if selected_json:
        try:
            selected = json.loads(selected_json)
            variant = variant_by_id.get(str(selected.get("id"))) or (variants[0] if variants else None)
            if variant:
                variant["barcode"] = _normalize_barcode(selected.get("barcode") or variant.get("barcode") or "")
                variant["sku"] = selected.get("sku") or variant.get("sku") or ""
        except Exception:
            pass

    for match in re.finditer(r'"sku"\s*:\s*"([^"]*)"[\s\S]{0,400}?"mpn"\s*:\s*"([^"]*)"', page_html):
        sku = _clean_string(match.group(1))
        upc = _normalize_barcode(match.group(2))
        if not upc:
            continue
        variant = next((item for item in variants if item.get("sku") == sku), None) or (variants[0] if variants else None)
        if variant and not variant.get("barcode"):
            variant["barcode"] = upc

    for match in re.finditer(r'"barcode"\s*:\s*"([^"]{8,32})"', page_html):
        upc = _normalize_barcode(match.group(1))
        if upc and variants and not variants[0].get("barcode"):
            variants[0]["barcode"] = upc
            break


def _enrich_missing_shopify_barcodes(
    session: requests.Session,
    products: list[dict],
    root: str,
) -> int:
    missing = [
        product for product in products
        if any(not _normalize_barcode(variant.get("barcode")) for variant in product.get("variants") or [])
    ]
    if not missing or os.environ.get("SCRAPEBUDDY_FEED_DETAIL_ENRICHMENT", "1") == "0":
        return 0

    max_products = _positive_int_env("SCRAPEBUDDY_FEED_DETAIL_MAX_PRODUCTS", 10000)
    missing = missing[:max_products]
    concurrency = _positive_int_env("SCRAPEBUDDY_FEED_CONCURRENCY", 2)
    checked = 0

    def fetch_and_enrich(product: dict) -> bool:
        handle = product.get("handle")
        if not handle:
            return False
        product_url = urljoin(root, f"/products/{handle}")
        before = sum(1 for variant in product.get("variants") or [] if _normalize_barcode(variant.get("barcode")))
        page_html = _fetch_detail_text(session, product_url)
        _enrich_shopify_product_from_html(product, page_html)
        after = sum(1 for variant in product.get("variants") or [] if _normalize_barcode(variant.get("barcode")))
        return after > before

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(fetch_and_enrich, product): product for product in missing}
        for future in as_completed(futures):
            try:
                if future.result():
                    checked += 1
            except Exception as e:
                product = futures[future]
                logger.debug(f"[FeedExport] Detail enrichment failed for {product.get('handle')}: {e}")
    logger.info(
        f"[FeedExport] Product detail enrichment checked {len(missing)} product(s); "
        f"{checked} gained barcode data"
    )
    return checked


def _shopify_rows(products: list[dict], root: str) -> list[dict]:
    rows: list[dict] = []
    for product in products:
        brand = _brand_from_shopify(product)
        category = _categories_from_shopify(product)
        case_size = _case_size_from_shopify(product)
        description = _strip_html(product.get("body_html") or product.get("description") or "")
        tags = product.get("tags") or []
        if isinstance(tags, str):
            tags_text = tags
        else:
            tags_text = "; ".join(_clean_string(tag) for tag in tags if _clean_string(tag))
        product_url = urljoin(root, f"/products/{product.get('handle') or ''}")
        images = product.get("images") or []
        fallback_image = ""
        if images and isinstance(images[0], dict):
            fallback_image = images[0].get("src") or ""
        for variant in product.get("variants") or [{}]:
            image_id = variant.get("image_id")
            image_url = fallback_image
            if image_id:
                for image in images:
                    if str(image.get("id")) == str(image_id):
                        image_url = image.get("src") or image_url
                        break
            barcode = _normalize_barcode(variant.get("barcode"))
            title = _clean_string(variant.get("name") or product.get("title"))
            row = {
                "product_name": title,
                "brand": brand,
                "sku": _clean_string(variant.get("sku")),
                "upc": barcode,
                "price": _money(variant.get("price") or product.get("price")),
                "pack_size": "",
                "case_pack": case_size,
                "image_url": image_url,
                "product_url": product_url,
                "category": category,
                "description": description,
                "availability": (
                    "Available" if variant.get("available") is True
                    else "Unavailable" if variant.get("available") is False
                    else ""
                ),
                "source_product_id": str(product.get("id") or ""),
                "source_variant_id": str(variant.get("id") or ""),
                "source_platform": "Product feed",
                "tags": tags_text,
                "upc_source": "supplier_feed" if barcode else "",
                "upc_enriched": "0",
                "missing_upc": "0" if barcode else "1",
            }
            rows.append(row)
    return normalize_products(rows)


def _run_shopify(url: str, state_file: str | None = None) -> dict | None:
    session = _session_from_state_file(state_file, url)
    root = _root_url(url)

    best_products: list[dict] = []
    best_feed = ""
    for feed_base in _shopify_feed_bases(url):
        products = _fetch_shopify_products(session, feed_base)
        if len(products) > len(best_products):
            best_products = products
            best_feed = feed_base
        if products and "/collections/" in feed_base:
            break

    if not best_products:
        return None

    gained = _enrich_missing_shopify_barcodes(session, best_products, root)
    rows = _shopify_rows(best_products, root)
    diagnostics = {
        "strategy_name": NAME,
        "score": 0.95 if rows else 0.0,
        "counts": {
            "products": len(rows),
            "name": sum(1 for row in rows if row.get("product_name")),
            "price": sum(1 for row in rows if row.get("price")),
            "sku": sum(1 for row in rows if row.get("sku")),
            "barcode": sum(1 for row in rows if row.get("upc") or row.get("ean") or row.get("gtin")),
            "product_url": sum(1 for row in rows if row.get("product_url")),
            "image_url": sum(1 for row in rows if row.get("image_url")),
        },
        "feed": {
            "platform": "Product feed",
            "feed_url": best_feed,
            "products_fetched": len(best_products),
            "rows": len(rows),
            "detail_products_gained_barcodes": gained,
        },
        "warnings": [],
    }
    if diagnostics["counts"]["barcode"] < len(rows):
        diagnostics["warnings"].append(
            "Some rows have no UPC/EAN/GTIN in the supplier feed or detail HTML."
        )
    return {
        "strategy_id": ID,
        "strategy_name": NAME,
        "reason": (
            f"Detected product feed and exported {len(rows)} variant row(s) "
            f"from {len(best_products)} product record(s)"
        ),
        "products": rows,
        "diagnostics": diagnostics,
        "_skip_external_upc_enrichment": True,
    }


def _fetch_woocommerce_products(session: requests.Session, root: str) -> tuple[list[dict], str]:
    products: list[dict] = []
    feed_base = urljoin(root, "/wp-json/wc/store/v1/products")
    max_pages = _positive_int_env("SCRAPEBUDDY_FEED_MAX_PAGES", 250)
    for page in range(1, max_pages + 1):
        url = f"{feed_base}?per_page=100&page={page}"
        payload = _fetch_json(session, url)
        if not isinstance(payload, list) or not payload:
            break
        products.extend([item for item in payload if isinstance(item, dict)])
        logger.info(f"[FeedExport] Store feed page {page}: +{len(payload)} product(s)")
        if len(payload) < 100:
            break
    return products, feed_base


def _woocommerce_terms(attribute: dict) -> list[str]:
    values: list[str] = []
    for key in ("terms", "options"):
        raw_values = attribute.get(key) or []
        if isinstance(raw_values, list):
            for value in raw_values:
                if isinstance(value, dict):
                    text = _clean_string(value.get("name") or value.get("slug"))
                else:
                    text = _clean_string(value)
                if text:
                    values.append(text)
    return values


def _woocommerce_brand(product: dict) -> str:
    for attribute in product.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = _clean_string(attribute.get("name") or attribute.get("taxonomy")).lower()
        if "brand" not in name and "manufacturer" not in name:
            continue
        terms = _woocommerce_terms(attribute)
        if terms:
            return terms[0]
    for category in product.get("categories") or []:
        name = _clean_string(category.get("name") if isinstance(category, dict) else category)
        if re.search(r"\bbrand", name, re.I):
            return re.sub(r"\bbrands?\b", "", name, flags=re.I).strip(" -:")
    return ""


def _woocommerce_identifier(product: dict) -> tuple[str, str]:
    for key in ("global_unique_id", "barcode", "upc", "ean", "gtin"):
        upc = _normalize_barcode(product.get(key))
        if _looks_like_identifier(upc):
            return upc, f"feed_{key}"

    for attribute in product.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = _clean_string(attribute.get("name") or attribute.get("taxonomy")).lower()
        if not any(token in name for token in ("upc", "ean", "gtin", "barcode")):
            continue
        for value in _woocommerce_terms(attribute):
            upc = _normalize_barcode(value)
            if _looks_like_identifier(upc):
                return upc, "feed_attribute"

    for field in ("description", "short_description"):
        upc = _identifier_from_text(product.get(field))
        if upc:
            return upc, f"feed_{field}"

    sku = _normalize_barcode(product.get("sku"))
    if _looks_like_identifier(sku):
        return sku, "numeric_sku"
    return "", ""


def _woocommerce_rows(products: list[dict], root: str) -> list[dict]:
    rows: list[dict] = []
    for product in products:
        upc, upc_source = _woocommerce_identifier(product)
        categories = [
            _clean_string(category.get("name") if isinstance(category, dict) else category)
            for category in product.get("categories") or []
        ]
        tags = [
            _clean_string(tag.get("name") if isinstance(tag, dict) else tag)
            for tag in product.get("tags") or []
        ]
        images = product.get("images") or []
        image_url = ""
        if images and isinstance(images[0], dict):
            image_url = images[0].get("src") or images[0].get("thumbnail") or ""
        product_url = product.get("permalink") or urljoin(root, f"/product/{product.get('slug') or product.get('id') or ''}")
        row = {
            "product_name": _clean_string(product.get("name")),
            "brand": _woocommerce_brand(product),
            "sku": _clean_string(product.get("sku")),
            "upc": upc,
            "price": _store_api_money(product.get("prices")),
            "pack_size": "",
            "case_pack": "",
            "image_url": image_url,
            "product_url": product_url,
            "category": "; ".join(category for category in categories if category),
            "description": _strip_html(product.get("short_description") or product.get("description") or ""),
            "availability": (
                "Available" if product.get("is_in_stock") is True
                else "Unavailable" if product.get("is_in_stock") is False
                else ""
            ),
            "source_product_id": str(product.get("id") or ""),
            "source_variant_id": "",
            "source_platform": "Product feed",
            "tags": "; ".join(tag for tag in tags if tag),
            "upc_source": upc_source,
            "upc_enriched": "0",
            "missing_upc": "0" if upc else "1",
        }
        rows.append(row)
    return normalize_products(rows)


def _run_woocommerce(url: str, state_file: str | None = None) -> dict | None:
    session = _session_from_state_file(state_file, url)
    root = _root_url(url)
    products, feed_url = _fetch_woocommerce_products(session, root)
    if not products:
        return None

    rows = _woocommerce_rows(products, root)
    gained = _enrich_rows_from_detail_pages(session, rows, platform="feed")
    rows = normalize_products(rows)
    diagnostics = {
        "strategy_name": NAME,
        "score": 0.9 if rows else 0.0,
        "counts": {
            "products": len(rows),
            "name": sum(1 for row in rows if row.get("product_name")),
            "price": sum(1 for row in rows if row.get("price")),
            "sku": sum(1 for row in rows if row.get("sku")),
            "barcode": sum(1 for row in rows if row.get("upc") or row.get("ean") or row.get("gtin")),
            "product_url": sum(1 for row in rows if row.get("product_url")),
            "image_url": sum(1 for row in rows if row.get("image_url")),
        },
        "feed": {
            "platform": "Product feed",
            "feed_url": feed_url,
            "products_fetched": len(products),
            "rows": len(rows),
            "detail_products_gained_barcodes": gained,
        },
        "warnings": [],
    }
    if diagnostics["counts"]["barcode"] < len(rows):
        diagnostics["warnings"].append(
            "Some rows have no UPC/EAN/GTIN in the product feed or detail HTML."
        )
    return {
        "strategy_id": ID,
        "strategy_name": NAME,
        "reason": (
            f"Detected product feed and exported {len(rows)} product row(s)"
        ),
        "products": rows,
        "diagnostics": diagnostics,
        "_skip_external_upc_enrichment": True,
    }


def run(url: str, *, state_file: str | None = None) -> dict | None:
    """Return a standard Scraper Buddy result dict, or None when no feed works."""
    try:
        result = _run_shopify(url, state_file=state_file)
        if result:
            logger.info(
                f"[FeedExport] Feed-first export succeeded: "
                f"{len(result.get('products', []))} row(s)"
            )
            return result
    except Exception as e:
        logger.info(f"[FeedExport] Product feed path unavailable: {e}")
    try:
        result = _run_woocommerce(url, state_file=state_file)
        if result:
            logger.info(
                f"[FeedExport] Store feed-first export succeeded: "
                f"{len(result.get('products', []))} row(s)"
            )
            return result
    except Exception as e:
        logger.info(f"[FeedExport] Store feed path unavailable: {e}")
    return None
