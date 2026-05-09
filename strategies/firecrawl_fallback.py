"""
Firecrawl hosted extraction strategy.

When FIRECRAWL_API_KEY is configured, public catalog scrapes can use Firecrawl
as the primary extraction pass or as a fallback, depending on router settings.
It is not used for authenticated supplier sessions because Firecrawl does not
automatically inherit the user's local browser cookies.
"""

from __future__ import annotations

import logging
import json
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .product_quality import normalize_products

logger = logging.getLogger(__name__)

ID = 40
NAME = "Firecrawl JSON Extraction"

API_URL = "https://api.firecrawl.dev/v2/scrape"


PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "brand": {"type": "string"},
                    "sku": {"type": "string"},
                    "upc": {"type": "string"},
                    "ean": {"type": "string"},
                    "gtin": {"type": "string"},
                    "price": {"type": "string"},
                    "pack_size": {"type": "string"},
                    "case_pack": {"type": "string"},
                    "image_url": {"type": "string"},
                    "product_url": {"type": "string"},
                },
            },
        }
    },
    "required": ["products"],
}

PROMPT = (
    "Extract every visible product from this wholesale, distributor, retailer, "
    "or online catalog page. For each product, capture product_name, brand, sku, "
    "UPC, EAN, GTIN, price, pack_size, case_pack, image_url, and product_url. "
    "Only return real products, not category headings, navigation, ads, filters, "
    "or result-count summary text. Preserve barcode digits exactly when shown."
)


AUTH_PROMPT = (
    PROMPT
    + " This page may be inside an authenticated supplier account. Use the "
    "current authenticated session state and extract the products visible to "
    "that logged-in account."
)


def enabled() -> bool:
    return bool(api_key()) and os.environ.get("SCRAPEBUDDY_FIRECRAWL_DISABLED") != "1"


def api_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY", "").strip()


def _timeout_seconds() -> int:
    try:
        value = int(os.environ.get("SCRAPEBUDDY_FIRECRAWL_TIMEOUT", "120"))
        return max(15, value)
    except Exception:
        return 120


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _extract_products_from_response(payload: dict, base_url: str) -> list[dict]:
    candidates: list[dict] = []

    for obj in _walk_json(payload):
        products = obj.get("products")
        if isinstance(products, list):
            candidates.extend(p for p in products if isinstance(p, dict))

    if not candidates:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            json_data = data.get("json") or data.get("extract") or data.get("structuredData")
            if isinstance(json_data, dict):
                products = json_data.get("products")
                if isinstance(products, list):
                    candidates.extend(p for p in products if isinstance(p, dict))

    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        product = {
            "product_name": item.get("product_name") or item.get("name") or item.get("title") or "",
            "brand": item.get("brand") or "",
            "sku": item.get("sku") or item.get("item_number") or item.get("itemNumber") or "",
            "upc": item.get("upc") or "",
            "ean": item.get("ean") or "",
            "gtin": item.get("gtin") or "",
            "price": item.get("price") or "",
            "pack_size": item.get("pack_size") or item.get("packSize") or "",
            "case_pack": item.get("case_pack") or item.get("casePack") or "",
            "image_url": item.get("image_url") or item.get("imageUrl") or "",
            "product_url": item.get("product_url") or item.get("productUrl") or item.get("url") or "",
        }
        if product["image_url"]:
            product["image_url"] = urljoin(base_url, str(product["image_url"]))
        if product["product_url"]:
            product["product_url"] = urljoin(base_url, str(product["product_url"]))

        name = str(product["product_name"] or "").strip()
        if not name or re.search(r"^\d[\d,]*\s+(?:results?|items?|products?)\s+for\b", name, re.I):
            continue

        key = product["product_url"] or product["sku"] or f"{name}|{product['price']}"
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(product)

    return normalize_products(cleaned)


def _domain_matches(cookie_domain: str, host: str) -> bool:
    cookie_domain = (cookie_domain or "").lstrip(".").lower()
    host = (host or "").lower()
    return bool(cookie_domain and host) and (
        host == cookie_domain or host.endswith("." + cookie_domain)
    )


def _cookie_header_from_state_file(state_file: str, url: str) -> str:
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        logger.warning(f"[Firecrawl] Could not read auth state file: {e}")
        return ""

    host = urlparse(url).hostname or ""
    cookie_parts: list[str] = []
    for cookie in state.get("cookies", []) if isinstance(state, dict) else []:
        domain = cookie.get("domain") or ""
        name = cookie.get("name") or ""
        value = cookie.get("value") or ""
        if not name or value is None:
            continue
        if _domain_matches(domain, host):
            cookie_parts.append(f"{name}={value}")
    return "; ".join(cookie_parts)


def _request_body(url: str, *, authenticated: bool = False) -> dict:
    return {
        "url": url,
        "formats": [
            {
                "type": "json",
                "schema": PRODUCT_SCHEMA,
                "prompt": AUTH_PROMPT if authenticated else PROMPT,
            }
        ],
        "onlyMainContent": False,
        "waitFor": 3000,
        "timeout": _timeout_seconds() * 1000,
        "removeBase64Images": True,
        "blockAds": True,
        "proxy": "auto",
        "actions": [
            {"type": "wait", "milliseconds": 1500},
            {"type": "scroll"},
        ],
    }


def run(url: str, *, headers_override: dict | None = None, authenticated: bool = False) -> list[dict]:
    key = api_key()
    if not key:
        logger.info("[Firecrawl] FIRECRAWL_API_KEY not set; skipping")
        return []

    body = _request_body(url, authenticated=authenticated)
    if headers_override:
        body["headers"] = headers_override

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        logger.info(f"[Firecrawl] Scraping with hosted JSON extraction: {url}")
        response = requests.post(
            API_URL,
            json=body,
            headers=headers,
            timeout=_timeout_seconds(),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        logger.warning(f"[Firecrawl] Request failed: {e}")
        return []

    products = _extract_products_from_response(payload, url)
    logger.info(f"[Firecrawl] Extracted {len(products)} product(s)")
    return products


def run_authenticated(url: str, state_file: str) -> list[dict]:
    cookie_header = _cookie_header_from_state_file(state_file, url)
    if not cookie_header:
        logger.info("[Firecrawl] No matching authenticated cookies found; skipping auth scrape")
        return []
    headers_override = {
        "Cookie": cookie_header,
        "Referer": url,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    logger.info("[Firecrawl] Trying authenticated hosted extraction with session cookies")
    return run(url, headers_override=headers_override, authenticated=True)
